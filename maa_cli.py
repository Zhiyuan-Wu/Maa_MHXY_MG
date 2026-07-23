"""maa_cli —— 针对单个游戏窗口的 MaaFramework 截图/识别/动作 调试 CLI（server + client）。

== 为什么有这个脚本 ==
``agent/run_5r.py``（MuMu/ADB 五开编排）、``shikong.py``（梦幻西游：时空 PC 客户端 Win32
五开）都是「连上控制器→跑整条 pipeline」的编排脚本。调参/排查时缺少一个能**针对当前窗口
做单次截图 / 单次识别 / 单次动作并回显完整结果**的轻量工具——本脚本补这个缺口。

== 两种角色（同一脚本）==
  1. ``--server``：常驻一个连好游戏窗口的 Tasker（Win32 控制器 + 桌面窗口），开 HTTP 服务。
  2. ``crop`` / ``reco`` / ``action`` / ``windows``：纯 HTTP 客户端，向已启动的 server 发请求，
     拿回完整结果。客户端**不依赖 MaaFw**——对应需求里「在 server 启动后，再次运行脚本 cli」。

控制器口径与 ``shikong.py`` 一致：**Win32 控制器 + 桌面窗口（hwnd）**。「可选指定 id」即可选
指定窗口句柄 hwnd（或 1-based 序号）。

== HTTP 端点（全部 JSON；出错 HTTP 4xx/5xx + ``{"error":...}``）==
  GET  /windows  重新枚举游戏窗口 → [{index,hwnd,pid,title,rect}]
  GET  /info     {resolution, uuid, resource_path, win32:{...}}
  POST /crop     {path?, image?}              截图存盘 → {path,width,height}
  POST /reco     {type, param, image?}        单次识别 → 完整 RecognitionDetail
                                              （hit/box/all_results/filtered_results/best_result/raw_detail）
  POST /action   {type, param, box?, reco_detail?}  单次动作 → 完整 ActionDetail
                                              （success/box/result/raw_detail）

== 用法（用 CLAUDE.md 指定的 python）==
  # 终端 1：开服务（游戏窗口已打开）
  python maa_cli.py --server                      # 自动选窗口（唯一则用之；多个默认 index=1）
  python maa_cli.py --server --hwnd 0x12345       # 指定窗口句柄（int 或 0x 十六进制）
  python maa_cli.py --server --index 2            # 按枚举序号选
  python maa_cli.py --server --port 8080          # 换端口；--resource 换资源（须 Win32 可用，如 shikong）

  # 终端 2：发请求
  python maa_cli.py windows
  python maa_cli.py crop C:/tmp/shot.png
  python maa_cli.py crop                                  # 路径缺省 → server 存到 debug/maa_cli/
  python maa_cli.py reco OCR "{\"roi\":[0,0,400,80]}"
  python maa_cli.py reco OCR @reco_ocr.json               # PARAM 从文件读
  python maa_cli.py reco TemplateMatch "{\"template\":[\"image/某个按钮.png\"],\"roi\":[100,100,200,80]}"
  python maa_cli.py action Click "{\"target\":[100,200,1,1]}"
  python maa_cli.py action ClickKey "{\"key\":[27]}"      # Win32 VK_ESCAPE=27（键码随控制器而异，见下）
  python maa_cli.py action Swipe "{\"begin\":[100,100],\"end\":[[300,100]],\"duration\":[200]}" --box 0 0 0 0

PARAM 是 JSON 对象（内联 / ``@file.json`` / ``-`` 读 stdin / 缺省 ``{}``）。可用识别/动作类型见
``maa.pipeline.JRecognitionType`` / ``JActionType``（DirectHit/TemplateMatch/OCR/... ；
Click/Swipe/ClickKey/InputText/...）。

== 关于按键（ClickKey/KeyDown/KeyUp，依据 deps/docs/zh_cn/3.1-任务流水线协议.md:1121）==
  - **键码随控制器而异**：Win32 用虚拟键码 VK（Alt=18、G=71、ESC=27、F1=112…，见
    https://learn.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes ）；Adb 用 Android
    KeyEvent（Alt=57、A=29…）；本工具默认 Win32。
  - **``ClickKey.key`` 是「逐个单击」序列，不是组合键**：``{"key":[18,71]}`` = 单击 Alt、再单击 G
    （两次独立 down+up），**不会**形成 Alt+G 和弦。要用组合键（如 Alt+G 开挂机页），必须用
    ``KeyDown``/``KeyUp`` 显式时序：KeyDown(18)→KeyDown(71)→KeyUp(71)→KeyUp(18)。
    （文档原文：KeyDown「按下按键但不立即松开，可与 KeyUp 配合实现自定义按键时序」。）

== 不做（明确范围）==
  - 不做 ADB/PlayCover 控制器（需求是「窗口」+ hwnd，对应 Win32）。
  - 不注册 custom 识别/动作（需独立 agent 进程，超出「单次 reco/action」定位）。
  - 不做鉴权/持久化（本地调试工具，监听 127.0.0.1）。
"""
import argparse
import builtins
import ctypes
import dataclasses
import faulthandler
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ctypes import wintypes

# native 崩溃（段错误/abort/访问违例）时把出错线程的 Python 栈打到 stderr——
# MaaFw C++ 侧崩了 Python 的 except/finally 跑不到，不加这个死得毫无线索（见 run_5r.py）。
faulthandler.enable(all_threads=True)

# 中文乱码的根因：本脚本按 UTF-8 输出字节，但中文 Windows 控制台默认代码页是 cp936(GBK)，
# 会把 UTF-8 字节当 GBK 渲染 → 乱码（例：UTF-8 的 "已"=e5b7b2 被 GBK 解成 "宸蹭"）。
# 两层一起修才彻底：(1) 把当前控制台代码页切到 UTF-8(65001)；(2) stdout/stderr 按 UTF-8 编码。
# 这样【控制台 / 重定向到文件 / 管道给别的进程】三种情形中文都正确——重定向/管道得到标准 UTF-8
# 字节流，消费方按 UTF-8 读即可（若消费方也是 Python，最稳是 PYTHONUTF8=1 或也 reconfigure）。
if sys.platform == "win32":
    try:
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.SetConsoleOutputCP(65001)   # CP_UTF8：让控制台按 UTF-8 解释输出字节
        _k32.SetConsoleCP(65001)         # 输入侧同步切 UTF-8
    except Exception:
        pass
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ==================== 配置区（换机器/改默认时改这里）====================
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(REPO_DIR, "debug")                                   # MaaFw 工作目录 + 日志
# 桌面端（Win32/时空）资源。注意：shikong 的 shuangbei.json 引用了 base 才有的 helper 节点
# （空节点 / panduan_zhujiemian），故 shikong/pipeline 下已补一份**最小依赖集合**（见 _base_deps.json），
# 使其能独立通过 PipelineChecker。换回 base 资源在 Win32 下不可用（base 是 ADB/模拟器向）。
RESOURCE_PATH = os.path.join(REPO_DIR, "assets", "resource", "shikong")
WINDOW_TITLE_KEYWORDS = ["梦幻西游"]   # 标题 startswith 任一关键字才算游戏窗口（避免误伤编辑器/终端）
# Win32 控制方式（取自 interface.json「桌面端1」当前默认：后台可用、不抢鼠标）。合法名见 maa.define：
#   MaaWin32ScreencapMethodEnum: FramePool / GDI / PrintWindow / ScreenDC / DXGI_* ...
#   MaaWin32InputMethodEnum:     Seize / SendMessage / PostMessage / SendMessageWithCursorPos ...
WIN32_SCREENCAP = "FramePool"
WIN32_MOUSE = "SendMessageWithCursorPos"
WIN32_KEYBOARD = "SendMessageWithCursorPos"
HOST = "127.0.0.1"
PORT = 13520
CROP_DIR = os.path.join(DEBUG_DIR, "maa_cli")   # crop 路径缺省时存这里
# =========================================================================

_orig_print = builtins.print


def _log_print(*args, **kwargs):
    """带 [HH:MM:SS] 前缀、强制 flush 的 print（透传 sep=/end=，吞掉 flush=）。仅本模块可见。"""
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    msg = sep.join(str(a) for a in args)
    _orig_print(f"[{time.strftime('%H:%M:%S')}] {msg}", end=end, flush=True)


print = _log_print  # 模块内 shadow builtins.print


# ---------------- maa.* 懒导入 ----------------
# 客户端子命令（crop/reco/action/windows）是纯 HTTP，**不应**触发 maa.* 导入（那会加载 MaaFw
# 原生库）。把所有 maa.* 导入放进 ``_import_maa()``，只在 server 模式与窗口枚举时调用。
_MAA = None


def _import_maa():
    global _MAA
    if _MAA is not None:
        return _MAA
    from maa.toolkit import Toolkit
    from maa.controller import Win32Controller
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.library import Library
    from maa.define import (MaaWin32ScreencapMethodEnum, MaaWin32InputMethodEnum,
                            Rect, Point)
    from maa.pipeline import (
        JRecognitionType, JActionType,
        JDirectHit, JTemplateMatch, JFeatureMatch, JColorMatch, JOCR,
        JNeuralNetworkClassify, JNeuralNetworkDetect, JAnd, JOr, JCustomRecognition,
        JDoNothing, JClick, JLongPress, JSwipe, JMultiSwipe, JTouch, JTouchUp,
        JClickKey, JLongPressKey, JKey, JInputText, JStartApp, JStopApp, JStopTask,
        JScroll, JCommand, JShell, JScreencap, JCustomAction,
    )
    _MAA = {
        "Toolkit": Toolkit, "Win32Controller": Win32Controller, "Resource": Resource,
        "Tasker": Tasker, "Library": Library,
        "MaaWin32ScreencapMethodEnum": MaaWin32ScreencapMethodEnum,
        "MaaWin32InputMethodEnum": MaaWin32InputMethodEnum, "Rect": Rect, "Point": Point,
        "JRecognitionType": JRecognitionType, "JActionType": JActionType,
        # recognition type → param dataclass（与 maa/pipeline.py 的 _parse_recognition_param 一致）
        "reco_map": {
            JRecognitionType.DirectHit: JDirectHit,
            JRecognitionType.TemplateMatch: JTemplateMatch,
            JRecognitionType.FeatureMatch: JFeatureMatch,
            JRecognitionType.ColorMatch: JColorMatch,
            JRecognitionType.OCR: JOCR,
            JRecognitionType.NeuralNetworkClassify: JNeuralNetworkClassify,
            JRecognitionType.NeuralNetworkDetect: JNeuralNetworkDetect,
            JRecognitionType.And: JAnd,
            JRecognitionType.Or: JOr,
            JRecognitionType.Custom: JCustomRecognition,
        },
        # action type → param dataclass（与 maa/pipeline.py 的 _parse_action_param 一致；
        # 注意 TouchDown/TouchMove→JTouch，KeyDown/KeyUp→JKey 的别名）
        "action_map": {
            JActionType.DoNothing: JDoNothing,
            JActionType.Click: JClick,
            JActionType.LongPress: JLongPress,
            JActionType.Swipe: JSwipe,
            JActionType.MultiSwipe: JMultiSwipe,
            JActionType.TouchDown: JTouch,
            JActionType.TouchMove: JTouch,
            JActionType.TouchUp: JTouchUp,
            JActionType.ClickKey: JClickKey,
            JActionType.LongPressKey: JLongPressKey,
            JActionType.KeyDown: JKey,
            JActionType.KeyUp: JKey,
            JActionType.InputText: JInputText,
            JActionType.StartApp: JStartApp,
            JActionType.StopApp: JStopApp,
            JActionType.StopTask: JStopTask,
            JActionType.Scroll: JScroll,
            JActionType.Command: JCommand,
            JActionType.Shell: JShell,
            JActionType.Screencap: JScreencap,
            JActionType.Custom: JCustomAction,
        },
    }
    return _MAA


# ---------------- Win32 辅助（找窗口/读 rect/pid；仅 server 端用）----------------
_user32 = ctypes.WinDLL('user32', use_last_error=True)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def _get_window_rect(hwnd):
    r = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return (0, 0, 0, 0)
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def _get_window_pid(hwnd):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _win32_method_value(kind, name):
    """``name`` (str) → MaaFw 枚举值。``kind`` = "screencap" | "mouse" | "keyboard"。
    screencap 用 ``MaaWin32ScreencapMethodEnum``；mouse/keyboard 用 ``MaaWin32InputMethodEnum``。
    取法对齐 shikong.py:_win32_method_value。"""
    m = _import_maa()
    enum = (m["MaaWin32ScreencapMethodEnum"] if kind == "screencap"
            else m["MaaWin32InputMethodEnum"])
    if not hasattr(enum, name):
        raise ValueError(f"未知 Win32 {kind} 方式 {name!r}（合法值见 maa.define.{enum.__name__}）")
    return int(getattr(enum, name))


def find_game_windows(keywords=None):
    """用 MaaToolkit 枚举桌面窗口，按标题关键字过滤，按 (y,x) 排序得「左→右、上→下」确定性顺序。
    返回 [{index(1-based), hwnd, pid, title, class_name, rect}]。"""
    keywords = keywords if keywords is not None else WINDOW_TITLE_KEYWORDS
    Toolkit = _import_maa()["Toolkit"]
    out = []
    for dw in Toolkit.find_desktop_windows():
        title = (dw.window_name or "").strip()
        if not (title and any(title == k or title.startswith(k) for k in keywords)):
            continue
        hwnd = dw.hwnd if isinstance(dw.hwnd, int) else (getattr(dw.hwnd, "value", 0) or 0)
        out.append({"hwnd": int(hwnd), "pid": _get_window_pid(int(hwnd)),
                    "title": title, "class_name": dw.class_name or "",
                    "rect": _get_window_rect(int(hwnd))})
    out.sort(key=lambda w: (round(w["rect"][1] / 50), w["rect"][0]))   # (y,x) 排序
    for i, w in enumerate(out, 1):
        w["index"] = i
    return out


# ---------------- 图像（BGR ndarray ↔ 文件；用 PIL，无 cv2 依赖）----------------
def _save_image(bgr, path):
    """BGR ndarray → 存盘（扩展名决定格式）。返回 (width, height)。"""
    from PIL import Image
    import numpy as np
    arr = bgr[..., ::-1]                       # BGR → RGB
    if arr.dtype != arr.dtype.type or str(arr.dtype) != "uint8":
        arr = arr.astype("uint8")
    img = Image.fromarray(arr, mode="RGB") if arr.ndim == 3 else Image.fromarray(arr)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path)
    return img.width, img.height


def _load_image(path):
    """文件 → BGR ndarray（供 reco 的 ``image`` 参数；与 MaaFw 截图同色序）。"""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    arr = np.asarray(img)                       # RGB
    return arr[..., ::-1].copy()                 # → BGR，C-contiguous


# ---------------- 参数构造（dict → dataclass，按字段名过滤未知键）----------------
def _build_param(cls, d):
    """``d`` (dict) → ``cls`` 实例。未知键（不在 dataclass 字段里）**忽略并返回其名**——
    避开 ``JPipelineParser._parse_param`` 的 ``except TypeError: return cls()`` 兜底（那会**静默**
    丢光全部参数）。返回 (instance, unknown_keys)。"""
    if not isinstance(d, dict):
        raise ValueError(f"param 必须是 JSON 对象，收到 {type(d).__name__}")
    valid = {f.name for f in dataclasses.fields(cls)}
    known = {k: v for k, v in d.items() if k in valid}
    unknown = [k for k in d if k not in valid]
    return cls(**known), unknown


# ---------------- 序列化（dataclass/Rect/Point/ndarray/Enum → JSON 友好）----------------
def _to_jsonable(o):
    from enum import Enum
    import numpy as np
    if o is None or isinstance(o, (bool, int, float)):
        return o
    if isinstance(o, str):                  # StrEnum 也是 str，这里直接返回其字符串值
        return o
    if isinstance(o, np.ndarray):           # 像素数据太大，只回 shape/dtype（raw_image/draw_images）
        return {"__ndarray__": True, "shape": list(o.shape), "dtype": str(o.dtype)}
    m = _import_maa() if _MAA is not None else None
    Rect = m["Rect"] if m else None
    Point = m["Point"] if m else None
    if Rect is not None and isinstance(o, Rect):
        return [o.x, o.y, o.w, o.h]
    if Point is not None and isinstance(o, Point):
        return [o.x, o.y]
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, dict):
        return {str(k): _to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_to_jsonable(x) for x in o]
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: _to_jsonable(getattr(o, f.name)) for f in dataclasses.fields(o)}
    return str(o)                           # 兜底：其它类型转字符串，绝不抛异常打断序列化


# ---------------- server 状态 ----------------
_STATE = {
    "tasker": None, "controller": None, "resource": None,
    "resource_path": "", "win32": {}, "keywords": list(WINDOW_TITLE_KEYWORDS),
    "lock": threading.RLock(),
}


def _screencap():
    """取一帧截图（BGR ndarray）。调用方须持锁。"""
    ctrl = _STATE["controller"]
    job = ctrl.post_screencap()
    job.wait()
    return job.get()                         # JobWithResult.get() → cached_image (ndarray)


def _do_reco(reco_type_str, param_dict, image_path=None):
    """单次识别，返回 RecognitionDetail（可能 None）。调用方须持锁。"""
    m = _import_maa()
    type_enum = m["JRecognitionType"](reco_type_str)
    cls = m["reco_map"].get(type_enum)
    if cls is None:
        raise ValueError(f"未知 recognition 类型: {reco_type_str}")
    param, unknown = _build_param(cls, param_dict)
    if unknown:
        print(f"!! reco 参数忽略未知键: {unknown}（{cls.__name__} 有效字段: "
              f"{[f.name for f in dataclasses.fields(cls)]}）")
    image = _load_image(image_path) if image_path else _screencap()
    tasker = _STATE["tasker"]
    job = tasker.post_recognition(type_enum, param, image)
    job.wait()
    td = job.get()                           # TaskDetail
    if td is None:
        return None, None
    # 找到带 recognition 的节点（单次识别任务通常只有一个节点）
    for node in td.nodes:
        if node.recognition is not None:
            return node.recognition, unknown
    return None, unknown


def _do_action(action_type_str, param_dict, box=(0, 0, 0, 0), reco_detail=""):
    """单次动作，返回 ActionDetail（可能 None）。调用方须持锁。"""
    m = _import_maa()
    type_enum = m["JActionType"](action_type_str)
    cls = m["action_map"].get(type_enum)
    if cls is None:
        raise ValueError(f"未知 action 类型: {action_type_str}")
    param, unknown = _build_param(cls, param_dict)
    if unknown:
        print(f"!! action 参数忽略未知键: {unknown}（{cls.__name__} 有效字段: "
              f"{[f.name for f in dataclasses.fields(cls)]}）")
    tasker = _STATE["tasker"]
    job = tasker.post_action(type_enum, param, box=tuple(box), reco_detail=reco_detail)
    job.wait()
    td = job.get()
    if td is None:
        return None, None
    for node in td.nodes:
        if node.action is not None:
            return node.action, unknown
    return None, unknown


# ---------------- HTTP handler ----------------
class _Handler(BaseHTTPRequestHandler):
    # 让 BaseHTTPRequestHandler 用我们的带时间戳 print（默认 log_message 走 stderr 无时间戳）
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    # ---- helpers ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code, msg):
        self._send_json({"error": msg}, code=code)

    def _read_body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"请求体不是合法 JSON: {e}")

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, dict(urllib.parse.parse_qsl(parsed.query))

    # ---- routing ----
    def do_GET(self):
        path, query = self._query()
        try:
            if path == "/windows":
                with _STATE["lock"]:
                    wins = find_game_windows(_STATE["keywords"])
                return self._send_json(wins)
            if path == "/info":
                with _STATE["lock"]:
                    ctrl = _STATE["controller"]
                    info = {
                        "resource_path": _STATE["resource_path"],
                        "win32": _STATE["win32"],
                        "connected": bool(ctrl and ctrl.connected),
                    }
                    try:
                        info["resolution"] = list(ctrl.resolution) if ctrl else [0, 0]
                    except Exception:
                        info["resolution"] = [0, 0]
                    try:
                        info["uuid"] = ctrl.uuid if ctrl else ""
                    except Exception:
                        info["uuid"] = ""
                return self._send_json(info)
            if path == "/" or path == "/help":
                return self._send_json({
                    "endpoints": ["GET /windows", "GET /info",
                                  "POST /crop {path?,image?}",
                                  "POST /reco {type,param,image?}",
                                  "POST /action {type,param,box?,reco_detail?}",
                                  "POST /task {entry,timeout?}"]})
            return self._send_error(404, f"未知路径: {path}")
        except Exception as e:
            traceback.print_exc()
            return self._send_error(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        path, query = self._query()
        try:
            body = self._read_body_json()
        except ValueError as e:
            return self._send_error(400, str(e))
        try:
            if path == "/crop":
                return self._handle_crop(body, query)
            if path == "/reco":
                return self._handle_reco(body)
            if path == "/action":
                return self._handle_action(body)
            if path == "/task":
                return self._handle_task(body)
            return self._send_error(404, f"未知路径: {path}")
        except Exception as e:
            traceback.print_exc()
            return self._send_error(500, f"{type(e).__name__}: {e}")

    # ---- endpoint impls ----
    def _handle_crop(self, body, query):
        path = body.get("path") or query.get("path")
        image_path = body.get("image") or query.get("image")
        if not path:
            os.makedirs(CROP_DIR, exist_ok=True)
            path = os.path.join(CROP_DIR, time.strftime("screencap_%Y%m%d_%H%M%S.png"))
        with _STATE["lock"]:
            if image_path:
                # 拷贝/转存指定图片到目标路径（统一成 PNG 等扩展名格式）
                from PIL import Image
                img = Image.open(image_path).convert("RGB")
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                img.save(path)
                w, h = img.size
            else:
                bgr = _screencap()
                w, h = _save_image(bgr, path)
        print(f"[crop] 截图已存 {path} ({w}x{h})")
        return self._send_json({"path": os.path.abspath(path), "width": w, "height": h})

    def _handle_reco(self, body):
        if "type" not in body:
            return self._send_error(400, "缺少 type（recognition 类型，如 OCR/TemplateMatch）")
        with _STATE["lock"]:
            reco, unknown = _do_reco(body["type"], body.get("param") or {},
                                     image_path=body.get("image"))
        if reco is None:
            return self._send_error(500, "recognition failed to start（识别未启动：type/param 非法或 image 为空？）")
        return self._send_json({"recognition": _to_jsonable(reco), "ignored_unknown_keys": unknown or []})

    def _handle_task(self, body):
        if "entry" not in body:
            return self._send_error(400, "缺少 entry（pipeline 入口节点名）")
        entry = body["entry"]
        timeout = float(body.get("timeout") or 60.0)
        with _STATE["lock"]:
            tasker = _STATE["tasker"]
            job = tasker.post_task(entry)
            t0 = time.time()
            while time.time() - t0 < timeout:
                if job.done:
                    break
                time.sleep(0.2)
            if not job.done:
                tasker.post_stop().wait()
            td = job.get()
            nodes = []
            for n in (getattr(td, "nodes", []) or []):
                reco = getattr(n, "recognition", None); act = getattr(n, "action", None)
                nodes.append({
                    "name": getattr(n, "name", "?"),
                    "recognition": {"hit": getattr(reco, "hit", None), "box": list(getattr(reco, "box", None)) if reco and getattr(reco, "box", None) else None} if reco else None,
                    "action": {"success": getattr(act, "success", None)} if act else None,
                })
            return self._send_json({
                "entry": entry, "completed": job.done, "elapsed_s": round(time.time() - t0, 2),
                "node_count": len(nodes), "nodes": nodes,
            })

    def _handle_action(self, body):
        if "type" not in body:
            return self._send_error(400, "缺少 type（action 类型，如 Click/Swipe/ClickKey）")
        box = body.get("box") or [0, 0, 0, 0]
        if len(box) != 4:
            return self._send_error(400, "box 必须是 [x,y,w,h]")
        reco_detail = body.get("reco_detail") or ""
        if not isinstance(reco_detail, str):
            reco_detail = json.dumps(reco_detail, ensure_ascii=False)
        with _STATE["lock"]:
            action, unknown = _do_action(body["type"], body.get("param") or {},
                                         box=box, reco_detail=reco_detail)
        if action is None:
            return self._send_error(500, "action failed to start（type/param 非法？）")
        return self._send_json({"action": _to_jsonable(action), "ignored_unknown_keys": unknown or []})


# ---------------- server 启动 ----------------
def _print_window_table(wins):
    print(f"{'idx':>3}  {'hwnd':>10}  {'pid':>8}  {'rect(x,y,w,h)':>24}  title")
    for w in wins:
        print(f"{w['index']:>3}  {w['hwnd']:>10}  {w['pid']:>8}  {str(w['rect']):>24}  {w['title']}")


def run_server(hwnd=None, index=None, keywords=None, resource_path=None,
               screencap=None, mouse=None, keyboard=None, host=None, port=None):
    """server 主流程：选窗 → 连 Win32 控制器 → 加载资源 → 起 Tasker → 开 HTTP。"""
    m = _import_maa()
    Toolkit, Win32Controller, Resource, Tasker = (m["Toolkit"], m["Win32Controller"],
                                                   m["Resource"], m["Tasker"])
    Toolkit.init_option(DEBUG_DIR)
    keywords = keywords if keywords is not None else list(WINDOW_TITLE_KEYWORDS)
    resource_path = resource_path or RESOURCE_PATH
    sc = _win32_method_value("screencap", screencap or WIN32_SCREENCAP)
    ms = _win32_method_value("mouse", mouse or WIN32_MOUSE)
    kb = _win32_method_value("keyboard", keyboard or WIN32_KEYBOARD)
    _STATE.update(resource_path=os.path.abspath(resource_path),
                  win32={"screencap": screencap or WIN32_SCREENCAP,
                         "mouse": mouse or WIN32_MOUSE,
                         "keyboard": keyboard or WIN32_KEYBOARD},
                  keywords=keywords)

    # ① 选窗：--hwnd 直接连；否则枚举 + --index/唯一/默认 index=1
    if hwnd is not None:
        target_hwnd = int(hwnd)
        print(f">>> 直接连指定 hwnd={target_hwnd}（0x{target_hwnd:x}）"
              f" pid={_get_window_pid(target_hwnd)} rect={_get_window_rect(target_hwnd)}")
    else:
        wins = find_game_windows(keywords)
        if not wins:
            raise RuntimeError(f"未找到标题以 {keywords} 开头的窗口。"
                               f" 用 --hwnd <句柄> 直接连，或 --title <关键字> 放宽。")
        if index is not None:
            match = [w for w in wins if w["index"] == index]
            if not match:
                _print_window_table(wins)
                raise RuntimeError(f"--index {index} 超出范围（共 {len(wins)} 个窗口）")
            target_hwnd = match[0]["hwnd"]
            print(f">>> 按序号选中 index={index}：hwnd={target_hwnd} {match[0]['title']}")
        elif len(wins) == 1:
            target_hwnd = wins[0]["hwnd"]
            print(f">>> 仅一个游戏窗口：hwnd={target_hwnd} {wins[0]['title']}")
        else:
            print("!! 发现多个游戏窗口，默认选 index=1（用 --index N 或 --hwnd 另选）：")
            _print_window_table(wins)
            target_hwnd = wins[0]["hwnd"]

    # ② 连 Win32 控制器
    print(f">>> Win32Controller(hwnd={target_hwnd}, screencap={screencap or WIN32_SCREENCAP}, "
          f"mouse={mouse or WIN32_MOUSE}, keyboard={keyboard or WIN32_KEYBOARD})")
    ctrl = Win32Controller(target_hwnd, sc, ms, kb)
    ctrl.post_connection().wait()
    if not ctrl.connected:
        raise RuntimeError("Win32 控制器连接失败（窗口可能已关闭/最小化，或截图方式不兼容）")

    # ③ 加载资源（OCR 模型 + 模板 + pipeline）
    if not os.path.isdir(resource_path):
        raise RuntimeError(f"资源目录不存在：{resource_path}（用 --resource 指定）")
    print(f">>> 加载资源 {resource_path}")
    resource = Resource()
    bundle_job = resource.post_bundle(resource_path).wait()
    if not bundle_job.status.succeeded:
        # 资源未通过 PipelineChecker（如节点引用缺失）。reco/action 依赖资源，故直接报错；
        # 详见 MaaFw 日志（debug/ 目录）。crop 仅需控制器，但本工具定位为 reco/action 调试，统一失败。
        raise RuntimeError(f"资源加载失败（post_bundle 未成功）——pipeline 校验未过或模板/模型缺失。"
                           f"检查 MaaFw 日志或换 --resource。path={resource_path}")

    # ④ 起 Tasker
    tasker = Tasker()
    if not tasker.bind(resource, ctrl):
        raise RuntimeError("Tasker.bind 失败")
    if not tasker.inited:
        raise RuntimeError("Tasker 未就绪")

    _STATE.update(tasker=tasker, controller=ctrl, resource=resource)

    # ⑤ 开 HTTP
    host = host or HOST
    port = port or PORT
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}"
    print("=" * 60)
    print(f"<<< 就绪：{len(find_game_windows(keywords))} 个窗口可选；Tasker 已连 hwnd={target_hwnd}")
    print(f"    HTTP 服务：{url}")
    print(f"    另开终端：python maa_cli.py windows  |  crop <path>  |  reco <type> '<json>'  |  action <type> '<json>'")
    print(f"    （所有 reco/action 经此服务串行执行，单窗口无需并发）")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("=== 收到 Ctrl+C，关闭服务 ===")
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass


# ---------------- 客户端（纯 HTTP）----------------
def _client_url(args):
    base = getattr(args, "url", None) or f"http://{HOST}:{PORT}"
    return base.rstrip("/")


class ServerError(Exception):
    """server 已响应但返回非 2xx（携带 server 给的 error 文本）。区别于连不上的 URLError。"""
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg}")
        self.code = code
        self.msg = msg


def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ServerError(e.code, _extract_error(e.read().decode("utf-8", "replace"))) from None


def _http_post(url, payload, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ServerError(e.code, _extract_error(e.read().decode("utf-8", "replace"))) from None


def _extract_error(body):
    """从 server 的错误响应体里取出 ``error`` 字段（server 统一返回 {"error": "..."}）。"""
    try:
        return json.loads(body).get("error", body)
    except Exception:
        return body[:500]


def _read_param(arg):
    """PARAM 解析：None→{} ；``-``→stdin ；``@file``→读文件 ；否则当 JSON 字符串解析。"""
    if arg is None:
        return {}
    if arg == "-":
        return json.load(sys.stdin)
    if arg.startswith("@"):
        with open(arg[1:], encoding="utf-8") as f:
            return json.load(f)
    return json.loads(arg)


def cmd_windows(args):
    data = _http_get(f"{_client_url(args)}/windows", args.timeout)
    if not data:
        _orig_print("（没有匹配的游戏窗口）")
        return
    _orig_print(f"{'idx':>3}  {'hwnd':>10}  {'pid':>8}  {'rect(x,y,w,h)':>24}  title")
    for w in data:
        _orig_print(f"{w['index']:>3}  {w['hwnd']:>10}  {w['pid']:>8}  {str(w['rect']):>24}  {w['title']}")


def cmd_crop(args):
    out_path = getattr(args, "path_opt", None) or args.path   # --path 优先于位置参数
    payload = {"path": out_path} if out_path else {}
    res = _http_post(f"{_client_url(args)}/crop", payload, args.timeout)
    _orig_print(f"已保存：{res['path']}  ({res['width']}x{res['height']})")


def cmd_reco(args):
    param = _read_param(args.param)
    payload = {"type": args.type, "param": param}
    if getattr(args, "image", None):
        payload["image"] = args.image
    res = _http_post(f"{_client_url(args)}/reco", payload, args.timeout)
    reco = res.get("recognition", {})
    # 一行摘要到 stderr（不污染 stdout 的 JSON）
    box = reco.get("box")
    _orig_print(f"[reco] type={args.type} hit={reco.get('hit')} algorithm={reco.get('algorithm')} "
                f"box={box}", file=sys.stderr)
    if res.get("ignored_unknown_keys"):
        _orig_print(f"[reco] 忽略未知参数键: {res['ignored_unknown_keys']}", file=sys.stderr)
    _orig_print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_action(args):
    param = _read_param(args.param)
    payload = {"type": args.type, "param": param, "box": list(args.box)}
    if getattr(args, "reco_detail", None):
        payload["reco_detail"] = args.reco_detail
    res = _http_post(f"{_client_url(args)}/action", payload, args.timeout)
    action = res.get("action", {})
    _orig_print(f"[action] type={args.type} success={action.get('success')} box={action.get('box')}",
                file=sys.stderr)
    if res.get("ignored_unknown_keys"):
        _orig_print(f"[action] 忽略未知参数键: {res['ignored_unknown_keys']}", file=sys.stderr)
    _orig_print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_task(args):
    """客户端：POST /task，打印节点执行轨迹。"""
    res = _http_post(f"{_client_url(args)}/task",
                     {"entry": args.entry, "timeout": args.timeout}, args.timeout)
    _orig_print(f"[task] entry={args.entry} completed={res.get('completed')} "
                f"elapsed={res.get('elapsed_s','?')}s nodes={res.get('node_count',0)}",
                file=sys.stderr)
    for n in res.get("nodes", []):
        reco = n.get("recognition", {}) or {}
        act = n.get("action", {}) or {}
        _orig_print(f"  {n['name']:30s} reco.hit={reco.get('hit')} "
                    f"box={reco.get('box')} act.success={act.get('success')}",
                    file=sys.stderr)
    _orig_print(json.dumps(res, ensure_ascii=False, indent=2))
def _add_client_args(p):
    p.add_argument("--url", default=None, help=f"server 地址（默认 http://{HOST}:{PORT}）")
    p.add_argument("--timeout", type=int, default=60, help="HTTP 超时秒数（默认 60）")


def build_parser():
    ap = argparse.ArgumentParser(prog="maa_cli",
                                 description="针对单个游戏窗口的 MaaFramework 截图/识别/动作 调试 CLI（server + client）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="PARAM 是 JSON 对象：内联 / @file.json / -(stdin) / 缺省{}。"
                                        " 类型见 maa.pipeline.JRecognitionType / JActionType。")
    sub = ap.add_subparsers(dest="cmd")

    # server（也支持 --server 形式，见 main 的预处理）
    ps = sub.add_parser("server", help="启动 HTTP 服务（连一个游戏窗口）")
    ps.add_argument("--hwnd", type=lambda s: int(s, 0), default=None,
                    help="直接连指定窗口句柄（int 或 0x 十六进制）")
    ps.add_argument("--index", type=int, default=None, help="按枚举序号选（1-based）")
    ps.add_argument("--title", default=None, help="窗口标题关键字，逗号分隔（默认 梦幻西游）")
    ps.add_argument("--resource", default=None, help=f"资源目录（默认 {RESOURCE_PATH}）")
    ps.add_argument("--screencap", default=None, help=f"Win32 截图方式（默认 {WIN32_SCREENCAP}）")
    ps.add_argument("--mouse", default=None, help=f"Win32 鼠标方式（默认 {WIN32_MOUSE}）")
    ps.add_argument("--keyboard", default=None, help=f"Win32 键盘方式（默认 {WIN32_KEYBOARD}）")
    ps.add_argument("--host", default=None, help=f"监听地址（默认 {HOST}）")
    ps.add_argument("--port", type=int, default=None, help=f"监听端口（默认 {PORT}）")

    pw = sub.add_parser("windows", help="列出游戏窗口")
    _add_client_args(pw)
    pw.set_defaults(func=cmd_windows)

    pc = sub.add_parser("crop", help="截图并存盘")
    pc.add_argument("path", nargs="?", default=None, help="保存路径（缺省则存到 debug/maa_cli/）")
    pc.add_argument("--path", dest="path_opt", default=None, help="保存路径（与位置参数二选一）")
    _add_client_args(pc)
    pc.set_defaults(func=cmd_crop)

    pr = sub.add_parser("reco", help="单次识别，回显完整结果")
    pr.add_argument("type", help="识别类型（OCR/TemplateMatch/ColorMatch/DirectHit/...）")
    pr.add_argument("param", nargs="?", default=None, help="JSON 对象（内联/@file/-）")
    pr.add_argument("--image", default=None, help="对指定图片识别（缺省则实时截图）")
    _add_client_args(pr)
    pr.set_defaults(func=cmd_reco)

    pa = sub.add_parser("action", help="单次动作，回显执行结果")
    pa.add_argument("type", help="动作类型（Click/Swipe/ClickKey/InputText/...）")
    pa.add_argument("param", nargs="?", default=None, help="JSON 对象（内联/@file/-）")
    pa.add_argument("--box", nargs=4, type=int, default=[0, 0, 0, 0], metavar=("X", "Y", "W", "H"),
                    help="前序识别位置 [x,y,w,h]（默认 0 0 0 0）")
    pa.add_argument("--reco-detail", default=None, help="前序识别详情（JSON 字符串）")
    _add_client_args(pa)
    pa.set_defaults(func=cmd_action)

    pt = sub.add_parser("task", help="运行一个 pipeline 任务，回显节点执行轨迹")
    pt.add_argument("entry", help="pipeline 入口节点名（如 zhengli_baibao）")
    _add_client_args(pt)
    pt.set_defaults(func=cmd_task)
    return ap


def main(argv):
    # 兼容 ``--server`` 形式：首参是 --server 则改成 server 子命令
    if argv and argv[0] == "--server":
        argv = ["server"] + argv[1:]
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 0
    if args.cmd == "server":
        keywords = ([k.strip() for k in args.title.split(",") if k.strip()]
                    if args.title else None)
        run_server(hwnd=args.hwnd, index=args.index, keywords=keywords,
                   resource_path=args.resource, screencap=args.screencap,
                   mouse=args.mouse, keyboard=args.keyboard, host=args.host, port=args.port)
    else:
        # 客户端：ServerError（server 响应了但报错）/ URLError（连不上）/ 客户端输入错误
        # （JSON 解析失败、@file 找不到等）一律干净提示，不打 traceback；其余意外才走顶层 traceback。
        try:
            args.func(args)
        except ServerError as e:
            _orig_print(f"!! server 返回错误：{e.msg}（HTTP {e.code}）", file=sys.stderr)
            return 1
        except urllib.error.URLError as e:
            _orig_print(f"!! 连不上 server：{e.reason}\n"
                        f"  先在另一终端启动：python maa_cli.py --server", file=sys.stderr)
            return 2
        except (ValueError, OSError) as e:   # JSONDecodeError⊂ValueError，FileNotFoundError⊂OSError
            _orig_print(f"!! 输入错误：{type(e).__name__}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    _exit_code = 0
    try:
        _exit_code = main(sys.argv[1:])
    except SystemExit as e:                  # argparse --help / parser.exit 走这里，别当异常
        _exit_code = int(e.code) if e.code is not None else 0
    except KeyboardInterrupt:
        _exit_code = 130
    except Exception:
        # 普通异常才打 traceback（SystemExit/KeyboardInterrupt 是正常退出通道）
        print("=== !!! main 未捕获异常 ===")
        traceback.print_exc()
        _exit_code = 1
    finally:
        # 若输出里看不到下面这行，说明进程被 native 崩溃直接杀掉（Python except/finally 跑不到），
        # 去 stderr 找 faulthandler 的栈。os._exit：跳过线程 join/atexit，避免 MaaFw C++ 后台线程
        # （Toolkit/残留 Resource/Controller）阻塞解释器正常退出（对齐 run_5r.py / shikong.py）。
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(_exit_code)
