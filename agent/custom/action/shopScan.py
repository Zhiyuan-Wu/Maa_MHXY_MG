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
            pipeline_override={
                self._DELIST_ENTRY: {
                    "next": [self._DELIST_CLOSE_NODE],   # 子任务内 JumpBack 栈空，直接串关浮窗
                    "timeout": self._DEFAULT_DELIST_TIMEOUT,
                },
                self._DELIST_CLOSE_NODE: {"timeout": self._DEFAULT_DELIST_TIMEOUT},
            },
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

        tag = self._account_tag(context)
        logger.info(
            f"[shopScan] [{tag}] ===== 开始（编排：A1 空栏位 → A2 过期扫描 → C 倒序下架 → "
            f"listable=min(8,x+y) → B 可选价格扫描）enable_price_scan="
            f"{bool(argv_dict.get('enable_price_scan'))} ====="
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

        # ===== 阶段 B（可选）：逐格扫描物品详情/价格 =====
        # 默认关闭——会盲点 20 格有误售/误点风险，仅在明确传 enable_price_scan=true 时跑。
        items = []
        centers = []
        if argv_dict.get("enable_price_scan"):
            centers = self._grid_centers(grid_roi, rows, cols)
            logger.info(
                f"[shopScan] [{tag}] 阶段B 开始扫描 grid_roi={grid_roi} {rows}x{cols}="
                f"{len(centers)} 格，close_roi={close_roi}"
            )

            for (r, c, cx, cy) in centers:
                logger.info(f"[shopScan] [{tag}] 点击格点 (r={r},c={c}) @ ({cx},{cy})")
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
                logger.info(
                    f"[shopScan] [{tag}] (r={r},c={c}) name={name!r} price={price!r} "
                    f"market1={market1!r} market2={market2!r} price_delta={price_delta!r} "
                    f"-> sellable={sellable}" + ("" if delta_valid else "（delta 非法，降级 False）")
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
                })

                # ⑥ 点关闭按钮中心关掉详情（复用 ③ 的命中框）
                bx, by, bw, bh = close_box
                context.tasker.controller.post_click(int(bx + bw / 2), int(by + bh / 2)).wait()
                if close_wait > 0:
                    time.sleep(close_wait)

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
            "items": items,                     # 阶段 B 结果（enable_price_scan 才有）
        }
        self._save(payload)
        sellable_n = sum(1 for it in items if it.get("sellable"))
        logger.info(
            f"[shopScan] [{tag}] 扫描完成 阶段A空栏位 x={empty_slots['empty_count']}/"
            f"{len(empty_slots['points'])}，阶段A2过期标记={expired_marks['hit_count']}/"
            f"{len(expired_marks['points'])}(best={expired_marks['best_score']:.3f})，"
            f"阶段C下架 y={delist['delisted']}，可上架 listable={listable}，"
            f"阶段B {len(items)}/{len(centers)} 格 sellable={sellable_n}"
        )
        return CustomAction.RunResult(success=True)
