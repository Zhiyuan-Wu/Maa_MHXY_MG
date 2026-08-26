"""拓印考验自动涂墨的自定义 action（算法核心见 ``tuyinStroke.py``）。

场景：侠士副本入口"拓印考验"弹窗（见 memory ``tuyin-popup-blocks-xiashi-entry``）——
点「进入」后游戏弹拓印弹窗，需按住涂墨覆盖笔画，完成度 ≥70% 点「完成」通过。
pipeline 无对应节点会 20s 超时 → 空节点假成功（08-24 job2 侠士-1/2 实证）。

流程：
  ① TemplateMatch ``roi`` 内匹配 ``zonghe/tuoyin.png``（弹窗左侧"拓印"竖排标题），
     确认拓印弹窗在场；
  ② 截图 → ``tuyinStroke.analyze`` 识别笔画并生成骨架中轴笔画路径；
  ③ 逐笔涂墨：每笔折线用 touch_down → 逐段 touch_move（约 100px/s 慢速）→ touch_up；
     笔间抬起（多笔）；
  ④ OCR ``degree_roi``（画面右下角百分比）回读完成度，≥ ``done_threshold``
     或点完所有笔 → 点「完成」提交；未达标可整轮重涂（``max_rounds`` 次内）。
"""
import json
import os
import time

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

from utils import logger

from .tuyinStroke import analyze as tuyin_analyze

# agent/custom/action/ → 上三级 = 仓库根；模板给绝对路径，不依赖 Resource 加载了哪个叠加
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
_DEFAULT_TEMPLATE = os.path.join(
    _REPO_DIR, "assets", "resource", "base", "image", "zonghe", "tuoyin.png"
)


@AgentServer.custom_action("tuyinTuMo")
class TuyinTuMo(CustomAction):
    """拓印涂墨：模板确认弹窗 → 识别笔画 → 多笔慢速涂墨 → 回读完成度 → 点「完成」。

    custom_action_param（均可选，有默认）::

        "roi": [508, 189, 331, 336]        # 笔画识别区（石碑）
        "color": [223, 186, 142]           # 笔画颜色 RGB
        "tolerance": 10                    # 每通道容差
        "cell": 5                          # 块粒度 px
        "popup_roi": [208, 88, 224, 394]   # 弹窗确认模板匹配区（左侧"拓印"标题）
        "popup_template": "<abs>/tuoyin.png"
        "degree_roi": [775, 526, 60, 34]   # 完成度百分比 OCR 区（右下角）
        "confirm_target": [769, 586, 88, 35]  # 「完成」按钮 [x,y,w,h]
        "stroke_speed_px_s": 100           # 涂墨移动速度（像素/秒）
        "inter_stroke_ms": 200             # 笔间隔
        "max_rounds": 3                    # 完成度不达标时最多整轮重涂次数
        "done_threshold": 70               # 判定通过的完成度（%）
        "fallback_entry": "panduan_zhujiemian"  # 全部失败后的回退 entry
    """

    _DEFAULT_ROI = [508, 189, 331, 336]
    _DEFAULT_COLOR = [223, 186, 142]
    _DEFAULT_POPUP_ROI = [208, 88, 224, 394]
    _DEFAULT_DEGREE_ROI = [775, 526, 60, 34]
    _DEFAULT_CONFIRM = [769, 586, 88, 35]
    _RECO_NODE = "tuyinTuMo-弹窗确认"
    _DEGREE_NODE = "tuyinTuMo-完成度回读"

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> CustomAction.RunResult:
        p: dict = json.loads(argv.custom_action_param or "{}")
        roi = tuple(p.get("roi", self._DEFAULT_ROI))
        # param 的 color 按 RGB 给（与 pipeline JSON 习惯一致）；截图是 BGR（MaaFw
        # ImageBuffer.get 文档明确 BGR），tuyinStroke.analyze 逐通道比对 → 反转成 BGR
        color_rgb = tuple(p.get("color", self._DEFAULT_COLOR))
        color_bgr = (color_rgb[2], color_rgb[1], color_rgb[0])
        tolerance = int(p.get("tolerance", 10))
        cell = int(p.get("cell", 5))
        popup_roi = p.get("popup_roi", self._DEFAULT_POPUP_ROI)
        popup_template = p.get("popup_template", _DEFAULT_TEMPLATE)
        degree_roi = p.get("degree_roi", self._DEFAULT_DEGREE_ROI)
        confirm = p.get("confirm_target", self._DEFAULT_CONFIRM)
        speed = float(p.get("stroke_speed_px_s", 100))
        inter_ms = int(p.get("inter_stroke_ms", 200))
        max_rounds = int(p.get("max_rounds", 3))
        done_th = int(p.get("done_threshold", 70))
        fallback = p.get("fallback_entry", "panduan_zhujiemian")
        # 点击预算：每笔 = 一次点击（down → 组内插值 move → up）。
        # 笔画数超过预算时不逐笔画，而是把后续笔画首尾相连合并进同一笔
        # （共享同一次 down/up）—— 保证总点击数 ≤ 预算。
        click_budget = int(p.get("click_budget", 6))

        ctrl = context.tasker.controller

        for round_i in range(1, max_rounds + 1):
            image = ctrl.post_screencap().wait().get()
            if image is None:
                logger.warning("[tuyinTuMo] 截图失败")
                return CustomAction.RunResult(success=False)

            # ① 模板匹配确认拓印弹窗还在（第一轮必须确认；重涂轮顺带确认无妨）
            reco = context.run_recognition(
                self._RECO_NODE,
                image,
                pipeline_override={self._RECO_NODE: {
                    "recognition": "TemplateMatch",
                    "roi": popup_roi,
                    "template": [popup_template],
                    "threshold": 0.8,
                }},
            )
            if not reco or not reco.hit:
                logger.info("[tuyinTuMo] 拓印弹窗不在场（可能已通过/倒计时结束），跳过")
                return CustomAction.RunResult(success=True)

            # ② 识别笔画 → 笔画路径（截图 BGR + BGR 色）
            raw, clean, strokes = tuyin_analyze(
                image, roi, color_bgr, tolerance, cell=cell
            )
            if not strokes.strokes:
                logger.warning(
                    f"[tuyinTuMo] 第{round_i}轮未识别到笔画（命中 {int(clean.sum())}px），"
                    f"剩 {max(0, max_rounds - round_i)} 轮"
                )
                time.sleep(1.0)
                continue

            # 笔画路径是 ROI 相对坐标，必须换算成屏幕绝对坐标再下发
            abs_strokes = strokes.absolute(roi)
            logger.info(
                f"[tuyinTuMo] 第{round_i}轮识别：{len(abs_strokes)} 笔, "
                f"覆盖 {strokes.coverage_ratio * 100:.0f}%, 绝对坐标: "
                + "; ".join(
                    f"笔{i + 1}[{len(s)}点:{s[0]}→{s[-1]}]"
                    for i, s in enumerate(abs_strokes)
                )
            )

            # ③ 涂墨：每笔一次点击（touch_down → 组内插值 touch_move → touch_up）。
            #    折线段 move 不是点击，真正的点击 = down/up 对；点击预算内逐笔画，
            #    超预算的笔画首尾相连合并进同一笔（共享一次 down/up），总点击 ≤ 预算。
            #    大段折线（骨架拐点间弦长 50px+）直接 move 会被游戏判"乱划"丢弃，
            #    所以每段插值成 ≤10px 小步 —— 连续平滑的涂墨轨迹。
            logger.info(
                f"[tuyinTuMo] 第{round_i}轮涂墨：{len(strokes.strokes)} 笔 "
                f"(覆盖 {strokes.coverage_ratio * 100:.0f}%)，限速 {speed:.0f}px/s，"
                f"点击预算 {click_budget}"
            )
            max_step_px = 10.0  # 插值步长上限（保证手势连续）
            # 笔画 → 有序点列；超过预算后把剩余笔画串接成"最后一笔"（共享 down/up）
            groups: list[list[tuple[int, int]]] = []
            held_pts: set[tuple[int, int]] = set()  # 孤立点（点按：到达后停留）
            if len(abs_strokes) <= click_budget:
                # 预算内：每笔独立
                for stroke in abs_strokes:
                    pts = list(stroke)
                    if len(pts) < 2:
                        held_pts.add(tuple(pts[0]))
                    groups.append(pts)
            else:
                # 超预算：前 budget-1 笔独立，其余首尾相接进最后一笔
                independent = abs_strokes[: click_budget - 1]
                rest = abs_strokes[click_budget - 1 :]
                for stroke in independent:
                    pts = list(stroke)
                    if len(pts) < 2:
                        held_pts.add(tuple(pts[0]))
                    groups.append(pts)
                merged: list[tuple[int, int]] = []
                for stroke in rest:
                    pts = list(stroke)
                    if len(pts) < 2:
                        held_pts.add(tuple(pts[0]))
                        merged.append(pts[0])
                        merged.append(pts[0])  # 孤立点并入：占位（到达即停留）
                        continue
                    if not merged:
                        merged.append(pts[0])
                    for pt in pts[1:]:
                        merged.append(pt)
                if merged:
                    groups.append(merged)

            logger.info(
                f"[tuyinTuMo] 分组涂墨：{len(groups)} 组（点击）→ "
                + "; ".join(
                    f"组{i + 1}[{len(g)}点 首{g[0]} 尾{g[-1]}]" for i, g in enumerate(groups)
                )
            )
            for gi, g in enumerate(groups):
                if not g:
                    continue
                logger.info(
                    f"[tuyinTuMo] 组{gi + 1}: down({g[0][0]},{g[0][1]}) → "
                    f"{len(g) - 1} 段 → up"
                )
                ctrl.post_touch_down(g[0][0], g[0][1]).wait()
                time.sleep(0.15)
                prev = g[0]
                for (x2, y2) in g[1:]:
                    x1, y1 = prev
                    seg_px = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                    n_steps = max(1, int(seg_px / max_step_px))
                    for k in range(1, n_steps + 1):
                        t = k / n_steps
                        mx = int(round(x1 + (x2 - x1) * t))
                        my = int(round(y1 + (y2 - y1) * t))
                        ctrl.post_touch_move(mx, my).wait()
                        time.sleep(min(0.05, (seg_px / n_steps) / speed))
                    if (x2, y2) in held_pts:
                        time.sleep(0.3)  # 孤立点：到达后停留（点按）
                    prev = (x2, y2)
                ctrl.post_touch_up().wait()
                time.sleep(inter_ms / 1000.0)

            # ④ 回读完成度：达标或已到最后一轮 → 点「完成」
            image2 = ctrl.post_screencap().wait().get()
            deg = context.run_recognition(
                self._DEGREE_NODE,
                image2,
                pipeline_override={self._DEGREE_NODE: {
                    "recognition": "OCR",
                    "roi": degree_roi,
                    "threshold": 0.6,
                }},
            )
            percent = self._parse_percent(
                deg.best_result.text if deg and deg.hit else ""
            )
            logger.info(f"[tuyinTuMo] 完成度回读: {percent}%（阈值 {done_th}%）")
            if percent is not None and percent < done_th and round_i < max_rounds:
                # 未达标且有剩余轮 → 下一轮重涂
                time.sleep(0.5)
                continue
            if percent is not None and percent < done_th:
                # 未达标且轮次已尽 → 不点「完成」（0% 提交=浪费挑战次数），回退
                logger.warning(
                    f"[tuyinTuMo] {max_rounds} 轮涂完仍只有 {percent}%（阈值 {done_th}%），"
                    f"回退 {fallback}"
                )
                try:
                    context.run_task(fallback)
                except Exception as e:
                    logger.warning(f"[tuyinTuMo] 回退 {fallback} 异常：{e}")
                return CustomAction.RunResult(success=False)
            # 达标（或完成度读不到 = 弹窗可能已过）→ 点「完成」提交
            tx, ty, tw, th = confirm
            ctrl.post_click(int(tx + tw / 2), int(ty + th / 2)).wait()
            logger.info("[tuyinTuMo] 已点「完成」提交")
            time.sleep(3.0)
            return CustomAction.RunResult(success=True)

        logger.warning(f"[tuyinTuMo] {max_rounds} 轮后仍未达标，回退 {fallback}")
        try:
            context.run_task(fallback)
        except Exception as e:
            logger.warning(f"[tuyinTuMo] 回退 {fallback} 异常：{e}")
        return CustomAction.RunResult(success=False)

    @staticmethod
    def _parse_percent(text: str) -> int | None:
        """从"42%"这类 OCR 文本里抠数字；抠不到返回 None。"""
        import re

        m = re.search(r"(\d{1,3})\s*%", text or "")
        return int(m.group(1)) if m else None
