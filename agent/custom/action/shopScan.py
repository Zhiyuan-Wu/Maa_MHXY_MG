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
   步长 ``300/103``）上对 ``baitan/expired.png`` 做 **TemplateMatch（green_mask 开启）**。
   该模板约一半像素是纯绿 (0,255,0) 遮罩，green_mask 让匹配忽略绿色区、只比对真实"过期"
   角标；逐点记录最佳匹配分数（``best_result.score``）与是否过阈值。
3. **阶段 B — 物品价格扫描**：把「待售卖区」均分为 5×4 格点，逐格点击 → 判断详情页 →
   OCR 商品名/当前价/市场价1/市场价2/当前价变化 → 关闭 → 下一格。

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
_DEFAULT_CLOSE_TEMPLATE = os.path.join(
    _REPO_DIR, "assets", "resource", "base", "image", "shop", "close_btn.png"  # TODO: 放真实模板
)
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

    # ===== 待填占位符（custom_action_param 可覆盖）=====
    _DEFAULT_GRID_ROI = [0, 0, 0, 0]            # TODO: 待售卖区整体 roi
    _DEFAULT_CLOSE_ROI = [0, 0, 0, 0]           # TODO: 关闭按钮所在区
    _DEFAULT_NAME_ROI = [0, 0, 0, 0]            # TODO: 商品名 OCR 区
    _DEFAULT_PRICE_ROI = [0, 0, 0, 0]           # TODO: 当前价 OCR 区
    _DEFAULT_MARKET_ROI = [0, 0, 0, 0]          # TODO: 市场价1 OCR 区（最高卖单）
    _DEFAULT_MARKET2_ROI = [0, 0, 0, 0]         # TODO: 市场价2 OCR 区（第二高卖单）
    _DEFAULT_PRICE_DELTA_ROI = [0, 0, 0, 0]     # TODO: 当前价变化 OCR 区
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
    _DEFAULT_EXP_THRESHOLD = 0.7

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
        for idx, roi in enumerate(points):
            has_item = self._color_has_item(context, image, roi, lower, upper, count)
            hits.append(has_item)
            (has_item_indices if has_item else empty_indices).append(idx)
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

        scores, hits, hit_indices = [], [], []
        for idx, roi in enumerate(points):
            score = self._template_score(
                context, image, self._EXPIRED_NODE, template, roi, threshold, green_mask
            )
            hit = score >= threshold
            scores.append(score)
            hits.append(hit)
            if hit:
                hit_indices.append(idx)
        best = max(scores) if scores else 0.0
        logger.info(
            f"[shopScan] [{tag}] 阶段A2 过期标记扫描 green_mask={green_mask} threshold={threshold} "
            f"分数={[round(s, 3) for s in scores]} 过阈值={hit_indices}（命中 {len(hit_indices)}/{len(points)}，"
            f"最佳={best:.3f}）"
        )
        return {
            "template": template,
            "rows": rows, "cols": cols,
            "step": [col_dx, row_dy], "origin": list(origin), "cell_wh": [cell_w, cell_h],
            "threshold": threshold, "green_mask": green_mask,
            "points": points,
            "scores": scores,
            "hits": hits,
            "hit_indices": hit_indices,
            "hit_count": len(hit_indices),
            "best_score": best,
        }

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

        # ===== 阶段 A：空栏位清点（纯 ColorMatch，无点击）=====
        empty_slots = self._scan_empty_slots(context, tag, argv_dict.get("empty_slots", {}))

        # ===== 阶段 A2：过期标记模板扫描（纯 TemplateMatch+green_mask，无点击）=====
        expired_marks = self._scan_expired(context, tag, argv_dict.get("expired_marks", {}))

        # ===== 阶段 B：逐格扫描物品详情/价格 =====
        centers = self._grid_centers(grid_roi, rows, cols)
        logger.info(
            f"[shopScan] [{tag}] 阶段B 开始扫描 grid_roi={grid_roi} {rows}x{cols}="
            f"{len(centers)} 格，close_roi={close_roi}"
        )

        items = []
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
            "empty_slots": empty_slots,         # 阶段 A 结果
            "expired_marks": expired_marks,     # 阶段 A2 结果（过期标记模板扫描）
            "grid_roi": grid_roi,
            "rows": rows,
            "cols": cols,
            "items": items,                     # 阶段 B 结果
        }
        self._save(payload)
        sellable_n = sum(1 for it in items if it.get("sellable"))
        logger.info(
            f"[shopScan] [{tag}] 扫描完成 阶段A空栏位={empty_slots['empty_count']}/"
            f"{len(empty_slots['points'])}，阶段A2过期标记={expired_marks['hit_count']}/"
            f"{len(expired_marks['points'])}(best={expired_marks['best_score']:.3f})，"
            f"阶段B {len(items)}/{len(centers)} 格 sellable={sellable_n}"
        )
        return CustomAction.RunResult(success=True)
