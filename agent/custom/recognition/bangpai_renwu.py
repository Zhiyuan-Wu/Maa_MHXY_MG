from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context
from utils import logger

import json
import threading


@AgentServer.custom_recognition("bangpai_renwu_decide")
class BangpaiRenwuDecide(CustomRecognition):
    """帮派任务面板识别 + 黑名单放弃决策（替代原 ``帮派任务单次点击`` 节点的纯 OCR）。

    对任务追踪面板 roi 做一次 OCR，三选一：
    - 命中黑名单关键词（如「金香玉」）→ 先 ``run_task("bangpai_放弃任务")`` 放弃（终点回主界面），
      再 ``run_task("主界面-领取帮派任务")`` 重新领取，然后返回**未命中**（box=None）。JumpBack
      弹栈回到中心节点「已领取帮派任务」，其 next 循环（间隔/单次点击）接着处理新领到的任务；
    - 无黑名单且识别到帮派任务（青龙/白虎/朱雀/玄武）→ 返回该 OCR 框，交给节点
      ``action:"Click"`` 点击，继续 pipeline 内链路；
    - 其余 → 未命中。

    **连续未命中兜底**：未命中（面板无文字 / 有文字但无四堂名也无黑名单）累加本账号的连续计数，
    达 ``_MISS_STREAK_LIMIT``（5）次则 ``run_task("打开大地图_69副本")`` 飞回长安一次，打破
    「面板 OCR 持续空/误读」卡死，再归零；任意命中分支（黑名单放弃 / 四堂名点击）归零。
    计数按账号（adb_serial）分桶——本类是 ``@AgentServer.custom_recognition`` 注册的单例，
    run_5r standalone 下被 5 个并发 Tasker 共享同一实例；单值计数会让兄弟账号的命中清掉
    真正卡住账号正在累积的计数（它永远到不了上限）。实现与 ``shimen_renwu._miss_streaks`` 同款。
    """

    _ABANDON_ENTRY = "bangpai_放弃任务"
    _REACQUIRE_ENTRY = "主界面-领取帮派任务"   # 放弃后重新领取的链路入口（主界面→活动→参加→领取）
    _ACCEPT_KEYWORD = ["青龙", "白虎", "朱雀", "玄武"]
    _DEFAULT_ROI = [1034, 171, 235, 336]
    _DEFAULT_BLACKLIST = ["金香玉", "九转", "蛇胆酒", "长寿面", "珍露酒"]
    _DEFAULT_BLACKLIST_AFTER8 = ["蛇胆酒"]
    # 连续 N 次未识别到帮派任务 → 飞回长安一次（fuben69.json 的可复用 entry，按键开图 + 传送长安）
    _MISS_STREAK_LIMIT = 5
    _BACK_TO_MAIN_ENTRY = "打开大地图_69副本"
    # 连续未命中计数按账号隔离：本类是单例、被 5 个并发 Tasker 共享（run_5r._register_customs
    # 把同一个 inst 注册到每账号 Resource）。单值计数会让兄弟账号的命中（_reset_miss_streak）
    # 清掉真正卡住账号正在累积的计数 → 它永远到不了 _MISS_STREAK_LIMIT。按 adb_serial 分桶 +
    # 锁，各号独立计数，互不踩踏。（与 shimen_renwu._miss_streaks 同款实现）
    _STREAK_LOCK = threading.Lock()
    _miss_streaks: dict = {}  # {adb_serial: 连续未命中次数}

    @staticmethod
    def _account_tag(context: Context) -> str:
        """稳定的账号标识：优先 adb_serial（如 127.0.0.1:16512），回退 uuid，再回退 ?。

        与 ``shimen_renwu._account_tag`` 同款——通过 controller 的 C handle 查，不依赖 Python
        wrapper 对象身份，5 开并发下跨 analyze 调用稳定（``id(context.tasker)`` 不稳定，禁用）。
        """
        try:
            ctrl = context.tasker.controller
            serial = (ctrl.info or {}).get("adb_serial")
            if serial:
                return serial
            return ctrl.uuid or "?"
        except Exception as e:
            logger.warning(f"[bangpai_decide] 取账号标识失败: {type(e).__name__}: {e}")
            return "?"

    def _reset_miss_streak(self, context: Context) -> None:
        """任意命中分支（黑名单放弃 / 四堂名点击）调用，归零**本账号**的连续未命中计数。

        计数按 adb_serial 分桶（见 ``_miss_streaks``），故一个账号命中不会清掉其它账号
        正在累积的计数——修 5 开下「兄弟账号命中踩踏卡住账号计数」的竞态。
        """
        tag = self._account_tag(context)
        with self._STREAK_LOCK:
            self._miss_streaks[tag] = 0

    def _on_miss(self, context: Context) -> CustomRecognition.AnalyzeResult:
        """记录一次「未识别到帮派任务」。

        连续达到 ``_MISS_STREAK_LIMIT`` 次时，``run_task`` 执行一次 ``_BACK_TO_MAIN_ENTRY``
        （打开大地图_69副本：按键开图 + 传送长安）兜底重置屏幕状态，再归零**本账号**计数；
        否则只递增并返回未命中。计数按 adb_serial 分桶，5 开下各账号互不踩踏。
        ``run_task`` 在锁外执行，避免长时间持锁阻塞其它账号。
        """
        tag = self._account_tag(context)
        with self._STREAK_LOCK:
            streak = self._miss_streaks.get(tag, 0) + 1
            self._miss_streaks[tag] = streak
        if streak >= self._MISS_STREAK_LIMIT:
            logger.info(f"[bangpai_decide] [{tag}] 连续 {streak} 次未识别到帮派任务，回大地图")
            context.run_task(self._BACK_TO_MAIN_ENTRY)  # 锁外：回大地图期间不阻塞其它账号
            with self._STREAK_LOCK:
                self._miss_streaks[tag] = 0
        else:
            logger.info(f"[bangpai_decide] [{tag}] 未识别到帮派任务（连续 {streak}/{self._MISS_STREAK_LIMIT}）")
        return CustomRecognition.AnalyzeResult(box=None, detail="未识别到帮派任务")

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult:
        param: dict = json.loads(argv.custom_recognition_param or "{}")
        roi = self._DEFAULT_ROI
        blacklist = self._DEFAULT_BLACKLIST

        image = context.tasker.controller.post_screencap().wait().get()
        reco = context.run_recognition(
            "帮派任务面板",
            image,
            pipeline_override={
                "帮派任务面板": {
                    "roi": roi,
                    "expected": [""],
                    "recognition": "OCR",
                }
            },
        )

        if not reco or not reco.hit:
            # logger.info("[bangpai_decide] 面板无文字，未命中")
            return self._on_miss(context)

        full_text = "".join(r.text for r in reco.all_results)
        if "8/10" in full_text or "9/10" in full_text or "10/10" in full_text:
            _black_list = self._DEFAULT_BLACKLIST_AFTER8
        else:
            _black_list = self._DEFAULT_BLACKLIST
        # logger.info(f"[bangpai_decide] 生效黑名单={_black_list} 面板 OCR: {full_text}")

        # ① 黑名单优先：放弃 → 重新领取 → 回中心节点。
        #    本节点返回未命中（box=None），JumpBack 弹栈回到「已领取帮派任务」，
        #    其 next 循环（间隔/单次点击）会接着处理新领到的任务。
        hit_black = next((kw for kw in _black_list if kw and kw in full_text), None)
        if hit_black:
            logger.info(f"[bangpai_decide] 命中黑名单「{hit_black}」，放弃后重新领取")
            context.run_task(self._ABANDON_ENTRY)     # 放弃，终点回主界面
            context.run_task(self._REACQUIRE_ENTRY)   # 主界面→活动→参加→领取帮派任务
            self._reset_miss_streak(context)
            return CustomRecognition.AnalyzeResult(box=None, detail=f"黑名单:{hit_black},已放弃并重新领取")

        # ② 无黑名单且识别到帮派任务（四堂名）：返回其框，交给节点 action:Click
        for res in reco.all_results:
            if any(_key in res.text for _key in self._ACCEPT_KEYWORD):
                # logger.info(f"[bangpai_decide] 识别到帮派任务，返回框 {res.box}")
                self._reset_miss_streak(context)
                return CustomRecognition.AnalyzeResult(box=res.box, detail="点击帮派任务")

        # ③ 都没有：未命中 → 计数，连续 _MISS_STREAK_LIMIT 次则回大地图打破卡死
        # logger.info("[bangpai_decide] 未识别到帮派任务，未命中")
        return self._on_miss(context)
