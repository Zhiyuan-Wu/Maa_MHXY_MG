"""五开简单编排 —— 单进程、每账号独立 Resource、全用原生 pipeline、各步墙钟超时。

# 架构（两种部署，同一份代码）
  - **本地模式（Win 单机）**：直接 ``python run_5r.py full``。``main()`` 顶部 ``_detect_backend``
    探测 mumu_server——WIN_IP 是本机网卡 → **local**（Win 永远 local，同机保护）。本机 MuMu +
    本机 adb（MuMu 自带 adb.exe），逐字节沿用旧行为。
  - **远端模式（Mac 执行 + Win 实例）**：把 Maa 执行（OCR/资源）卸到 Mac、Win 只管 MuMu 生命周期，
    缓解 Win 28.7GB 内存压力。链路：
      Win ``mumu_server``（专用 0.0.0.0:5038 adb server + HTTP API :5080，**不加载 Maa 资源**）
        ↓
      Mac ``cli_server``（HTTP API :5090；每个 /run 起独立子进程跑 main()，**Maa 资源仅在此加载**）
        ↓
      Win ``remote <mode>`` → POST Mac:5090 → Mac 探测到 remote → 经 win:5038 adb 驱动设备、
      经 win:5080 API 管生命周期（launch/shutdown/restart/ensure 实例）。
    Mac 上 WIN_IP 非本机 + mumu_server healthy → **remote**。``be_*`` 包装层在 MuMu 生命周期入口
    dispatch（remote→HTTP，local→现有函数）。关键简化：adb shell 探针经 ``ADB_SERVER_SOCKET``
    透明打到 Win 设备，**只有 MuMuManager.exe 生命周期**走 HTTP（Win 二进制）。

# 模式（``python run_5r.py <mode> [args]``）
  任务模式（local/remote 自动）：
    full      ensure 实例→连接→启动5个→组队→副本+解散→并行单人→关停全部实例
    launch    只启动+登录 5 个账号
    team      完整5人任务：启动 + 组队 + 副本+解散
    form      启动登录 + 只组队（到全员入队即停，测组队用）
    run       只执行副本+解散（前提：已组好队）
    zhuagui   队长单人无限捉鬼（关闭人员检测-不进入轮次选择）；Ctrl+C 停
    solo [任务名...] [--ids 1,3,5]   并行单人；任务名指定只跑哪些，--ids 指定账号(1-based，默认全部)
    log_analysis [YYYYMMDD|YYYY-MM-DD]   解析日志打印【耗时矩阵】+【账号信息变化矩阵】（默认今天）
              复刻自 .claude/skills/5r_log_analysis/SKILL.md；只读，不碰设备。
    help / -h / --help   看用法

  服务/客户端模式：
    mumu_server   **Win 上跑**：起专用 0.0.0.0 adb server（5038）+ MuMu 生命周期 HTTP API（5080），
                  不加载 Maa 资源。需 ≥37 的 platform-tools adb（MuMu 自带 36 不绑 0.0.0.0），
                  设 MAA_5R_DEDICATED_ADB 指向它。Ctrl+C 退出（仅 kill 5038，不碰 MuMu 5037）。
    cli_server    **Mac 上跑**：HTTP API（5090）接 CLI 指令。POST /run 起子进程跑 <mode>；
                  POST /jobs/<id>/cancel = 杀子进程 = OS 回收 MaaFw/线程/adb 连接（等价退出进程清理）。
                  GET /jobs/<id>/{status,logs} 轮询。单飞（一次一任务）。
    remote <mode> [args]   **Win→Mac 客户端**：POST Mac cli_server /run + 轮询 logs/status，
                  支持 full/launch/solo/log_analysis 等。等价本机 ``python run_5r.py <mode>``，
                  但在 Mac 上执行。

# 全局选项 ``--config <name>``（可出现在任意位置）：从 ``agent/<name>.py`` 加载配置覆盖
                  头部配置区（ROLES / FUBEN69NEW_PLAN / SOLO_ENTRIES / KEJU_AI / 超时 等）。
                  config 文件在 run_5r 的 globals 命名空间里 exec：同名变量赋值直接覆盖、
                  未列出的字段保持默认、可引用 run_5r 已定义的变量。``remote`` 会把它透传给
                  Mac 子进程。例：``python run_5r.py --config team2 full``。

# 配置区（头部）：角色/地址(ROLES)、包名、副本 entry、超时、单人任务列表、远端 IP/端口。
  Win/Mac 同一份代码：REPO_DIR 由 ``__file__`` 推导（``MAA_5R_REPO`` 可覆盖）。
"""
import builtins
import faulthandler
import gc
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

from maa.toolkit import Toolkit
from maa.resource import Resource
from maa.controller import AdbController
from maa.tasker import Tasker, TaskerEventSink
from maa.pipeline import JRecognitionType, JTemplateMatch

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    # 全局禁止子进程新建控制台窗口：本脚本可能以**无控制台**方式跑（mumu_server 被 detached/
    # 后台拉起），此时 subprocess 跑 adb.exe / MuMuManager.exe / netstat 等控制台程序会各自
    # 弹出一个秒开秒关的控制台窗口并抢占前台。CREATE_NO_WINDOW 让所有子进程不创建新窗口
    # （也覆盖 MaaFw 内部 spawn 的 adb）。本地终端跑不受影响（父进程有控制台、子进程继承）。
    _sub_run, _sub_popen = subprocess.run, subprocess.Popen
    _sub_popen_init = _sub_popen.__init__
    _CREATE_NO_WINDOW = 0x08000000

    def _sub_run_nw(args, *a, **kw):
        kw.setdefault("creationflags", _CREATE_NO_WINDOW)
        return _sub_run(args, *a, **kw)

    # 只原地包 Popen.__init__，**不要**把 subprocess.Popen 换成函数：asyncio/windows_utils.py
    # 有 ``class Popen(subprocess.Popen)``，基类不是 type 会让 __build_class__ 回退用 function 当
    # 元类 → "function() argument 'code' must be code, not str"（import custom→loguru→首次 import
    # asyncio 时触发）。保持 Popen 原对象、只改 __init__ 默认值，子类化与 isinstance 全无感。
    def _sub_popen_init_nw(self, args, *a, **kw):
        kw.setdefault("creationflags", _CREATE_NO_WINDOW)
        return _sub_popen_init(self, args, *a, **kw)
    _sub_popen.__init__ = _sub_popen_init_nw

    subprocess.run = _sub_run_nw

# native 崩溃（段错误 / abort / 访问违例）时，把出错线程的 Python 栈打到 stderr。
# 用于定位"进程静默退出、无任何 traceback"的情况——多半是 C++ 侧（MaaFw/OCR 模型）
# 崩了，Python 的 except/finally 根本跑不到。不加这个，这种死法完全无线索。
faulthandler.enable(all_threads=True)


# ---- --config <name>：从另一份 python 配置文件覆盖头部配置（队伍/任务列表等）----
# config 文件（如 agent/team2.py）在本模块 globals() 命名空间里 exec，其内同名变量
# 赋值直接覆盖 run_5r 全局；没列出的字段保持默认。详见下方 _apply_config 与文件 docstring。
def _split_config(argv):
    """从 argv 抽出全局选项 ``--config <name>``（可出现在任意位置）。

    返回 ``(config_name, rest_argv)``：未指定时 config_name=''；
    rest_argv 为去掉 ``--config`` 对之后的剩余参数（mode + 其余 args）。
    """
    name, rest = "", []
    i = 0
    while i < len(argv):
        if argv[i] == "--config" and i + 1 < len(argv):
            name = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return name, rest


def _apply_config(name):
    """加载 ``--config <name>`` 指定的配置文件，把其中的变量赋值覆盖到本模块全局。

    搜索顺序：``agent/<name>.py`` → ``<repo>/<name>.py`` → 当作路径（相对 cwd/绝对，
    含路径分隔符或 ``.py`` 后缀时）。文件在本模块 ``globals()`` 命名空间里 exec，故：

    * 同名变量赋值**直接覆盖** run_5r 全局（ROLES / FUBEN69NEW_PLAN / SOLO_ENTRIES / …）；
    * **未列出的字段保持 run_5r 默认**；
    * 可引用 run_5r 已定义的变量（如 ``os`` / ``datetime`` / ``KEJU_AI``）做条件或派生。

    在配置区基础变量之后、派生（DEFAULT_OVERRIDES）之前调用，使 KEJU_AI 等覆盖能让
    DEFAULT_OVERRIDES 的 kejuxiangshi 分支正确重算。
    """
    if not name:
        return
    candidates = [
        os.path.join(REPO_DIR, "agent", f"{name}.py"),
        os.path.join(REPO_DIR, f"{name}.py"),
    ]
    if os.sep in name or "/" in name or name.endswith(".py"):
        candidates.insert(0, name)
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path is None:
        raise RuntimeError(
            f"--config {name!r} 找不到配置文件（搜索过：{candidates}）")
    print(f"=== 加载配置覆盖：{path} ===")
    with open(path, encoding="utf-8") as f:
        code = f.read()
    exec(compile(code, path, "exec"), globals())


# 顶部预解析 --config：本进程 apply 用 _CONFIG_NAME（下方配置区调用 _apply_config）；
# __main__ / _remote_client 用 _ARGV_REST（已去掉 --config 对）。
_CONFIG_NAME, _ARGV_REST = _split_config(sys.argv[1:])

# ==================== 配置区（移动脚本 / 换机器时改这里）====================
# 仓库根目录（须含 assets/ agent/）。默认按 __file__ 推导（agent/run_5r.py 的上上级 = 仓库根），
# 这样 Win/Mac 同一份代码都能跑；可用环境变量 MAA_5R_REPO 覆盖。
REPO_DIR = os.environ.get("MAA_5R_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """从 REPO_DIR/.env 读 KEY=VALUE 写入 os.environ（setdefault 不覆盖已有）。免装 python-dotenv。"""
    _p = os.path.join(REPO_DIR, ".env")
    if not os.path.isfile(_p):
        return
    for _line in open(_p, encoding="utf-8"):
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


_load_dotenv()   # 读 .env（如 OPENAI_KEY）→ os.environ，供 KEJU_AI 等配置引用

# 五开角色 → MuMu 实例 ADB 地址。MuMu12 约定 adb_port = 16384 + 32*index，
# 故 index = (port - 16384) // 32；ROLES 的顺序即账号编号顺序（solo --ids 用，从 1 起）。
ROLES = {
    "队长": "127.0.0.1:16512", #id=4 title=3
    "渣中": "127.0.0.1:16576", #id=6 title=5
    "6130": "127.0.0.1:16544", #id=5 title=4
    "欧阳": "127.0.0.1:16448", #id=2 title=1
    "晚风": "127.0.0.1:16480", #id=3 title=2
}
PACKAGE        = "com.netease.my"      # 游戏包名（仓库 start.json 里的 myq 是错的，用这个）
# 组队副本 = 5 本 fuben69new 串联（每本一个 entry，override 选目标）+ ZHUOGUI_ROUNDS 轮捉鬼。
# (是否侠士, 第几个)：侠士 idx∈{1,2} = 50侠士/70侠士；普通 idx∈{1,2,3} = 50普通-1/2、70普通。
# fuben69new 是"打一个副本打通关"的自包含链路，重复调用实现多本（取代旧 fuben115 一次 entry 串联）。
# 覆盖 fuben69/fuben115 全部副本选择，详见 assets/resource/base/pipeline/fuben69new.json。
FUBEN69NEW_PLAN = [
    (True, 1),   # 50侠士
    (True, 2),   # 70侠士
    (False, 1),  # 50普通-1
    (False, 2),  # 50普通-2
    (False, 3),  # 70普通
]
# 副本完成、桥接捉鬼后跑几轮鬼。注意 **实际轮数 = max_hit + 1**：首轮由入口 钟馗-捉鬼任务-循环
# 启动（不耗计数器），抓鬼轮次计算-max 在每轮末尾命中一次再启下一轮——故 4 轮对应 max_hit=3。
# standalone 的 post_task 不读 interface.json option，base 默认不挂限轮器（抓鬼一轮完成→队伍满员判断
# 会无限循环），故 team_run 必须显式把 抓鬼一轮完成.next 改写到 抓鬼轮次计算-max。
ZHUOGUI_ROUNDS = 4

# 队长单人无限捉鬼模式的 override（``python run_5r.py zhuagui``）—— 对应 interface.json option
# 「（开启/关闭）人员检测-（是否）进入轮次选择」→「关闭人员检测-不进入轮次选择」。
# 把 抓鬼一轮完成.next 从裸默认（[[JumpBack]抓鬼一轮完成-再次点击确定, 捉鬼-队伍人员满员判断]）
# 改成 [钟馗-捉鬼任务-循环, [JumpBack]抓鬼一轮完成-再次点击确定]：跳过队伍满员判断（=关闭人员检测）、
# 不接限轮器（=不进入轮次选择），每轮捉完直接回 钟馗 续接 → 无限循环。
# 与上方 ZHUOGUI_ROUNDS 无关：那是**组队副本桥接捉鬼**的轮数（team_run 用，接限轮器 max-fuben）；
# 这里是**队长单人无限捉鬼**（zhuagui 模式用，不接限轮器）。standalone 不读 interface.json option，
# 故必须显式 override，否则走裸默认会进 捉鬼-队伍人员满员判断（人员检测分支）。
ZHUAGUI_OVERRIDE = {
    "抓鬼一轮完成": {"next": [
        "钟馗-捉鬼任务-循环",
        "[JumpBack]抓鬼一轮完成-再次点击确定",
    ]},
}
# MUMU_MANAGER   = r"C:\Program Files\Netease\MuMu Player 12\nx_main\MuMuManager.exe"
MUMU_MANAGER   = r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"
LAUNCH_STAGGER = 20                    # 启动错峰间隔秒数：ensure_instances 逐台拉 MuMu 实例、
                                       # launch_parallel 逐账号跑 start 登录，相邻两次都至少隔这么久（0=齐发）
TIMEOUTS = {                           # 各步墙钟超时（秒）
    "start": 600, "chuangjianduiwu": 180, "duizhang": 600,
    "fuben_per": 1800,     # 单本 fuben69new（回长安+进本+打怪+结算）墙钟超时
    "zhuogui": 1800,       # ZHUOGUI_ROUNDS 每轮捉鬼墙钟超时
    "duizhang_TR": 300, "duiyuan": 14400,   # 队员覆盖整段 5本+捉鬼（≤4h）
    "solo": 2400,          # 单个 solo 任务的墙钟超时
    "solo_overall": 7200,  # 整轮 solo（全部账号×全部任务）的墙钟总超时；到点未完则收口退出
    "bangpai_renwu": 3600,
    "zhuagui": 14400,      # 队长无限捉鬼的墙钟安全帽（4h）；实际靠 Ctrl+C 停，到点 post_stop 收口
}
# 单人任务列表（任务间自动插 barrier：solo_all 每个entry前 sleep5 + 打开大地图重置位置，
# 故这里不再手动插"打开大地图_69副本"）。
SOLO_ENTRIES = ["5R_duiyuan_tuichuduiwu", "shuangbei", "huoli", "fuli_qiandao", "shimen_renwu_new", "yunbiao_renwu2", "baotu_renwu", "wabaotu_qingli",
                "mijing_renwu", "sanjieqiyuan", "zhengli_baibao", "jiayuan_zhengli", "huoli", "zhanghao_xinxi", "jialan", "baitanchushou"]

if datetime.now().weekday() == 2:
    SOLO_ENTRIES.insert(1, "bangpai_renwu")
if datetime.now().weekday() < 5:
    # SOLO_ENTRIES.insert(0, "kejuxiangshi")
    SOLO_ENTRIES.append("kejuxiangshi")

# 科举乡试 AI 答题凭证（对应 interface.json「是否使用Ai进行答题=Yes」）。
# 用 deepseek（openai 兼容端点）。apikey 从 .env 的 OPENAI_KEY 读（_load_dotenv 已加载进 os.environ）。
# 关思考固定 thinking:{type:disabled}（AIAnswer._ask_with_openai 内，deepseek 实测 ~1s/题、1 token 直出）。
# 不再支持 ollama——其关思考写法（reasoning_effort:none）与 deepseek（thinking:disabled）不兼容，
# 要同时支持须探测，违背极简；故放弃 ollama、统一用 deepseek。详见 memory ollama-qwen3-thinking-disable。
KEJU_AI = {
    "apikey": os.environ.get("OPENAI_KEY", ""),
    "url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
}

# ---- 应用 --config <name> 覆盖：基础配置之后、派生（DEFAULT_OVERRIDES）之前 ----
# config 对 ROLES/FUBEN69NEW_PLAN/SOLO_ENTRIES/KEJU_AI 等的覆盖在此生效；放此处是为了
# 让下方 DEFAULT_OVERRIDES 的 kejuxiangshi 分支基于覆盖后的 KEJU_AI 重新判定。
_apply_config(_CONFIG_NAME)
# =========================================================================
# 以下为派生路径与内部常量，一般无需修改
RESOURCE_PATH = os.path.join(REPO_DIR, "assets", "resource", "base")
AGENT_DIR     = os.path.join(REPO_DIR, "agent")   # import custom（自定义识别/动作）
DEBUG_DIR     = os.path.join(REPO_DIR, "debug")   # MaaFramework 工作目录 + 日志

# ==================== 远端/服务模式配置（Mac 经 Tailscale 远控 Win MuMu）====================
# 链路：Win 跑 mumu_server（专用 adb server + MuMu 生命周期 HTTP API，不加载 Maa 资源）；
#       Mac 跑 cli_server（接收 CLI 指令、子进程跑 main()、Maa 资源仅在此加载）；
#       Win 发 `remote <mode>` → Mac cli_server → 探测到远端 → 经 Win:5038 adb 驱动设备、
#       经 Win:5080 API 管生命周期。详见 plan sleepy-inventing-pearl.md。
WIN_IP   = "192.168.5.5"     # Win（MuMu 主机）局域网 IP —— 跑 mumu_server
MAC_IP   = "192.168.5.148"   # Mac（资源/执行） 局域网 IP —— 跑 cli_server
                               # （Tailscale 不可用时的替代链路；同机保护 _ip_is_local 与 IP 值无关）
ADB_SERVER_PORT = 5038       # mumu_server 拉起的专用 0.0.0.0 adb server（绝不碰 MuMu 自管的 5037）
MUMU_API_PORT   = 5080       # mumu_server 的 HTTP API（Win）
CLI_API_PORT    = 5090       # cli_server  的 HTTP API（Mac）
REMOTE_PROBE_TIMEOUT = 2.5   # main() 探测 mumu_server /health 的单次超时（秒）
SHARED_TOKEN = os.environ.get("MAA_5R_TOKEN", "")   # 可选 bearer；空 = 不鉴权（Tailscale 即安全边界）
# =========================================================================

# 各 solo 任务的建议 pipeline_override（对照 interface.json 的 option 默认值 + 各 pipeline 裸默认）。
# 仅在 run_task 未传同名 override 时补充（不覆盖调用方显式传的）。逐项依据：
#   yunbiao_renwu2   「是否检测活力到50=No」：跳过活跃度检测（pipeline 裸默认是 Yes 路径）。
#   mijing_renwu     「海底秘境 + 第25关」：pipeline 裸默认 海底秘境-指定关卡结束任务.expected=[] 有歧义
#                    （OCR 空 expected 可能"命中任意文本"→秘境刚进就退出），显式锁 第25关；选择模式一项
#                    与 pipeline 默认重复，防御性写死成 海底秘境。
#   zhengli_baibao  「使用及清理列表=全部6项」：pipeline 默认 6 节点全 enabled:false，不 override 则
#                    只整理排序、什么都不消耗；这里开全部 6 项（匹配 GUI 默认）。
#   jiayuan_zhengli 跳过召唤灵室：点击管家寻路.next 去掉两支召唤灵室节点，直奔 卧室-点击打理
#                    （参照 pipeline 自带「召唤灵室-未开启→卧室-点击打理」，保留大扫除+卧室回活力）。
#   kejuxiangshi    「是否使用Ai进行答题=Yes」：仅当上方 KEJU_AI 填了 key 才启用，否则走 pipeline
#                    默认的普通答题（见 KEJU_AI 注释）。
# 若 ``--config`` 已整体覆盖 DEFAULT_OVERRIDES（在上方 _apply_config 里赋值），尊重 config 的值；
# 否则用下列 run_5r 默认。KEJU_AI 联动：即使 config 覆盖了 DEFAULT_OVERRIDES，只要它没显式写
# kejuxiangshi 且 KEJU_AI 有 apikey，仍自动补（让 config 改 KEJU_AI 即可启用 AI 答题）。
DEFAULT_OVERRIDES = globals().get("DEFAULT_OVERRIDES") or {
    "yunbiao_renwu2": {"活动-运镖": {"next": ["活动-运镖-点击日常活动"]}},
    "mijing_renwu": {
        "秘境降妖-选择模式": {"next": ["秘境降妖-选择海底秘境"]},
        "海底秘境-指定关卡结束任务": {"expected": ["第25关", "第26关", "第27关", "第28关", "第29关", "第30关"]},
        "秘境-点击进入战斗前重新确认关卡数": {"expected": ["第25关", "第26关", "第27关", "第28关", "第29关", "第30关"]},
    },
    "zhengli_baibao": {
        "使用红罗羹": {"enabled": True}, "使用绿芦羹": {"enabled": True},
        "使用心魔宝珠": {"enabled": True}, "合成阵法": {"enabled": True},
        "使用秘境材料": {"enabled": True}, "使用过期物品": {"enabled": True},
        "出售百炼精铁": {"enabled": True}, "出售制造书": {"enabled": True},
    },
    # "jiayuan_zhengli": {
    #     "点击管家寻路": {"next": ["卧室-点击打理", "[JumpBack]panduan_zhujiemian"]},
    # },
}
if KEJU_AI.get("apikey") and "kejuxiangshi" not in DEFAULT_OVERRIDES:
    # AIAnswer 从节点 attach 读 apikey/url/model，三项缺一不可；url 要带 /v1/chat/completions。
    DEFAULT_OVERRIDES["kejuxiangshi"] = {
        "活动-科举乡试-进入答题界面": {"next": [
            "活动-科举乡试-结束-图片",
            "活动-科举乡试-结束ocr",
            "[JumpBack]活动-科举乡试-开始答题API",
        ]},
        "活动-科举乡试-开始答题API": {"attach": {
            "apikey": KEJU_AI["apikey"], "url": KEJU_AI["url"], "model": KEJU_AI["model"],
        }},
    }

# 常用单人任务名（``python run_5r.py help`` 打印；也是 ``solo`` 可指定的值）
SOLO_TASKS_HELP = {
    "yunbiao_renwu2": "运镖（普通）",
    "baotu_renwu": "宝图任务",
    "wabaotu_qingli": "挖宝图",
    "fuli_qiandao": "每日签到/福利",
    "huoyue_lingqu": "活跃领取",
    "sanjieqiyuan": "三界奇缘（答题）",
    "shimen_renwu": "师门任务",
    "mijing_renwu": "秘境降妖",
    "chushoushanghui": "出售商会",
    "jiayuan_zhengli": "家园整理",
    "chongwuqifu": "宠物祈福",
}


# ---------------- 日志：print 自动加时间戳 + tee 到文件 ----------------
# 模块内所有 print(...) 自动走 _log_print：加 [YYYY-MM-DD HH:MM:SS] 前缀，并 tee 到
# 一份日志文件。实现是在模块顶层把 ``print`` 同名 shadow 成 _log_print——仅本模块可见，
# 不影响 maa.* / custom 等其它模块（它们各自的全局表里没有 print，仍走 builtins.print）。
_orig_print = builtins.print   # 原 builtin，_log_print 内部回退用
_LOG_FILE = None               # open_log() 打开的句柄；None=只写控制台
_LOGGING_ON = False            # open_log() 翻 True 后才加时间戳/写文件（help 模式不翻）


def _log_print(*args, **kwargs):
    """带时间戳、同时写文件的 print（吞掉 flush=，透传 file=/sep=/end=）。"""
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    file = kwargs.get("file")
    msg = sep.join(str(a) for a in args)
    if _LOGGING_ON:
        msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    _orig_print(msg, end=end, file=file, flush=True)
    if _LOGGING_ON and _LOG_FILE is not None and file is None:
        _LOG_FILE.write(msg + end)


def open_log(path=None):
    """开启带时间戳 + 写文件的日志。path 默认 ``<DEBUG_DIR>/run_5r/run_5r_<时间戳>.log``。

    在 ``main()`` 首行调用，确保后续所有 print（ensure_instances / connect_all /
    launch / …）都进文件。幂等：重复调用先关旧文件再开新的。
    """
    global _LOG_FILE, _LOGGING_ON
    if _LOG_FILE is not None:
        _LOG_FILE.close()
    if path is None:
        d = os.path.join(DEBUG_DIR, "run_5r")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("run_5r_%Y%m%d_%H%M%S.log"))
    _LOG_FILE = open(path, "a", encoding="utf-8", buffering=1)  # 行缓冲
    _LOGGING_ON = True
    print(f"=== 日志写入 {path} ===")


print = _log_print  # 模块内 shadow builtins.print


# ---------------- 核心工具 ----------------

def _assert_repo():
    """校验 REPO_DIR 指向有效仓库（找得到 assets/resource/base）——移动脚本后配错时快速失败。"""
    if not os.path.isdir(RESOURCE_PATH):
        raise RuntimeError(
            f"REPO_DIR 指向的仓库找不到 pipeline 资源：{RESOURCE_PATH}\n"
            f"请改脚本头部 REPO_DIR（当前 = {REPO_DIR!r}）为正确的 Maa_MHXY_MG 仓库根目录。")


def _register_customs(resource):
    """把 ``agent/custom/*`` 的自定义识别/动作注册到共享 Resource（standalone / Model B）。

    背景：本项目 custom 类用的是 ``@AgentServer.custom_*`` 装饰器（为 GUI 的**独立 agent
    进程**设计）——装饰器把实例存进 ``AgentServer._custom_recognition_holder``，并注册到 C++
    **MaaAgentServer** 那张回调表（standalone 下没 ``start_up``，是死的）。而 standalone 要让
    pipeline 命中 custom 时能回调到 Python，必须注册到 **Resource 的 C++ 回调表**
    （``MaaResourceRegisterCustomRecognition``，另一个入口）。

    桥梁就是 ``_custom_recognition_holder`` / ``_custom_action_holder`` 这两个普通 Python dict：
    装饰器往里存了实例，这里读出来逐个 ``resource.register_custom_*`` 搬到 Resource。

    ``import custom`` 会触发各模块的 ``from maa.agent...`` → ``maa/agent/__init__.py`` 调
    ``Library.open(agent_server=True)``，把全局 ``Library._is_agent_server`` 翻成 True。
    **翻转源是 ``maa/agent/__init__.py``，不是装饰器本身**——装饰器只是标志为 True 时才调得通
    ``Library.agent_server()``（否则抛 ``ValueError``）。``import`` 之后必须手动翻回 False，
    否则后续 ``Resource``/``Tasker`` API 会改去调 ``MaaAgentServer.dll``（"MaaAgentServer Not implement"）。

    不这么做，用 Custom 的 pipeline（运镖 OCRNum、宝图 count、三界 tiku…）会报 ``recognition is null``。
    完整机制分析见 ``docs/description/context_run_task_standalone_分析.md`` 第 2 章。
    """
    if AGENT_DIR not in sys.path:
        sys.path.insert(0, AGENT_DIR)
    from maa.library import Library
    import custom  # noqa: E402  触发 @AgentServer.custom_* 装饰器（会把 Library 翻成 AgentServer 模式）
    Library._is_agent_server = False  # 翻回 standalone/framework 模式
    from maa.agent.agent_server import AgentServer
    recs = list(AgentServer._custom_recognition_holder.items())
    acts = list(AgentServer._custom_action_holder.items())
    for name, inst in recs:
        resource.register_custom_recognition(name, inst)
    for name, inst in acts:
        resource.register_custom_action(name, inst)
    # 每账号各注册一份（独立 Resource 各自需要）；但汇总行只打一次，避免 5 行重复噪声。
    if not getattr(_register_customs, "_summary_printed", False):
        print(f"已注册自定义识别 {len(recs)} 个、自定义动作 {len(acts)} 个"
              f"（每个 Resource 各注册一份）")
        _register_customs._summary_printed = True


# ---------------- MuMu 实例生命周期（MuMuManager CLI）----------------
# MuMu12 自带 MuMuManager.exe，可按实例索引（index）启动/关闭/查询模拟器，
# ``control launch -pkg`` 还能一并自动启动游戏。文档：
# https://mumu.163.com/help/20240807/40912_1170006.html


def _mumu_path():
    if not os.path.isfile(MUMU_MANAGER):
        raise RuntimeError(
            f"找不到 MuMuManager.exe：{MUMU_MANAGER}\n"
            f"请改脚本头部 MUMU_MANAGER 指向它。")
    return MUMU_MANAGER


def _mumu(args, timeout=60):
    """跑一条 MuMuManager.exe 命令，返回 stdout（``info`` 的 stdout 是 JSON）。

    显式按 UTF-8 解码：MuMuManager 输出 UTF-8，但 Windows 默认按 GBK 解会崩。
    """
    exe = _mumu_path()
    cp = subprocess.run([exe, *args], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError(f"MuMuManager {args} 失败 rc={cp.returncode}: {cp.stderr.strip()}")
    return cp.stdout


def mumu_info(indices="all"):
    """``info -v <indices>``（indices 形如 ``"all"`` / ``"0,2,4"``），返回 ``{index_str: {...}}``。

    注意 MuMuManager 的返回不一致：单索引返回裸对象 ``{"index":"0",...}``，
    多索引 / all 返回 ``{"0":{...},...}``。这里统一成后者。
    """
    data = json.loads(_mumu(["info", "-v", str(indices)]))
    if isinstance(data, dict) and "index" in data:
        return {str(data["index"]): data}
    return data


def _launch_one(idx, package, retries=2):
    """逐台启动 MuMu 实例 ``idx``，容错 MuMuManager 偶发崩溃。

    背景：MuMuManager 一次 ``launch`` 多实例（带 ``-pkg``）时偶发 ``0xC0000005``（access
    violation）段错误，且常"崩了却把实例拉起来了"。逐台 launch 大幅降低崩溃面；单台 launch
    返回非 0 时，复查 ``is_process_started``——已起则当成功（覆盖"崩了却起来了"），没起则重试，
    重试耗尽仍没起才抛。
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            _mumu(["control", "-v", str(idx), "launch", "-pkg", package], timeout=120)
            return
        except RuntimeError as e:
            last_err = e
            time.sleep(1.5)  # 给 is_process_started 一点翻转时间再复查
            started = mumu_info(str(idx)).get(str(idx), {}).get("is_process_started")
            if started:
                print(f"    实例 {idx}：launch 报错（{e}）但进程已起，视为成功")
                return
            if attempt < retries:
                print(f"    实例 {idx}：launch 失败，重试（{attempt + 1}/{retries}）")
                time.sleep(2)
    raise RuntimeError(f"MuMu 实例 {idx} launch 失败（重试 {retries} 次仍未起）：{last_err}")


def ensure_instances(indices, package=PACKAGE, retries=2, cool_down=10, boot_come_up=180, cancel=None):
    """确保每个 MuMu 实例的模拟器已启动、adb 可用（**交付界面：adb ready**）。

    两阶段：
      A. 逐台 ``control launch -pkg``（错峰 ``LAUNCH_STAGGER``）——``-pkg`` 幂等、作保险；已跑的跳过。
      B. 每台双重检查 adb 可用（``_adb_ready`` = connect + ``boot_completed=1``）：
         检查①轮询到就绪（≤``boot_come_up``）→ 冷却 ``cool_down`` → 检查②。
         任一未过即重拉该实例（``_launch_one``），``retries`` 轮仍不过则 raise。

    崩溃 / 冷启动闪退**隐含**在 double-check 失败里——adb 不可用即重拉，无需状态跟踪。
    幂等。返回 ``{index: addr}``。

    ``cancel``（threading.Event / callable / None）：mumu_server HTTP handler 在客户端断连时传入，
    本函数在每个错峰/冷却/轮询点探测；命中即打印并 **提前 return（不 raise）**——已 launch 的实例
    保留（不回滚），只是不再继续替一个已经走掉的请求方 launch/retry 剩余实例。
    """
    indices = sorted(set(int(i) for i in indices))
    adb = _adb_path()
    _adb_reset(adb, [_idx_to_addr(i) for i in indices])

    # A. 逐台 launch（已跑的跳过）
    info = mumu_info(",".join(map(str, indices)))
    to_launch = [i for i in indices if not info.get(str(i), {}).get("is_process_started")]
    for k, i in enumerate(to_launch):
        if _cancelled(cancel):
            print(">>> ensure_instances：HTTP 客户端已断连，中止（已 launch 的实例保留）")
            return {}
        if k > 0 and _sleep_cancel(LAUNCH_STAGGER, cancel):  # 相邻实例启动间隔 ≥LAUNCH_STAGGER 秒，避免 5 个模拟器同时拉起压满宿主
            print(">>> ensure_instances：错峰等待期间客户端断连，中止")
            return {}
        print(f">>> 启动 MuMu 实例 {i}（control launch -pkg {package}）")
        _launch_one(i, package)

    # B. 逐台 double-check adb 可用；不过则重拉该实例
    print(f">>> 逐台双重检查 adb 可用（cool_down={cool_down}s, boot_come_up={boot_come_up}s）")
    failed = []
    for i in indices:
        if _cancelled(cancel):
            print(">>> ensure_instances：HTTP 客户端已断连，中止")
            return {}
        addr = _idx_to_addr(i)
        t0 = time.time()
        for attempt in range(retries + 1):
            if _double_check(lambda a=addr: _adb_ready(adb, a), cool_down, boot_come_up, cancel=cancel):
                print(f"    实例 {i}({addr}) adb 就绪（双重检查通过，用时 {int(time.time() - t0)}s）")
                break
            if attempt < retries:
                if _cancelled(cancel):
                    return {}
                print(f"    实例 {i}({addr}) adb 双重检查未过，重拉模拟器（{attempt + 1}/{retries}）")
                _launch_one(i, package)
        else:
            failed.append(i)
    if failed:
        raise RuntimeError(f"MuMu 实例 {failed} 的 adb 在重试 {retries} 轮后仍未就绪")
    print(f"<<< MuMu 实例就绪（adb 可用）：{[_idx_to_addr(i) for i in indices]}")
    return {i: _idx_to_addr(i) for i in indices}


def _shutdown_one(idx, retries=2):
    """逐台关闭 MuMu 实例 ``idx``，容错 MuMuManager 偶发崩溃（``_launch_one`` 的 shutdown 版）。

    批量 ``shutdown`` 和批量 ``launch`` 一样会触发 MuMuManager 的 ``0xC0000005``；逐台 + 容错 +
    重试。shutdown 返回非 0 时复查 ``is_process_started``——已停则当成功（覆盖"崩了却关掉了"），
    没停则重试，重试耗尽仍没停才抛。
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            _mumu(["control", "-v", str(idx), "shutdown"], timeout=120)
            return
        except RuntimeError as e:
            last_err = e
            time.sleep(1.5)
            started = mumu_info(str(idx)).get(str(idx), {}).get("is_process_started")
            if not started:
                print(f"    实例 {idx}：shutdown 报错（{e}）但进程已停，视为成功")
                return
            if attempt < retries:
                print(f"    实例 {idx}：shutdown 失败，重试（{attempt + 1}/{retries}）")
                time.sleep(2)
    raise RuntimeError(f"MuMu 实例 {idx} shutdown 失败（重试 {retries} 次仍未停）：{last_err}")


def shutdown_instances(indices):
    """逐台 ``control -v <i> shutdown`` 关闭给定实例，释放内存（批量 shutdown 会触发
    MuMuManager 偶发 ``0xC0000005`` 崩溃，见 ``_shutdown_one``）。"""
    indices = sorted(set(int(i) for i in indices))
    if not indices:
        return
    print(f">>> 关闭 MuMu 实例 {indices}（逐台 control shutdown）")
    for i in indices:
        _shutdown_one(i)
    print(f"<<< 已关闭 MuMu 实例 {indices}")


def _role_indices(roles):
    """``ROLES``（role→``ip:port``）→ ``{role: muMu_index}``。

    MuMu 约定 ``adb_port = 16384 + 32*index``，据此由端口反推 index；再用 ``info`` 校验
    实例已创建、且（若在跑）端口一致。这样未启动的实例（info 里无 adb_port）也能定位。
    """
    info = mumu_info("all")
    by_index = {int(d.get("index", i)): d for i, d in info.items()}
    out = {}
    for role, addr in roles.items():
        port = int(addr.rsplit(":", 1)[1])
        index = (port - 16384) // 32
        if index not in by_index:
            raise RuntimeError(f"{role}（端口 {port}）推得 MuMu 索引 {index}，但 info 里无此实例（未创建？）")
        d = by_index[index]
        if d.get("adb_port") and int(d["adb_port"]) != port:
            raise RuntimeError(f"{role} 端口 {port} 与 MuMu 实例 {index} 的 adb_port {d['adb_port']} 不一致")
        out[role] = index
    return out


# ---------------- adb 直连：游戏进程探测/拉起（ensure_process 用）----------------
# 这一组 helper 绕开 MaaFw，直接用 MuMu 自带 adb 与设备 shell 交互。用于在交给 Maa 的
# start 之前，由 Python 侧显式确认游戏进程存活（ensure_process）。adb.exe 与 MuMuManager
# 同目录（connect_all 实测路径：.../nx_main/adb.exe）。


def _adb_path():
    """MuMu 自带 adb.exe 路径（与 ``MUMU_MANAGER`` 同目录）。找不到则快速失败。"""
    p = os.path.join(os.path.dirname(MUMU_MANAGER), "adb.exe")
    if not os.path.isfile(p):
        raise RuntimeError(
            f"找不到 adb.exe：{p}\n"
            f"请确认 MUMU_MANAGER（{MUMU_MANAGER!r}）所在目录含 adb.exe。")
    return p


def _adb_connect(adb, address, timeout=10):
    """``adb connect <address>``，幂等。MuMu 是网络设备（127.0.0.1:<port>），``-s`` 操作前需
    先 connect 注册到 adb server。已连接时 adb 输出 "already connected"，无副作用。"""
    try:
        subprocess.run([adb, "connect", address], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:
        print(f"    adb connect {address} 异常（忽略继续）：{e}")


def _game_process_alive(adb, address, package=PACKAGE, timeout=8):
    """游戏进程是否在 ``address`` 设备上存活。

    ``adb -s <addr> shell pidof <package>``：返回 0 且 stdout 非空（有 PID）即存在；
    pidof 无匹配返回 1、adb 通信失败也非 0——一律按"不存在"处理（交给上层重试）。
    """
    try:
        cp = subprocess.run([adb, "-s", address, "shell", "pidof", package],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    except Exception:
        return False
    return cp.returncode == 0 and bool(cp.stdout.strip())


def _start_game_process(adb, address, package=PACKAGE, timeout=15):
    """``adb monkey`` 拉起游戏进程（启动 launcher activity，无需 activity 名）。

    只起游戏、不动模拟器实例；已运行时把游戏带到前台，安全。失败不抛——ensure_process 的
    double-check 会再判，必要时进重试。
    """
    try:
        subprocess.run([adb, "-s", address, "shell", "monkey", "-p", package,
                        "-c", "android.intent.category.LAUNCHER", "1"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    except Exception as e:
        print(f"    [{address}] monkey 拉起游戏异常（忽略，后续检查兜底）：{e}")


def _adb_ready(adb, address, timeout=8):
    """adb 是否可用（设备已连 + Android 开机完成）—— ensure_instances 的就绪判据。

    MuMu 是网络设备，连接会随 adb server 状态变化/掉线，故每次先 ``adb connect``（幂等，保证
    连接新鲜），再 ``getprop sys.boot_completed``。返回 ``"1"`` 即设备已连且开机完成（monkey 起
    游戏需要这个程度）。设备未起 / 未开机 / 连不上都返回 False。
    """
    try:
        subprocess.run([adb, "connect", address], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
        cp = subprocess.run([adb, "-s", address, "shell", "getprop", "sys.boot_completed"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    except Exception:
        return False
    return cp.returncode == 0 and cp.stdout.strip() == "1"


def _cancelled(cancel):
    """``cancel`` 为 None / threading.Event / callable → 是否已请求取消。

    供 ensure_instances / ensure_process 等长流程在轮询、错峰、冷却点探测；mumu_server 的 HTTP
    handler 在客户端断连时 set 一个 Event 传入，使正在替远端执行的 ensure 能尽快中止，避免
    "客户端已 cancel、mumu_server 仍继续 launch/retry"的鬼魂 ensure。"""
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return cancel.is_set()
    if callable(cancel):
        return bool(cancel())
    return False


def _sleep_cancel(seconds, cancel, step=1.0):
    """可被 ``cancel`` 中断的 sleep。返回 True=中途被取消、False=睡满。step 控制取消响应粒度。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _cancelled(cancel):
            return True
        time.sleep(min(step, max(0.0, deadline - time.time())))
    return _cancelled(cancel)


def _wait_for(check, timeout, interval=2, cancel=None):
    """轮询 ``check()`` 直到返回真或 ``timeout`` 到期。命中返回 True，超时/取消返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cancelled(cancel):
            return False
        if check():
            return True
        if _sleep_cancel(interval, cancel):
            return False
    return False


def _double_check(check, cool_down=10, come_up=60, cancel=None):
    """双重检查（ensure_instances / ensure_process 共用）。

    检查①：``_wait_for(check, come_up)``——轮询直到命中（容忍模拟器/游戏慢启动）；
    冷却 ``cool_down`` 秒；检查②：再 ``check()`` 一次——抓"起来又崩 / 闪退"。
    两次都过才返回 True。崩 / 闪退 / 起不来都隐含在此：检查不过 → 上层重拉。
    ``cancel`` 透传给 _wait_for / 冷却 sleep，客户端断连即提前返回 False。
    """
    if not _wait_for(check, come_up, cancel=cancel):
        return False
    if _sleep_cancel(cool_down, cancel):
        return False
    return check()


def _adb_reset(adb, addresses):
    """清 adb server 的 stale offline 缓存，再 ``connect`` 全部地址（幂等）。"""
    # 触线：remote 模式根本不该进这里（ensure_instances/ensure_process 在 remote 走 RemoteBackend），
    # 误入会 kill 掉共享的 Win:5038 adb server 甚至 MuMu 的 5037。见 plan §D/R2。
    assert _BACKEND != "remote", "_adb_reset 在 remote 模式绝不应被调用（会 kill 共享/远端 adb server）"
    try:
        subprocess.run([adb, "kill-server"], capture_output=True, timeout=15)
        print(f">>> adb kill-server（{adb}）清陈旧 offline 缓存")
    except Exception as e:
        print(f"!! adb kill-server 失败（忽略继续）：{e}")
    time.sleep(0.5)
    for addr in addresses:
        _adb_connect(adb, addr)


def _idx_to_addr(idx):
    """MuMu 索引 → adb 地址。MuMu 约定 ``adb_port = 16384 + 32*index``（与 ``_role_indices`` 同公式）。"""
    return f"127.0.0.1:{16384 + 32 * idx}"


def ensure_process(addresses, package=PACKAGE, retries=2, cool_down=10, come_up=40, cancel=None):
    """在每个地址上确保游戏进程已启动（**交付界面：游戏进程存活**，建立在 ensure_instances 之上）。

    每个地址：不在则 ``monkey`` 拉起 → 双重检查（检查①轮询到 pidof 命中 ≤``come_up`` →
    冷却 ``cool_down`` → 检查② pidof）。任一未过即重新 monkey，``retries`` 轮仍不过则 raise。

    背景：实测 ``control launch -pkg`` 在本环境**不能可靠起游戏**（120s 未起），monkey 才是可靠
    手段（~2s）。闪退隐含在 double-check 失败 → 重新 monkey。

    ``cancel``：mumu_server HTTP handler 客户端断连时传入，命中即提前 return（与 ensure_instances 同语义）。
    """
    adb = _adb_path()
    _adb_reset(adb, addresses)
    print(f">>> 逐台双重检查游戏进程（cool_down={cool_down}s, come_up={come_up}s）")
    failed = []
    for addr in addresses:
        if _cancelled(cancel):
            print(">>> ensure_process：HTTP 客户端已断连，中止")
            return
        t0 = time.time()
        for attempt in range(retries + 1):
            if not _game_process_alive(adb, addr, package):
                print(f"    [{addr}] 游戏进程不存在，monkey 拉起")
                _start_game_process(adb, addr, package)
            if _double_check(lambda a=addr: _game_process_alive(adb, a, package), cool_down, come_up, cancel=cancel):
                print(f"    [{addr}] 游戏进程就绪（双重检查通过，用时 {int(time.time() - t0)}s）")
                break
            if attempt < retries:
                if _cancelled(cancel):
                    return
                print(f"    [{addr}] 游戏进程双重检查未过，重新 monkey（{attempt + 1}/{retries}）")
        else:
            failed.append(addr)
    if failed:
        raise RuntimeError(f"游戏进程在 {failed} 上重试 {retries} 轮仍未就绪")
    print(f"<<< 游戏进程就绪：{list(addresses)}")


class _RoleTagSink(TaskerEventSink):
    """把「角色名 ↔ Tasker uuid」显式写进编排日志的探针（Tx/uuid→角色 归号方案）。

    为什么需要：maafw 原生日志按 ``[Tx<tid>]`` 线程交错记录 5 个号，日志里**没有任何角色
    名**——排查时只能靠"连接顺序 + 时间窗"间接推断 Tx 对应哪个号（08-15 实证该推断可行但
    脆弱：同秒提交 / 改启动间隔就断）。而每条 ``task start`` 行都带 ``uuid``（Tasker 实例
    指纹，5 号各一个、全程不变，且 Tx 线程号 per-uuid 全程稳定）——只要在 job 开头把
    「角色 → uuid」显式打出来，之后原生日志 grep uuid 即可精确归号，无需时间对齐。

    工作方式：``connect_all`` 每建好一个 tasker 就 ``add_sink`` 挂一个本类实例（闭包携带
    role/addr）；首个 ``Tasker.Task.Starting`` 事件打一行标记（之后静默，避免每任务 2 行
    噪声）。uuid 从事件 detail 里取（与 native log ``task start:`` 行的 uuid 同源）。

    日志形态（编排日志，每号恰好一行）::

        [tag] 欢喜(127.0.0.1:16736) uuid=9fcb6c12df310cb3   # maafw 按 Tx/uuid 归号用

    排查时拿这张表到 maafw：``task start: .*"uuid":"9fcb…"`` 所在行的 ``[TxNNNNN]`` 即该号
    的线程号（per-uuid 稳定，见 5r_log_analysis skill §1）。
    """

    def __init__(self, role, addr):
        super().__init__()
        self._role = role
        self._addr = addr
        self._tagged = False          # 只打首个 Starting；后续任务静默

    def on_tasker_task(self, tasker, noti_type, detail):
        if self._tagged or noti_type.value != 1:   # 1 = Starting
            return
        self._tagged = True
        print(f"[tag] {self._role}({self._addr}) uuid={detail.uuid}"
              f"   # maafw 按 Tx/uuid 归号用")


def connect_all(roles, package=PACKAGE):
    """连若干设备，**每账号一份独立 Resource**（各自加载 pipeline + OCR 模型 + custom 注册）。

    为什么独立而非共享：MaaFw 的 OCR（PaddleOCR）模型推理**非线程安全**。多个 Tasker
    共享同一份 Resource 并发跑识别时，会触发原生层 ``abort()``——进程直接退出、exit 3、
    无 Python traceback（``faulthandler`` 抓到的是 ``Fatal Python error: Aborted``，5 个
    Python worker 都停在 ``time.sleep``，真正 abort 的是一个无 Python 栈的 MaaFw 线程）。
    每账号一份 Resource 让各 Tasker 自带模型实例，并发 OCR 互不干扰；solo / launch /
    team 等所有并发点一并解除该隐患。代价：内存 ×N（OCR 模型每份约 0.1GB）、连接阶段
    多几秒加载。controller/resource 均由各自 tasker 保活，返回 ``{role: Tasker}``。
    """
    Toolkit.init_option(DEBUG_DIR)
    found = {d.address: d for d in _adb_find_devices()}

    # 清掉 adb server（5037）里的陈旧 offline 缓存：之前任何 adb 使用（手动探测 / GUI / 上一轮
    # 5r 未正常退）都可能给某些端口留下 offline 条目。find_adb_devices 能穿透缓存发现真设备，但
    # 下面 AdbController.post_connection 第一步 ``adb -s <addr> get-state`` 会直接读缓存——命中
    # offline 就秒级失败（"failed to connect" → "Tasker 未就绪"，本次 6130@16448 即此）。kill-server
    # 后下一条 adb 命令会自动重启一个干净 server、重新握手。run_5r 是独占 adb 的进程，无副作用。
    # **仅 local 模式**：remote 模式下 adb server 是 Win 上共享的 5038（mumu_server 拥有），kill 会
    # 断掉所有远端连接、且可能波及 MuMu 5037——绝不能 kill。
    if found and _BACKEND != "remote":
        _adb = str(next(iter(found.values())).adb_path)
        try:
            subprocess.run([_adb, "kill-server"], capture_output=True, timeout=15)
            print(f">>> adb kill-server（{_adb}）清陈旧 offline 缓存")
        except Exception as _e:
            print(f"!! adb kill-server 失败（忽略继续）：{_e}")
        time.sleep(0.5)
    elif _BACKEND == "remote":
        print(">>> remote 模式：跳过 kill-server（共享 Win:%d adb server，不能 kill）" % ADB_SERVER_PORT)

    taskers = {}
    for idx, (role, addr) in enumerate(roles.items(), 1):
        if addr not in found:
            if _BACKEND == "remote":
                # 远端 5038 server 可能还没把该角色端口 connect 进来——让 mumu_server 补 connect 后重查。
                print(f"    [{role}] {addr} 不在远端设备列表，请求 mumu_server /adb/connect 后重查")
                be_adb_connect(addr)
                found = {d.address: d for d in _adb_find_devices()}
            if addr not in found:
                raise RuntimeError(f"{role} 的设备 {addr} 未找到；已发现 {list(found)}")
        dev = found[addr]
        ctrl = AdbController(
            adb_path=str(dev.adb_path), address=dev.address,
            screencap_methods=dev.screencap_methods, input_methods=dev.input_methods,
            config=dev.config,
        )
        ctrl.post_connection().wait()
        resource = Resource()                   # 每账号独立 Resource（独立 OCR 模型，避免并发 abort）
        resource.post_bundle(RESOURCE_PATH).wait()
        _register_customs(resource)             # 各自注册 custom（OCRNum/count/returnOCR/…）
        t = Tasker()
        t.bind(resource, ctrl)
        if not t.inited:
            raise RuntimeError(f"{role} Tasker 未就绪（{addr}）")
        # 角色↔uuid 显式打标：首个 Task.Starting 事件落一行 [tag]，之后原生日志按 uuid 精确归号
        # （sink 对象由 t._sink_holder 保活，本地无需另行持引用）。
        t.add_sink(_RoleTagSink(role, addr))
        taskers[role] = t
        print(f"[{idx}] {role} 已连接 {addr}（独立 Resource）")  # [id] 用于 solo --ids
    return taskers


def _save_timeout_screenshot(tasker, label):
    """超时收口时抓一张当前画面存盘（``debug/debug/timeout/<时间戳>_<label>.png``）。

    为什么需要：MaaFw 的 on_error 截图只在 pipeline **命中 on_error 节点**时落盘；而墙钟
    超时是被 ``post_stop`` 强停的——若任务卡在兜底循环（如 shimen 点完"去完成"后空转
    ``issub_sleep``、从未触发 on_error），就**完全没有截图留存**，事后无法定位"卡在哪一帧"。
    本函数在 stop 之后主动 ``controller.post_screencap`` 补一张。

    顺序：先 ``post_stop``（停 pipeline job，释放 controller 截图通道）再截图——避免与正在
    跑的 pipeline 抢截图。stop 不触发游戏点击，画面 ≈ 超时瞬间（差 <1s）。

    MaaFw 截图返回 **BGR** ndarray（见 ``maa/buffer.py::ImageBuffer`` 注释，与 OpenCV 兼容）；
    本环境无 cv2，用 PIL 存图，故先 ``img[:,:,::-1]`` 翻成 RGB，否则红蓝反转。

    失败只打一行日志、不抛——超时本就要 return False 收口，截图是尽力而为。
    """
    try:
        ctrl = tasker.controller
        img = ctrl.post_screencap().wait().get()    # BGR ndarray（可能 None / 空）；.get() 不是 .result
                                                    # （JobWithResult 无 result 属性，访问器是 .get）
        if img is None or getattr(img, "size", 0) == 0:
            print(f"    （超时截图：空图像，跳过 [{label}]）")
            return
        from PIL import Image
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in label)
        while "__" in safe:   # 折叠连续下划线（label 开头的 "[ " 等）+ 去首尾
            safe = safe.replace("__", "_")
        safe = safe.strip("_")
        ts = datetime.now().strftime("%Y.%m.%d-%H.%M.%S")
        out_dir = os.path.join(DEBUG_DIR, "debug", "timeout")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{ts}_{safe}.png")
        Image.fromarray(img[:, :, ::-1]).save(path)   # BGR → RGB
        print(f"    （超时截图已存：{path}）")
    except Exception as e:
        print(f"    （超时截图失败，忽略 [{label}]：{e}）")


def run_task(tasker, entry, override=None, timeout=600, label=None, watch_alive=None,
             watch_dead_streak=3, sentinel=None):
    """跑一个**原生** entry，带墙钟超时：超时则 ``post_stop`` 中断。返回是否在超时内完成。

    自动合并 ``DEFAULT_OVERRIDES`` 里的项目建议默认值（如运镖跳过活力检测）。
    调用方显式传的同名 node override 优先。

    超时收口时顺带抓一张当前画面存盘（``_save_timeout_screenshot``）——墙钟超时被 stop 强停
    不触发 pipeline 的 on_error 截图，补一张便于事后定位"卡在哪一帧"。

    ``watch_alive``：可选 ``() -> bool`` 游戏存活探针。任务运行中若探针**连续**
    ``watch_dead_streak`` 秒返回 False（如游戏被 force-stop 杀死 / 崩溃不再重启），**提前**
    ``post_stop`` 止损返回 False——避免 start 这类长任务对着死游戏空等满墙钟超时（600s），把恢复
    快速交给上层 L1/L3。start/panduan 场景传 ``lambda: _game_process_alive(adb, address, package)``。

    ``watch_dead_streak`` 默认 3：要求**连续** 3 次探针 False 才判"游戏已死"。必须——pidof 瞬时为空
    （进程崩溃后自动重启的空窗）或 adb 抖动（恢复阶段 adb 操作密集）都会让单次 False 误报，过早
    掐断本可自己恢复的 start。连续 3s 才置信，仍能抓住真正被 force-stop 杀死的情况（~43s→~46s）。

    ``sentinel``：可选 ``() -> bool`` 完成确认回调。``job.done`` 后调一次，返回 False 即判"假完成"
    （走 on_error→空节点）→ run_task 返回 False，不让空节点伪装成成功。默认 None=不校验（维持
    旧行为）。solo_all 传 ``_sentinel_main``（主界面模板识别）以抓"任务 done 但角色停在挖宝/战斗/
    弹窗界面"的假完成（8/6 欧阳挖宝卡死→后续 7 任务全假完成即此症）。
    """
    label = label or entry
    ov = dict(override or {})
    if entry in DEFAULT_OVERRIDES:
        for node, fields in DEFAULT_OVERRIDES[entry].items():
            ov.setdefault(node, fields)  # 不覆盖调用方显式传的
    print(f">>> {label}（≤{timeout}s）")
    job = tasker.post_task(entry, ov)
    _t0 = time.time()
    deadline = _t0 + timeout
    dead_streak = 0
    while time.time() < deadline:
        done = job.done  # 读这个属性会进 MaaFw native；若进程在这附近崩，faulthandler 会在 stderr 打栈
        if done:
            if sentinel is not None and not sentinel():
                # done 但 sentinel 不满足 = 走了空节点假成功（default_pipeline 全局 on_error:["空节点"]
                # 让 next 超时也"成功"）。不计完成，让上层按失败处理（solo_all 计超时统计等）。
                print(f"!!! {label} done 但 sentinel 判未达成（疑 on_error→空节点假成功），按失败计")
                return False
            print(f"<<< {label} 完成（用时 {int(time.time() - _t0)}s）")
            return True
        if watch_alive is not None:
            if not watch_alive():
                dead_streak += 1
                if dead_streak >= watch_dead_streak:
                    tasker.post_stop().wait()
                    _save_timeout_screenshot(tasker, label)
                    print(f"!!! {label} 游戏进程持续死亡 {watch_dead_streak}s，提前 stop（用时 {int(time.time() - _t0)}s）")
                    return False
            else:
                dead_streak = 0
        time.sleep(1)
    tasker.post_stop().wait()
    _save_timeout_screenshot(tasker, label)  # stop 后补一张超时截图（兜底循环不触发 on_error 的场景必备）
    print(f"!!! {label} 超时 {timeout}s，已 stop")
    return False


def _barrier_reset(tasker, role, per_timeout=60):
    """任务间隔离 barrier：sleep 5s + 跑「打开大地图_69副本」重置角色位置到稳定主界面。

    防止前一任务卡死/停在异常界面（战斗/挖宝/弹窗）污染后续任务——典型场景：欧阳
    wabaotu 卡在挖宝界面，后续 7 个单人任务入口全 on_error→空节点假完成（8/6 实例）。
    打开大地图→关闭 把角色拉回稳定主界面锚点，等价"任务间显式回主界面"哨兵；副本间
    用它让长安城 UI 稳定（防普通-2/3 连续进本时 ClickKey 小地图键被吞）。

    尽力清场：barrier 自身失败/超时不抛（打 warning 继续），职责只是提高下一任务成功率，
    非硬前置；整轮预算由 solo_all 的 overall_deadline 兜底。
    """
    time.sleep(5)
    try:
        run_task(tasker, "打开大地图_69副本", timeout=per_timeout,
                 label=f"[{role}] barrier 打开大地图")
    except Exception as e:
        print(f"    [{role}] barrier 异常（忽略继续）：{e}")


# ---------------- 启动就绪哨兵：主界面确认 + L1/L3 自愈 ----------------
# ensure_instances/ensure_process 只能保证"设备开机 + 游戏进程存活"，抓不住:
#   A. 起来 >10s 后闪退（pidof 双重检查的 10s 窗口早过）；
#   B. 卡死（进程活着但 UI 黑屏 / 登录卡住 / 卡 ProtocolLauncher，永远到不了主界面）。
# 且 launch 跑的 start/panduan 的 done-status 不可信（default_pipeline 全局 on_error:["空节点"]
# 让 next 超时也"成功"）。故这里用**独立哨兵** tasker.post_recognition 做权威判定：
# 它是纯识别、无 pipeline 节点 / 无 next / 无 on_error，job.succeeded 即真实命中，绕开空节点陷阱。
# （Or 复合识别若以后要用：any_of 元素须 JRecognition(type=,param=) 包裹，否则序列化缺 type 判别。）

# 就绪判据：仅"主界面"。start 跑完后只该看到主界面；若还看得到"登录游戏"= start 没点进去
# （登录卡住 / 崩了）→ 失败、走 L1/L3。**绝不能把"看到登录游戏"当成功**。
_MAIN_RECO = JTemplateMatch(
    template=["zonghe/jiahao.png", "zonghe/chenghao.png",
              "zonghe/baoguo.png", "zonghe/baoguo_man.png"],
    roi=[1197, 540, 78, 157], threshold=[0.7])


def _screencap(tasker):
    """截一帧；失败返 None。

    cached_image 在截图失败时**抛 RuntimeError** 而非返 None（见 maa/controller.py），故必须
    try/except，否则会崩掉外层的轮询循环。截图通道被 pipeline 占着时也可能异常——交给上层重试。
    """
    try:
        img = tasker.controller.post_screencap().wait().get()    # .get() 返回 ndarray;别用 .result——
                                                                 # JobWithResult 没有该属性(post_screencap 文档
                                                                 # "可通过 result 获取"是误导,访问器实为 .get)
    except Exception as e:
        print(f"    （哨兵截图失败，跳过本轮：{e}）")
        return None
    if img is None or getattr(img, "size", 0) == 0:
        return None
    return img


def _recognize(tasker, reco_type, reco_param, img):
    """对**给定帧**跑一次性识别，返回是否命中（job.succeeded）。识别异常 → False。"""
    try:
        job = tasker.post_recognition(reco_type, reco_param, img)
        job.wait()
        return bool(job.succeeded)
    except Exception as e:
        print(f"    （哨兵识别异常，按未命中：{e}）")
        return False


def _sentinel_main(tasker):
    """默认任务完成 sentinel：跑一次主界面模板识别（纯识别、无帧差/focus，轻量）。

    供 run_task 的 ``sentinel`` 参数用——任务 ``done`` 后确认角色确实在主界面，绕开
    ``on_error:["空节点"]`` 假成功（任务入口走空节点时 done=True 但角色其实在挖宝/战斗/
    弹窗界面）。比 ``_ready_main`` 轻：不做帧差/focus，只判主界面模板命中，适合每个 solo
    任务后低成本复核。
    """
    img = _screencap(tasker)
    if img is None:
        return False
    return _recognize(tasker, JRecognitionType.TemplateMatch, _MAIN_RECO, img)


def _game_focused(adb, address, timeout=8):
    """adb 核对前台窗口是否为**游戏主活动**（com.netease.game.MessiahNativeActivity）。

    返回 True=游戏主活动在前台；False=明显不是（ProtocolLauncher / 启动器 / 系统 UI / ANR 弹窗）；
    None=查询自身失败（adb 不稳）——此时**不否决**，交给帧差/模板判据。

    为什么需要：6130 卡 ANR 时 focus=ProtocolLauncher（也在 com.netease.my 包下），且
    post_screencap 会吐陈旧主界面帧 → 模板假命中。focus 是独立于 Maa 截图的 ground truth。
    """
    try:
        cp = subprocess.run([adb, "-s", address, "shell", "dumpsys", "window"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    for line in cp.stdout.splitlines():
        if "mCurrentFocus" in line:
            return "MessiahNativeActivity" in line
    return None   # 找不到 focus 行（极少见），当不确定


# 就绪判据的核心：start 跑完后必须在**活着的主界面**。
# 单凭"主界面模板命中"不够——卡死/ANR 时 post_screencap 会吐陈旧主界面帧导致假命中
# （2026-08-02 实测：6130 卡系统 ANR 弹窗，哨兵仍报"登录就绪"，整跑误判全成功）。
# 故模板命中后再加两道独立确认：
#   ① 帧差：隔 live_gap 秒再截一帧，必须与上一帧**不同**（主界面有环境动画→必变；
#      冻结 / ANR 静态弹窗 / 陈旧缓存帧 → 完全相同）。
#   ② focus：前台必须是游戏主活动 MessiahNativeActivity（独立于 Maa 截图）。
def _ready_main(tasker, adb, address, live_gap=2.5):
    """权威就绪：主界面模板命中 + 画面在变（非冻结/陈旧）+ 游戏主活动在前台。

    只在模板命中后才做（昂贵的）帧差 + focus 确认——模板 miss 直接 False（快路径），不白等 live_gap。"""
    img = _screencap(tasker)
    if img is None:
        return False
    if not _recognize(tasker, JRecognitionType.TemplateMatch, _MAIN_RECO, img):
        return False   # 模板 miss：快路径
    # 模板命中 → 活性①：再截一帧，必须不同
    time.sleep(live_gap)
    img2 = _screencap(tasker)
    if img2 is None or img.tobytes() == img2.tobytes():
        print(f"    [{address}] 主界面模板命中但画面未变（疑冻结/陈旧帧）→ 未就绪")
        return False
    # 活性②：focus 必须是游戏主活动（None=查询失败时不下结论，交给①）
    if _game_focused(adb, address) is False:
        print(f"    [{address}] 前台非游戏主活动（疑 ANR/启动器/弹窗）→ 未就绪")
        return False
    return True


def _restart_instance(adb, address, idx, package, boot_come_up=180):
    """L3：实例级重启（shutdown + launch + 等 adb + monkey 拉游戏）。

    走和冷启动同款路径，专治"脏实例"死结——长跑过的实例上 force-stop+monkey 重启游戏会卡在
    ProtocolLauncher / 停在后台 Android 主界面，MaaFw 对该 activity 取不到 display id
    （截图/点击全废，start 必超时）。实例级重启给一个干净 Android 启动，游戏才会正常进主界面。

    **不走 _adb_reset**：kill-server 是全局的，会扰动并发的其它 4 个账号；这里只用 per-address
    的 _adb_ready（connect + boot_completed）。
    """
    print(f"!!! [{address}] L3 实例级重启（shutdown idx{idx} + launch + monkey）")
    try:
        _shutdown_one(idx)
    except Exception as e:
        print(f"    [{address}] shutdown 异常（忽略继续）：{e}")
    time.sleep(6)
    try:
        _launch_one(idx, package)
    except Exception as e:
        print(f"    [{address}] launch 异常：{e}")
    if not _wait_for(lambda: _adb_ready(adb, address), boot_come_up, interval=3):
        print(f"    [{address}] L3 后 adb 未就绪")
        return False
    if not _game_process_alive(adb, address, package):
        _start_game_process(adb, address, package)   # launch -pkg 不保证在运行实例上拉起游戏
    _wait_for(lambda: _game_process_alive(adb, address, package), 40, interval=2)
    return True


# ---------------- 能力 1：启动一个账号 ----------------

def launch(tasker, package=PACKAGE, timeout=240, address=None):
    """任意状态→主界面，**返回权威就绪判定**（经主界面哨兵确认，不信 start/panduan 的 done-status）。

    ``start``（StartApp + 登录流程）+ ``panduan_zhujiemian``（Back 清场到主界面）跑完后，用独立哨兵
    ``_ready_main`` 确认**活着的主界面**已呈现（模板 + 帧差 + focus）：就绪 = True；否则返 False，
    **由上层 ``_launch_account`` 升级 L3（实例级重启 + 重连 tasker + 重试）**。start 会点登录游戏，
    故跑完后**只该看到主界面**——还看得到登录游戏即 start 失败，不算就绪。

    已登录出口在资源层：``start.json`` 的 ``启动游戏.next`` 首候选是 ``主界面``（定义在
    ``my_task.json``，纯模板检测、无 action、命中即结束）——已登录账号不再空转 timeout。

    ``address`` 由 ``launch_parallel`` 从 ``ROLES`` 注入（adb force-stop / 哨兵地址用）；为 None
    时（zhuagui 单账号旧路径）退化为只 start、不哨兵，沿用旧行为。
    """
    adb = _adb_for_mode()   # local=MuMu adb.exe（Win）；remote=Mac adb（经 ADB_SERVER_SOCKET 打 Win 设备）
    _watch = (lambda: _game_process_alive(adb, address, package)) if address is not None else None
    run_task(tasker, "start", override={"启动游戏": {"package": package}},
             timeout=timeout, label="start 启动/登录", watch_alive=_watch)
    run_task(tasker, "panduan_zhujiemian", timeout=120, label="panduan_zhujiemian 清场到主界面",
             watch_alive=_watch)
    if address is None:
        return True   # 退化路径（无 address 不哨兵）：仅 zhuagui 单账号，保留旧行为
    # 权威判定：start 跑完后必须在**活着的主界面**（模板 + 帧差 + focus）。不是 → 返 False，
    # 由上层 _launch_account 升级 L3（实例级重启 + 重连 tasker + 重试）。无 L1——L3 严格优于它。
    if _wait_for(lambda: _ready_main(tasker, adb, address), 60, 3):
        print(f"<<< [{address}] 已就绪（主界面 + 活性 + focus 确认）")
        return True
    print(f"!!! [{address}] start 后未到活着的主界面")
    return False


def _reconnect_tasker(address):
    """实例重启后重建 tasker：**新建独立 Resource**（全新三元组 = 新 Resource + 新 Controller +
    新 Tasker，与冷启动 ``connect_devices`` 一致）。

    ⚠ **历史设计曾"复用旧 Resource"省内存，已于 2026-08-10 废弃**：复用会让 L3 后的新 tasker 与
    残留旧 tasker 共享同一 Resource 的 OCR 模型；team_form 阶段多 Tasker 并发 post_task 时触发
    原生 access violation（Mac=SIGBUS exit -10 / Win=SIGSEGV exit 139）——2026-08-10 job 155003
    邀请晚风即此症（faulthandler 栈死在 ``maa.tasker.post_task``），本地最小复现见
    ``tools/_repro_l3_resource_race.py``（共享 Resource 必崩、独立 Resource 存活）。根因同
    Resource 的 OCR/pipeline 上下文**非线程安全**：多 Tasker 共享一份 Resource 并发进 native 识别
    路径必崩。故 L3 必须新建独立 Resource，彻底切断新旧 tasker 的 resource 共享。

    代价 ~0.1GB/号 OCR 模型内存 + ~2s 加载——L3 是偶发恢复路径（实例级重启已释放该实例 ~3-4GB），
    完全可接受。**不做 kill-server**（会扰动并发的其它账号）。旧 tasker 的释放由调用方
    ``_launch_account`` 在 L3 前显式 post_stop（见该函数）。
    """
    Toolkit.init_option(DEBUG_DIR)
    found = {d.address: d for d in (_adb_find_devices())}
    if address not in found:
        print(f"    [{address}] 重连失败：实例未在 adb 设备列表")
        return None
    dev = found[address]
    ctrl = AdbController(adb_path=str(dev.adb_path), address=dev.address,
                         screencap_methods=dev.screencap_methods, input_methods=dev.input_methods,
                         config=dev.config)
    ctrl.post_connection().wait()
    resource = Resource()                       # 新建独立 Resource（不再 bind old_tasker.resource）
    resource.post_bundle(RESOURCE_PATH).wait()
    _register_customs(resource)
    t = Tasker()
    t.bind(resource, ctrl)
    if not t.inited:
        print(f"    [{address}] 重连后 tasker 未就绪")
        return None
    return t


def _launch_account(tasker, role, address, package=PACKAGE, timeout=240):
    """单账号完整启动阶梯：``launch`` 失败（start 没把游戏带到主界面）→ 直接升级 L3（实例级重启
    + 新建独立 Resource 重连 tasker + 重试 launch）。返回 ``(是否就绪, L3 后的新 tasker 或 None)``。

    为什么没有 L1：实测 L1（force-stop+monkey 重启游戏）在**脏实例**上必败——游戏重启后卡
    ProtocolLauncher / 停在后台 Android 主界面，MaaFw 取不到 display id，start 白跑满超时（~150s）
    才升级。L3 实例级重启给干净 Android 启动，start 才能正常把游戏带到主界面（多轮实测）。L3
    严格优于 L1，故砍掉 L1，失败即 L3，机制精简且更快（省 L1 那 150s）。

    内存安全：L3 **新建独立 Resource**（不复用旧 Resource——多 Tasker 共享会触发并发 native 崩，
    见 ``_reconnect_tasker``）；shutdown 实例时还释放该实例 ~3-4GB 再重分配，远覆盖 ~0.1GB 新 resource。
    """
    try:
        if launch(tasker, package, timeout, address):
            return True, None
    except Exception as e:
        print(f"!!! [{role}] launch 异常：{e}")
    print(f"!!! [{role}] launch 未就绪，升级 L3 实例级重启 + 重连 tasker")
    # 方案2：L3 前 显式停止旧 tasker。其 controller 将随实例重启失效，故 post_stop **不 wait**
    # （避免在失效 native handle 上阻塞或二次崩），仅发停止信号；try/except 兜底 native 异常。
    # 旧 tasker 的 Resource 已在 _reconnect_tasker 里弃用（改新建独立 Resource），不与新 tasker
    # 共享；这里 post_stop 纯为释放旧 tasker 自身 native 状态、防止残留。
    try:
        tasker.post_stop()
    except Exception:
        pass
    adb = _adb_for_mode()   # remote 下 _restart_instance 走 HTTP（Win 端跑），adb 仅备用
    idx = (int(address.rsplit(":", 1)[1]) - 16384) // 32   # MuMu 约定 adb_port = 16384 + 32*idx
    if not be_restart_instance(adb, address, idx, package):
        return False, None
    new_t = _reconnect_tasker(address)
    if new_t is None:
        print(f"!!! [{role}] L3 后重连 tasker 失败")
        return False, None
    try:
        if launch(new_t, package, timeout, address):
            print(f"<<< [{role}] 登录就绪（L3 实例级重启 + 重连救回）")
            return True, new_t
    except Exception as e:
        print(f"!!! [{role}] L3 重连后 launch 异常：{e}")
    print(f"!!! [{role}] L3 后仍未就绪")
    return False, new_t


def launch_parallel(taskers, package=PACKAGE, timeout=240, stagger=10, hard_cap=1800):
    """并行启动多账号，**任一号未就绪即 raise RuntimeError、整跑中止**（列出全部失败号）。

    每个账号在独立线程里跑 ``launch(address=ROLES[role])``；按 ``taskers`` 顺序 submit，相邻两次
    sleep ``stagger`` 秒。错峰而非齐发：各账号虽各自独立 Resource（已无 OCR 模型并发竞态），但
    5 个 StartApp + 游戏进程同时拉起会瞬间压满宿主 CPU/磁盘/ADB；错峰让前一个越过 StartApp 的
    post_delay 再起下一个，整体更稳。设 0 即齐发。

    ``launch`` 返回 bool（就绪与否）、不抛；这里 ``f.result(timeout=hard_cap)`` 取 bool 并汇总。
    ``hard_cap`` 是单账号整体墙钟上限——兜尾部风险：start/panduan 的 ``post_stop().wait()`` 在
    adb/controller 楔死时会无限挂（``Job.wait`` 无 timeout），此处超时即把该号判失败、不拖死整跑。
    挂死的线程由进程退出的 TerminateProcess 收掉（见 ``__main__``）。

    任一号失败 → raise RuntimeError（消息含失败号列表）→ 传到 ``__main__`` 的 except → traceback +
    exit 1，在 team_form / solo_all 之前中止。**不再带死实例跑全程**。
    """
    items = list(taskers.items())
    print(f">>> 并行启动 {len(items)} 个账号（每个间隔 {stagger}s；单号含 L1/L3 自愈）")
    outcomes = {}        # role -> (是否就绪, L3 后新 tasker 或 None)
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        futs = {}
        for i, (role, t) in enumerate(items):
            if i > 0:
                time.sleep(stagger)
            futs[ex.submit(_launch_account, t, role, ROLES[role], package, timeout)] = role
        for f, role in [(fu, futs[fu]) for fu in futs]:
            try:
                outcomes[role] = f.result(timeout=hard_cap)
            except TimeoutError:
                print(f"!!! [{role}] launch 超过 {hard_cap}s 未返回（疑 controller 楔死），判失败")
                outcomes[role] = (False, None)
            except Exception as e:
                print(f"!!! [{role}] launch 异常：{e}")
                outcomes[role] = (False, None)
    # L3 重建过的 tasker 换回 dict（后续 team/solo 用新 tasker）；旧 tasker 丢引用由 GC 回收
    for role, (_ok, new_t) in outcomes.items():
        if new_t is not None:
            taskers[role] = new_t
    failed = [r for r, (ok, _) in outcomes.items() if not ok]
    if failed:
        raise RuntimeError(
            f"以下账号登录未就绪，整跑中止：{failed}"
            f"（其余 {len(items) - len(failed)} 台就绪）"
            f"——请检查这几台 MuMu 的游戏是否崩 / 卡黑屏 / 登录卡死 / 实例内存不足，手动恢复后重跑。")
    print(f"<<< 并行启动完成（全部就绪）：{', '.join(r for r, _ in items)}")


# ---------------- 能力 2：组队副本（2-1 组队 / 2-2 执行副本+解散）----------------
# 组队阶段的全局状态：队员的 ``5R_duiyuan`` 是长任务（接受邀请→进本→挂机），其 future 必须
# 跨「组队」与「执行副本+解散」两个阶段存活，故放在模块级全局。组队时 submit，副本跑完、
# 解散后才 post_stop + 回收。
_TEAM_MEMBER_FUTS = []   # 队员 5R_duiyuan 的 future
_MEMBER_POOL = None      # 跑上述 future 的线程池（组队时建，收尾时 shutdown(wait=False)）


def _stop_members(taskers, member_names):
    """停掉队员的 ``5R_duiyuan``（``post_stop``）并回收全局 future / 线程池。

    角色仍留在游戏队伍里（服务端状态不变），仅停止客户端自动化。``team_run`` 解散后调用；
    ``form`` 模式结束时也调用——否则非守护工作线程会阻塞进程退出。
    """
    global _MEMBER_POOL
    for name in member_names:
        taskers[name].post_stop().wait()
    for f in _TEAM_MEMBER_FUTS:
        try:
            f.result(timeout=15)
        except Exception:
            pass
    del _TEAM_MEMBER_FUTS[:]
    if _MEMBER_POOL is not None:
        _MEMBER_POOL.shutdown(wait=False)
        _MEMBER_POOL = None


def team_form(taskers, member_names, timeouts=None):
    """组队（能力 2-1）：**每次只邀请一个**队员。

    ``5R_duizhang`` 在队长发出邀请时会自动建队，故不先跑 ``chuangjianduiwu``——后者用于
    非 5R 场景，会自动招募陌生人，五开不用。

    对每个队员——
      1. 该队员在后台 ``submit 5R_duiyuan``（接受邀请→进本→挂机的整段状态机，长任务），
         future 存入全局 ``_TEAM_MEMBER_FUTS``；
      2. 队长跑一次 ``5R_duizhang``，override：
         - ``邀请队员-输入队员名称1`` 的 ``input_text`` = 该队员名（只填第 1 个名字位）；
         - ``邀请队员-向下滑动`` 的 ``max_hit`` = 1（原值 4，控制邀请循环次数；改 1 即本次只邀请一人）。
    每次 ``5R_duizhang`` 恰好邀请一名队员（首次还会自动建队），串行直到全员入队。

    队员 future 用模块级线程池 ``_MEMBER_POOL`` 跑（**不用 with**：duiyuan 要活到副本结束，
    with 退出时会 ``shutdown(wait=True)`` 阻塞）。线程池在 ``team_run`` 收尾时 shutdown。
    """
    global _MEMBER_POOL
    timeouts = {**TIMEOUTS, **(timeouts or {})}
    L = taskers["队长"]
    members = [(name, taskers[name]) for name in member_names]
    if not members:
        raise RuntimeError("组队至少需要 1 名队员")

    _MEMBER_POOL = ThreadPoolExecutor(max_workers=len(members))
    del _TEAM_MEMBER_FUTS[:]
    for name, m in members:
        _TEAM_MEMBER_FUTS.append(
            _MEMBER_POOL.submit(run_task, m, "5R_duiyuan", None, timeouts["duiyuan"], "5R_duiyuan"))
        run_task(L, "5R_duizhang",
                 override={"邀请队员-输入队员名称1": {"input_text": name},
                           "邀请队员-向下滑动": {"max_hit": 1}},
                 timeout=timeouts["duizhang"], label=f"队长 5R_duizhang 邀请 {name}")
    print(f"<<< 组队完成：已邀请 {member_names}")


def build_fuben69new_override(xiashi: bool, idx: int) -> dict:
    """合成 ``fuben69new`` 的 pipeline_override（两维独立：是否侠士 + 第几个）。

    - 维度A（类型）→ override ``fuben69new-路由-类型`` 的 next：侠士先点侠士tab、普通直走序号路由。
    - 维度B（序号）→ **只 override 当前 xiashi 对应的那个序号路由节点**（另一类型不被维度A
      路由到、保持默认即可）。**不能两个都 override**：侠士只有 1/2、普通有 1/2/3，idx=3 时若把
      侠士路由也 override 成 ``fuben69new-进入-侠士-3``（不存在），MaaFw PipelineChecker 校验
      next 列表会判 Invalid → override_pipeline 失败 → post_task 返回 task_id=0 → run_task
      死循环刷 ``runner id not found``（2026-08-05 5r 普通-3 卡死根因）。

    链路与两维节点设计详见 ``assets/resource/base/pipeline/fuben69new.json``。
    """
    type_next = (["fuben69new-选择侠士副本-tab"] if xiashi
                 else ["fuben69new-路由-序号-普通"])
    ov = {"fuben69new-路由-类型": {"next": type_next}}
    # 只 override 当前类型对应的序号路由；另一类型不被路由到，保持默认（避免指向不存在的节点）。
    serial_key = "fuben69new-路由-序号-侠士" if xiashi else "fuben69new-路由-序号-普通"
    entry = f"fuben69new-进入-{'侠士' if xiashi else '普通'}-{idx}"
    ov[serial_key] = {"next": [entry]}
    return ov


def team_run(taskers, member_names, timeouts=None):
    """执行 5 本副本 + ZHUOGUI_ROUNDS 轮捉鬼 + 解散（能力 2-2）。

    队长串行跑 :data:`FUBEN69NEW_PLAN` 里的 5 个副本——每本一次
    ``run_task("fuben69new", override=build_fuben69new_override(...))``，再跑 ``zhuoguirenwu``
    限 ``ZHUOGUI_ROUNDS`` 轮捉鬼，最后 ``5R_duizhang_TR`` 解散、停掉队员。

    取代旧的一次 ``post_task("fuben115")`` 串联；fuben69new 是"打一个副本"的自包含链路，重复调用
    实现多本。队员在 :func:`team_form` submit 的 ``5R_duiyuan`` 是长任务，覆盖整段副本+捉鬼，队长
    每次 post 新 entry 时队员端循环自动响应（进本/打怪/结算），无需重新组队。

    捉鬼限轮：把 ``抓鬼一轮完成.next`` 改写到 ``抓鬼轮次计算-max``（max_hit = ZHUOGUI_ROUNDS - 1，
    实际跑 ZHUOGUI_ROUNDS 轮），跑完 ``捉鬼-结束`` 回主界面。standalone 不读 interface.json option，
    base 默认无限循环——必须在此显式接限轮器。
    """
    timeouts = {**TIMEOUTS, **(timeouts or {})}
    L = taskers["队长"]

    # 5 本副本：fuben69new 单本链路，重复调用 with 不同 override
    for xiashi, idx in FUBEN69NEW_PLAN:
        _barrier_reset(L, "队长")
        run_task(L, "fuben69new",
                 override=build_fuben69new_override(xiashi, idx),
                 timeout=timeouts["fuben_per"],
                 label=f"队长 fuben69new {'侠士' if xiashi else '普通'}-{idx}")

    # ZHUOGUI_ROUNDS 轮捉鬼：已有 zhuoguirenwu + 限轮器（entry 自带回长安城找钟馗导航）
    zhuogui_override = {
        "抓鬼一轮完成": {"next": [
            "[JumpBack]抓鬼一轮完成-再次点击确定",
            "抓鬼轮次计算-max",
            "捉鬼-结束",
        ]},
        "抓鬼轮次计算-max": {"max_hit": ZHUOGUI_ROUNDS - 1},
    }
    _barrier_reset(L, "队长")
    run_task(L, "zhuoguirenwu", override=zhuogui_override,
             timeout=timeouts["zhuogui"]*ZHUOGUI_ROUNDS,
             label=f"队长 zhuoguirenwu（{ZHUOGUI_ROUNDS}轮鬼）")

    _barrier_reset(L, "队长")
    run_task(L, "5R_duizhang_TR", timeout=timeouts["duizhang_TR"], label="队长 5R_duizhang_TR 解散")

    _stop_members(taskers, member_names)  # 解散后停掉队员（post_stop 中断 5R_duiyuan）+ 回收 future/池
    print("<<< 5本副本+捉鬼+解散完成，已停掉队员")


def team_dungeon(taskers, member_names, timeouts=None):
    """组队副本 = ``team_form`` + ``team_run``（一键入口，供 full/team 模式一次调完）。"""
    team_form(taskers, member_names, timeouts=timeouts)
    team_run(taskers, member_names, timeouts=timeouts)


# ---------------- 能力 3：并行单人 pipeline ----------------

def solo_all(taskers, entries=None, ids=None, per_timeout=3600, overall_timeout=None, solo_timeouts=None):
    """5 账号**并行**执行单人 pipeline；每个账号内**顺序**跑 ``entries``。

    ids: ``None``=全部账号；或账号 id 集合（1-based，按 ``ROLES`` 顺序，连接日志里有 ``[id] 角色``）。

    超时：
      - ``per_timeout``：单个任务的**默认**墙钟超时（``run_task`` 内 ``post_stop`` 收口）。
      - ``solo_timeouts``：可选 ``{entry: 秒}`` —— 对**指定单人任务**覆盖超时；没列到的任务回退
        ``per_timeout``。``main()`` 里从 ``TIMEOUTS`` 构造：``TIMEOUTS.get(entry, TIMEOUTS['solo'])``，
        于是可在 ``TIMEOUTS`` 里给任意 SOLO_ENTRIES 精细设超时（如 ``"bangpai_renwu": 3600``），
        没有的自动回退 ``"solo": 2400``。
      - ``overall_timeout``：**整轮 solo** 墙钟总超时。到点后各账号当前任务被收口、后续任务不再执行。
        实现是"收缩式"：每个任务实际 timeout = ``min(单任务超时, 距整轮截止的剩余时间)``，于是
        ``run_task`` 现有的单任务超时机制自然把整轮截止传到每个任务——无需额外看门狗线程，整轮
        截止被各账号在 ~1s 轮询粒度内一致地兑现。
    """
    entries = entries or SOLO_ENTRIES
    items = list(taskers.items())  # [(role, tasker)]，按 ROLES 顺序
    chosen = [(r, t) for i, (r, t) in enumerate(items, 1) if ids is None or i in ids]
    who = ", ".join(f"{i}:{r}" for i, (r, _) in enumerate(items, 1)
                    if ids is None or i in ids) or "无"
    print(f">>> 并行单人（每账号顺序跑 {entries}；单任务≤{per_timeout}s"
          + (f"，整轮≤{overall_timeout}s" if overall_timeout else "")
          + f"）对象: {who}")
    _custom = {e: v for e, v in (solo_timeouts or {}).items() if v != per_timeout and e in entries}
    if _custom:
        print("    自定义单任务超时: " + ", ".join(f"{k}={v}s" for k, v in _custom.items()))
    if not chosen:
        print("（未选中任何账号，跳过）")
        return

    overall_deadline = (time.time() + overall_timeout) if overall_timeout else None

    def one(role, t):
        for e in entries:
            left = (overall_deadline - time.time()) if overall_deadline else None
            if left is not None and left <= 0:
                print(f"    [{role}] 整轮超时，停止后续单人任务")
                return
            _barrier_reset(t, role)   # 任务间隔离：sleep5 + 打开大地图重置位置（防前一任务卡死污染）
            entry_to = (solo_timeouts or {}).get(e, per_timeout)
            this_to = entry_to if left is None else min(entry_to, left)
            try:
                run_task(t, e, timeout=this_to, label=f"[{role}] 单人 {e}",
                         sentinel=lambda: _sentinel_main(t))
            except Exception:
                print(f"    [{role}] !!! {e} 抛异常：")
                traceback.print_exc()
                raise

    with ThreadPoolExecutor(max_workers=len(chosen)) as ex:
        list(ex.map(one, [r for r, _ in chosen], [t for _, t in chosen]))
    print("<<< 并行单人完成")


# ---------------- 能力 4：队长单人捉鬼（无限）----------------

def zhuagui(taskers, timeout=None):
    """队长单人无限捉鬼（能力 4）—— ``python run_5r.py zhuagui``。

    对应 interface.json「关闭人员检测-不进入轮次选择」：``ZHUAGUI_OVERRIDE`` 把
    ``抓鬼一轮完成.next`` 改写到 ``钟馗-捉鬼任务-循环`` + JumpBack 回 ``抓鬼一轮完成-再次点击确定``，
    跳过队伍满员判断、不接限轮器，每轮捉完直接续接 → 无限循环。

    只用队长一个账号跑（关闭人员检测 → 不要求队伍满员；不足 5 人时游戏自带 NPC 助战补位，
    pipeline 里 抓鬼不足五人-图片识别 分支也会自动点取消再续接）。standalone 不读 interface.json
    option，故 override 必须在此显式给。实际靠 Ctrl+C 停；``timeout`` 是安全帽（默认
    ``TIMEOUTS["zhuagui"]``），到点 ``post_stop`` 收口退出。
    """
    timeout = timeout or TIMEOUTS["zhuagui"]
    leader = taskers["队长"]
    run_task(leader, "zhuoguirenwu", override=ZHUAGUI_OVERRIDE,
             timeout=timeout, label="队长 zhuoguirenwu 无限捉鬼")


# ---------------- 能力 5：主入口 ----------------

def _parse_ids_flag(args):
    """从 solo 参数里提取 ``--ids/--accounts/-i <1,3,5>``。返回 (ids_set 或 None, 其余任务名列表)。"""
    ids, rest, i = None, [], 0
    while i < len(args):
        a = args[i]
        if a in ("--ids", "--accounts", "-i") and i + 1 < len(args):
            ids = {int(x) for x in args[i + 1].replace("，", ",").split(",") if x.strip()}
            i += 2
        else:
            rest.append(a)
            i += 1
    return ids, rest


def print_help():
    print("用法: python run_5r.py [full|launch|team|form|run|zhuagui|solo [任务名...] [--ids 1,3,5] | help]")
    print("  full    ensure 实例→连接→启动5个→组队→副本+解散→并行单人→关掉全部实例")
    print("  launch  只启动+登录 5 个账号")
    print("  team    完整5人任务：启动 + 组队 + 副本+解散")
    print("  form    启动登录 + 只组队（逐个邀请，首次自动建队），到全员入队即停（测组队用）")
    print("  run     只执行副本+解散（前提：已组好队；不启动登录）")
    print("  zhuagui 队长单人无限捉鬼（关闭人员检测-不进入轮次选择）；登录队长→无限捉鬼，Ctrl+C 停")
    print("  solo    并行单人：任务名指定只跑哪些；--ids 指定账号(1-based，默认全部)")
    print("          例: solo baotu_renwu                # 5 账号都跑宝图")
    print("              solo baotu_renwu --ids 1,3,5    # 只在账号 1/3/5 跑宝图")
    print("  账号编号 = 连接日志里的 [id] 角色（按 ROLES 顺序，从 1 开始）")
    print("  log_analysis [YYYYMMDD|YYYY-MM-DD]   打印指定日期的耗时矩阵 + 账号信息变化矩阵（默认今天）")
    print("  服务/客户端：mumu_server（Win，adb:5038+API:5080）| cli_server（Mac，API:5090）|")
    print("    remote <mode> [args]（Win→Mac 提交；如 remote full / remote solo shimen_renwu_new --ids 1）")
    print("    remote cancel    显式取消 cli_server 当前在跑的任务（Ctrl+C 只断开，任务继续在 Mac 跑）")
    print("    remote show      显示 cli_server 当前在跑的任务 + 最近日志尾")
    print("  --config <name>   全局选项（任意位置）：从 agent/<name>.py 加载配置覆盖 run_5r 头部")
    print("                    （ROLES/任务列表/超时等）；没列出的字段保持默认。remote 模式会透传给 Mac")
    print("                    例: --config team2 full   |   remote --config team2 full")
    print("\n常用单人任务名（solo 可指定）：")
    for k, v in SOLO_TASKS_HELP.items():
        print(f"  {k:18} {v}")


# ============================================================================
# log_analysis：解析 run_5r 日志打印【耗时矩阵】+【账号信息变化矩阵】。
# 复刻自 .claude/skills/5r_log_analysis/SKILL.md（无脚本，纯内联 -c 逻辑，此处落地为函数）。
# ============================================================================
# 单人任务"典型基线留意线"（秒）：单元格超过它则标 *（仅提醒，非异常）。不在表里的任务不标。
_LOG_ANALYSIS_LIM = {
    "shuangbei": 60, "fuli_qiandao": 120, "shimen_renwu": 600, "yunbiao_renwu2": 1000,
    "baotu_renwu": 1000, "打开大地图_69副本": 60, "mijing_renwu": 2100, "sanjieqiyuan": 200,
    "huoyue_lingqu": 60, "zhengli_baibao": 150, "jiayuan_zhengli": 150, "huoli": 120,
    "kejuxiangshi": 300, "zhanghao_xinxi": 60,
}


def print_log_analysis(date_str=None):
    """打印指定日期的【耗时矩阵】+【账号信息变化矩阵】。
    日期：``YYYYMMDD`` 或 ``YYYY-MM-DD``，默认今天。
    源文件：``debug/run_5r/run_5r_<date>_*.log``（取该日期**最新**一份）+ ``agent/data/account_info.log``。
    """
    import glob, re
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    ymd = date_str.replace("-", "")
    if not (len(ymd) == 8 and ymd.isdigit()):
        print(f"!! 日期格式不对：{date_str}（要 YYYYMMDD 或 YYYY-MM-DD）"); return
    ymd_line = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"   # 行时间戳里的日期段 [YYYY-MM-DD ...]

    files = sorted(glob.glob(os.path.join(DEBUG_DIR, "run_5r", f"run_5r_{ymd}_*.log")),
                   key=lambda p: os.path.getmtime(p))
    if not files:
        print(f"!! 找不到 {ymd} 的 run_5r 日志（{DEBUG_DIR}/run_5r/run_5r_{ymd}_*.log）"); return
    log_path = files[-1]
    print(f"=== 日志分析（{ymd_line}）源：{os.path.basename(log_path)} ===\n")
    log = open(log_path, encoding="utf-8", errors="replace").read()

    # ---- 角色发现：扫「对象:」行（必须逐行，勿跨行 re —— [^，;] 会吞换行）----
    roles = []
    for line in log.splitlines():
        if "对象:" in line or "对象：" in line:
            seg = line.split("对象:", 1)[-1] if "对象:" in line else line.split("对象：", 1)[-1]
            for _, n in re.findall(r"(\d+):([^\s,，;：]+)", seg):
                if n not in roles:
                    roles.append(n)
            break

    # ---- 耗时矩阵：完成 / 超时（同名任务记最后一次）----
    order, data = [], {}
    for line in log.splitlines():
        m = re.search(r"<<< \[([^\]]+)\] 单人 (\S+) 完成（用时 ([\d.]+)s）", line)
        if m:
            t = m.group(2)
            if t not in data:
                order.append(t); data[t] = {}
            data[t][m.group(1)] = int(float(m.group(3))); continue
        m = re.search(r"!!! \[([^\]]+)\] 单人 (\S+) 超时 ([\d.]+)s", line)
        if m:
            t = m.group(2)
            if t not in data:
                order.append(t); data[t] = {}
            data[t][m.group(1)] = -int(float(m.group(3)))   # 负数 = 超时
    if not roles:   # fallback：从完成行提取角色
        for line in log.splitlines():
            m = re.search(r"<<< \[([^\]]+)\] 单人 \S+ 完成", line)
            if m and m.group(1) not in roles:
                roles.append(m.group(1))
        roles = roles[:5] or ["?"]

    print("--- 耗时矩阵（行=任务，列=5 号，格=秒；*=超基线留意线，X<N>=墙钟超时，-=未跑）---")
    print("%-16s" % "任务" + "".join("%-9s" % r for r in roles))
    for t in order:
        cells = []
        for r in roles:
            v = data[t].get(r)
            if v is None:
                cells.append("-")
            elif v < 0:
                cells.append("X" + str(-v))
            else:
                cells.append(str(v) + ("*" if (_LOG_ANALYSIS_LIM.get(t) and v > _LOG_ANALYSIS_LIM[t]) else ""))
        print("%-16s" % t + "".join("%-9s" % c for c in cells))
    print("(* = 超过典型基线留意线，X = 墙钟超时；异常短=疑似假成功，人工留意见)\n")

    # ---- 账号信息变化矩阵（本次=指定日期内末条；上次=其前一条[可跨日期]；Δ=本次-上次）----
    info_path = os.path.join(AGENT_DIR, "data", "account_info.log")
    print("--- 账号信息变化矩阵（本次 = 指定日期内末条；上次 = 其前一条；Δ = 本次-上次，负=净消耗）---")
    print("%-16s" % "账号(port)" + "".join("%-11s" % x for x in ["上次金币", "本次金币", "Δ金币", "上次银币", "本次银币", "Δ银币"]))
    by_port = {}            # port -> [(全局行号, 金币, 银币)]，按文件顺序
    date_line_idx = set()   # 属于指定日期的行号
    if os.path.isfile(info_path):
        for i, line in enumerate(open(info_path, encoding="utf-8", errors="replace")):
            m = re.search(r"账号:\s*(127\.0\.0\.1:\d+)\s*\|\s*金币:\s*(-?\d+)\s*\|\s*银币:\s*(-?\d+)", line)
            if m:
                by_port.setdefault(m.group(1), []).append((i, int(m.group(2)), int(m.group(3))))
                if ymd_line in line:
                    date_line_idx.add(i)
        hit = False
        for port in sorted(by_port):
            recs = by_port[port]
            date_recs = [r for r in recs if r[0] in date_line_idx]
            if not date_recs:
                continue
            hit = True
            cur = date_recs[-1]                    # 该端口指定日期内末条
            pos = recs.index(cur)
            prev = recs[pos - 1] if pos > 0 else None   # 该端口上一条（可跨日期）
            if prev is None:
                print("%-16s" % port + "  (无前一条，无法算 Δ)"); continue
            (g0, s0), (g1, s1) = (prev[1], prev[2]), (cur[1], cur[2])
            d = lambda n: ("+" + str(n)) if n >= 0 else str(n)
            print("%-16s" % port + "".join("%-11s" % x for x in [g0, g1, d(g1 - g0), s0, s1, d(s1 - s0)]))
        if not hit:
            print(f"（{ymd_line} 无 account_info 记录）")
    else:
        print(f"（找不到 {info_path}）")
    print("Δ = 本次(指定日期内末条) - 上次(其前一条)；负=净消耗。")


# ============================================================================
# 远端/本地 transport 抽象（plan §B/C）
# main() 顶部 _detect_backend() 设 _BACKEND；be_* 包装层在"碰 MuMu 生命周期的入口"dispatch：
#   remote → RemoteBackend（HTTP 到 Win mumu_server）；local → 现有 free 函数（逐字节不变）。
# 关键简化：adb shell 探针（pidof/monkey/adb_ready/screencap）无需 HTTP——Mac 的 adb 经
#   ADB_SERVER_SOCKET=tcp:<WIN_IP>:5038 透明打到 Win 设备。只有 MuMuManager.exe 生命周期
#   （launch/shutdown/restart/ensure 实例）必须走 HTTP（Win 二进制，Mac 跑不了）。
# ============================================================================
_BACKEND = None   # "remote"/"local"/None（main 探测前）；None 视同 local


def _mac_adb():
    """Mac 远端模式用的 adb 二进制（MaaFw 经此 adb + ADB_SERVER_SOCKET 连 Win:ADB_SERVER_PORT）。
    env MAA_5R_MAC_ADB 优先；否则默认 ~/platform-tools/adb（PoC 下载位置）。"""
    p = os.environ.get("MAA_5R_MAC_ADB")
    if p and os.path.isfile(p):
        return p
    return os.path.join(os.path.expanduser("~"), "platform-tools", "adb")


def _adb_for_mode():
    """当前模式用的 adb 二进制：local=MuMu 自带 adb.exe（Win）；remote=Mac 的 adb。"""
    return _mac_adb() if _BACKEND == "remote" else _adb_path()


def _adb_find_devices():
    """按模式发现 adb 设备：remote 用 Mac adb（specified_adb）连远端 Win server；local 用默认。"""
    if _BACKEND == "remote":
        return list(Toolkit.find_adb_devices(specified_adb=_mac_adb()))
    return list(Toolkit.find_adb_devices())


def _ip_is_local(ip):
    """ip 是否是本机某网卡地址（同机保护：Win 上 WIN_IP 命中 → 强制 local）。bind 成功即本机。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


class RemoteBackend:
    """HTTP 客户端：Mac 侧调用 Win mumu_server（http://WIN_IP:MUMU_API_PORT）做 MuMu 生命周期。
    用 stdlib urllib（不引 requests，server/client 共享无依赖 helper）。"""
    BASE = "http://%s:%d" % (WIN_IP, MUMU_API_PORT)

    @staticmethod
    def _req(method, path, body=None, timeout=REMOTE_PROBE_TIMEOUT):
        url = RemoteBackend.BASE + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if SHARED_TOKEN:
            req.add_header("Authorization", "Bearer " + SHARED_TOKEN)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}

    @staticmethod
    def get(path, timeout=REMOTE_PROBE_TIMEOUT):
        return RemoteBackend._req("GET", path, timeout=timeout)

    @staticmethod
    def post(path, body, timeout=300):
        return RemoteBackend._req("POST", path, body=body, timeout=timeout)


def _detect_backend():
    """探测远端 mumu_server：WIN_IP 是本机 → local（同机保护）；否则 GET /health，
    ok && adb_up → remote，否则 local。"""
    if _ip_is_local(WIN_IP):
        return "local"
    try:
        h = RemoteBackend.get("/health", timeout=REMOTE_PROBE_TIMEOUT)
        if h.get("ok") and h.get("adb_up"):
            return "remote"
        return "local"
    except Exception:
        return "local"


# ---- be_* 包装层：main()/L3 调这些（不直接调 free 函数）。local 时透传，逐字节不变。----

def be_role_indices(roles):
    if _BACKEND == "remote":
        # MuMu 约定 adb_port = 16384 + 32*idx（与 _role_indices 同公式）；remote 下直接算，
        # 不依赖 mumu_info 字段结构（实例缺失由 ensure_instances 兜底报错）。
        return {r: (int(addr.rsplit(":", 1)[1]) - 16384) // 32 for r, addr in roles.items()}
    return _role_indices(roles)


def be_ensure_instances(indices, package=PACKAGE):
    if _BACKEND == "remote":
        r = RemoteBackend.post("/instance/ensure", {"indices": list(indices), "package": package}, timeout=900)
        if not r.get("ok"):
            raise RuntimeError(f"远端 ensure_instances 失败：{r}")
        return r
    return ensure_instances(indices, package=package)


def be_ensure_process(addresses, package=PACKAGE):
    if _BACKEND == "remote":
        r = RemoteBackend.post("/process/ensure", {"addresses": list(addresses), "package": package}, timeout=900)
        if not r.get("ok"):
            raise RuntimeError(f"远端 ensure_process 失败：{r}")
        return r
    return ensure_process(addresses, package=package)


def be_shutdown_instances(indices):
    if _BACKEND == "remote":
        for idx in indices:
            RemoteBackend.post("/instance/shutdown", {"idx": int(idx)}, timeout=120)
        return
    return shutdown_instances(indices)


def be_restart_instance(adb, address, idx, package, boot_come_up=180):
    if _BACKEND == "remote":
        r = RemoteBackend.post("/instance/restart",
                               {"idx": int(idx), "address": address, "package": package}, timeout=500)
        return bool(r.get("ok"))
    return _restart_instance(adb, address, idx, package, boot_come_up)


def be_adb_connect(address):
    """remote 下确保某地址在远端 5038 server 已 connect（connect_all 找不到该角色时兜底）。"""
    if _BACKEND == "remote":
        RemoteBackend.post("/adb/connect", {"address": address}, timeout=20)


def main(mode="full", solo_tasks=None, solo_ids=None):
    open_log()  # 先开日志：后续所有 print 自动加 [时间戳] 前缀并 tee 到 DEBUG_DIR/run_5r/
    if _CONFIG_NAME:
        print(f"=== 当前配置覆盖：--config {_CONFIG_NAME}（本配置文件 > run_5r 默认）===")
    _assert_repo()
    # 探测远端 mumu_server：healthy（且非本机）→ remote（Mac 期望路径，经 Win:5038 adb + Win:5080 生命周期
    # API）；否则 local（Win 直达，沿用旧行为）。详见 plan §C。Mac 上 WIN_IP 非本机 → 探到即 remote；
    # Win 上 WIN_IP 是本机 → 永远 local（同机保护，避免 Win 直达 5r 误走自己的 mumu_server）。
    global _BACKEND
    _BACKEND = _detect_backend()
    if _BACKEND == "remote":
        os.environ["ADB_SERVER_SOCKET"] = f"tcp:{WIN_IP}:{ADB_SERVER_PORT}"
        print(f"=== 探测到 mumu_server（{WIN_IP}:{MUMU_API_PORT}），使用远端 MuMu + 远端 adb（{WIN_IP}:{ADB_SERVER_PORT}）===")
    else:
        print(f"=== mumu_server 不可达或为本机，使用本地 MuMu + 本地 adb ===")
    roles = ROLES
    package = PACKAGE
    timeouts = TIMEOUTS
    member_names = [r for r in roles if r != "队长"]

    # 生命周期：确保 ROLES 里的实例都启动到 start_finished（幂等；未启动的用
    # ``control launch -pkg`` 拉起并自动开游戏）
    role_idx = be_role_indices(roles)
    print("=== 启动/检查 MuMu 实例 ===")
    be_ensure_instances(sorted(role_idx.values()), package=package)

    # 在交给 Maa 的 start 之前，由 Python 侧显式确认游戏进程稳态存活（双重检查 + 重试）。
    # ensure_instances 的 -pkg 虽顺带拉游戏，但不保证进程稳定；这里 adb 兜底，缺一台即中止整轮。
    print("=== 确认游戏进程存活（双重检查 + 重试）===")
    be_ensure_process(list(roles.values()), package=package)

    print("=== 连接 5 设备（共享 Resource）===")
    taskers = connect_all(roles, package=package)

    if mode in ("full", "launch", "team", "form"):
        # 并行启动、每个相隔 LAUNCH_STAGGER 秒（错峰）。设 0 即齐发。各账号独立 Resource，
        # 已无 OCR 并发竞态；错峰仅为平滑宿主负载（避免 5 个 StartApp/游戏同时拉起压满 CPU/磁盘）。
        # wall-clock 从 N×单账号降到 stagger×(N-1) + 单账号。
        print(f"=== 启动 {len(taskers)} 个账号（并行，每个间隔 {LAUNCH_STAGGER}s）===")
        launch_parallel(taskers, package=package, timeout=timeouts["start"], stagger=LAUNCH_STAGGER)

    if mode == "zhuagui":
        # 队长单人无限捉鬼：只需队长登录到主界面（关闭人员检测 → 不要求队伍满员），
        # 只 launch 队长一人，不起其余账号。
        print("=== 启动队长（登录到主界面）===")
        launch(taskers["队长"], package=package, timeout=timeouts["start"],
               address=ROLES["队长"])
        print("=== 队长无限捉鬼（关闭人员检测-不进入轮次选择）===")
        zhuagui(taskers)

    if mode in ("full", "team", "form"):
        print("=== 组队（建队 + 逐个邀请）===")
        team_form(taskers, member_names, timeouts=timeouts)

    if mode in ("full", "team", "run", "fuben"):
        print("=== 执行 5 本副本 + 捉鬼 + 解散 ===")
        team_run(taskers, member_names, timeouts=timeouts)

    # form 模式：组队测试到此为止。停掉队员自动化（角色仍留队伍）、回收线程池，
    # 否则非守护工作线程会阻塞进程退出。
    if mode == "form":
        _stop_members(taskers, member_names)
        print("=== 组队测试完成（队员仍在队伍中，已停止其自动化）===")

    if mode in ("full", "solo"):
        # solo 任务来源优先级：命令行指定 > 头部 SOLO_ENTRIES
        solo_entries = solo_tasks or SOLO_ENTRIES
        print("=== 并行单人 pipeline ===")
        solo_all(taskers, entries=solo_entries, ids=solo_ids,
                 per_timeout=timeouts["solo"], overall_timeout=timeouts["solo_overall"],
                 solo_timeouts={e: timeouts.get(e, timeouts["solo"])
                                for e in solo_entries if e in timeouts})

    # full 模式跑完
    if mode == "full":
        # to_stop = sorted(i for r, i in role_idx.items() if r != "队长")
        to_stop = sorted(i for r, i in role_idx.items())
        if to_stop:
            # 先释放全部 MaaFw 对象（让 controller 在设备仍存活时清理 /data/local/tmp 临时文件），
            # 再关实例。否则实例已关、controller 析构时 adb shell rm 打死设备 → 一串 [ERR] 噪音。
            taskers.clear()
            gc.collect()
            print("=== 收尾：关闭 MuMu 实例 ===")
            be_shutdown_instances(to_stop)

    print("=== 全部完成 ===")


# ============================================================================
# HTTP 服务模式 + remote 客户端（plan §E/F/G）。stdlib http.server，零新依赖。
# 仿 maa_cli.py 的 _send_json/_send_error/_read_body_json helper 风格。
# ============================================================================

# ---- 通用 JSON helper（两个 server 共用）----
def _send_json(h, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def _send_err(h, code, msg):
    _send_json(h, {"error": msg}, code)


def _read_body_json(h):
    try:
        n = int(h.headers.get("Content-Length", 0))
        raw = h.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except Exception:
        return {}


def _auth_ok(h):
    if not SHARED_TOKEN:
        return True
    if h.headers.get("Authorization", "") == "Bearer " + SHARED_TOKEN:
        return True
    _send_err(h, 401, "unauthorized")
    return False


def _log_message_silent(h, *a):
    """静默 BaseHTTPRequestHandler 的默认访问日志（自己 print 关键步骤即可）。"""
    pass


# ============================================================================
# mumu_server（Win）：专用 0.0.0.0 adb server + MuMu 生命周期 HTTP API。不加载 Maa 资源。
# ============================================================================
_INSTANCE_LOCKS = {}   # idx -> threading.Lock（变更单实例的端点持锁，防并发双启）


def _inst_lock(idx):
    return _INSTANCE_LOCKS.setdefault(int(idx), threading.Lock())


def _dedicated_adb():
    """专用 0.0.0.0 adb server 用的 adb 二进制。**必须是 ≥37 的 platform-tools adb**——MuMu 自带的
    36.0.0 不认 ``-a``、只能绑 127.0.0.1，Mac 经 Tailscale 够不到。env MAA_5R_DEDICATED_ADB 优先；
    再查常见位置（PoC 下载点 / ~/platform-tools）；都找不到回退 MuMu 自带（仅本机可用，远端会失败）。"""
    for cand in (os.environ.get("MAA_5R_DEDICATED_ADB"),
                 r"C:\dev\platform-tools\adb.exe",
                 os.path.join(os.path.expanduser("~"), "platform-tools", "adb.exe")):
        if cand and os.path.isfile(cand):
            return cand
    return _adb_path()   # MuMu 自带（回退；远端不可用，run_mumu_server 会告警）


def _dedicated_bind_ok():
    """专用 adb server 是否绑了 0.0.0.0:ADB_SERVER_PORT（而非仅 127.0.0.1）。Win 用 netstat 探测。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=8).stdout
        return any(f"0.0.0.0:{ADB_SERVER_PORT}" in ln and "LISTENING" in ln for ln in out.splitlines())
    except Exception:
        return True   # 探测失败不阻断（非 Win 等）


def _dedicated_adb_up():
    """专用 5038 adb server 是否在跑（adb -P 5038 devices 能回设备列表）。"""
    try:
        out = subprocess.run([_dedicated_adb(), "-P", str(ADB_SERVER_PORT), "devices"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=8).stdout
        return "List of devices" in out
    except Exception:
        return False


def _connect_roles_into_dedicated():
    """把所有 ROLES 端口 connect 进专用 5038 server，让 Mac find_adb_devices 能见到 127.0.0.1:16xxx。"""
    for addr in ROLES.values():
        try:
            subprocess.run([_dedicated_adb(), "-P", str(ADB_SERVER_PORT), "connect", addr],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        except Exception:
            pass


def _start_dedicated_adb():
    """启动专用 0.0.0.0:ADB_SERVER_PORT adb server（幂等）。**只认 0.0.0.0**——只绑 127.0.0.1
    （Mac 经 Tailscale 够不到，等同没起）视为未启动；返回值 = 是否成功绑 0.0.0.0。

    已起且绑 0.0.0.0 → 复用；已起但只绑 127.0.0.1（旧 adb / 残留 / 裸 adb 探活复活）→ kill
    后用现代 adb 重绑；start-server 后仍非 0.0.0.0 → 再 kill 重试一次（旧 MuMu adb 36.0.0
    的 -a 失效、或 start-server 竞态）。"""
    for _attempt in (1, 2):
        if _dedicated_adb_up() and not _dedicated_bind_ok():
            print(">>> 专用 5038 仅绑 127.0.0.1（残留 / 裸 adb 探活复活），kill 后用 -a 重绑 0.0.0.0")
            _stop_dedicated_adb()
            time.sleep(0.8)
        if _dedicated_bind_ok():
            break
        try:
            # -a 绑 0.0.0.0（需 ≥37 的 platform-tools adb；MuMu 36.0.0 不认）；start-server 自守护。
            subprocess.run([_dedicated_adb(), "-a", "-P", str(ADB_SERVER_PORT), "start-server"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        except Exception as e:
            print(f"!! 启动专用 adb server 异常：{e}")
        time.sleep(1.0)
    _connect_roles_into_dedicated()
    return _dedicated_bind_ok()


def _stop_dedicated_adb():
    """只 kill 专用 5038 server（绝不碰 MuMu 自管的 5037）。"""
    try:
        subprocess.run([_dedicated_adb(), "-P", str(ADB_SERVER_PORT), "kill-server"],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def _client_gone_watcher(conn, cancel, poll=2.0):
    """后台线程：HTTP 客户端断连即 set(cancel)。``conn`` 为 handler 的 ``self.connection``。

    用 select 监听连接可读事件——客户端在等响应期间不发数据，故"可读"只意味着对端 orderly
    close（recv 返回 ``b""``）或连接被重置（抛 ConnectionResetError/BrokenPipeError），两者都判
    定为断连。``mumu_server`` 的 ensure_instances/ensure_process 是长阻塞且不感知客户端断开
    （ThreadingHTTPServer 每请求一线程、远端 cancel 只杀 Mac 子进程不断 mumu_server 的连接上下文），
    若不在客户端断开后中止，会留下"鬼魂 ensure"继续 launch/retry 实例。"""
    try:
        while not cancel.is_set():
            r, _, _ = select.select([conn], [], [], poll)
            if not r:
                continue
            try:
                data = conn.recv(1, socket.MSG_PEEK)
            except (ConnectionResetError, BrokenPipeError, OSError):
                cancel.set(); return
            if data == b"":           # 对端 orderly close（FIN）
                cancel.set(); return
            # 非空 = 客户端又发了字节（keep-alive 下一请求等），不算断连，继续监听
    except Exception:
        pass


class _MuMuHandler(BaseHTTPRequestHandler):
    log_message = _log_message_silent

    def _guarded(self, func):
        """在"客户端断连即取消"保护下运行 ``func(cancel)``，返回其返回值。

        起一个 daemon watcher 监听本连接，客户端断开即 set(cancel)；``func``（如
        ``ensure_instances``/``ensure_process``）在长循环里据此尽早返回。finally 里 set(cancel)
        让 watcher 退出，不留线程。仅用于长阻塞端点（/instance/ensure、/process/ensure）。"""
        cancel = threading.Event()
        t = threading.Thread(target=_client_gone_watcher, args=(self.connection, cancel), daemon=True)
        t.start()
        try:
            return func(cancel)
        finally:
            cancel.set()

    def do_GET(self):
        if not _auth_ok(self):
            return
        parts = urllib.parse.urlparse(self.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        try:
            if path == "/health":
                # 自愈：仅"5038 在跑"不够——还须绑了 0.0.0.0。落到 127.0.0.1 时 _dedicated_adb_up
                # 仍为 True（本机 adb 够得到），但 Mac 经 Tailscale 够不到、SYN 被默认 Block 静默
                # 丢弃（~75s 超时）。故把 bind 校验并进"已健康"快路径；bind 不达标即落到
                # _start_dedicated_adb 重建（kill loopback-only 残留 + 用 -a 重绑 0.0.0.0）。
                adb_up = (_dedicated_adb_up() and _dedicated_bind_ok()) or _start_dedicated_adb()
                mumu_ok = True
                try:
                    mumu_info("0")
                except Exception:
                    mumu_ok = False
                _send_json(self, {"ok": True, "adb_port": ADB_SERVER_PORT, "adb_up": adb_up, "mumu": mumu_ok})
            elif path == "/mumu/info":
                indices = query.get("indices", ["all"])[0]
                _send_json(self, mumu_info(indices))
            elif path == "/process/alive":
                addr = query.get("address", [None])[0]
                if not addr:
                    _send_err(self, 400, "missing address"); return
                _send_json(self, {"alive": _game_process_alive(_adb_path(), addr)})
            elif path in ("", "/", "/help"):
                _send_json(self, {"endpoints": [
                    "GET /health", "GET /mumu/info?indices=all", "GET /process/alive?address=",
                    "POST /instance/{launch,shutdown,restart,ensure}", "POST /process/ensure",
                    "POST /adb/connect", "POST /adb/server"]})
            else:
                _send_err(self, 404, f"unknown path {path}")
        except Exception as e:
            _send_err(self, 500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        if not _auth_ok(self):
            return
        path = urllib.parse.urlparse(self.path).path
        body = _read_body_json(self)
        try:
            if path == "/instance/launch":
                idx, pkg = int(body["idx"]), body.get("package", PACKAGE)
                with _inst_lock(idx):
                    _launch_one(idx, pkg)
                _send_json(self, {"ok": True})
            elif path == "/instance/shutdown":
                idx = int(body["idx"])
                with _inst_lock(idx):
                    _shutdown_one(idx)
                _send_json(self, {"ok": True})
            elif path == "/instance/restart":
                idx, addr = int(body["idx"]), body["address"]
                pkg = body.get("package", PACKAGE)
                with _inst_lock(idx):
                    ok = _restart_instance(_adb_path(), addr, idx, pkg)
                _send_json(self, {"ok": ok})
            elif path == "/instance/ensure":
                indices = sorted(set(int(i) for i in body["indices"]))
                self._guarded(lambda c: ensure_instances(indices, package=body.get("package", PACKAGE), cancel=c))
                _send_json(self, {"ok": True, "ready": indices})
            elif path == "/process/ensure":
                self._guarded(lambda c: ensure_process(list(body["addresses"]), package=body.get("package", PACKAGE), cancel=c))
                _send_json(self, {"ok": True})
            elif path == "/adb/connect":
                # 用专用 server 端口 connect（不走默认 5037）：把 address 注册进 5038 的设备表。
                subprocess.run([_dedicated_adb(), "-P", str(ADB_SERVER_PORT), "connect", body["address"]],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
                _send_json(self, {"ok": True})
            elif path == "/adb/server":
                action = body.get("action")
                if action == "stop":
                    _stop_dedicated_adb(); _send_json(self, {"ok": True, "port": ADB_SERVER_PORT})
                else:
                    ok = _start_dedicated_adb(); _send_json(self, {"ok": ok, "port": ADB_SERVER_PORT})
            else:
                _send_err(self, 404, f"unknown path {path}")
        except Exception as e:
            _send_err(self, 500, f"{type(e).__name__}: {e}")


def run_mumu_server(argv=None):
    """mumu_server 模式：起专用 adb server + HTTP API（不加载 Maa 资源）。"""
    print(f"=== mumu_server：专用 adb server 0.0.0.0:{ADB_SERVER_PORT} + HTTP API 0.0.0.0:{MUMU_API_PORT} ===")
    # **只以 0.0.0.0 启动，拒绝本地模式**：专用 adb 必须绑 0.0.0.0，Mac 经 Tailscale 才够得到。
    # 绑不成 0.0.0.0（_dedicated_adb 落到 MuMu 自带旧 adb 36.0.0、-a 失效只绑 127.0.0.1）→ 直接
    # 退出，绝不带病上线——否则 /health 假报 adb_up、Mac 远程全 ~75s 超时（见 memory pitfall #5）。
    if not _start_dedicated_adb():
        print(f"!! 专用 adb server 未能绑 0.0.0.0:{ADB_SERVER_PORT}（仍 127.0.0.1 或未起）——拒绝以本地模式启动。")
        print("!!   多半 _dedicated_adb 用了 MuMu 自带旧 adb（36.0.0，-a 失效）。")
        print(f"!!   设 MAA_5R_DEDICATED_ADB=<≥37 的 platform-tools/adb.exe>（默认 C:\\dev\\platform-tools\\adb.exe）后重启 mumu_server。")
        return 1
    print(f"<<< 专用 adb server 就绪 0.0.0.0:{ADB_SERVER_PORT}（拒绝本地模式；不碰 MuMu 5037）")
    httpd = ThreadingHTTPServer(("0.0.0.0", MUMU_API_PORT), _MuMuHandler)
    print(f"<<< mumu_server 就绪：HTTP 0.0.0.0:{MUMU_API_PORT}（Mac 经此 + adb:5038 远控；Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("=== 收到 Ctrl+C，关闭 mumu_server ===")
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        print("=== 关闭专用 adb server（仅 5038，不碰 MuMu 5037）===")
        _stop_dedicated_adb()
    return 0


# ============================================================================
# cli_server（Mac）：HTTP API 接收 CLI 指令；每个 /run 起独立子进程跑 main()，
# cancel = 杀子进程 = OS 回收一切（MaaFw/线程/adb 连接），等价"像退出进程一样清理干净"。
# ============================================================================
_JOBS = {}                # job_id -> {state, exit, lines, lock, started, ended, mode, proc, cancel_requested}
_JOBS_LOCK = threading.Lock()
_CURRENT_JOB_ID = None    # 单飞：一次只一个 running job（五开设备共享资源）


def _new_job(mode):
    return {"state": "running", "exit": None, "lines": [], "lock": threading.Lock(),
            "started": time.time(), "ended": None, "mode": mode, "proc": None, "cancel_requested": False}


def _spawn_job(job_id, body):
    """子进程跑 ``python run_5r.py <mode> [args]``；stdout 逐行读进 job['lines']。"""
    mode = body.get("mode", "full")
    cmd = [sys.executable, os.path.abspath(__file__), mode]
    if body.get("config"):
        cmd += ["--config", str(body["config"])]
    if mode == "solo":
        if body.get("solo_ids"):
            cmd += ["--ids", ",".join(str(i) for i in sorted(body["solo_ids"]))]
        if body.get("solo_tasks"):
            cmd += list(body["solo_tasks"])
    elif mode == "log_analysis":
        if body.get("date"):
            cmd += [str(body["date"])]
    print(f"    [{job_id}] spawn: {' '.join(cmd)}  (cwd={REPO_DIR})")
    proc = subprocess.Popen(cmd, cwd=REPO_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1, text=True, encoding="utf-8", errors="replace")
    with _JOBS_LOCK:
        _JOBS[job_id]["proc"] = proc

    def _reader():
        for line in proc.stdout:
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is None:
                    break
                job["lines"].append({"i": len(job["lines"]), "t": line.rstrip("\n")})
        rc = proc.wait()
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None:
                job["exit"] = rc
                job["ended"] = time.time()
                if job["state"] == "running":   # 未被 cancel 预置
                    job["state"] = "done" if rc == 0 else "failed"
                global _CURRENT_JOB_ID
                if _CURRENT_JOB_ID == job_id:
                    _CURRENT_JOB_ID = None
            print(f"    [{job_id}] 子进程退出 rc={rc} → state={job['state'] if job else '?'}")

    threading.Thread(target=_reader, daemon=True).start()


def _cancel_job(job_id):
    """terminate→grace 5s→kill。OS 回收子进程的 MaaFw/线程/adb 连接。幂等。"""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return "not_found"
        if job["state"] != "running":
            return job["state"]
        job["cancel_requested"] = True
        proc = job["proc"]
    if proc is None:
        return "cancelled"
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    with _JOBS_LOCK:
        job["state"] = "cancelled"
        job["ended"] = time.time()
        if job["exit"] is None:
            job["exit"] = proc.returncode if proc.returncode is not None else -15
        global _CURRENT_JOB_ID
        if _CURRENT_JOB_ID == job_id:
            _CURRENT_JOB_ID = None
    return "cancelled"


class _CliHandler(BaseHTTPRequestHandler):
    log_message = _log_message_silent

    def do_GET(self):
        if not _auth_ok(self):
            return
        parts = urllib.parse.urlparse(self.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        try:
            if path == "/health":
                with _JOBS_LOCK:
                    busy = _CURRENT_JOB_ID is not None
                _send_json(self, {"ok": True, "busy": busy, "current_job": _CURRENT_JOB_ID})
            elif path == "/jobs":
                with _JOBS_LOCK:
                    lst = [{"job_id": jid, "state": j["state"], "mode": j["mode"],
                            "started": j["started"], "ended": j["ended"], "exit": j["exit"]}
                           for jid, j in _JOBS.items()]
                _send_json(self, {"jobs": lst})
            elif path == "/show":
                # 当前（或最近）任务 + 最近 50 行日志尾
                with _JOBS_LOCK:
                    jid = _CURRENT_JOB_ID or (sorted(_JOBS)[-1] if _JOBS else None)
                    if not jid:
                        _send_json(self, {"ok": True, "running": False, "job_id": None, "lines": []})
                        return
                    j = _JOBS[jid]
                    tail = [ln["t"] for ln in j["lines"][-50:]]
                    _send_json(self, {"ok": True, "running": j["state"] == "running", "job_id": jid,
                                      "state": j["state"], "mode": j["mode"], "exit": j["exit"],
                                      "started": j["started"], "ended": j["ended"], "lines": tail})
            elif path.startswith("/jobs/") and path.endswith("/status"):
                jid = path[len("/jobs/"):-len("/status")]
                with _JOBS_LOCK:
                    j = _JOBS.get(jid)
                    if not j:
                        _send_err(self, 404, "job not found"); return
                    _send_json(self, {"job_id": jid, "state": j["state"], "exit": j["exit"],
                                      "started": j["started"], "ended": j["ended"], "mode": j["mode"]})
            elif path.startswith("/jobs/") and path.endswith("/logs"):
                jid = path[len("/jobs/"):-len("/logs")]
                since = int(query.get("since", ["0"])[0])
                with _JOBS_LOCK:
                    j = _JOBS.get(jid)
                    if not j:
                        _send_err(self, 404, "job not found"); return
                    lines = [ln for ln in j["lines"] if ln["i"] >= since]
                body = "\n".join(json.dumps(ln, ensure_ascii=False) for ln in lines)
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path in ("", "/", "/help"):
                _send_json(self, {"endpoints": [
                    "POST /run {mode,solo_tasks?,solo_ids?}", "GET /jobs/<id>/status",
                    "GET /jobs/<id>/logs?since=N", "POST /jobs/<id>/cancel", "GET /jobs", "GET /health"]})
            else:
                _send_err(self, 404, f"unknown path {path}")
        except Exception as e:
            _send_err(self, 500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        if not _auth_ok(self):
            return
        path = urllib.parse.urlparse(self.path).path
        body = _read_body_json(self)
        try:
            if path == "/run":
                global _CURRENT_JOB_ID
                with _JOBS_LOCK:
                    if _CURRENT_JOB_ID is not None:
                        _send_err(self, 409, f"busy: job {_CURRENT_JOB_ID} running；先 POST /jobs/{_CURRENT_JOB_ID}/cancel")
                        return
                mode = body.get("mode", "full")
                if mode not in ("full", "launch", "team", "form", "run", "fuben", "zhuagui", "solo", "log_analysis"):
                    _send_err(self, 400, f"bad mode {mode}"); return
                job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                with _JOBS_LOCK:
                    _JOBS[job_id] = _new_job(mode)
                    _CURRENT_JOB_ID = job_id
                print(f">>> 接收任务 job_id={job_id} mode={mode}")
                _spawn_job(job_id, body)
                _send_json(self, {"job_id": job_id, "status_url": f"/jobs/{job_id}/status",
                                  "logs_url": f"/jobs/{job_id}/logs"}, code=202)
            elif path.startswith("/jobs/") and path.endswith("/cancel"):
                jid = path[len("/jobs/"):-len("/cancel")]
                state = _cancel_job(jid)
                if state == "not_found":
                    _send_err(self, 404, "job not found"); return
                _send_json(self, {"ok": True, "state": state})
            else:
                _send_err(self, 404, f"unknown path {path}")
        except Exception as e:
            _send_err(self, 500, f"{type(e).__name__}: {e}")


def run_cli_server(argv=None):
    """cli_server 模式：HTTP API 接收 CLI 指令，子进程跑 main()（Maa 资源在此加载）。"""
    httpd = ThreadingHTTPServer(("0.0.0.0", CLI_API_PORT), _CliHandler)
    print(f"<<< cli_server 就绪：HTTP 0.0.0.0:{CLI_API_PORT}（POST /run 起子进程；Ctrl+C 退出）")
    print(f"    Win 侧用 `python run_5r.py remote full` 提交；GET /jobs/<id>/logs 轮询日志")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("=== 收到 Ctrl+C，关闭 cli_server ===")
    finally:
        # server 退出时终止任何 running 子进程，不留孤儿（像退出进程一样清理干净）
        with _JOBS_LOCK:
            running = [jid for jid, j in _JOBS.items() if j["state"] == "running"]
        for jid in running:
            print(f"=== 退出清理：cancel running job {jid} ===")
            _cancel_job(jid)
        try:
            httpd.shutdown()
        except Exception:
            pass
    return 0


# ============================================================================
# remote 客户端（Win→Mac）：POST /run + 轮询 logs/status，复用 _parse_ids_flag。
# ============================================================================
def _remote_client(argv):
    import requests   # 客户端侧（已在 requirements）；懒加载，server 端不依赖
    base = os.environ.get("MAA_5R_CLI_URL", f"http://{MAC_IP}:{CLI_API_PORT}")
    headers = {"Authorization": "Bearer " + SHARED_TOKEN} if SHARED_TOKEN else {}
    # 扫 argv 里的 --config；没有则回退本进程启动时的 _CONFIG_NAME（--config 可能写在
    # `remote` 之前，已被顶部 _split_config 抽进 _CONFIG_NAME，这里补回以透传给 Mac 子进程）。
    config_name, args = _split_config(list(argv or []))
    if not config_name:
        config_name = _CONFIG_NAME
    mode = args[0] if args else "full"

    # ---- 显式取消：remote cancel —— 取消 cli_server 当前在跑的 job（job 在 Mac，独立于本客户端）----
    if mode == "cancel":
        try:
            h = requests.get(base + "/health", headers=headers, timeout=10).json()
        except Exception as e:
            print(f"!! 连不上 cli_server（{base}）：{e}"); return 1
        jid = h.get("current_job")
        if not jid:
            print("（cli_server 当前无任务在跑）"); return 0
        print(f">>> 取消当前任务 job_id={jid}")
        try:
            c = requests.post(f"{base}/jobs/{jid}/cancel", headers=headers, timeout=15)
            ok = c.status_code == 200 and c.json().get("ok", False)
            print(f"    cancel HTTP {c.status_code} → {'成功' if ok else c.text}")
            if not ok:
                return 1
        except Exception as e:
            print(f"!! 远程 cancel 发送失败：{e}"); return 1
        try:
            time.sleep(3)   # 等 cancel 落地（子进程被 SIGTERM/SIGKILL + 槽位释放）
            s = requests.get(f"{base}/jobs/{jid}/status", headers=headers, timeout=10).json()
            print(f"=== job 终态：{s.get('state')} exit={s.get('exit')} ===")
        except Exception:
            pass
        return 0

    # ---- show：服务端当前在跑的任务 + 日志尾 ----
    if mode == "show":
        try:
            sh = requests.get(base + "/show", headers=headers, timeout=10).json()
        except Exception as e:
            print(f"!! 连不上 cli_server（{base}）：{e}"); return 1
        print(f"=== cli_server 任务（job={sh.get('job_id')} state={sh.get('state')} mode={sh.get('mode')} "
              f"running={sh.get('running')} exit={sh.get('exit')}）===")
        for t in sh.get("lines", []):
            print(t)
        if not sh.get("lines"):
            print("（无日志）")
        return 0

    # ---- 提交任务 + 轮询日志 ----
    body = {"mode": mode}
    if config_name:
        body["config"] = config_name
    if mode == "solo":
        ids, tasks = _parse_ids_flag(args[1:])
        if ids:
            body["solo_ids"] = sorted(ids)
        if tasks:
            body["solo_tasks"] = tasks
    elif mode == "log_analysis":
        if len(args) > 1:
            body["date"] = args[1]
    print(f">>> 远程提交 {body} → {base}/run")
    try:
        r = requests.post(base + "/run", json=body, headers=headers, timeout=30)
    except Exception as e:
        print(f"!! 连不上 cli_server（{base}）：{e}"); return 1
    if r.status_code == 409:
        print(f"!! cli_server 忙：{r.text}"); return 1
    if r.status_code != 202:
        print(f"!! /run 失败 {r.status_code}：{r.text}"); return 1
    job_id = r.json()["job_id"]
    print(f"<<< 已提交 job_id={job_id}；轮询日志（Ctrl+C 断开，**任务继续在 Mac 跑**；取消用 `remote cancel`）...")
    since = 0
    try:
        while True:
            try:
                lr = requests.get(f"{base}/jobs/{job_id}/logs",
                                  params={"since": since}, headers=headers, timeout=10)
                for line in lr.text.splitlines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                        print(obj.get("t", ""))
                        since = max(since, int(obj.get("i", since)) + 1)
                    except Exception:
                        pass
            except Exception as e:
                print(f"!! 拉日志失败（继续）：{e}")
            try:
                s = requests.get(f"{base}/jobs/{job_id}/status", headers=headers, timeout=10).json()
            except Exception as e:
                print(f"!! 查状态失败（继续）：{e}"); time.sleep(2); continue
            st = s.get("state")
            if st in ("done", "failed", "cancelled", "not_found"):
                print(f"=== job 终态：{st} exit={s.get('exit')} ===")
                return 0 if st == "done" else 1
            time.sleep(2)
    except KeyboardInterrupt:
        # 客户端断开即可——任务在 Mac 的 cli_server 子进程里继续跑，不受本客户端生命周期影响。
        print("\n=== 已断开（任务继续在 Mac 跑）；如需取消：python run_5r.py remote cancel ===")
        return 0


if __name__ == "__main__":
    _args = _ARGV_REST   # sys.argv[1:] 已去掉 --config 对（见模块顶部 _split_config）
    _mode = _args[0] if _args else "full"
    # 服务/客户端模式：在 TerminateProcess try 之前分流，各自独立退出（不经那套 finally）
    if _mode in ("-h", "--help", "help"):
        print_help(); sys.exit(0)
    if _mode == "log_analysis":
        print_log_analysis(_args[1] if len(_args) > 1 else None); sys.exit(0)
    if _mode == "mumu_server":
        sys.exit(run_mumu_server(_args[1:]))
    if _mode == "cli_server":
        sys.exit(run_cli_server(_args[1:]))
    if _mode == "remote":
        sys.exit(_remote_client(_args[1:]))
    _exit_code = 0
    try:
        if _mode in ("full", "launch", "team", "form", "run", "fuben", "zhuagui"):
            main(_mode)
        elif _mode == "solo":
            _ids, _tasks = _parse_ids_flag(_args[1:])
            main("solo", solo_tasks=_tasks or None, solo_ids=_ids)
        else:
            print(f"未知模式: {_mode}")
            print_help()
    except BaseException:
        # BaseException 连 KeyboardInterrupt 一起抓，便于区分"用户 Ctrl+C"和"native 崩溃"
        print("=== !!! main 未捕获异常 ===")
        traceback.print_exc()
        _exit_code = 1
    finally:
        # 关键诊断信号：若输出里【看不到】下面这行，说明进程是被 native 崩溃（段错误/abort）
        # 直接杀掉的，Python 的 except/finally 都跑不到——此时去 stderr 找 faulthandler 打的栈。
        #
        # 强制 TerminateProcess（**不是** os._exit！）：Windows 上 os._exit → ExitProcess 仍会跑
        # DLL_PROCESS_DETACH，而 MaaFw/onnxruntime 这类带后台线程的 native 库的 detach 会卡住，
        # 导致进程吊着不真死、仍持有自己的 stdout 管道 → 父进程（autolife）的 stdout 读永远等不到
        # EOF → 撑到 4h wait_for 超时（这就是上次"4 小时才报超时"的真根因）。
        # TerminateProcess 直接结束进程、跳过 DLL detach：进程瞬死、句柄全释放、父进程立刻拿 EOF。
        # 日志已逐行 flush（_log_print flush=True + 文件行缓冲），不会丢；MaaFw 的 Python 级资源
        # 已在上游 taskers.clear()+gc.collect() 释放过，跳过 detach 不漏。
        if _LOG_FILE is not None:
            try:
                _LOG_FILE.flush()
            except Exception:
                pass
        if _IS_WINDOWS:
            print("=== main 结束，TerminateProcess 强杀进程（跳过 DLL detach）===")
            try:
                import ctypes
                _k32 = ctypes.windll.kernel32
                _k32.GetCurrentProcess.restype = ctypes.c_void_p
                _k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                _k32.TerminateProcess(_k32.GetCurrentProcess(), _exit_code)
            except Exception:
                os._exit(_exit_code)   # ctypes 失败兜底（理论上到不了这里）
        else:
            # Mac/非 Win：无 DLL detach 卡死问题，正常退出（cli_server 子进程靠此干净收尾）。
            sys.exit(_exit_code)
