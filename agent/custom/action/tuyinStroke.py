"""拓印考验：颜色区域识别 + 骨架笔画路径规划（纯算法模块，可独立运行验证）。

场景：侠士副本入口"拓印考验"弹窗（见 memory ``tuyin-popup-blocks-xiashi-entry``）——
弹窗中央石碑上有一个浅色汉字，需按住涂墨覆盖笔画，完成度 ≥70% 点「完成」通过。

算法（三步）：
  ① 颜色命中：ROI 内逐像素与 ``color``（默认 [223,186,142]，笔画浅金色）比对，
     每个维度 ± ``tolerance``（默认 10）容错，得到二值 mask；
  ② 降噪：5×5 分块投票（块内 25 像素 ≥ ``min_hits_in_cell`` 个命中才判整块命中）
     + 形态学 erosion（吃掉零散噪点/边缘过渡带），聚合到块网格；
  ③ 骨架笔画路径（"收缩到骨架再遍历"，像写字的笔顺）：
     a. **骨架化**：把笔画区域收缩成单格宽的中轴（Zhang-Suen 细化，纯 numpy，
        逐轮剥离边缘格直到只剩骨架）——骨架天然就是"沿笔画中央"的路径；
     b. **图遍历拆笔段**：骨架点上建 8-邻域图，按邻域数分类
        （1=端点，2=普通点，≥3=交叉点）。交叉点收拢成节点后，
        图变成"端点/节点之间由普通点串成的边"——每条边就是一段笔画的骨架；
     c. **交叉配对合笔**：一个交叉点的多条边按"进/出方向夹角最大"两两配对
        （笔画穿过交叉继续走，如 乂 的撇捺在交点各保持一笔），
        配对成功的边首尾相接成一条折线 = 一笔 swipe；配不上的边（如 丁 的顶钩）
        自成一笔。最后按书写顺序（起点 y,x）排序输出。

独立验证（不依赖 MaaFw）::

    python agent/custom/action/tuyinStroke.py --image <screenshot.png>

    # 输出：
    #   debug/tuyin_mask.png       —— 识别热力图（红=原始命中，绿=降噪后保留）
    #   debug/tuyin_path.png       —— 路径可视化（每笔一色 + 序号，起笔红圈；笔间不画线）
    #   stdout                     —— 命中统计 + 每笔顶点数/覆盖率 + 整图绝对坐标
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field

import numpy as np

try:  # OpenCV 可选：缺了只影响可视化（erosion 有 numpy 兜底）
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


# ---------------------------------------------------------------- ① 颜色命中

def color_mask(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
    color: tuple[int, int, int] = (223, 186, 142),
    tolerance: int = 10,
) -> np.ndarray:
    """ROI 内颜色命中二值图。

    :param image:   BGR 或 RGB 整图（只做逐通道差，通道序与 ``color`` 一致即可）
    :param roi:     (x, y, w, h) 感兴趣区
    :param color:   目标颜色（与 image 同通道序）
    :param tolerance: 每通道容差
    :return:        bool 数组，形状 (h, w)，True = 命中
    """
    x, y, w, h = roi
    patch = np.asarray(image, dtype=np.int16)[y : y + h, x : x + w]
    target = np.array(color, dtype=np.int16)
    diff = np.abs(patch - target)  # (h, w, 3)
    return np.all(diff <= tolerance, axis=2)


# ---------------------------------------------------------------- ② 降噪

def block_vote(mask: np.ndarray, cell: int = 5, min_hits: int = 20) -> np.ndarray:
    """5×5 分块投票：块内 25 像素 ≥ min_hits 命中 → 整块置命中（边缘不满 cell 的块按面积折算）。

    等效于"绝大多数像素命中才算"的局部共识，天然滤掉椒盐噪声。
    """
    h, w = mask.shape
    out = np.zeros_like(mask)
    for y0 in range(0, h, cell):
        for x0 in range(0, w, cell):
            block = mask[y0 : y0 + cell, x0 : x0 + cell]
            need = max(1, round(min_hits * block.size / (cell * cell)))
            if int(block.sum()) >= need:
                out[y0 : y0 + cell, x0 : x0 + cell] = True
    return out


def erode(mask: np.ndarray, px: int = 1) -> np.ndarray:
    """erosion 去小噪声点。优先 OpenCV；缺失时用 numpy 滑窗 min（等效方形核腐蚀）。"""
    if px <= 0:
        return mask.copy()
    if cv2 is not None:
        kernel = np.ones((px, px), np.uint8)
        return cv2.erode(mask.astype(np.uint8), kernel, iterations=1).astype(bool)
    m = mask.copy()
    for _ in range(px):
        p = np.pad(m, 1, constant_values=False)
        m = p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return m


def to_grid(mask: np.ndarray, cell: int = 5) -> np.ndarray:
    """像素 mask 聚合到块网格：每 cell×cell 块 ≥ 一半像素命中则该格为 1。"""
    gh, gw = mask.shape[0] // cell, mask.shape[1] // cell
    grid = np.zeros((gh, gw), dtype=bool)
    for gy in range(gh):
        for gx in range(gw):
            blk = mask[gy * cell : (gy + 1) * cell, gx * cell : (gx + 1) * cell]
            if blk.size and blk.mean() >= 0.5:
                grid[gy, gx] = True
    return grid


# ---------------------------------------------------------------- ③ 骨架笔画路径

@dataclass
class StrokePaths:
    """多笔 swipe 路径：每笔一条折线（ROI 内相对坐标）+ 统计。"""

    strokes: list[list[tuple[int, int]]] = field(default_factory=list)
    cells_total: int = 0          # 降噪后命中块数（cell×cell 块）
    coverage_ratio: float = 0.0   # 笔画半径圆盘覆盖的命中格比例
    off_stroke_ratio: float = 0.0 # 路径总长中落在命中格外的比例（越低越贴合）

    def absolute(self, roi: tuple[int, int, int, int]) -> list[list[tuple[int, int]]]:
        """换算整图绝对坐标（给 post_swipe 用），返回与 strokes 同构的嵌套列表。"""
        x, y, _, _ = roi
        return [[(x + px, y + py) for px, py in stroke] for stroke in self.strokes]


# ---- a. 骨架化（Zhang-Suen 细化，纯 numpy） ----

_NB_OFFSETS = [(-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1)]
# 上面按 P2,P3,...,P9 顺时针排列（P2=北），Zhang-Suen 条件按此下标环取邻居


def skeletonize(grid: np.ndarray) -> np.ndarray:
    """Zhang-Suen 细化：把命中区域收缩成 8-连通单格宽骨架（中轴）。

    纯 numpy 向量化按轮剥离：子迭代 1 剥"可删的南/东边界点"，子迭代 2 剥北/西，
    直到两轮都无变化。输出骨架 = 笔画中央路径。
    """
    img = grid.astype(np.uint8)
    if img.sum() == 0:
        return img.astype(bool)

    def neighbors(pad: np.ndarray) -> list[np.ndarray]:
        """pad = np.pad(img,1) 后取 P2..P9 视图（顺时针，P2=北）。"""
        return [
            pad[0:-2, 1:-1],  # P2 N
            pad[0:-2, 2:],    # P3 NE
            pad[1:-1, 2:],    # P4 E
            pad[2:, 2:],      # P5 SE
            pad[2:, 1:-1],    # P6 S
            pad[2:, 0:-2],    # P7 SW
            pad[1:-1, 0:-2],  # P8 W
            pad[0:-2, 0:-2],  # P9 NW
        ]

    while True:
        changed = False
        for phase in (0, 1):  # 0: 剥 S/E 边，1: 剥 N/W 边
            pad = np.pad(img, 1)
            P = neighbors(pad)
            B = sum(P)
            # 顺时针序列 0→2→4→6 的 01 模式数（连通分量转角数）
            A = (
                ((P[0] == 0) & (P[1] == 1)).astype(np.uint8)
                + ((P[2] == 0) & (P[3] == 1)).astype(np.uint8)
                + ((P[4] == 0) & (P[5] == 1)).astype(np.uint8)
                + ((P[6] == 0) & (P[7] == 1)).astype(np.uint8)
                + ((P[1] == 0) & (P[2] == 1)).astype(np.uint8)
                + ((P[3] == 0) & (P[4] == 1)).astype(np.uint8)
                + ((P[5] == 0) & (P[6] == 1)).astype(np.uint8)
                + ((P[7] == 0) & (P[0] == 1)).astype(np.uint8)
            )
            cond = (
                (img == 1)
                & (B >= 2)
                & (B <= 6)
                & (A == 1)
            )
            if phase == 0:
                cond &= (P[0] * P[2] * P[4] == 0) & (P[2] * P[4] * P[6] == 0)
            else:
                cond &= (P[0] * P[2] * P[6] == 0) & (P[0] * P[4] * P[6] == 0)
            if cond.any():
                img[cond] = 0
                changed = True
        if not changed:
            break
    return img.astype(bool)


# ---- b+c. 骨架 → 笔画：方向延续走笔（continuation walk） ----

def _neighbors8(sk: np.ndarray, p: tuple[int, int]) -> list[tuple[int, int]]:
    h, w = sk.shape
    y, x = p
    out = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            yy, xx = y + dy, x + dx
            if 0 <= yy < h and 0 <= xx < w and sk[yy, xx]:
                out.append((yy, xx))
    return out


def _cluster_junctions(
    sk: np.ndarray, junc: set[tuple[int, int]]
) -> list[set[tuple[int, int]]]:
    """把直接 8-相邻的交叉点格聚成节点（细化在对角交叉处会产生 2-4 格的小簇）。"""
    parent = {p: p for p in junc}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for p in junc:
        for q in _neighbors8(sk, p):
            if q in junc:
                union(p, q)
    clusters: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for p in junc:
        clusters.setdefault(find(p), set()).add(p)
    return list(clusters.values())


def _walk_strokes(
    sk: np.ndarray, turn_cos: float = 0.0
) -> list[list[tuple[int, int]]]:
    """骨架 → 笔画折线（格坐标）：方向延续走笔。

    像一支笔沿骨架走：每步从当前方向（上一步位移）出发，挑"与当前方向夹角最小"
    的未走邻格前进；交叉点也不例外——最直的延伸就是笔画本体（撇穿过交叉、
    框架拐过角落）。当最好的延续都要转 ≥90°（cos ≤ turn_cos）或无路可走时收笔。

    **交叉格共享**：deg≥3 的骨架格允许多条笔画共用（只做 pass-through 不计入
    visited）——乂 的两条对角线各自穿过交叉点互不阻断；普通格独占。

    :param turn_cos: 允许的最大转角 cos（0.0=可直角转，再狠就分笔）
    """
    pts = set(zip(*np.nonzero(sk)))
    if not pts:
        return []
    nbr = {p: _neighbors8(sk, p) for p in pts}
    deg = {p: len(nbr[p]) for p in pts}
    junc = {p for p, d in deg.items() if d >= 3}
    ends = [p for p, d in deg.items() if d == 1]
    starts = sorted(ends, key=lambda p: (p[0], p[1])) or sorted(pts)[:1]

    visited: set[tuple[int, int]] = set()

    def norm(v: tuple[int, int]) -> tuple[float, float]:
        n = (v[0] * v[0] + v[1] * v[1]) ** 0.5 or 1.0
        return (v[0] / n, v[1] / n)

    def free(q: tuple[int, int]) -> bool:
        """格可用 = 未访问，或交叉格（可被多笔共用）。"""
        return q not in visited or q in junc

    def extend(start: tuple[int, int], first: tuple[int, int]) -> list[tuple[int, int]]:
        """从 start→first 单向走笔到尽头。"""
        path = [start, first]
        if start not in junc:
            visited.add(start)
        if first not in junc:
            visited.add(first)
        while True:
            cur = path[-1]
            d = norm((cur[0] - path[-2][0], cur[1] - path[-2][1]))
            cands = [q for q in nbr[cur] if free(q)]
            if not cands:
                break
            best = None
            for q in cands:
                qd = norm((q[0] - cur[0], q[1] - cur[1]))
                cos = d[0] * qd[0] + d[1] * qd[1]
                if best is None or cos > best[0]:
                    best = (cos, q)
            cos, q = best
            if cos < turn_cos:
                break
            path.append(q)
            if q not in junc:
                visited.add(q)
        return path

    def walk(start: tuple[int, int]) -> list[tuple[int, int]] | None:
        """从端点双向延伸拼一笔。"""
        nb = [q for q in nbr[start] if free(q)]
        if not nb:
            # 孤立格（点/块状骨架）：自身成一笔（点按一下）
            if start not in visited:
                visited.add(start)
                return [start, start]
            return None
        if len(nb) >= 2:
            # 两个相反方向各走一头：先走第一头，再从 start 走另一头
            h1 = extend(start, nb[0])
            h2 = extend(start, nb[1])
            if len(h1) < len(h2):  # 长的一头在前（笔画主体方向）
                h1, h2 = h2, h1
            # 防闭环：若 h1 的尾部（不含起笔格）已碰到 h2 走过的格（同一条路
            # 汇合/环回起点），剪掉 h1 的环回段 —— 否则拼出首尾重复的环状折线
            # （涂墨时每段 50px+ 大跳 → 游戏判"乱划"丢弃，见 08-26 排查）。
            h1_tail = set(h1[1:])
            cut = 0
            for i, p in enumerate(h1):
                if p in h2[1:] and p != h1[0]:
                    cut = i
                    break
            if cut > 0:
                h1 = h1[:cut]
            full = list(reversed(h2)) + h1[1:]
            # 去重：若尾 == 首（环回），去掉尾（闭环张开成一条路径）
            if len(full) > 2 and full[-1] == full[0]:
                full = full[:-1]
        else:
            full = extend(start, nb[0])
        return full if len(full) >= 2 else None

    strokes: list[list[tuple[int, int]]] = []
    for s in starts:
        if s in visited:
            continue
        st = walk(s)
        if st:
            strokes.append(st)

    # 剩余未访问骨架（走笔被 turn 限制截断处）：从任意未访问格继续走
    while True:
        rest = [p for p in pts if p not in visited and p not in junc]
        if not rest:
            # 只剩交叉格 → 全部走完
            break
        st = walk(rest[0])
        if not st or all(p in junc or p in visited for p in (st[0], st[-1])) and len(st) < 2:
            visited.add(rest[0])
            continue
        if st:
            strokes.append(st)
        else:
            visited.add(rest[0])

    return [s for s in strokes if len(s) >= 2]


def _seg_dist(p, a, b) -> float:
    """点 p 到线段 ab 的距离。"""
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L = (dx * dx + dy * dy) ** 0.5
    if L == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    return abs(dy * px - dx * py + bx * ay - by * ax) / L


def _rdp(pts: list[tuple[int, int]], eps: float) -> list[tuple[int, int]]:
    """Ramer-Douglas-Peucker 抽稀：去掉偏离直线 ≤ eps 的点，保留真拐点。

    骨架在对角笔画上是阶梯状（每步 1 格），RDP 把阶梯压成干净的斜线段。
    """
    if len(pts) <= 2:
        return pts
    a, b = pts[0], pts[-1]
    dmax, imax = 0.0, 0
    for i in range(1, len(pts) - 1):
        d = _seg_dist(pts[i], a, b)
        if d > dmax:
            dmax, imax = d, i
    if dmax > eps:
        left = _rdp(pts[: imax + 1], eps)
        right = _rdp(pts[imax:], eps)
        return left[:-1] + right
    return [a, b]


def _simplify(poly: list[tuple[int, int]], eps: float = 2.0) -> list[tuple[int, int]]:
    """折线抽稀（RDP，eps 格）：压掉骨架阶梯抖动，只留笔画级拐点。"""
    return _rdp([(int(p[0]), int(p[1])) for p in poly], eps)


def build_stroke_paths(
    mask: np.ndarray,
    cell: int = 5,
    min_branch: int = 4,
    turn_cos: float = 0.0,
) -> StrokePaths:
    """从降噪 mask 生成骨架中轴笔画路径。

    :param cell:       mask 聚合块粒度（px）
    :param min_branch: 短于此格数的笔画当毛刺丢弃
    :param turn_cos:   走笔允许的最大转角 cos（0.0=允许直角内继续，再狠就分笔）
    """
    grid = to_grid(mask, cell)
    total = int(grid.sum())
    if total == 0:
        return StrokePaths(cells_total=0)

    sk = skeletonize(grid)
    grid_strokes = _walk_strokes(sk, turn_cos=turn_cos)

    # 按书写顺序：起笔 y、x 排序（从上到下、从左到右）
    grid_strokes.sort(key=lambda s: (s[0][0], s[0][1]))
    # 抽稀（保留拐点）+ 过短笔丢弃
    grid_strokes = [_simplify(s) for s in grid_strokes]
    grid_strokes = [s for s in grid_strokes if len(s) >= 2]

    strokes = [
        [(int(gyx[1]) * cell + cell // 2, int(gyx[0]) * cell + cell // 2) for gyx in gs]  # (gy,gx)→(px,py)
        for gs in grid_strokes
    ]
    result = StrokePaths(strokes=strokes, cells_total=total)
    _measure(result, grid, cell)
    return result


def _measure(result: StrokePaths, grid: np.ndarray, cell: int) -> None:
    """统计：①圆盘覆盖率（路径沿线采样点半径 2 格圆盘覆盖的命中格比例）；
    ②笔画外占比（采样点落在命中格半径外）。均沿折线边采样，非仅顶点。"""
    gh, gw = grid.shape
    radius = 3  # 刷子半径（格）≈ 游戏涂墨手指半径 15px
    if not result.strokes:
        result.coverage_ratio = 0.0
        result.off_stroke_ratio = 0.0
        return

    def sample_cells(stroke: list[tuple[int, int]]):
        """沿折线边采样，yield (gy, gx)。"""
        for (x1, y1), (x2, y2) in zip(stroke, stroke[1:]):
            n = max(2, int(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5))
            for t in np.linspace(0, 1, n):
                sx = int(round(x1 + (x2 - x1) * t))
                sy = int(round(y1 + (y2 - y1) * t))
                yield sy // cell, sx // cell

    covered: set[tuple[int, int]] = set()
    for stroke in result.strokes:
        for gy, gx in sample_cells(stroke):
            for oy in range(-radius, radius + 1):
                for ox in range(-radius, radius + 1):
                    yy, xx = gy + oy, gx + ox
                    if 0 <= yy < gh and 0 <= xx < gw:
                        covered.add((yy, xx))
    hit_cells = {(y, x) for y in range(gh) for x in range(gw) if grid[y, x]}
    result.coverage_ratio = (
        len(hit_cells & covered) / len(hit_cells) if hit_cells else 0.0
    )

    total = off = 0
    for stroke in result.strokes:
        for gy, gx in sample_cells(stroke):
            near = any(
                0 <= gy + oy < gh and 0 <= gx + ox < gw and grid[gy + oy, gx + ox]
                for oy in range(-radius, radius + 1)
                for ox in range(-radius, radius + 1)
            )
            total += 1
            if not near:
                off += 1
    result.off_stroke_ratio = off / total if total else 0.0


# ---------------------------------------------------------------- 对外入口

def analyze(
    image: np.ndarray,
    roi: tuple[int, int, int, int] = (508, 189, 331, 336),
    color: tuple[int, int, int] = (223, 186, 142),
    tolerance: int = 10,
    cell: int = 5,
    min_hits: int = 20,
    erosion_px: int = 1,
    min_branch: int = 4,
) -> tuple[np.ndarray, np.ndarray, StrokePaths]:
    """完整流水线：整图 → (raw_mask, clean_mask, strokes)。

    ``image`` 为 BGR（cv2.imread）或 RGB（PIL）整图，通道序与 ``color`` 保持一致即可。
    """
    raw = color_mask(image, roi, color, tolerance)
    clean = erode(block_vote(raw, cell=cell, min_hits=min_hits), erosion_px)
    strokes = build_stroke_paths(clean, cell=cell, min_branch=min_branch)
    return raw, clean, strokes


# ---------------------------------------------------------------- CLI 验证

_PALETTE = [  # BGR，每笔一色
    (255, 128, 0), (0, 200, 255), (200, 0, 255), (0, 255, 100),
    (255, 0, 150), (160, 160, 255), (80, 255, 255), (255, 180, 80),
]


def _visualize(
    image_bgr: np.ndarray,
    roi: tuple[int, int, int, int],
    raw: np.ndarray,
    clean: np.ndarray,
    strokes: StrokePaths,
    out_dir: str,
) -> None:
    if cv2 is None:
        print("(cv2 不可用，跳过可视化)")
        return
    x, y, w, h = roi
    os.makedirs(out_dir, exist_ok=True)

    # 热力图：整图 + ROI 上色（红=原始命中，绿=降噪保留）
    heat = image_bgr.copy()
    heat[y : y + h, x : x + w][raw] = (0, 0, 255)      # BGR 红
    heat[y : y + h, x : x + w][clean] = (0, 255, 0)    # BGR 绿
    cv2.imwrite(os.path.join(out_dir, "tuyin_mask.png"), heat)

    # 路径图：ROI 放大，每笔一种颜色、起笔红圈+序号；笔间不画连接线；拐点画黄点
    canvas = image_bgr[y : y + h, x : x + w].copy()
    for i, stroke in enumerate(strokes.strokes):
        color = _PALETTE[i % len(_PALETTE)]
        for (x1, y1), (x2, y2) in zip(stroke, stroke[1:]):
            cv2.line(canvas, (x1, y1), (x2, y2), color, 3)
        for px, py in stroke:
            cv2.circle(canvas, (px, py), 3, (0, 255, 255), -1)  # 黄点=拐点
        sx, sy = stroke[0]
        cv2.circle(canvas, (sx, sy), 7, (0, 0, 255), 2)          # 起笔红圈
        cv2.putText(canvas, str(i + 1), (sx + 9, sy - 9),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.imwrite(os.path.join(out_dir, "tuyin_path.png"), canvas)


def main() -> None:
    ap = argparse.ArgumentParser(description="拓印颜色识别 + 骨架笔画路径 验证 CLI")
    ap.add_argument("--image", required=True, help="截图路径（1280x720 整图）")
    ap.add_argument("--roi", nargs=4, type=int, default=[508, 189, 331, 336])
    ap.add_argument("--color", nargs=3, type=int, default=[223, 186, 142])
    ap.add_argument("--tolerance", type=int, default=10)
    ap.add_argument("--cell", type=int, default=5)
    ap.add_argument("--min-hits", type=int, default=20)
    ap.add_argument("--erosion", type=int, default=1)
    ap.add_argument("--min-branch", type=int, default=4, help="短于此格数的笔段当毛刺丢弃")
    ap.add_argument("--out", default="debug", help="可视化输出目录")
    args = ap.parse_args()

    img = cv2.imread(args.image)  # BGR —— color 参数按 RGB 给时需换序
    color_bgr = tuple(reversed(args.color))
    roi = tuple(args.roi)

    raw, clean, strokes = analyze(
        img, roi, color_bgr, args.tolerance,
        cell=args.cell, min_hits=args.min_hits, erosion_px=args.erosion,
        min_branch=args.min_branch,
    )

    x, y, w, h = roi
    print(f"ROI {roi}（{w}x{h} = {w * h} px）")
    print(f"原始颜色命中: {int(raw.sum())} px ({raw.mean() * 100:.1f}%)")
    print(f"块投票+erosion 后: {int(clean.sum())} px ({clean.mean() * 100:.1f}%)")
    print(f"命中块数({args.cell}x{args.cell}): {strokes.cells_total}")
    if not strokes.strokes:
        print("!! 无路径 —— 降噪后没有可识别的笔画区域")
        return
    print(f"骨架笔画路径: {len(strokes.strokes)} 笔，每笔拐点数 "
          f"{[len(s) for s in strokes.strokes]}")
    print(f"圆盘覆盖率: {strokes.coverage_ratio * 100:.1f}%"
          f" | 笔画外路径占比: {strokes.off_stroke_ratio * 100:.1f}%")
    print("路径坐标（整图绝对，每笔一条折线，可直接喂 post_swipe）:")
    for i, stroke in enumerate(strokes.absolute(roi)):
        print(f"  第{i + 1}笔: {stroke}")

    _visualize(img, roi, raw, clean, strokes, args.out)
    print(f"可视化已写入 {args.out}/tuyin_mask.png 与 {args.out}/tuyin_path.png")


if __name__ == "__main__":
    main()
