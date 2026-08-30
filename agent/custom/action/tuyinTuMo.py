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
import random
import threading
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


# ---------------- 人手噪声（noise 系数 0 = 完全旧行为） ----------------
# 目的：打掉"每次执行逐字节相同"的机器人指纹。四类噪声都只动**表现层**（触点坐标/
# 时序/笔画分组/按钮点位），不动识别参数（color/tolerance/cell —— 动它们会伤识别稳定性）。
# 约束（见 memory tuyin-touch-gesture-needs-small-steps）：相邻触点仍须 ≤10px 连续小步，
# 故抖动幅度 ≤2px、且以"抖动后的顶点"做插值（插值点自身只 ±1px 微抖，不会破坏连续性）。


def _jit(p: tuple[int, int], amp: float) -> tuple[int, int]:
    """坐标抖动：p 加 ±amp px 随机偏移（amp=0 原样返回）。"""
    if amp <= 0:
        return (int(p[0]), int(p[1]))
    return (
        int(round(p[0] + random.uniform(-amp, amp))),
        int(round(p[1] + random.uniform(-amp, amp))),
    )


def _merge_pair(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """两笔连成一笔（a → b），中间可随机反转 b 让连接更顺（像人写连笔挑顺手方向）。

    与超预算尾部合并同机制：纯点列首尾相接，共享一次 down/up；连接线是两笔端点的
    直线插值，端点都在石碑 ROI 内 → 连接线也在 ROI 凸包内，不会画出弹窗。
    孤立点（首尾重合的占位 double-append）沿用尾部合并的占位语义。
    """
    if random.random() < 0.5:
        b = list(reversed(b))
    return list(a) + list(b)


def _random_merge(
    strokes: list[list[tuple[int, int]]], noise: float
) -> tuple[list[list[tuple[int, int]]], list[str]]:
    """随机挑 1-2 对笔画连成一笔（连笔），偏向端点近的对（人连笔连相邻笔画）。

    返回（新笔画列表, 变更说明）供日志。只有 <2 笔 / noise=0 时原样返回。
    每次执行的合并对数、挑中谁、是否反转都是随机的 → down/up 次数与分组结构
    每轮不同（结构级指纹，比逐像素抖动更难被识别）。
    """
    if noise <= 0 or len(strokes) < 2:
        return strokes, []
    out = [list(s) for s in strokes]
    notes: list[str] = []
    n_pairs = random.randint(1, 2)
    for _ in range(n_pairs):
        if len(out) < 2:
            break
        # 候选 = 所有余笔对，按"尾→头直线距离"升序；距离本身带随机扰动，
        # 让"最近对"不总被选中（多数时候近、偶尔远——像人偶尔跨笔连）。
        pairs = []
        for i in range(len(out)):
            for j in range(len(out)):
                if i == j:
                    continue
                d = ((out[i][-1][0] - out[j][0][0]) ** 2
                     + (out[i][-1][1] - out[j][0][1]) ** 2) ** 0.5
                pairs.append((d * random.uniform(0.6, 1.6), i, j))
        pairs.sort()
        _, i, j = pairs[0]
        merged = _merge_pair(out[i], out[j])
        notes.append(f"#{j + 1}→#{i + 1}" + ("(反)" if random.random() < 0.5 else ""))
        # 先删大索引再删小索引，避免索引位移
        for k in sorted((i, j), reverse=True):
            del out[k]
        out.insert(min(i, j), merged)
    return out, notes


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
        "huanzhi_roi": [490, 586, 100, 37]    # 「换纸」按钮 OCR 区（兜底首选：点它换新纸直接返回）
        "huanzhi_limit": 5                    # 每账号换纸上限（类内计数，超限强制走关闭+回退）
        "close_target": [999, 70, 14, 13]     # 「关闭」按钮 [x,y,w,h]（兜底次选：换纸不在时点它+fallback）
        "stroke_speed_px_s": 100           # 涂墨移动速度（像素/秒）
        "inter_stroke_ms": 200             # 笔间隔
        "max_rounds": 3                    # 完成度不达标时最多整轮重涂次数
        "done_threshold": 70               # 判定通过的完成度（%）
        "fallback_entry": "panduan_zhujiemian"  # 全部失败后的回退 entry
        "noise": 1.0                       # 人手噪声系数 0.0~1.0（0=完全旧行为，可回退）：
                                           #   ① 触点 ±1px / 拐点 ±2px 坐标抖动
                                           #   ② 速度/停顿/步进时序抖动
                                           #   ③ 随机合并 1-2 对笔画（连笔，像尾部合并共享 down/up）
                                           #   ④ 「完成」按钮点击去中心化
    """

    _DEFAULT_ROI = [508, 189, 331, 336]
    _DEFAULT_COLOR = [223, 186, 142]
    _DEFAULT_POPUP_ROI = [208, 88, 224, 394]
    _DEFAULT_DEGREE_ROI = [775, 526, 60, 34]
    _DEFAULT_CONFIRM = [769, 586, 88, 35]
    _DEFAULT_HUANZHI_ROI = [490, 586, 100, 37]  # 「换纸」按钮 OCR 区（轮次耗尽时的首选出路）
    _DEFAULT_CLOSE = [999, 70, 14, 13]          # 弹窗右上角「关闭」（换纸不在时的次选出路）
    _RECO_NODE = "tuyinTuMo-弹窗确认"
    _DEGREE_NODE = "tuyinTuMo-完成度回读"
    _HUANZHI_NODE = "tuyinTuMo-换纸识别"
    # 换纸次数上限（per 账号）：换纸只是"换张纸重涂"，不消耗失败次数但也不产出——
    # 涂墨持续不达标时无限换纸 = 无限空转（08-30 X2400 同型风险）。超过上限强制走
    # 关闭 + panduan_zhujiemian 有损退出，把控制权还给编排层。
    _HUANZHI_LIMIT = 5
    # 本类是 @AgentServer.custom_action 注册的单例，run_5r 下被 5 个并发 Tasker 共享同一实例
    # （见 shimen_renwu.py _miss_streaks 同款坑）：换纸计数必须按 adb_serial 分桶 + 锁，
    # 否则兄弟账号的换纸会互相吃掉额度、或一起把某个号顶到上限。
    _HUANZHI_LOCK = threading.Lock()
    _huanzhi_counts: dict = {}  # {adb_serial: 已换纸次数}

    @staticmethod
    def _account_tag(context: Context) -> str:
        """稳定的账号标识：优先 adb_serial（如 127.0.0.1:16512），回退 uuid，再回退 ?。

        与 ``logOcr._account_tag`` / ``shimen_renwu._account_tag`` 同款——通过 controller 的
        C handle 查，不依赖 Python wrapper 对象身份，5 开并发下跨 run 调用稳定。
        """
        try:
            ctrl = context.tasker.controller
            serial = (ctrl.info or {}).get("adb_serial")
            if serial:
                return serial
            return ctrl.uuid or "?"
        except Exception as e:
            logger.warning(f"[tuyinTuMo] 取账号标识失败: {type(e).__name__}: {e}")
            return "?"

    def _huanzhi_left(self, context: Context) -> int:
        """读**本账号**的换纸剩余额度（不修改计数）。"""
        tag = self._account_tag(context)
        with self._HUANZHI_LOCK:
            return max(0, self._HUANZHI_LIMIT - self._huanzhi_counts.get(tag, 0))

    def _huanzhi_used_one(self, context: Context) -> None:
        """记一次换纸（**本账号**计数 +1）。"""
        tag = self._account_tag(context)
        with self._HUANZHI_LOCK:
            self._huanzhi_counts[tag] = self._huanzhi_counts.get(tag, 0) + 1

    def _bailout(self, context, ctrl, huanzhi_roi, close, fallback, reason, n=0.0, huanzhi_limit=None):
        """轮次耗尽的兜底出路（两选一，优先换纸；换纸每账号最多 ``huanzhi_limit`` 次）：

        1. **换纸**：OCR ``huanzhi_roi`` 识别「换纸」按钮，在场**且本账号换纸额度未耗尽** →
           点击它。换纸后游戏换一张新的字重开涂墨考验——弹窗被游戏自身流程消化，**直接返回**
           （不点关闭、不跑 fallback）：外层 pipeline 的下一轮 tuoyinjiance 会重新走
           "弹窗识别→涂墨"，等于把重试机会交还给编排层。额度用尽（涂墨持续不达标，换纸
           无限循环空转风险，08-30 X2400 同型）→ 视同换纸不在场，强制走 ②。
        2. **关闭 + fallback**：换纸不在（或额度已尽）→ 点弹窗右上角「关闭」退出弹窗（防弹窗
           留场堵后续任务入口），再跑 ``fallback``（panduan_zhujiemian 清场回主界面）。
        """
        if huanzhi_limit is None:
            huanzhi_limit = self._HUANZHI_LIMIT
        left = self._huanzhi_left(context)
        image = None
        try:
            image = ctrl.post_screencap().wait().get()
        except Exception:
            pass
        if image is not None and left > 0:
            hz = context.run_recognition(
                self._HUANZHI_NODE,
                image,
                pipeline_override={self._HUANZHI_NODE: {
                    "recognition": "OCR",
                    "expected": ["换纸"],
                    "roi": huanzhi_roi,
                    "threshold": 0.6,
                }},
            )
            if hz and hz.hit:
                box = hz.best_result.box  # [x, y, w, h] 识别框
                if n > 0:
                    fx = box[0] + box[2] * random.uniform(0.3, 0.7)
                    fy = box[1] + box[3] * random.uniform(0.3, 0.7)
                else:
                    fx, fy = box[0] + box[2] / 2, box[1] + box[3] / 2
                self._huanzhi_used_one(context)
                used = huanzhi_limit - self._huanzhi_left(context)
                logger.info(
                    f"[tuyinTuMo] {reason} → 点「换纸」({int(fx)},{int(fy)})，换纸后直接返回"
                    f"（已用 {used}/{huanzhi_limit}）"
                )
                ctrl.post_click(int(fx), int(fy)).wait()
                time.sleep(1.0)   # 等换纸动画（新石碑弹出）
                return
        if left <= 0:
            logger.info(
                f"[tuyinTuMo] {reason} → 换纸额度已用尽（{huanzhi_limit}/{huanzhi_limit}），"
                f"强制走关闭 + 回退"
            )
        # 换纸不在场（或额度已尽）→ 点关闭退弹窗 + fallback 清场
        logger.info(f"[tuyinTuMo] {reason} → 「换纸」不在场，点关闭({close}) + 回退 {fallback}")
        cx_, cy_, cw_, ch_ = close
        if n > 0:
            fx = cx_ + cw_ * random.uniform(0.3, 0.7)
            fy = cy_ + ch_ * random.uniform(0.3, 0.7)
        else:
            fx, fy = cx_ + cw_ / 2, cy_ + ch_ / 2
        try:
            ctrl.post_click(int(fx), int(fy)).wait()
            time.sleep(1.0)   # 等弹窗收起
        except Exception as e:
            logger.warning(f"[tuyinTuMo] 点关闭异常（继续 fallback）：{e}")
        try:
            context.run_task(fallback)
        except Exception as e:
            logger.warning(f"[tuyinTuMo] 回退 {fallback} 异常：{e}")

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
        huanzhi_roi = p.get("huanzhi_roi", self._DEFAULT_HUANZHI_ROI)
        huanzhi_limit = int(p.get("huanzhi_limit", self._HUANZHI_LIMIT))
        close = p.get("close_target", self._DEFAULT_CLOSE)
        speed = float(p.get("stroke_speed_px_s", 100))
        inter_ms = int(p.get("inter_stroke_ms", 200))
        max_rounds = int(p.get("max_rounds", 3))
        done_th = int(p.get("done_threshold", 70))
        fallback = p.get("fallback_entry", "panduan_zhujiemian")
        # 点击预算：每笔 = 一次点击（down → 组内插值 move → up）。
        # 笔画数超过预算时不逐笔画，而是把后续笔画首尾相连合并进同一笔
        # （共享同一次 down/up）—— 保证总点击数 ≤ 预算。
        click_budget = int(p.get("click_budget", 6))
        # 人手噪声系数（0.0~1.0）：坐标/时序抖动 + 随机连笔 + 按钮去中心化。
        # 0 = 完全旧行为（出问题一键回退）。
        noise = float(p.get("noise", 1.0))
        n = max(0.0, min(1.0, noise))

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
            # ---- noise ③：随机合并 1-2 对笔画（连笔，同尾部合并共享 down/up）----
            merge_notes: list[str] = []
            if n > 0:
                abs_strokes, merge_notes = _random_merge(abs_strokes, n)
            # ---- noise ①：拐点坐标抖动（±2px 内，插值步长 ≤10px 约束仍满足）----
            if n > 0:
                abs_strokes = [
                    [_jit(pt, 2.0 * n) if k in (0, len(s) - 1) else _jit(pt, 1.0 * n)
                     for k, pt in enumerate(s)]
                    for s in abs_strokes
                ]
            logger.info(
                f"[tuyinTuMo] 第{round_i}轮识别：{len(abs_strokes)} 笔, "
                f"覆盖 {strokes.coverage_ratio * 100:.0f}%, 绝对坐标: "
                + "; ".join(
                    f"笔{i + 1}[{len(s)}点:{s[0]}→{s[-1]}]"
                    for i, s in enumerate(abs_strokes)
                )
                + (f"，连笔: {','.join(merge_notes)}" if merge_notes else "")
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
                # ---- noise ②：本笔时序抖动（速度 ±20%、down 停留 0.10~0.25s）----
                g_speed = speed * random.uniform(0.85, 1.2) if n > 0 else speed
                down_hold = random.uniform(0.10, 0.25) if n > 0 else 0.15
                # 偶发"迟疑"：一笔中随机挑一个点换腕停顿（人手特征）
                hesitate_at = (
                    random.randrange(len(g)) if n > 0 and len(g) > 3 and random.random() < 0.35
                    else -1
                )
                logger.info(
                    f"[tuyinTuMo] 组{gi + 1}: down({g[0][0]},{g[0][1]}) → "
                    f"{len(g) - 1} 段 → up"
                    + (f"（{g_speed:.0f}px/s）" if n > 0 else "")
                )
                ctrl.post_touch_down(g[0][0], g[0][1]).wait()
                time.sleep(down_hold)
                prev = g[0]
                for step_i, (x2, y2) in enumerate(g[1:]):
                    x1, y1 = prev
                    seg_px = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                    # ceil：10~20px 的短段（RDP 拐点相邻/随机合并的跨笔连接线）也给 ≥2 步，
                    # 否则 int(19/10)=1 步 → 单步 19px 跳变（旧代码固有，随机合并放大了概率）
                    # int() 必须：float floor 除返回 float，max(1, 2.0)=2.0 → range(float) 抛
                    # TypeError 被 MaaFw ctypes 吞掉 = 涂墨只按 down 不 move/up（08-30 全军 120min 锁）
                    n_steps = max(1, int(-(-seg_px // max_step_px)))
                    for k in range(1, n_steps + 1):
                        t = k / n_steps
                        # ---- noise ①：插值点 ±1px 微抖（步长 ≤10px 约束下安全）----
                        jx = random.uniform(-1.0, 1.0) * n
                        jy = random.uniform(-1.0, 1.0) * n
                        mx = int(round(x1 + (x2 - x1) * t + jx))
                        my = int(round(y1 + (y2 - y1) * t + jy))
                        ctrl.post_touch_move(mx, my).wait()
                        step_delay = min(0.05, (seg_px / n_steps) / g_speed)
                        if n > 0:
                            step_delay *= random.uniform(0.8, 1.2)
                        time.sleep(step_delay)
                    if (x2, y2) in held_pts:
                        time.sleep(0.3)  # 孤立点：到达后停留（点按）
                    if step_i == hesitate_at:
                        time.sleep(random.uniform(0.08, 0.2))
                    prev = (x2, y2)
                ctrl.post_touch_up().wait()
                inter = inter_ms
                if n > 0:
                    # ---- noise ②：笔间隔 150~350ms ----
                    inter = int(inter_ms * random.uniform(0.75, 1.75))
                time.sleep(inter / 1000.0)

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
                # 未达标且轮次已尽 → 不点「完成」（0% 提交=浪费挑战次数）；
                # 兜底：优先「换纸」直接返回（每账号限 huanzhi_limit 次），额度尽才点关闭 + fallback
                self._bailout(
                    context, ctrl, huanzhi_roi, close, fallback,
                    reason=f"{max_rounds} 轮涂完仍只有 {percent}%（阈值 {done_th}%）", n=n,
                    huanzhi_limit=huanzhi_limit,
                )
                return CustomAction.RunResult(success=False)
            # 达标（或完成度读不到 = 弹窗可能已过）→ 点「完成」提交
            # ---- noise ④：按钮去中心化（30%~70% 区间随机落点，避开正中心指纹）----
            tx, ty, tw, th = confirm
            if n > 0:
                cx = tx + tw * random.uniform(0.3, 0.7)
                cy = ty + th * random.uniform(0.3, 0.7)
            else:
                cx, cy = tx + tw / 2, ty + th / 2
            ctrl.post_click(int(cx), int(cy)).wait()
            logger.info("[tuyinTuMo] 已点「完成」提交")
            time.sleep(3.0)
            return CustomAction.RunResult(success=True)

        self._bailout(
            context, ctrl, huanzhi_roi, close, fallback,
            reason=f"{max_rounds} 轮后仍未达标", n=n,
            huanzhi_limit=huanzhi_limit,
        )
        return CustomAction.RunResult(success=False)

    @staticmethod
    def _parse_percent(text: str) -> int | None:
        """从"42%"这类 OCR 文本里抠数字；抠不到返回 None。"""
        import re

        m = re.search(r"(\d{1,3})\s*%", text or "")
        return int(m.group(1)) if m else None
