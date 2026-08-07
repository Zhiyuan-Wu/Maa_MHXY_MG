"""填写数字验证码的自定义 action。

场景：shimen「装备上交」弹窗——上交高价值装备时游戏要求输入验证码（4 位数字），
pipeline 无对应节点会卡死（见 ``.claude/skills/5r_log_analysis`` SKILL ⑭）。

流程：OCR 读验证码 → TemplateMatch 定位数字键盘 → 按布局顺次点击 → 回读输入框校验 →
一致点「确定」/ 不一致回主界面（放弃本次，等下次重试）。
"""
import json
import os
import time

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

from utils import logger


# agent/custom/action/ → 上三级 = 仓库根（含 assets/）。拼 keypad 模板绝对路径，
# 不依赖 Resource 是否加载了 NeteaseServer 叠加（run_5r standalone 只 post_bundle(base/)，
# 直接给 template 绝对路径让 MaaFw 内部 imread 读）。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
_DEFAULT_KEYPAD = os.path.join(
    _REPO_DIR, "assets", "resource", "NeteaseServer", "image", "keypad1.png"
)


@AgentServer.custom_action("fillCaptcha")
class FillCaptcha(CustomAction):
    """填写数字验证码（含输入校验 + 确定/回退）。

    流程：
      ① OCR ``ocr_roi`` 读验证码文本 → 提取数字序列（如 ``"2243"`` → ``['2','2','4','3']``）；
      ② TemplateMatch ``keypad_template`` 定位数字键盘框 ``box=[x,y,w,h]``；
      ③ 按 ``keypad_layout``（数字→``[row,col]``，3 行 4 列）算各键中心顺次 ``post_click``：
        ``cx = x + (col+0.5)*w/4``、``cy = y + (row+0.5)*h/3``；
      ④ 重新截图，OCR ``verify_roi`` 回读输入框，与原验证码比对：
         - 不一致 → ``run_task(fallback_entry)`` 回主界面（放弃，等下次重试），返回失败；
         - 一致 → ``post_click`` ``confirm_target`` 中心（点「确定」），返回成功。

    键盘布局（默认，3 行 4 列）::

          col0  col1  col2  col3
        row0  1     2     3     退格
        row1  4     5     6     0
        row2  7     8     9     (空占位)

    custom_action_param（均可选，有默认）::

        "ocr_roi": [649, 228, 121, 65]                       # 验证码显示区（读目标）
        "verify_roi": [458, 228, 117, 66]                     # 输入框回读区（校验已输入）
        "keypad_template": "<abs>/keypad1.png"                # 数字键盘模板
        "keypad_layout": {"1":[0,0], "2":[0,1], ...}          # 数字→[row,col]
        "click_interval": 0.3                                 # 键击间隔秒
        "confirm_target": [730, 624, 85, 33]                  # 「确定」按钮 [x,y,w,h]，一致后点其中心
        "fallback_entry": "panduan_zhujiemian"                # 输入不一致时回退的 entry
    """

    _DEFAULT_OCR_ROI = [649, 228, 121, 65]
    _DEFAULT_VERIFY_ROI = [458, 228, 117, 66]
    _DEFAULT_CONFIRM_TARGET = [730, 624, 85, 33]
    _DEFAULT_FALLBACK_ENTRY = "panduan_zhujiemian"
    _DEFAULT_CLICK_INTERVAL = 0.3
    # 数字 → [row, col]（0-indexed），3 行 4 列
    _DEFAULT_LAYOUT = {
        "1": [0, 0], "2": [0, 1], "3": [0, 2],
        "4": [1, 0], "5": [1, 1], "6": [1, 2], "0": [1, 3],
        "7": [2, 0], "8": [2, 1], "9": [2, 2],
    }
    _ROWS = 3
    _COLS = 4
    _OCR_NODE = "fillCaptcha-验证码OCR"
    _VERIFY_NODE = "fillCaptcha-输入回读OCR"
    _KEYPAD_NODE = "fillCaptcha-键盘定位"

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> CustomAction.RunResult:
        argv_dict: dict = json.loads(argv.custom_action_param or "{}")
        ocr_roi = argv_dict.get("ocr_roi", self._DEFAULT_OCR_ROI)
        verify_roi = argv_dict.get("verify_roi", self._DEFAULT_VERIFY_ROI)
        keypad_template = argv_dict.get("keypad_template", _DEFAULT_KEYPAD)
        layout = argv_dict.get("keypad_layout", self._DEFAULT_LAYOUT)
        click_interval = argv_dict.get("click_interval", self._DEFAULT_CLICK_INTERVAL)
        confirm_target = argv_dict.get("confirm_target", self._DEFAULT_CONFIRM_TARGET)
        fallback_entry = argv_dict.get("fallback_entry", self._DEFAULT_FALLBACK_ENTRY)

        image = context.tasker.controller.post_screencap().wait().get()

        # ① OCR 读验证码
        ocr_reco = context.run_recognition(
            self._OCR_NODE,
            image,
            pipeline_override={self._OCR_NODE: {
                "recognition": "OCR",
                "roi": ocr_roi,
                "expected": [""],
                "threshold": 0.7,
            }},
        )
        if not ocr_reco or not ocr_reco.hit:
            logger.warning(f"[fillCaptcha] 验证码 OCR 未命中 (roi={ocr_roi})")
            return CustomAction.RunResult(success=False)
        raw = ocr_reco.best_result.text or ""
        digits = [c for c in raw if c.isdigit()]
        if not digits:
            logger.warning(f"[fillCaptcha] 验证码 OCR 无数字（原文 {raw!r}, roi={ocr_roi}）")
            return CustomAction.RunResult(success=False)
        expected = "".join(digits)
        logger.info(f"[fillCaptcha] 验证码 OCR: {raw!r} → 数字序列 {digits}")

        # ② TemplateMatch 定位数字键盘框（box=[x,y,w,h]）
        kp_reco = context.run_recognition(
            self._KEYPAD_NODE,
            image,
            pipeline_override={self._KEYPAD_NODE: {
                "recognition": "TemplateMatch",
                "template": [keypad_template],
                "threshold": 0.7,
            }},
        )
        if not kp_reco or not kp_reco.hit or not kp_reco.best_result.box:
            logger.warning(f"[fillCaptcha] 键盘模板未命中: {keypad_template}")
            return CustomAction.RunResult(success=False)
        kx, ky, kw, kh = kp_reco.best_result.box  # [x, y, w, h]
        cell_w = kw / self._COLS
        cell_h = kh / self._ROWS
        logger.info(f"[fillCaptcha] 键盘框 ({kx},{ky},{kw},{kh}) 单元格 {cell_w:.0f}x{cell_h:.0f}")

        # ③ 按布局顺次点击每个数字键中心
        for d in digits:
            pos = layout.get(d) if isinstance(layout, dict) else None
            if not pos or len(pos) < 2:
                logger.warning(f"[fillCaptcha] 数字 {d} 不在键盘布局，跳过")
                continue
            row, col = pos[0], pos[1]
            cx = int(kx + (col + 0.5) * cell_w)
            cy = int(ky + (row + 0.5) * cell_h)
            logger.info(f"[fillCaptcha] 点击数字 {d} @ ({cx},{cy})")
            context.tasker.controller.post_click(cx, cy).wait()
            if click_interval > 0:
                time.sleep(click_interval)

        # ④ 回读输入框校验：一致 → 点「确定」；不一致 → 回主界面放弃（等下次重试）
        image2 = context.tasker.controller.post_screencap().wait().get()
        verify_reco = context.run_recognition(
            self._VERIFY_NODE,
            image2,
            pipeline_override={self._VERIFY_NODE: {
                "recognition": "OCR",
                "roi": verify_roi,
                "expected": [""],
                "threshold": 0.7,
            }},
        )
        verify_text = ""
        if verify_reco and verify_reco.hit:
            verify_text = verify_reco.best_result.text or ""
        verify_digits = "".join(c for c in verify_text if c.isdigit())
        logger.info(f"[fillCaptcha] 输入回读 OCR: {verify_text!r} → {verify_digits!r}（期望 {expected!r}）")

        if verify_digits != expected:
            logger.warning(
                f"[fillCaptcha] 输入不一致（{verify_digits!r} ≠ {expected!r}），回退 {fallback_entry}")
            try:
                context.run_task(fallback_entry)
            except Exception as e:
                logger.warning(f"[fillCaptcha] 回退 {fallback_entry} 异常：{e}")
            return CustomAction.RunResult(success=False)

        # 一致 → 点「确定」中心
        tx, ty, tw, th = confirm_target
        cx = int(tx + tw / 2)
        cy = int(ty + th / 2)
        logger.info(f"[fillCaptcha] 输入一致，点确定 @ ({cx},{cy})")
        context.tasker.controller.post_click(cx, cy).wait()
        return CustomAction.RunResult(success=True)
