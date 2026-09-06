from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context
import json
import random
import threading
import time

from utils import logger


@AgentServer.custom_recognition("shimen_renwu_decide")
class ShimenRenwuDecide(CustomRecognition):
    """师门任务单次点击的识别决策。

    在 agent 侧做"点哪个 / 是否点 / 是否触发子链路"的决策，对师门任务追踪面板做一次 OCR，拼出 ``full_text`` 后：

    - **命中打造类任务**：``full_text`` 里出现 ``_50_MAP`` / ``_60_MAP`` 的某个装备名（value）→
      解析出对应的 (等级, 品类, 装备名)，``override_pipeline`` 改写 dazao 链路的三个 OCR 节点
      （选等级 / 选品类 / 装备名判定），``run_task("dazao")`` 启动打造，最后返回未识别（``box=None``）。
    - **非打造类师门任务**：直接在 agent 内 ``post_click`` 点击识别框中心，等 7 秒（替代原节点
      ``post_delay:7000``），再 ``run_task`` 运行一次「抄本兜底」节点清除点击后出现的「使用抄本」按钮，
      最后返回未识别（``box=None``）。**本识别一律不返回命中信号**——点击副作用由 agent 侧完成.
    - **都没有**：返回 ``box=None``（未命中）；连续 ``_MISS_STREAK_LIMIT`` 次未命中则 ``run_task``
      一次 ``_BACK_TO_MAIN_ENTRY``（打开大地图回长安，兜底重置屏幕状态）再归零计数。任意命中分支
      （①②）归零计数。**计数按账号（adb_serial）分桶**——本类是 ``@AgentServer.custom_recognition``
      注册的单例，run_5r standalone 下被 5 个并发 Tasker 共享同一实例（见 ``run_5r._register_customs``）；
      若用单值，任一账号命中会把全体计数清零，真正卡住的号永远攒不到上限（见 ``_miss_streaks``）。

    MAP 形如 ``{"男衣": "夜魔披风", ...}``：key=品类（dazao 左侧列表项），value=装备名（右侧展示）。
    合并两个 MAP 反查时，同名装备 60 级优先（``setdefault`` 保留先插入者）。

    OCR 的 roi 起点 (x,y) 每次 ``analyze`` 随机波动 ±``_ROI_JITTER``（``_jittered_roi``），打破固定
    roi 下 OCR 持续 badcase 导致的决策死循环。
    """

    _RECO_NAME = "师门任务-单次点击-OCR"
    _DEFAULT_ROI = [1036, 113, 240, 180]
    _ROI_JITTER = 5  # OCR 起点 (x,y) 每次随机波动 ±N，打破固定 ROI 的 OCR badcase 死循环
    _DAZAO_ENTRY = "dazao"
    # override 目标：dazao 链路里「选等级 / 选品类 / 装备名判定」三个 OCR 节点
    _DAZAO_LEVEL_NODE = "打造-切换等级-选择目标等级"
    _DAZAO_CATEGORY_NODE = "打造-切换装备-选择目标装备"
    _DAZAO_NAME_NODE = "打造-打造装备-收集材料"
    # 点击师门任务条目后，面板出现「使用抄本」按钮 —— 用兜底节点清除
    _CHAOBEN_FALLBACK_NODE = "师门任务-任务分支-抄本兜底-入口"
    _CHAOBEN_FALLBACK_TIMEOUT = 1000  # 兜底识别上限，防非抄本任务时阻塞状态机
    # 连续 N 次未识别到师门任务 → 飞回长安一次，打破「面板 OCR 持续空/误读」卡死
    _MISS_STREAK_LIMIT = 5
    _BACK_TO_MAIN_ENTRY = "打开大地图_69副本"
    # 连续未命中计数按账号隔离：本类是单例、被 5 个并发 Tasker 共享（run_5r._register_customs
    # 把同一个 inst 注册到每账号 Resource）。单值计数会让兄弟账号的命中（_reset_miss_streak）
    # 清掉真正卡住账号正在累积的计数 → 它永远到不了 _MISS_STREAK_LIMIT。按 adb_serial 分桶 +
    # 锁，各号独立计数，互不踩踏。
    _STREAK_LOCK = threading.Lock()
    _miss_streaks: dict = {}  # {adb_serial: 连续未命中次数}

    # OCR 误读别名：key = 装备名（canonical，MAP 里的正式名），value = 面板 OCR 常见误读变体。
    # 「桃之夭夭」的「夭」被 PaddleOCR 稳定误读成「天」（08-22 job 实证：6 次 OCR 全部读作
    # 「桃之天天」、精确子串匹配永不命中、打造分支从未触发，全被当普通任务点掉了）。匹配时
    # canonical 与变体任一命中即算；dazao 装备名判定的 expected 也同时带两者（打造面板的 OCR
    # 对同字形同样可能误读，只写 canonical 会 miss）。
    # 后两条是历史日志里的**一次性**误读（08-20 连珠神弓→连珠神写、08-21 冷月弯刀→冷月弯力，
    # 均靠下一帧重 OCR 自愈）；加入作廉价保险——子串很特异、无误报风险。
    _OCR_ALIASES = {
        "桃之夭夭": ["桃之天天"],
        "连珠神弓": ["连珠神写"],
        "冷月弯刀": ["冷月弯力"],
    }

    _50_MAP = {
        # 防具
        "鞋": "绿靴", "腰带": "乱牙咬", "项链": "荧光坠子", "发钗": "媚狐头饰",
        "头盔": "羊角盔", "女衣": "金缕羽衣", "男衣": "钢甲",
        # 武器
        "长戈": "三星戈", "牵星尺": "星帆尺", "云锦扇": "蝉翼锦", "宝珠": "蓬莱珠",
        "弯刀": "冷月弯刀", "降魔杵": "金刚杵", "双短剑": "鱼骨双剑", "长刀": "破天宝刀",
        "鞭": "青藤鞭", "锤": "破甲战锤", "环圈": "赤炎环", "斧钺": "黄金钺",
        "飘带": "云龙绸带", "扇": "劈水扇", "爪刺": "玄冰刺", "弓": "玉腰弯弓",
        "魔棒": "幽路引魂", "枪": "墨杆金钩", "法杖": "星云杖", "剑": "黄金剑",
        "煌剑": "傩面拈花"
    }
    _60_MAP = {
        # 防具
        "鞋": "追星踏月", "腰带": "双魂引", "项链": "风月宝链", "发钗": "玉女发冠",
        "头盔": "水晶帽", "女衣": "霓裳羽衣", "男衣": "夜魔披风",
        # 武器
        "长戈": "天山辰律", "牵星尺": "北落师门", "云锦扇": "桃之夭夭", "宝珠": "金露函烟",
        "弯刀": "埃兰弯刀", "降魔杵": "摩星杵", "双短剑": "落星双剑", "长刀": "秋水刀",
        "鞭": "雪绒鞭", "锤": "震天锤", "环圈": "蛇形月", "斧钺": "乌金鬼头镰",
        "飘带": "七彩罗刹", "扇": "清秋扇", "爪刺": "青刚刺", "魔棒": "满天星",
        "枪": "玄铁矛", "法杖": "天山雪", "剑": "游龙剑", "弓": "连珠神弓",
        "煌剑": "五羊醉舞"
    }

    def _jittered_roi(self) -> list:
        """对 ``_DEFAULT_ROI`` 的起点 (x,y) 各加 ±``_ROI_JITTER`` 随机扰动，宽高不变。

        每次 ``analyze`` 用不同的 roi 去截图裁剪做 OCR，扰动喂给 OCR 的像素，避免某个固定 roi
        下 OCR 持续 badcase（如把装备名/「师门」稳定误识别）导致 agent 每轮做同样的错误决策、卡死。
        """
        x, y, w, h = self._DEFAULT_ROI
        dx = random.randint(-self._ROI_JITTER, self._ROI_JITTER)
        dy = random.randint(-self._ROI_JITTER, self._ROI_JITTER)
        return [x + dx, y + dy, w + dx, h + dy]

    @staticmethod
    def _account_tag(context: Context) -> str:
        """稳定的账号标识：优先 adb_serial（如 127.0.0.1:16512），回退 uuid，再回退 ?。

        与 ``logOcr._account_tag`` 同款——通过 controller 的 C handle 查，不依赖 Python wrapper
        对象身份，5 开并发下跨 analyze 调用稳定（``id(context.tasker)`` 不稳定，禁用）。
        """
        try:
            ctrl = context.tasker.controller
            serial = (ctrl.info or {}).get("adb_serial")
            if serial:
                return serial
            return ctrl.uuid or "?"
        except Exception as e:
            logger.warning(f"[shimen_decide] 取账号标识失败: {type(e).__name__}: {e}")
            return "?"

    def _reset_miss_streak(self, context: Context) -> None:
        """任意命中分支（打造/师门点击）调用，归零**本账号**的连续未命中计数。

        计数按 adb_serial 分桶（见 ``_miss_streaks``），故一个账号命中不会清掉其它账号
        正在累积的计数——修 5 开下「兄弟账号命中踩踏卡住账号计数」的竞态。
        """
        tag = self._account_tag(context)
        with self._STREAK_LOCK:
            self._miss_streaks[tag] = 0

    def _on_miss(self, context: Context) -> CustomRecognition.AnalyzeResult:
        """记录一次「未识别到师门任务」。

        连续达到 ``_MISS_STREAK_LIMIT`` 次时，``run_task`` 执行一次 ``_BACK_TO_MAIN_ENTRY`` 回主界面
        （兜底重置屏幕状态），再归零**本账号**计数；否则只递增并返回未命中。计数按 adb_serial 分桶，
        5 开下各账号互不踩踏。``run_task`` 在锁外执行，避免长时间持锁阻塞其它账号。
        """
        tag = self._account_tag(context)
        with self._STREAK_LOCK:
            streak = self._miss_streaks.get(tag, 0) + 1
            self._miss_streaks[tag] = streak
        if streak >= self._MISS_STREAK_LIMIT:
            logger.info(f"[shimen_decide] [{tag}] 连续 {streak} 次未识别到师门任务，回主界面")
            context.run_task(self._BACK_TO_MAIN_ENTRY)  # 锁外：回主界面期间不阻塞其它账号
            with self._STREAK_LOCK:
                self._miss_streaks[tag] = 0
        else:
            logger.info(f"[shimen_decide] [{tag}] 未识别到师门任务（连续 {streak}/{self._MISS_STREAK_LIMIT}）")
        return CustomRecognition.AnalyzeResult(box=None, detail="未识别到师门任务")

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult:
        param: dict = json.loads(argv.custom_recognition_param or "{}")

        roi_used = self._jittered_roi()
        image = context.tasker.controller.post_screencap().wait().get()
        reco = context.run_recognition(
            self._RECO_NAME,
            image,
            pipeline_override={
                self._RECO_NAME: {
                    "roi": roi_used,
                    "expected": [""],
                    "recognition": "OCR",
                    "threshold": 0.7
                }
            },
        )

        if not reco or not reco.hit or not reco.all_results:
            return self._on_miss(context)

        # 按 y 升序（从上到下）拼接，y 相同再按 x 升序（从左到右）——固定阅读顺序
        results = sorted(reco.all_results,
                         key=lambda r: ((r.box[1] if r.box else 0), (r.box[0] if r.box else 0)))
        full_text = "".join(r.text for r in results)
        logger.info(f"[shimen_decide] 面板 OCR (roi={roi_used}): {full_text}")

        # 合并两个等级 MAP：装备名 → (等级, 品类)。同名装备 60 级优先（setdefault 保留先插入者）。
        # 60 先入表：命中打造时 60 级优先（与旧语义一致——同名时 60 先插入被 setdefault 保留）。
        name_to_target = {}
        for cat, name in self._60_MAP.items():
            name_to_target.setdefault(name, ("60", cat))
        for cat, name in self._50_MAP.items():
            name_to_target.setdefault(name, ("50", cat))
        # 误读变体 → canonical（_OCR_ALIASES 反查）。匹配 full_text 时变体也算命中，取 canonical
        # 去查等级/品类，避免「夭→天」这类稳定 OCR 误读让打造分支永不触发。
        alias_to_name = {a: n for n, al in self._OCR_ALIASES.items() for a in al}

        # ① 命中打造类任务：full_text 含某装备名（或其 OCR 误读变体）→ override dazao 三节点
        #    + run_task dazao。先试 canonical 精确子串；未命中再试误读变体（命中映射回 canonical）。
        hit_name = next((n for n in name_to_target if n in full_text), None)
        if hit_name is None:
            hit_name = next((alias_to_name[a] for a in alias_to_name if a in full_text), None)
        not_own = "拥有0" in full_text or "0/1" in full_text
        if hit_name and not_own:
            level, category = name_to_target[hit_name]
            logger.info(f"[shimen_decide] 命中打造任务: {level}级 {category} -> {hit_name}")
            # 装备名判定的 expected 带 canonical + OCR 误读变体：打造面板 OCR 对同字形（如 夭→天）
            # 同样可能误读，只写 canonical 会在 dazao 链路里 miss 卡死。
            name_expected = [hit_name] + self._OCR_ALIASES.get(hit_name, [])
            context.override_pipeline({
                self._DAZAO_LEVEL_NODE: {"expected": [level]},
                self._DAZAO_CATEGORY_NODE: {"expected": [category]},
                self._DAZAO_NAME_NODE: {"expected": name_expected},
            })
            context.run_task(self._DAZAO_ENTRY)

            self._reset_miss_streak(context)
            return CustomRecognition.AnalyzeResult(box=None, detail=f"打造任务:{hit_name} 完成")

        # ② 非打造类师门任务：agent 内直接下发点击，返回未识别（box=None）
        for res in results:
            if "师门" in res.text:
                center_x = res.box[0] + res.box[2] // 2
                center_y = res.box[1] + res.box[3] // 2
                logger.info(f"[shimen_decide] 识别到师门任务，agent 内点击 ({center_x}, {center_y})")
                context.tasker.controller.post_click(center_x, center_y).wait()
                time.sleep(7)  # 原节点 post_delay:7000，等任务面板跳转

                # 点击后面板可能出现「使用抄本」按钮 → 运行一次兜底节点清除。
                # run_task 以该节点为 entry 同步执行；节点 next 为空，命中即点一次即结束。
                # 用 override 把 timeout 收窄到 1s，避免非抄本任务时白等 20s 阻塞状态机。
                context.run_task(
                    self._CHAOBEN_FALLBACK_NODE,
                    pipeline_override={
                        self._CHAOBEN_FALLBACK_NODE: {"timeout": self._CHAOBEN_FALLBACK_TIMEOUT},
                    },
                )
                self._reset_miss_streak(context)
                return CustomRecognition.AnalyzeResult(box=None, detail="已点击师门任务")

        # ③ 都没有：未命中 → 计数，连续 _MISS_STREAK_LIMIT 次则回主界面打破卡死
        return self._on_miss(context)
