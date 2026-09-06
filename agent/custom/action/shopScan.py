"""商店物品扫描 / 出售前置识别的自定义 action。

场景：在「商店 / 摆摊 / 售卖」界面，把「待售卖区」均分为 5 行 4 列的格点，
逐格点击 → 判断是否弹出物品详情页 → OCR 商品名 / 当前价 / 市场价 / 当前价变化 → 关闭 → 下一格。
**每一格都产一条记录**（``sellable`` 标记是否弹出详情），全部结果汇总成结构化数据落盘到
``agent/data/shop_scan_result.json``，供后续（按价格规则筛选、自动点「出售」等）消费。

写法参照：
- ``fillCaptcha`` —— 自定义 action 内串起「截图 → run_recognition(pipeline_override
  一次性节点) → post_click」的整条流水（本类的主体骨架）；
- ``shimen_renwu`` / ``logOcr`` —— OCR 用 ``expected:[""]`` 做裸读、按账号分桶、
  文件落盘 + 锁。

本类把摆摊「我要出售」页的两件事合在一个 action 里跑：

1. **阶段 A — 空栏位清点**：在给定 4×2=8 个采样点（背包栏位矩阵）上做 ColorMatch。
   色域命中（浅米黄，``[234,220,201]–[254,240,221]``）= **该格有物品**（栏位底色被物品图标占据）；
   未命中（颜色更深，``[234..]`` 之外）= **空栏位**。统计空栏位数量 + 各点命中状态。
2. **阶段 A2 — 过期标记扫描**：在与 A 同款 4×2 网格（起点 ``[163,185]``、roi ``80×80``、
   步长 ``285/103``）上对 ``baitan/expired.png`` 做 **TemplateMatch（green_mask 开启）**。
   该模板约一半像素是纯绿 (0,255,0) 遮罩，green_mask 让匹配忽略绿色区、只比对真实"过期"
   角标；逐点记录最佳匹配分数（``best_result.score``）。**分数 ≥ 0.8 → 该格记 ``expired``
   （过期物品），否则 ``valid``**。
3. **阶段 C — 下架过期物品**：**从后向前**逐个点击过期格点（点击坐标用 **A1 网格同索引**
   的 roi，例 A2 (0,0) 过期 → 点 [365,206]），每点一格 ``run_task("摆摊-下架过期物品")``
   执行取回（子任务内把 next 覆写成直接串「摆摊-关闭浮窗」，因独立子任务 JumpBack 栈空）。
   **必须倒序**：post 取回后待售队列向前移动，倒序保证未处理格点坐标不错位。下架成功数 = y
   （查 TaskDetail 节点 recognition.hit，防 on_error→空节点假成功）。
4. **可上架计算**：``listable = min(8, x + y)``，x=空栏位数、y=下架数。
5. **阶段 B（可选，默认关）— 物品价格扫描**：``enable_price_scan=true`` 时把「待售卖区」均分
   为 5×4 格点，逐格点击 → 判断详情页 → OCR 商品名/当前价/市场价1/市场价2/当前价变化 →
   关闭 → 下一格。默认关闭因会盲点 20 格（误售/误点风险）。

写法参照：
- ``fillCaptcha`` —— 自定义 action 内串起「截图 → run_recognition(pipeline_override
  一次性节点) → post_click」的整条流水（本类的主体骨架）；
- ``shimen_renwu`` / ``logOcr`` —— OCR 用 ``expected:[""]`` 做裸读、按账号分桶、
  文件落盘 + 锁；
- pipeline 原生 ``ColorMatch``（``assets/resource/mac/pipeline/ClickkeyChangeClick.json``
  的 ``打开大地图_69副本``）—— ``upper``/``lower`` RGB 上下界 + ``count``。

custom_action_param（均可选，缺省走类常量占位符）::

    {
      # —— 阶段 A：空栏位清点（8 点 ColorMatch）——
      "empty_slots": {
        "origin": [365,206],     # (0,0) roi 左上角
        "col_dx": 300,           # 列步长 (0,1).x-(0,0).x
        "row_dy": 103,           # 行步长 (1,0).y-(0,0).y
        "rows": 4, "cols": 2,
        "cell_w": 30, "cell_h": 30,
        "lower": [234,220,201],  # 命中此色域 = 有物品；色更深 = 空栏位
        "upper": [254,240,221],
        "count": 720             # 命中像素数下限（绝对值）= cell_w*cell_h*0.8
      },
      # —— 阶段 A2：过期标记模板扫描（8 点 TemplateMatch + green_mask）——
      "expired_marks": {
        "template": "<abs>/baitan/expired.png",  # 过期角标模板（带绿色遮罩）
        "origin": [163,185],    # (0,0) roi 左上角（与 A1 不同的另一套网格起点）
        "col_dx": 285,          # 列步长（横向格点间距）
        "row_dy": 103,          # 行步长（同 A1）
        "rows": 4, "cols": 2,
        "cell_w": 80, "cell_h": 80,  # roi 比 A1 大
        "threshold": 0.7        # 模板匹配置信度门限
        # green_mask 在代码里硬编码为 true（expired.png 含纯绿遮罩）
      },
      # —— 阶段 B：物品价格扫描 ——
      "grid_roi":        [751,149,354,449],          # 待售卖区整体 roi [x,y,w,h]
      "rows": 5, "cols": 4,                     # 格点行列数
      "close_template":  "zonghe/baitan_xiangqing_guanbi.png", # 关闭按钮模板（绝对路径最稳）
      "close_roi":       [977,31,92,83],          # 关闭按钮可能出现的区域
      "name_roi":        [763,76,224,44],          # 商品名 OCR 区
      "price_roi":       [827,454,141,42],          # 当前价 OCR 区
      "market_roi":      [396,203,171,42],          # 市场价1（最高卖单）OCR 区
      "market2_roi":     [397,314,132,41],          # 市场价2（第二高卖单）OCR 区
      "price_delta_roi": [884,493,80,42],           # 当前价变化 OCR 区
      "detail_wait":     1.0,                   # 点击格点后等弹窗的秒数
      "close_wait":      0.5,                   # 点关闭后等回归的秒数
      "threshold":       0.7                    # OCR / 模板匹配置信度门限
    }

阶段 A 语义：ColorMatch 命中（像素落在 ``[lower,upper]`` 色域内）= **有物品**（非空）；
未命中（颜色更深、在色域外）= **空栏位**。``count`` 默认按 cell 面积 80% 算（30×30×0.8=720）。

价格清洗：``price`` / ``market_price1`` / ``market_price2`` 一律只保留 ``[0-9]``
（游戏显示千分位分隔符，OCR 不稳；价格必为整数，剥分隔符即可）。非数字文本（"推荐价格"）→ ``""``。
``price_delta`` 是百分比（"+10%"）保留原值，且必须落在合法集合（``_VALID_PRICE_DELTA``，
共 11 种：±10/20/30/40/50% + 空）内，否则该格 ``sellable=False``。
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from os.path import dirname, join, normpath

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

from utils import logger


# 价格清洗：游戏金币显示成带千分位分隔符的整数（"4,385"），但 OCR 常把分隔符读成
# 句点（"4.385"）/空格等。价格**一定是整数**，故统一只保留 [0-9]，剥掉一切分隔符
# （复刻 logOcr._DIGIT_RE 思路）。非数字文本（如"推荐价格"）→ ""。
_DIGIT_RE = re.compile(r"\D")

# price_delta 合法取值（共 11 种）：±10% / ±20% / ±30% / ±40% / ±50% （正负号不可省略）+ 空。
# OCR 出的 delta 不在此集合 → 视为识别不可靠，该格 sellable=False（详情仍照常关闭）。
_VALID_PRICE_DELTA = frozenset({
    "", "+10%", "+20%", "+30%", "+40%", "+50%",
    "-10%", "-20%", "-30%", "-40%", "-50%",
})


# agent/custom/action/ → 上三级 = 仓库根（含 assets/）。模板给绝对路径，不依赖
# Resource 是否加载了某个服务器叠加（与 fillCaptcha 同款手法）。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
# 阶段 B 关闭按钮模板：用 image 文件夹的相对路径（Resource 已加载 base，与
# shop_test.json 同款）；相对路径由 MaaFw 资源层解析。
_DEFAULT_CLOSE_TEMPLATE = "zonghe/baitan_xiangqing_guanbi.png"
# 结构化结果落盘路径（与 logOcr 的 account_info.log 同目录 agent/data/）
_RESULT_PATH = normpath(join(_THIS_DIR, "..", "..", "data", "shop_scan_result.json"))

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
_SKILL_BOOK = [
    # 原有
    "再生", "法术波动", "法术连击",
    # 2026-08-19 idx=7 摆摊「我要出售」兽诀列表滚动 OCR 实录（滚到底，滤噪声后 +41 项）
    "强力", "必杀", "偷袭", "连击", "突进", "法术暴击",
    "冥思", "魔之心", "会心", "吸血", "神佑复生",
    "招架", "神迹", "敏捷", "反震", "反击", "防御",
    "迟钝", "土属性吸收", "固法", "火属性吸收", "幸运", "鬼魂",
    "水属性吸收", "雷属性吸收",
    "落岩", "驱鬼", "精神集中", "永恒", "雷击", "水攻",
    "烈火", "感知", "毒", "隐身", "强壮", "协力",
    "跃进", "先发", "法术跃进", "法术招架",
]
_HUIDAN = [
    # 原有
    "慧心", "静岳",
    # 2026-08-19 idx=7 摆摊「我要出售」列表滚动 OCR 实录（滤价格/等级角标噪声后 22 项）
    "迅敏", "勘破", "阴伤", "愤恨", "狂怒", "连环", "奇袭", "无畏",
    "擅咒", "焦渴", "灵身", "狙刺", "灵光",
    "坚甲", "圣洁", "矫健", "御法", "顺势",
    "自然", "了然", "护佑",
]

# 可售卖物品 → 最低卖出价（MIN_PRICE 硬下限）。键可以是：
#   - 字面物品名（"临时符"、"玫瑰花"）；
#   - **模块级变量名**（"_50_MAP"/"_60_MAP" 等 dict → 展开 value；"_SKILL_BOOK"/"_HUIDAN"
#     等 list → 展开元素）——同组元素共享一个价格。
# 展开结果缓存在 _SELL_TABLE：{物品名: 最低价}。物品名只要作为 OCR 文本子串命中即算可售。
_MIN_PRICE = {
    "_50_MAP": 500,
    "_60_MAP": 900,
    "临时符": 0,
    "_SKILL_BOOK": 4201,
    "_HUIDAN": 4200,
    "玫瑰花": 2700,
    "金兰花": 2700
}


def _expand_sell_table(min_price_def):
    """把 ``_MIN_PRICE``（键可为变量名）展开成 ``{物品名: 最低价}``。

    键是本模块的全局变量名（以 ``_`` 开头、能在 globals() 找到）→ dict 取 values、
    list 取元素，共享同一价格；否则视为字面物品名。展开在 import 期一次完成。
    """
    g = globals()
    table = {}
    for key, price in min_price_def.items():
        if key.startswith("_") and key in g and isinstance(g[key], (dict, list, tuple)):
            names = list(g[key].values()) if isinstance(g[key], dict) else list(g[key])
        else:
            names = [key]
        for n in names:
            table[n] = price
    return table


_SELL_TABLE = _expand_sell_table(_MIN_PRICE)

@AgentServer.custom_action("shopScan")
class ShopScan(CustomAction):
    """商店待售卖区格点扫描器。

    流程（每个格点）::

        ① 点击格点中心 (cx, cy)；
        ② 等 detail_wait 秒让详情弹窗弹出（理想是用 wait_freezes，见下注）；
        ③ 截图，在 close_roi 内 TemplateMatch close_template：
           - 未命中 → ``sellable=False``，记一条空记录（无 name/price…），不点关闭；
           - 命中   → 进入 ④；
        ④ 分别在 name_roi / price_roi / market_roi / market2_roi / price_delta_roi 做一次裸 OCR；
        ⑤ ``price_delta`` 必须落在合法集合（``_VALID_PRICE_DELTA``，共 11 种：±10/20/30/40/50% + 空）
           内，否则该格视为识别不可靠 → ``sellable=False``（仍照常点关闭，避免遗留弹窗）；
        ⑥ 汇总成一条记录 {row, col, grid_center, sellable, name, price,
           market_price1, market_price2, price_delta, close_box}；
        ⑦ ``sellable=True`` 时点「关闭按钮」匹配框中心关闭详情（复用 ③ 的 box）；
        ⑧ 下一格。

    **每格一条记录**（``sellable`` 区分能否打开详情页**且 OCR 合法**）——不再 `continue`
    跳过空格，便于后续消费方拿到「格点 → 是否可售 → 详情」的完整映射。

    全部格点扫完后，结果列表（含扫描时间、账号标识）写入 ``_RESULT_PATH``（带锁，
    5 开并发串行写）。
    """

    # ===== 阶段 B：价格扫描默认参数（值取自 shop_test.json 实测标定）=====
    _DEFAULT_GRID_ROI = [751, 149, 354, 449]    # 待售卖区整体 roi [x,y,w,h]
    _DEFAULT_CLOSE_ROI = [977, 31, 92, 83]      # 关闭按钮所在区
    _DEFAULT_NAME_ROI = [763, 76, 224, 44]      # 商品名 OCR 区
    _DEFAULT_PRICE_ROI = [827, 454, 141, 42]    # 当前价 OCR 区
    _DEFAULT_MARKET_ROI = [396, 203, 171, 42]   # 市场价1 OCR 区（最高卖单）
    _DEFAULT_MARKET2_ROI = [397, 314, 132, 41]  # 市场价2 OCR 区（第二高卖单）
    _DEFAULT_PRICE_DELTA_ROI = [884, 493, 80, 42]  # 当前价变化 OCR 区
    _DEFAULT_ROWS = 5
    _DEFAULT_COLS = 4
    _DEFAULT_DETAIL_WAIT = 1.0                  # 点击格点后等弹窗的秒数
    _DEFAULT_CLOSE_WAIT = 0.5                   # 点关闭后等回归的秒数
    _DEFAULT_THRESHOLD = 0.7

    # ===== 阶段 A：空栏位清点（8 点 ColorMatch）默认参数 =====
    _DEFAULT_ES_ORIGIN = [365, 206]             # (0,0) roi 左上角
    _DEFAULT_ES_COL_DX = 300                    # 列步长
    _DEFAULT_ES_ROW_DY = 103                    # 行步长
    _DEFAULT_ES_ROWS = 4
    _DEFAULT_ES_COLS = 2
    _DEFAULT_ES_CELL_W = 30
    _DEFAULT_ES_CELL_H = 30
    _DEFAULT_ES_LOWER = [234, 220, 201]         # 命中此色域 = 有物品（非空）
    _DEFAULT_ES_UPPER = [254, 240, 221]         # 色更深（在色域外）= 空栏位
    _DEFAULT_ES_COUNT = int(_DEFAULT_ES_CELL_W * _DEFAULT_ES_CELL_H * 0.8)  # 720（>80% 像素）

    # pipeline_override 一次性节点名（仅本类内部用，不进任何 pipeline JSON）
    _CLOSE_NODE = "shopScan-关闭按钮"
    _NAME_NODE = "shopScan-商品名OCR"
    _PRICE_NODE = "shopScan-当前价OCR"
    _MARKET_NODE = "shopScan-市场价1OCR"
    _MARKET2_NODE = "shopScan-市场价2OCR"
    _PRICE_DELTA_NODE = "shopScan-当前价变化OCR"
    _COLOR_NODE = "shopScan-空格色判"
    _EXPIRED_NODE = "shopScan-过期标记模板"   # 阶段 A2：expired.png 模板扫描

    # ===== 阶段 A2：过期标记模板扫描（8 点 TemplateMatch）默认参数 =====
    # 与阶段 A1 同款 4×2 网格，但起点改到 [163,185]、roi 放大到 80×80，步长仍 300/103。
    # 模板 ``baitan/expired.png``（59×52）约一半像素是纯绿 (0,255,0)——开启 green_mask 让
    # 模板匹配**忽略绿色区**，只比对真实"过期"图标，避免把背景/底色误匹配进去。
    _DEFAULT_EXPIRED_TEMPLATE = os.path.join(
        _REPO_DIR, "assets", "resource", "base", "image", "baitan", "expired.png"
    )
    _DEFAULT_EXP_ORIGIN = [163, 185]
    _DEFAULT_EXP_COL_DX = 285
    _DEFAULT_EXP_ROW_DY = 103
    _DEFAULT_EXP_ROWS = 4
    _DEFAULT_EXP_COLS = 2
    _DEFAULT_EXP_CELL_W = 80
    _DEFAULT_EXP_CELL_H = 80
    _DEFAULT_EXP_THRESHOLD = 0.8

    # ===== 下架编排（点击过期格点 → run_task 取回）默认参数 =====
    # 过期格点用 **A1 网格同索引** 的 roi 坐标点击（例 A2 (0,0) 过期 → 点 A1 [365,206]）。
    # 必须从后向前：post 取回后队列向前移动，倒序点保证未处理格点坐标不变。
    _DELIST_ENTRY = "摆摊-下架过期物品"       # OCR「取回」→ 点击；子链关浮窗
    _DELIST_CLOSE_NODE = "摆摊-关闭浮窗"      # run_task 独立子任务，JumpBack 栈空 → 直接串
    _DEFAULT_DELIST_CLICK_WAIT = 1.0          # 点格点后等详情浮窗的秒数
    _DEFAULT_DELIST_TIMEOUT = 5000            # 下架节点识别上限（ms），防空格白等 20s
    _DEFAULT_LISTABLE_CAP = 8                 # 待售队列总长上限

    # ===== 卖出编排（两遍法：先只读扫描 20 格 → 子集内倒序卖出）默认参数 =====
    # 可售卖物品清单 = _MIN_PRICE 的键（dict/list 变量名自动展开，见 _SELL_TABLE）；
    # 最低卖出价 = max(_MIN_PRICE[物品], (market1+market2)/2 - 1)
    # （_MIN_PRICE 是硬下限保护价，市场均价更大时跟市场）。
    _SELL_ENTRY = "摆摊-上架物品"              # OCR「本服上架」→ 点击；原样 run_task，不 override
    # 详情页价格档按钮（roi，点击其中心）：一档 = 基准价 ±10%
    _PRICE_DOWN_ROI = [746, 462, 29, 24]       # 减小一档（基准价-10%）
    _PRICE_UP_ROI = [985, 463, 25, 25]         # 增大一档（基准价+10%）
    _DEFAULT_SELL_CLICK_WAIT = 1.0             # 卖出流程各步点击后等待秒数
    _DEFAULT_SELL_TIMEOUT = 8000               # 上架节点识别上限（ms）

    _LOCK = threading.Lock()  # 5 开并发串行写结果文件

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _clean_price(raw: str) -> str:
        """价格只保留数字（剥千分位分隔符）；无数字（"推荐价格"等）→ ""。

        price_delta 是百分比（"+10%"），**不**经此函数，单独存原值。
        """
        return _DIGIT_RE.sub("", raw or "")

    @staticmethod
    def _account_tag(context: Context) -> str:
        """稳定的账号标识：优先 adb_serial，回退 uuid，再回退 ?。

        与 ``logOcr._account_tag`` 同款——单例被 5 个并发 Tasker 共享，跨 ``run``
        调用稳定，``id(context.tasker)`` 不稳定故禁用。
        """
        try:
            ctrl = context.tasker.controller
            serial = (ctrl.info or {}).get("adb_serial")
            if serial:
                return serial
            return ctrl.uuid or "?"
        except Exception as e:
            logger.warning(f"[shopScan] 取账号标识失败: {type(e).__name__}: {e}")
            return "?"

    @staticmethod
    def _grid_centers(grid_roi, rows, cols):
        """把 ``grid_roi = [x, y, w, h]`` 均分为 ``rows`` 行 ``cols`` 列，
        返回 ``[(row, col, cx, cy), ...]``，中心坐标按行优先。

        与 fillCaptcha 键盘单元格中心同款数学：``cx = x + (col+0.5)*w/cols``。
        """
        x, y, w, h = grid_roi
        cell_w = w / cols
        cell_h = h / rows
        centers = []
        for r in range(rows):
            for c in range(cols):
                cx = int(x + (c + 0.5) * cell_w)
                cy = int(y + (r + 0.5) * cell_h)
                centers.append((r, c, cx, cy))
        return centers

    # ---------------- 阶段 A：空栏位清点 ----------------

    @staticmethod
    def _empty_slot_points(origin, col_dx, row_dy, cell_w, cell_h, rows, cols):
        """8 点 roi 列表（按行优先）= [x, y, cell_w, cell_h]。

        x = origin_x + col * col_dx，y = origin_y + row * row_dy。
        """
        ox, oy = origin
        pts = []
        for r in range(rows):
            for c in range(cols):
                pts.append([ox + c * col_dx, oy + r * row_dy, cell_w, cell_h])
        return pts

    def _color_has_item(self, context, image, roi, lower, upper, count):
        """单点 ColorMatch：roi 内 count 个像素落入 [lower,upper] 色域 → **有物品**（非空）。

        语义：浅米黄底色被物品图标占据时命中色域 → 有物品；颜色更深（在色域外）→ 空栏位。
        """
        reco = context.run_recognition(
            self._COLOR_NODE,
            image,
            pipeline_override={self._COLOR_NODE: {
                "recognition": "ColorMatch",
                "roi": roi,
                "lower": list(lower),
                "upper": list(upper),
                "count": int(count),       # 绝对像素数阈值；30×30×0.8=720（>80%）
            }},
        )
        return bool(reco and reco.hit)

    def _scan_empty_slots(self, context, tag, params):
        """阶段 A：8 点 ColorMatch 清点空栏位。

        返回 ``{"points": [...], "hits": [bool]*N, "has_item_indices": [...],
        "empty_indices": [...], "empty_count": int}``（``hits`` = 是否有物品）。
        """
        origin = params.get("origin", self._DEFAULT_ES_ORIGIN)
        col_dx = int(params.get("col_dx", self._DEFAULT_ES_COL_DX))
        row_dy = int(params.get("row_dy", self._DEFAULT_ES_ROW_DY))
        rows = int(params.get("rows", self._DEFAULT_ES_ROWS))
        cols = int(params.get("cols", self._DEFAULT_ES_COLS))
        cell_w = int(params.get("cell_w", self._DEFAULT_ES_CELL_W))
        cell_h = int(params.get("cell_h", self._DEFAULT_ES_CELL_H))
        lower = params.get("lower", self._DEFAULT_ES_LOWER)
        upper = params.get("upper", self._DEFAULT_ES_UPPER)
        count = int(params.get("count", self._DEFAULT_ES_COUNT))

        points = self._empty_slot_points(origin, col_dx, row_dy, cell_w, cell_h, rows, cols)
        image = context.tasker.controller.post_screencap().wait().get()

        hits = []  # hits[i] = 该点是否有物品（命中色域）
        has_item_indices, empty_indices = [], []
        logger.info(
            f"[shopScan] [{tag}] 阶段A 开始：origin={origin} step=({col_dx},{row_dy}) "
            f"{rows}x{cols} cell={cell_w}x{cell_h} 色域=[{lower},{upper}] count={count}"
        )
        for idx, roi in enumerate(points):
            has_item = self._color_has_item(context, image, roi, lower, upper, count)
            hits.append(has_item)
            (has_item_indices if has_item else empty_indices).append(idx)
            r, c = divmod(idx, cols)
            logger.info(
                f"[shopScan] [{tag}] 阶段A 点{idx} (r{r},c{c}) roi={roi} -> "
                f"{'有物品' if has_item else '空栏位'}"
            )
        logger.info(
            f"[shopScan] [{tag}] 阶段A 空栏位清点 count={count} 有物={has_item_indices} "
            f"空栏位={empty_indices}（空 {len(empty_indices)}/{len(points)}）"
        )
        return {
            "rows": rows, "cols": cols,
            "step": [col_dx, row_dy], "origin": list(origin), "cell_wh": [cell_w, cell_h],
            "lower": list(lower), "upper": list(upper), "count": count,
            "points": points,
            "hits": hits,                       # True = 有物品
            "has_item_indices": has_item_indices,
            "empty_indices": empty_indices,
            "empty_count": len(empty_indices),
        }

    # ---------------- 阶段 A2：过期标记模板扫描 ----------------

    def _scan_expired(self, context, tag, params):
        """阶段 A2：8 点 TemplateMatch 扫描 ``baitan/expired.png`` 过期标记。

        与阶段 A1 同样的 4×2 网格（行优先），但起点 ``[163,185]``、roi ``80×80``、
        步长 ``300/103`` 不变。对每个 roi 做一次 **green_mask 开启**的 TemplateMatch，
        记录每点的最佳匹配分数（``best_result.score``）与是否过阈值（``hit``）。

        ``expired.png``（59×52）约一半像素是纯绿 (0,255,0)——green_mask 让匹配忽略这层
        绿色遮罩，只比对真实"过期"角标。返回::

            {"template":..., "rows":4,"cols":2,"step":[300,103],"origin":[163,185],
             "cell_wh":[80,80],"threshold":0.7,"green_mask":true,"points":[...],
             "scores":[float]*8,"hits":[bool]*8,"hit_indices":[...],"best_score":float}
        """
        template = params.get("template", self._DEFAULT_EXPIRED_TEMPLATE)
        origin = params.get("origin", self._DEFAULT_EXP_ORIGIN)
        col_dx = int(params.get("col_dx", self._DEFAULT_EXP_COL_DX))
        row_dy = int(params.get("row_dy", self._DEFAULT_EXP_ROW_DY))
        rows = int(params.get("rows", self._DEFAULT_EXP_ROWS))
        cols = int(params.get("cols", self._DEFAULT_EXP_COLS))
        cell_w = int(params.get("cell_w", self._DEFAULT_EXP_CELL_W))
        cell_h = int(params.get("cell_h", self._DEFAULT_EXP_CELL_H))
        threshold = float(params.get("threshold", self._DEFAULT_EXP_THRESHOLD))
        green_mask = True   # expired.png 带绿色遮罩，必须开

        points = self._empty_slot_points(origin, col_dx, row_dy, cell_w, cell_h, rows, cols)
        image = context.tasker.controller.post_screencap().wait().get()

        scores, hits, hit_indices, statuses = [], [], [], []
        logger.info(
            f"[shopScan] [{tag}] 阶段A2 开始：template={template} origin={origin} "
            f"step=({col_dx},{row_dy}) {rows}x{cols} cell={cell_w}x{cell_h} "
            f"threshold={threshold} green_mask={green_mask}"
        )
        for idx, roi in enumerate(points):
            score = self._template_score(
                context, image, self._EXPIRED_NODE, template, roi, threshold, green_mask
            )
            hit = score >= threshold
            scores.append(score)
            hits.append(hit)
            # 状态语义：过阈值 = expired（过期物品）；否则 = valid（正常在售）
            statuses.append("expired" if hit else "valid")
            if hit:
                hit_indices.append(idx)
            r, c = divmod(idx, cols)
            logger.info(
                f"[shopScan] [{tag}] 阶段A2 点{idx} (r{r},c{c}) roi={roi} "
                f"score={score:.3f} -> {statuses[-1]}{'（过阈值）' if hit else ''}"
            )
        best = max(scores) if scores else 0.0
        logger.info(
            f"[shopScan] [{tag}] 阶段A2 过期标记扫描 green_mask={green_mask} threshold={threshold} "
            f"分数={[round(s, 3) for s in scores]} 状态={statuses} 过期={hit_indices}"
            f"（expired {len(hit_indices)}/{len(points)}，最佳={best:.3f}）"
        )
        return {
            "template": template,
            "rows": rows, "cols": cols,
            "step": [col_dx, row_dy], "origin": list(origin), "cell_wh": [cell_w, cell_h],
            "threshold": threshold, "green_mask": green_mask,
            "points": points,
            "scores": scores,
            "hits": hits,
            "statuses": statuses,                 # "expired" | "valid"
            "hit_indices": hit_indices,
            "hit_count": len(hit_indices),
            "best_score": best,
        }

    # ---------------- 阶段 C：下架过期物品编排 ----------------

    def _delist_one(self, context, tag, click_roi):
        """下架单个过期格点：点 A1 同索引 roi → run_task 下架 → 关浮窗。

        ``run_task`` 是独立子任务（JumpBack 栈空），须把「摆摊-下架过期物品」的 next
        由 ``[JumpBack]摆摊-关闭浮窗`` 覆写成直接串「摆摊-关闭浮窗」。返回是否真的点中
        「取回」（查 TaskDetail 节点的 recognition.hit，防 on_error→空节点假成功）。
        """
        cx = int(click_roi[0] + click_roi[2] / 2)
        cy = int(click_roi[1] + click_roi[3] / 2)
        logger.info(
            f"[shopScan] [{tag}] 阶段C 点击过期格点 roi={click_roi} @ ({cx},{cy})，"
            f"等 {self._DEFAULT_DELIST_CLICK_WAIT}s 后查「取回」"
        )
        context.tasker.controller.post_click(cx, cy).wait()
        if self._DEFAULT_DELIST_CLICK_WAIT > 0:
            time.sleep(self._DEFAULT_DELIST_CLICK_WAIT)

        logger.info(
            f"[shopScan] [{tag}] 阶段C run_task({self._DELIST_ENTRY!r}) → next=[{self._DELIST_CLOSE_NODE}] "
            f"timeout={self._DEFAULT_DELIST_TIMEOUT}ms"
        )
        td = context.run_task(
            self._DELIST_ENTRY,
        )
        hit = False
        if td is None:
            logger.warning(f"[shopScan] [{tag}] 阶段C run_task 返回 None（任务未启动）")
        elif getattr(td, "nodes", None):
            for n in td.nodes:
                reco = getattr(n, "recognition", None)
                n_hit = bool(reco and reco.hit)
                logger.info(
                    f"[shopScan] [{tag}] 阶段C 子任务节点 {n.name!r} hit={n_hit}"
                )
                if n.name == self._DELIST_ENTRY:
                    hit = n_hit
                    break
        else:
            logger.warning(f"[shopScan] [{tag}] 阶段C 子任务无节点轨迹（td.nodes 空）")
        logger.info(
            f"[shopScan] [{tag}] 阶段C 下架节点「{self._DELIST_ENTRY}」hit={hit}"
            f"{'，y+1' if hit else '，未取回（不计入 y）'}"
        )
        return hit

    def _delist_expired(self, context, tag, empty_slots, expired_marks):
        """阶段 C：**从后向前**逐个下架过期物品。

        为什么必须倒序：post「取回」后待售队列向前移动（后面的物品补上空位），若正序点，
        第 i 格取回后第 i+1 格的实际内容已经前移，坐标就错位了。倒序从最后一格点起，
        前面未处理的格点位置不受影响。点击坐标用 **A1 网格同索引** 的 roi（A2 的 80×80
        roi 是给模板匹配扫过期角标用的，点选取区域要用 A1 的 30×30 色判 roi，例
        A2 (0,0) 过期 → 点 [365,206]）。

        返回 ``{"delisted": int, "indices": [...], "clicks": [...]}`` —— ``delisted``=y
        （下架成功数），``indices``=处理的 A2 网格索引（倒序），``clicks``=实际点击的 A1 roi。
        """
        a1_points = empty_slots["points"]
        hit_indices = expired_marks["hit_indices"]
        cap = self._DEFAULT_LISTABLE_CAP
        delisted, indices, clicks = 0, [], []
        logger.info(
            f"[shopScan] [{tag}] 阶段C 开始：过期格点={hit_indices}（将倒序处理 "
            f"{sorted(hit_indices, reverse=True)}），A1 点击网格 {len(a1_points)} 点"
        )
        # 倒序：idx 大（靠后）的先点，队列前移不影响更靠前的未处理格点
        for idx in sorted(hit_indices, reverse=True):
            if idx >= len(a1_points):
                logger.warning(f"[shopScan] [{tag}] 阶段C 索引 {idx} 超出 A1 网格，跳过")
                continue
            click_roi = a1_points[idx]
            logger.info(f"[shopScan] [{tag}] 阶段C 处理过期格点 idx={idx}（倒序）")
            ok = self._delist_one(context, tag, click_roi)
            indices.append(idx)
            clicks.append(click_roi)
            if ok:
                delisted += 1
        logger.info(
            f"[shopScan] [{tag}] 阶段C 下架完成 y={delisted}/{len(indices)}"
            f"（处理 {indices}，点击 {clicks}，上限 {cap}）"
        )
        return {"delisted": delisted, "indices": indices, "clicks": clicks}

    # ---------------- 阶段 D：卖出编排（两遍法） ----------------

    @staticmethod
    def _match_sell_target(name):
        """物品名命中 ``_SELL_TABLE``（_MIN_PRICE 展开后的可售卖清单）→ 返回最低价，否则 None。

        匹配为**子串包含**（OCR 文本可能带修饰前后缀，如「夜魔披风·精制」）。
        例 name="夜魔披风" 命中 _60_MAP 展开的 value → ``900``；
        name="再生" 命中 _SKILL_BOOK 展开的元素 → ``4500``。
        """
        for sell_name, min_price in _SELL_TABLE.items():
            if sell_name in (name or ""):
                return min_price
        return None

    @staticmethod
    def _plan_price_adjust(current, delta_str, min_sell):
        """由当前价 + 详情页「当前价变化」推基准价，算出调到 ≥ min_sell 的点击次数。

        基准价**不能**只靠 current 反推——``(base=current, k=0)`` 永远完美重构（例 450 既可
        是 500 的 -10% 档、也可自为基准）。真正的信号是详情页 OCR 的 ``delta_str``
        （``"-10%"`` → ``current/0.9`` = 基准价）。档位 = 基准价 ±10% 整数倍（±5 档封顶）。

        返回 ``(base, k_now, n_up, n_down, new_price)``：``n_up``/``n_down`` = 点
        「增大/减小一档」次数（其一为 0）、``new_price`` = 调整后所在档的价格。
        例 ``plan(450, "-10%", 600)`` → 基准 500、当前 -1 档、目标 +2 档（600）→ +3 次。
        """
        # delta_str（"+10%"/"-20%"…，""=0 档）→ 当前档位 k_now
        k_now = 0
        ds = (delta_str or "").replace(" ", "")
        if ds.endswith("%"):
            try:
                k_now = int(round(int(ds[:-1]) / 10))
            except ValueError:
                k_now = 0
        k_now = max(-5, min(5, k_now))
        # 反推基准价（取整到 10；游戏标价个位为 0）
        base = int(round(current / (1 + 0.1 * k_now) / 10.0)) * 10
        if base <= 0:
            base = max(10, int(round(current / 10.0)) * 10)
            k_now = 0
        # 目标：调到 **≥ min_sell 的最小档位**（双向调——高了要降，尽快卖出）。
        # 例：600（+2 档，基准 500）、floor=474 → 目标 0 档（500），点「-10%」2 次。
        k_target = None
        for k in range(-5, 6):
            if int(round(base * (1 + 0.1 * k))) >= min_sell:
                k_target = k
                break
        if k_target is None:
            k_target = 5   # 全档位都到不了（min_sell 异常大），顶格 +50%
        new_price = int(round(base * (1 + 0.1 * k_target)))
        n_up = max(0, k_target - k_now)
        n_down = max(0, k_now - k_target)
        return base, k_now, n_up, n_down, new_price

    def _click_roi_center(self, context, roi):
        """点 roi 中心（调价按钮等固定区域）。"""
        cx = int(roi[0] + roi[2] / 2)
        cy = int(roi[1] + roi[3] / 2)
        context.tasker.controller.post_click(cx, cy).wait()
        return cx, cy

    def _sell_one(self, context, tag, item):
        """卖出单个物品：点格点 → 核对名字 → 读当前价 → 调价 → run_task 上架。

        ``item`` 是阶段 B1 只读扫描的记录（含 name/price/market_price1/market_price2/
        grid_center/min_price）。返回是否上架成功（查「摆摊-上架物品」节点 hit）。
        """
        cx, cy = item["grid_center"]
        name, min_price = item["name"], item["min_price"]
        m1, m2 = int(item["market_price1"]), int(item["market_price2"])

        # 最低卖出价 = max(_MIN_PRICE[物品], (market1+market2)/2 - 1)：
        # _MIN_PRICE 是**硬下限**（保护价，绝不击穿）；
        # 市场均价更高时跟市场（(m1+m2)/2-1 比第二高卖单还低 1，保证竞争力）。
        floor_price = max(min_price, int((m1 + m2) / 2) - 1)
        logger.info(
            f"[shopScan] [{tag}] 阶段D 卖出 {name!r}(保底{min_price}) @ ({cx},{cy})："
            f"当前价={item['price']} 市场价1={m1} 市场价2={m2} → "
            f"max({min_price}, ({m1}+{m2})/2-1) = {floor_price}"
        )

        # ① 点格点开详情
        context.tasker.controller.post_click(cx, cy).wait()
        if self._DEFAULT_SELL_CLICK_WAIT > 0:
            time.sleep(self._DEFAULT_SELL_CLICK_WAIT)
        image = context.tasker.controller.post_screencap().wait().get()

        # ② 重新读名字核对（两遍法之间队列可能变了，防卖错东西）
        cur_name = self._ocr_text(context, image, self._NAME_NODE,
                                  item.get("name_roi"), item.get("threshold", 0.7))
        if name not in cur_name and cur_name not in name:
            logger.warning(
                f"[shopScan] [{tag}] 阶段D 格点名字变了（期望 {name!r} 实读 {cur_name!r}），跳过"
            )
            self._close_detail(context, image, item)
            return False

        # ③ 重读当前价 + 当前价变化（卖出时的实时值，比扫描时的快照更可靠；
        #    delta 是推基准价的关键信号——仅凭 current 无法区分 0 档与 ±档）
        price_str = self._clean_price(self._ocr_text(
            context, image, self._PRICE_NODE, item.get("price_roi"), item.get("threshold", 0.7)))
        delta_str = self._ocr_text(
            context, image, self._PRICE_DELTA_NODE, item.get("price_delta_roi"),
            item.get("threshold", 0.7))
        if not price_str.isdigit():
            logger.warning(f"[shopScan] [{tag}] 阶段D 当前价 OCR 无数字（{price_str!r}），跳过")
            self._close_detail(context, image, item)
            return False
        current = int(price_str)

        # ④ 算档位并调价（delta_str 推基准价）
        base, k_now, n_up, n_down, new_price = self._plan_price_adjust(
            current, delta_str, floor_price)
        logger.info(
            f"[shopScan] [{tag}] 阶段D 调价：当前 {current}（delta={delta_str!r} → 基准 {base}，"
            f"{k_now:+d} 档）→ 需 ≥{floor_price} → 目标档价 {new_price}（+{n_up} 档 / -{n_down} 档）"
        )
        for _ in range(n_up):
            x, y = self._click_roi_center(context, self._PRICE_UP_ROI)
            logger.info(f"[shopScan] [{tag}] 阶段D 点「+10%」@ ({x},{y})")
            if self._DEFAULT_SELL_CLICK_WAIT > 0:
                time.sleep(self._DEFAULT_SELL_CLICK_WAIT)
        for _ in range(n_down):
            x, y = self._click_roi_center(context, self._PRICE_DOWN_ROI)
            logger.info(f"[shopScan] [{tag}] 阶段D 点「-10%」@ ({x},{y})")
            if self._DEFAULT_SELL_CLICK_WAIT > 0:
                time.sleep(self._DEFAULT_SELL_CLICK_WAIT)

        # ⑤ 原样 run_task 上架（不 override —— 节点自带 post_delay/关闭子链）
        logger.info(f"[shopScan] [{tag}] 阶段D run_task({self._SELL_ENTRY!r})（原样，不 override）")
        td = context.run_task(self._SELL_ENTRY)
        hit = False
        if td is None:
            logger.warning(f"[shopScan] [{tag}] 阶段D run_task 返回 None（任务未启动）")
        else:
            for n in (getattr(td, "nodes", None) or []):
                reco = getattr(n, "recognition", None)
                n_hit = bool(reco and reco.hit)
                logger.info(f"[shopScan] [{tag}] 阶段D 上架子任务节点 {n.name!r} hit={n_hit}")
                if n.name == self._SELL_ENTRY:
                    hit = n_hit
                    break
        logger.info(f"[shopScan] [{tag}] 阶段D 上架「{name}」hit={hit}{'，卖出+1' if hit else '，失败'}")
        return hit

    def _close_detail(self, context, image, item):
        """用关闭按钮模板关掉当前详情浮窗（扫不到则点 close_roi 中心兜底）。"""
        box = self._template_box(
            context, image, self._CLOSE_NODE, _DEFAULT_CLOSE_TEMPLATE,
            item.get("close_roi", self._DEFAULT_CLOSE_ROI), item.get("threshold", 0.7)
        )
        if box:
            self._click_roi_center(context, box)
        else:
            logger.warning("[shopScan] 阶段D 关闭按钮未命中，点 close_roi 中心兜底")
            self._click_roi_center(context, item.get("close_roi", self._DEFAULT_CLOSE_ROI))
        if self._DEFAULT_SELL_CLICK_WAIT > 0:
            time.sleep(self._DEFAULT_SELL_CLICK_WAIT)

    def _sell_phase(self, context, tag, items, listable):
        """阶段 D：先**正序**选出前 ``listable`` 个候选，再在这个子集内**倒序**卖出。

        两遍法的第二遍。为什么子集内倒序：上架成功后待售卖区所有物品**向前移动一格**，
        正序点会让子集内后续格点错位；倒序从子集最后一格卖起，子集内未卖物品的坐标
        不变。子集取正序前 N 个（例：候选 ABCD、listable=2 → 子集 AB → 卖出顺序 BA，
        而非从全体倒序取 DC）。每格卖出前会重新 OCR 名字核对（两遍之间队列若被动过，防卖错）。
        """
        # 只卖「命中 _SELL_TABLE + 有完整市场价」的记录
        candidates = []
        for it in items:
            tgt = self._match_sell_target(it.get("name", ""))
            if tgt is None:   # 保底价可为 0（临时符），不能用 if not tgt 判
                continue
            if not (str(it.get("market_price1", "")).isdigit()
                    and str(it.get("market_price2", "")).isdigit()):
                logger.info(
                    f"[shopScan] [{tag}] 阶段D {it.get('name')!r} 命中售卖表但市场价不全"
                    f"（m1={it.get('market_price1')!r} m2={it.get('market_price2')!r}），跳过"
                )
                continue
            candidates.append({**it, "min_price": tgt})
        # ① 正序选前 listable 个为本次卖出子集
        subset = candidates[:listable] if listable > 0 else []
        logger.info(
            f"[shopScan] [{tag}] 阶段D 候选共 {len(candidates)} 个："
            f"{[c['name'] for c in candidates]}；listable={listable} → 子集（正序前 N）"
            f"{[c['name'] for c in subset]}；卖出顺序（子集内倒序）："
            f"{[c['name'] for c in reversed(subset)]}"
        )
        # ② 子集内倒序卖出
        sold = []
        for it in reversed(subset):
            ok = self._sell_one(context, tag, it)
            if ok:
                sold.append({"name": it["name"], "min_price": it["min_price"],
                             "grid_center": it["grid_center"]})
        logger.info(f"[shopScan] [{tag}] 阶段D 卖出完成：{len(sold)}/{len(subset)} → {sold}")
        return {"sold": sold, "sold_count": len(sold),
                "candidates": len(candidates), "subset": len(subset)}

    def _plan_only(self, tag, items, listable):
        """dry-run：与 ``_sell_phase`` 同款候选/子集推导，但**只打印计划不执行**。

        输出每件候选的「保底价 / 当前价 / 市场价 / 拟定 floor」，供人工核对扫描结果
        与卖出计划（``enable_sell=false`` 时走此路径）。
        """
        candidates = []
        for it in items:
            tgt = self._match_sell_target(it.get("name", ""))
            if tgt is None:   # 保底价可为 0（临时符），不能用 if not tgt 判
                continue
            if not (str(it.get("market_price1", "")).isdigit()
                    and str(it.get("market_price2", "")).isdigit()):
                logger.info(
                    f"[shopScan] [{tag}] 阶段D(dry) {it.get('name')!r} 命中售卖表但市场价不全，跳过"
                )
                continue
            m1, m2 = int(it["market_price1"]), int(it["market_price2"])
            floor = max(tgt, int((m1 + m2) / 2) - 1)
            candidates.append({**it, "min_price": tgt, "floor_price": floor})
        subset = candidates[:listable] if listable > 0 else []
        logger.info(
            f"[shopScan] [{tag}] 阶段D(dry) 候选 {len(candidates)} 个："
            + "; ".join(f"{c['name']}(保底{c['min_price']} 当前{c['price']} "
                        f"市场{c['market_price1']}/{c['market_price2']} 拟卖{c['floor_price']})"
                        for c in candidates)
        )
        logger.info(
            f"[shopScan] [{tag}] 阶段D(dry) listable={listable} → 子集（正序前 N）："
            f"{[c['name'] for c in subset]}；卖出顺序（子集内倒序）："
            f"{[c['name'] for c in reversed(subset)]}"
        )
        return {"sold": [], "sold_count": 0, "candidates": len(candidates),
                "subset": len(subset), "dry_run": True,
                "plan": [{"name": c["name"], "min_price": c["min_price"],
                          "floor_price": c["floor_price"], "grid_center": c["grid_center"]}
                         for c in subset]}

    def _template_box(self, context, image, node, template, roi, threshold):
        """在 ``roi`` 内 TemplateMatch ``template``，命中返回 ``best_result.box=[x,y,w,h]``，否则 None。"""
        reco = context.run_recognition(
            node,
            image,
            pipeline_override={node: {
                "recognition": "TemplateMatch",
                "template": [template],
                "roi": roi,
                "threshold": threshold,
            }},
        )
        if reco and reco.hit and reco.best_result and reco.best_result.box:
            return reco.best_result.box
        return None

    def _template_score(self, context, image, node, template, roi, threshold, green_mask):
        """在 ``roi`` 内 TemplateMatch ``template``，返回最佳匹配分数（``best_result.score``）。

        与 ``_template_box`` 同源，但**不**受 ``threshold`` 截断影响地暴露分数——即使未过
        阈值（``reco.hit=False``）也想拿到 raw score 用于调参/对比。``green_mask=True`` 时
        模板里的纯绿 (0,255,0) 像素被忽略（baitan/expired.png 约一半像素是绿色遮罩）。

        MaaFw 的 TemplateMatchResult（``maa/define.py:973`` = ``BoxAndScoreResult``）同时带
        ``box`` 与 ``score``。命中阈值 → ``best_result`` 取得分最高者；未命中 → 返回 0.0。
        """
        reco = context.run_recognition(
            node,
            image,
            pipeline_override={node: {
                "recognition": "TemplateMatch",
                "template": [template],
                "roi": roi,
                "threshold": threshold,
                "green_mask": bool(green_mask),
            }},
        )
        if reco and reco.best_result and getattr(reco.best_result, "score", None) is not None:
            return float(reco.best_result.score)
        return 0.0

    def _ocr_text(self, context, image, node, roi, threshold):
        """在 ``roi`` 内做一次裸 OCR（``expected:[""]`` = 匹配任意），返回原文（未命中返回 ""）。"""
        reco = context.run_recognition(
            node,
            image,
            pipeline_override={node: {
                "recognition": "OCR",
                "roi": roi,
                "expected": [""],
                "threshold": threshold,
            }},
        )
        if reco and reco.hit and reco.best_result:
            return reco.best_result.text or ""
        return ""

    def _save(self, payload):
        """带锁追加一条扫描批次到 ``_RESULT_PATH``（JSON Lines 风格，每行一个批次）。"""
        line = json.dumps(payload, ensure_ascii=False)
        with self._LOCK:
            os.makedirs(dirname(_RESULT_PATH), exist_ok=True)
            with open(_RESULT_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        logger.info(f"[shopScan] 已写入 {len(payload['items'])} 条到 {_RESULT_PATH}")

    # ------------------------------------------------------------------ 主流程

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> CustomAction.RunResult:
        argv_dict: dict = json.loads(argv.custom_action_param or "{}")
        grid_roi = argv_dict.get("grid_roi", self._DEFAULT_GRID_ROI)
        rows = int(argv_dict.get("rows", self._DEFAULT_ROWS))
        cols = int(argv_dict.get("cols", self._DEFAULT_COLS))
        close_template = argv_dict.get("close_template", _DEFAULT_CLOSE_TEMPLATE)
        close_roi = argv_dict.get("close_roi", self._DEFAULT_CLOSE_ROI)
        name_roi = argv_dict.get("name_roi", self._DEFAULT_NAME_ROI)
        price_roi = argv_dict.get("price_roi", self._DEFAULT_PRICE_ROI)
        market_roi = argv_dict.get("market_roi", self._DEFAULT_MARKET_ROI)
        market2_roi = argv_dict.get("market2_roi", self._DEFAULT_MARKET2_ROI)
        price_delta_roi = argv_dict.get("price_delta_roi", self._DEFAULT_PRICE_DELTA_ROI)
        detail_wait = float(argv_dict.get("detail_wait", self._DEFAULT_DETAIL_WAIT))
        close_wait = float(argv_dict.get("close_wait", self._DEFAULT_CLOSE_WAIT))
        threshold = float(argv_dict.get("threshold", self._DEFAULT_THRESHOLD))
        enable_price_scan = bool(argv_dict.get('enable_price_scan', True))

        tag = self._account_tag(context)
        logger.info(
            f"[shopScan] [{tag}] ===== 开始（编排：A1 空栏位 → A2 过期扫描 → C 倒序下架 → "
            f"listable=min(8,x+y) → B 可选价格扫描）enable_price_scan="
            f"{enable_price_scan} ====="
        )

        # ===== 阶段 A：空栏位清点（纯 ColorMatch，无点击）=====
        empty_slots = self._scan_empty_slots(context, tag, argv_dict.get("empty_slots", {}))
        x = empty_slots["empty_count"]   # x = 空栏位数
        logger.info(f"[shopScan] [{tag}] 阶段A 完成：空栏位 x={x}/{len(empty_slots['points'])}")

        # ===== 阶段 A2：过期标记模板扫描（纯 TemplateMatch+green_mask，无点击）=====
        expired_marks = self._scan_expired(context, tag, argv_dict.get("expired_marks", {}))

        # ===== 阶段 C：从后向前下架过期物品（点击 A1 同索引 roi → run_task 取回）=====
        # 必须倒序：post 取回后队列向前移动，倒序点保证未处理格点坐标不错位。
        delist = self._delist_expired(context, tag, empty_slots, expired_marks)
        y = delist["delisted"]           # y = 下架成功数

        # ===== 计算：可上架物品数量 = min(8, x + y) =====
        listable = min(self._DEFAULT_LISTABLE_CAP, x + y)
        logger.info(
            f"[shopScan] [{tag}] 可上架计算：空栏位 x={x} + 下架 y={y} → "
            f"min({self._DEFAULT_LISTABLE_CAP}, {x}+{y}) = {listable}"
        )

        # ===== 阶段 B（可选，两遍法）：B1 只读扫描 20 格 → D 倒序卖出 =====
        # 默认关闭——会点 20 格有误售/误点风险，仅在明确传 enable_price_scan=true 时跑。
        # B1 只读（读完即关详情，不卖）；D 遍（_sell_phase）从后向前卖出，因为上架成功后
        # 待售卖区所有物品向前移动一格，倒序保证未卖格点坐标不错位。
        items = []
        centers = []
        sold = {"sold": [], "sold_count": 0, "candidates": 0}
        if enable_price_scan:
            centers = self._grid_centers(grid_roi, rows, cols)
            logger.info(
                f"[shopScan] [{tag}] 阶段B1 只读扫描开始 grid_roi={grid_roi} {rows}x{cols}="
                f"{len(centers)} 格，close_roi={close_roi}"
            )

            for (r, c, cx, cy) in centers:
                logger.info(f"[shopScan] [{tag}] B1 点击格点 (r={r},c={c}) @ ({cx},{cy})")
                context.tasker.controller.post_click(cx, cy).wait()
                if detail_wait > 0:
                    time.sleep(detail_wait)
                    # TODO: 更稳的做法 —— 用 controller 的 wait_freezes 等画面静止，
                    #       替代固定 sleep（CLAUDE.md「少用 delay」约定）。

                image = context.tasker.controller.post_screencap().wait().get()

                # ③ 关闭按钮模板 = 详情页是否弹出的哨兵
                close_box = self._template_box(
                    context, image, self._CLOSE_NODE, close_template, close_roi, threshold
                )
                if not close_box:
                    logger.info(f"[shopScan] [{tag}] (r={r},c={c}) 未弹出详情（sellable=False）")
                    items.append({
                        "row": r,
                        "col": c,
                        "grid_center": [cx, cy],
                        "sellable": False,
                    })
                    continue

                # ④ 详情已弹出：五处分别裸 OCR
                name = self._ocr_text(context, image, self._NAME_NODE, name_roi, threshold)
                price = self._clean_price(self._ocr_text(context, image, self._PRICE_NODE, price_roi, threshold))
                market1 = self._clean_price(self._ocr_text(context, image, self._MARKET_NODE, market_roi, threshold))
                market2 = self._clean_price(self._ocr_text(context, image, self._MARKET2_NODE, market2_roi, threshold))
                # price_delta 是百分比（"+10%"）而非纯整数，保留原值不去分隔符
                price_delta = self._ocr_text(
                    context, image, self._PRICE_DELTA_NODE, price_delta_roi, threshold
                )
                # ⑤ price_delta 合法性断言：必须落在 _VALID_PRICE_DELTA（±10/20/30/40/50% + 空，共 11 种）。
                #    不合法 → OCR 不可靠，该格 sellable=False（详情仍照常关闭，避免遗留弹窗挡住下一格）。
                delta_valid = price_delta in _VALID_PRICE_DELTA
                sellable = delta_valid
                # 命中售卖表（_SELL_TABLE）→ 阶段 D 的卖出候选
                tgt = self._match_sell_target(name)
                if tgt is not None:   # 保底价可为 0（临时符），不能用 if tgt 判
                    sellable = sellable and bool(
                        market1.isdigit() and market2.isdigit()
                    )
                logger.info(
                    f"[shopScan] [{tag}] (r={r},c={c}) name={name!r} price={price!r} "
                    f"market1={market1!r} market2={market2!r} price_delta={price_delta!r} "
                    f"-> sellable={sellable}"
                    + ("" if delta_valid else "（delta 非法，降级 False）")
                    + (f"（命中售卖表 保底{tgt}）" if tgt is not None else "")
                )

                items.append({
                    "row": r,
                    "col": c,
                    "grid_center": [cx, cy],
                    "sellable": sellable,
                    "name": name,
                    "price": price,                 # 已清洗：纯整数字符串（""=OCR 无数字）
                    "market_price1": market1,       # 市场价1（最高卖单），同上
                    "market_price2": market2,       # 市场价2（第二高卖单），同上
                    "price_delta": price_delta,
                    "close_box": list(close_box),
                    # 供阶段 D 复用的 roi 快照（同一 detail/close/OCR 区域）
                    "name_roi": name_roi,
                    "price_roi": price_roi,
                    "price_delta_roi": price_delta_roi,
                    "close_roi": close_roi,
                    "threshold": threshold,
                })

                # ⑥ 点关闭按钮中心关掉详情（复用 ③ 的命中框）—— B1 只读，不卖
                bx, by, bw, bh = close_box
                context.tasker.controller.post_click(int(bx + bw / 2), int(by + bh / 2)).wait()
                if close_wait > 0:
                    time.sleep(close_wait)

            # ===== 阶段 D：正序选前 listable 个候选 → 子集内倒序卖出 =====
            # enable_sell=false（dry-run）：只打印卖出计划（候选/子集/顺序/每件保底价），
            # 不点击不出售——供调试验证扫描与计划逻辑。
            if argv_dict.get("enable_sell", True):
                sold = self._sell_phase(context, tag, items, listable)
            else:
                sold = self._plan_only(tag, items, listable)

        # 全部扫完 → 落盘
        payload = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "account": tag,
            "empty_slots": empty_slots,         # 阶段 A 结果（x = empty_count）
            "expired_marks": expired_marks,     # 阶段 A2 结果（过期标记模板扫描）
            "delist": delist,                   # 阶段 C 结果（y = delisted）
            "listable": listable,               # min(8, x+y)
            "grid_roi": grid_roi,
            "rows": rows,
            "cols": cols,
            "items": items,                     # 阶段 B1 结果（enable_price_scan 才有）
            "sold": sold,                       # 阶段 D 结果（卖出明细）
        }
        # self._save(payload)
        sellable_n = sum(1 for it in items if it.get("sellable"))
        logger.info(
            f"[shopScan] [{tag}] 扫描完成 阶段A空栏位 x={empty_slots['empty_count']}/"
            f"{len(empty_slots['points'])}，阶段A2过期标记={expired_marks['hit_count']}/"
            f"{len(expired_marks['points'])}(best={expired_marks['best_score']:.3f})，"
            f"阶段C下架 y={delist['delisted']}，可上架 listable={listable}，"
            f"阶段B1 {len(items)}/{len(centers)} 格 sellable={sellable_n}，"
            f"阶段D 卖出 {sold['sold_count']}/{listable}"
        )
        return CustomAction.RunResult(success=True)
