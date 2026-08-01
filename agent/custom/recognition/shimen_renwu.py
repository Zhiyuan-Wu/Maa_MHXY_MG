from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context
import json

from utils import logger


@AgentServer.custom_recognition("shimen_renwu_decide")
class ShimenRenwuDecide(CustomRecognition):
    """师门任务单次点击的识别决策。

    在 agent 侧做"点哪个 / 是否点 / 是否触发子链路"的决策，对师门任务追踪面板（``_DEFAULT_ROI``）做一次 OCR，拼出 ``full_text`` 后：

    - **命中打造类任务**：``full_text`` 里出现 ``_50_MAP`` / ``_60_MAP`` 的某个装备名（value）→
      解析出对应的 (等级, 品类, 装备名)，``override_pipeline`` 改写 dazao 链路的三个 OCR 节点
      （选等级 / 选品类 / 装备名判定），``run_task("dazao")`` 启动打造，最后返回该装备名的识别框
      交给节点 ``action: Click`` 点击（仿 bangpai：识别 + 副作用 + 返回框）。
    - **非打造类师门任务**：回退为原行为——返回含"师门"的识别框点击。
    - **都没有**：返回 ``box=None``（未命中）。

    MAP 形如 ``{"男衣": "夜魔披风", ...}``：key=品类（dazao 左侧列表项），value=装备名（右侧展示）。
    合并两个 MAP 反查时，同名装备 60 级优先（``setdefault`` 保留先插入者）。
    """

    _RECO_NAME = "师门任务-单次点击-OCR"
    _DEFAULT_ROI = [1036, 113, 240, 180]
    _DAZAO_ENTRY = "dazao"
    # override 目标：dazao 链路里「选等级 / 选品类 / 装备名判定」三个 OCR 节点
    _DAZAO_LEVEL_NODE = "打造-切换等级-选择目标等级"
    _DAZAO_CATEGORY_NODE = "打造-切换装备-选择目标装备"
    _DAZAO_NAME_NODE = "打造-打造装备-收集材料"

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
    }

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult:
        param: dict = json.loads(argv.custom_recognition_param or "{}")

        image = context.tasker.controller.post_screencap().wait().get()
        reco = context.run_recognition(
            self._RECO_NAME,
            image,
            pipeline_override={
                self._RECO_NAME: {
                    "roi": self._DEFAULT_ROI,
                    "expected": [""],
                    "recognition": "OCR",
                    "threshold": 0.7
                }
            },
        )

        if not reco or not reco.hit or not reco.all_results:
            return CustomRecognition.AnalyzeResult(box=None, detail="未识别到师门任务")

        # 按 y 升序（从上到下）拼接，y 相同再按 x 升序（从左到右）——固定阅读顺序
        results = sorted(reco.all_results,
                         key=lambda r: ((r.box[1] if r.box else 0), (r.box[0] if r.box else 0)))
        full_text = "".join(r.text for r in results)
        logger.info(f"[shimen_decide] 面板 OCR: {full_text}")

        # 合并两个等级 MAP：装备名 → (等级, 品类)。
        name_to_target = {}
        for cat, name in self._60_MAP.items():
            name_to_target.setdefault(name, ("60", cat))
        for cat, name in self._50_MAP.items():
            name_to_target.setdefault(name, ("50", cat))

        # ① 命中打造类任务：full_text 含某装备名 → override dazao 三节点 + run_task dazao
        hit_name = next((n for n in name_to_target if n in full_text), None)
        not_own = "拥有0" in full_text or "0/1" in full_text
        if hit_name and not_own:
            level, category = name_to_target[hit_name]
            logger.info(f"[shimen_decide] 命中打造任务: {level}级 {category} -> {hit_name}")
            context.override_pipeline({
                self._DAZAO_LEVEL_NODE: {"expected": [level]},
                self._DAZAO_CATEGORY_NODE: {"expected": [category]},
                self._DAZAO_NAME_NODE: {"expected": [hit_name]},
            })
            context.run_task(self._DAZAO_ENTRY)

            return CustomRecognition.AnalyzeResult(box=None, detail=f"打造任务:{hit_name} 完成")

        # ② 非打造类师门任务：原行为，点击师门任务条目
        for res in results:
            if "师门" in res.text:
                logger.info(f"[shimen_decide] 识别到师门任务，返回框 {res.box}")
                return CustomRecognition.AnalyzeResult(box=res.box, detail="点击师门任务")

        # ③ 都没有：未命中
        logger.info(f"[shimen_decide] 未识别到师门任务")
        return CustomRecognition.AnalyzeResult(box=None, detail="未识别到师门任务")
