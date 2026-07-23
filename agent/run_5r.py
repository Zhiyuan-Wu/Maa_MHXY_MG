"""五开简单编排 —— 单进程、每账号独立 Resource、全用原生 pipeline、各步墙钟超时。

参考 ``maa_daily_gift.py`` 的命令式 ``post_task`` 范式，不做任何自定义原子操作/God's eye。
所有逻辑都复用仓库现有 pipeline：start / chuangjianduiwu / 5R_duizhang / 5R_duiyuan /
fuben115 / 5R_duizhang_TR / 各种单人任务（运镖、宝图、每日签到…）。

五个能力：
  1. launch(tasker)              启动一个账号（任意状态→主界面），用原生 start
  2a. team_form(taskers, ...)    组队：逐个邀请（队长每次跑 5R_duizhang，override 邀请名+
                                  向下滑动 max_hit=1，每次只邀请一人、首次自动建队；对应队员
                                  submit 5R_duiyuan，future 存全局 _TEAM_MEMBER_FUTS）
  2b. team_run(taskers, ...)     执行副本+解散：队长 fuben_entry→5R_duizhang_TR；停掉队员并回收 future
  3. solo_all(taskers, entries)  并行执行单人 pipeline（每个账号内顺序跑多个 entry）
  4. zhuagui(taskers)            队长单人无限捉鬼：原生 zhuoguirenwu + ZHUAGUI_OVERRIDE
                                  （关闭人员检测-不进入轮次选择，[JumpBack]钟馗-捉鬼任务-循环 续接）
  5. main()                      串起：ensure 实例→连接→启动5个→组队→副本+解散→并行单人
                                  （full 模式跑完会关闭除队长外的实例，释放内存）

自包含 & 可移动：
  - 角色名/地址、包名、副本 entry、超时、单人任务列表等**全部内联在脚本头部「配置区」**，
    不再读 config/5r_roles.json。MaaFw 的 DLL 由 pip 包自带，**无需把 deps/bin 放进 PATH**。
  - 脚本可放在任意目录；移走后只需把头部 ``REPO_DIR`` 改成 Maa_MHXY_MG 仓库根目录
    （须含 assets/ agent/）。换机器再改 ``MUMU_MANAGER``。

用法（任意目录，Python313）::
    python run_5r.py                        # 全流程
    python run_5r.py launch                 # 只启动+登录 5 个账号
    python run_5r.py team                   # 只组队副本
    python run_5r.py solo                   # 并行单人（头部 SOLO_ENTRIES）
    python run_5r.py solo baotu_renwu       # 并行单人，只跑指定任务（宝图）
    python run_5r.py solo baotu_renwu yunbiao_renwu2   # 只跑指定的几个
    python run_5r.py zhuagui                # 队长单人无限捉鬼（关闭人员检测-不进入轮次选择）
    python run_5r.py help                   # 查看常用单人任务名
"""
import builtins
import faulthandler
import gc
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from maa.toolkit import Toolkit
from maa.resource import Resource
from maa.controller import AdbController
from maa.tasker import Tasker

# native 崩溃（段错误 / abort / 访问违例）时，把出错线程的 Python 栈打到 stderr。
# 用于定位"进程静默退出、无任何 traceback"的情况——多半是 C++ 侧（MaaFw/OCR 模型）
# 崩了，Python 的 except/finally 根本跑不到。不加这个，这种死法完全无线索。
faulthandler.enable(all_threads=True)

# ==================== 配置区（移动脚本 / 换机器时改这里）====================
# 仓库根目录（须含 assets/ agent/）。脚本可放在任意位置——移走后只改这一行。
REPO_DIR = r"C:\dev\Maa_MHXY_MG"

# 五开角色 → MuMu 实例 ADB 地址。MuMu12 约定 adb_port = 16384 + 32*index，
# 故 index = (port - 16384) // 32；ROLES 的顺序即账号编号顺序（solo --ids 用，从 1 起）。
ROLES = {
    "队长": "127.0.0.1:16512", #id=4 title=3
    "渣中": "127.0.0.1:16576", #id=6 title=5
    "6130": "127.0.0.1:16544", #id=5 title=4
    "缤纷": "127.0.0.1:16448", #id=2 title=1
    "晚风": "127.0.0.1:16480", #id=3 title=2
}
PACKAGE        = "com.netease.my"      # 游戏包名（仓库 start.json 里的 myq 是错的，用这个）
FUBEN_ENTRY    = "fuben115"            # 组队副本 entry（base 即 全自动 接双侠士路线，无需 option）
# 副本完成、桥接捉鬼后跑几轮鬼。注意 **实际轮数 = max_hit + 1**：首轮由入口 钟馗-捉鬼任务-循环
# 启动（不耗计数器），抓鬼轮次计算-max-fuben 在每轮末尾命中一次再启下一轮——故 4 轮对应 max_hit=3。
# standalone 的 post_task 不读 interface.json option，base 默认不挂限轮器（抓鬼一轮完成→队伍满员判断
# 会无限循环），故 team_run 必须显式把 抓鬼一轮完成.next 改写到 抓鬼轮次计算-max-fuben。
ZHUOGUI_ROUNDS = 2

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
LAUNCH_STAGGER = 30                    # 启动错峰间隔秒数：ensure_instances 逐台拉 MuMu 实例、
                                       # launch_parallel 逐账号跑 start 登录，相邻两次都至少隔这么久（0=齐发）
TIMEOUTS = {                           # 各步墙钟超时（秒）
    "start": 600, "chuangjianduiwu": 180, "duizhang": 600,
    "fuben": 7200, "duizhang_TR": 300, "duiyuan": 7200,
    "solo": 2400,          # 单个 solo 任务的墙钟超时
    "solo_overall": 7200,  # 整轮 solo（全部账号×全部任务）的墙钟总超时；到点未完则收口退出
    "zhuagui": 14400,      # 队长无限捉鬼的墙钟安全帽（4h）；实际靠 Ctrl+C 停，到点 post_stop 收口
}
SOLO_ENTRIES = ["shuangbei", "fuli_qiandao", "shimen_renwu", "yunbiao_renwu2", "baotu_renwu", "wabaotu_qingli",
                "mijing_renwu", "sanjieqiyuan", "huoyue_lingqu", "zhengli_baibao", "jiayuan_zhengli", "huoli"]
if datetime.now().weekday() < 6:
    SOLO_ENTRIES.append("kejuxiangshi")

# 科举乡试 AI 答题凭证（对应 interface.json「是否使用Ai进行答题=Yes」）。
# 这里接的是 100.116.176.34 上的 Ollama（Tailscale 内网）。**url 必须用原生 /api/chat**——
# AIAnswer 据此走 Ollama 原生协议 + think:false 关掉 qwen3 系列的思考；OpenAI 兼容的 /v1 端点
# 无法关思考，会把 max_tokens 吃光、content 返回空（详见 AIAnswer.py 注释）。
# 已连通性测试通过：qwen3.5:4b-mlx + think:false → 单 token 直出答案（~1.6s/题）。
# apikey 任意非空即可（Ollama 不校验；这里的值只为触发下方 DEFAULT_OVERRIDES 的 keju 分支）。
# 想改用智谱（zai-sdk，仅需 apikey）：把 DEFAULT_OVERRIDES 里 kejuxiangshi 的
# 「活动-科举乡试-开始答题API」换成「活动-科举乡试-开始答题agent-智谱」，attach 只留 apikey。
KEJU_AI = {
    "apikey": "ollama",
    "url": "http://100.116.176.34:11434/api/chat",
    "model": "qwen3.5:4b-mlx",
}
# =========================================================================
# 以下为派生路径与内部常量，一般无需修改
RESOURCE_PATH = os.path.join(REPO_DIR, "assets", "resource", "base")
AGENT_DIR     = os.path.join(REPO_DIR, "agent")   # import custom（自定义识别/动作）
DEBUG_DIR     = os.path.join(REPO_DIR, "debug")   # MaaFramework 工作目录 + 日志

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
DEFAULT_OVERRIDES = {
    "yunbiao_renwu2": {"活动-运镖": {"next": ["活动-运镖-点击日常活动"]}},
    "mijing_renwu": {
        "秘境降妖-选择模式": {"next": ["秘境降妖-选择海底秘境"]},
        "海底秘境-指定关卡结束任务": {"expected": ["第25关"]},
    },
    "zhengli_baibao": {
        "使用红罗羹": {"enabled": True}, "使用绿芦羹": {"enabled": True},
        "使用心魔宝珠": {"enabled": True}, "合成阵法": {"enabled": True},
        "使用秘境材料": {"enabled": True}, "使用过期物品": {"enabled": True},
        "出售百炼精铁": {"enabled": True}, "出售制造书": {"enabled": True},
    },
    "jiayuan_zhengli": {
        "点击管家寻路": {"next": ["卧室-点击打理", "[JumpBack]panduan_zhujiemian"]},
    },
}
if KEJU_AI.get("apikey"):
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


def ensure_instances(indices, package=PACKAGE, retries=2, cool_down=10, boot_come_up=180):
    """确保每个 MuMu 实例的模拟器已启动、adb 可用（**交付界面：adb ready**）。

    两阶段：
      A. 逐台 ``control launch -pkg``（错峰 ``LAUNCH_STAGGER``）——``-pkg`` 幂等、作保险；已跑的跳过。
      B. 每台双重检查 adb 可用（``_adb_ready`` = connect + ``boot_completed=1``）：
         检查①轮询到就绪（≤``boot_come_up``）→ 冷却 ``cool_down`` → 检查②。
         任一未过即重拉该实例（``_launch_one``），``retries`` 轮仍不过则 raise。

    崩溃 / 冷启动闪退**隐含**在 double-check 失败里——adb 不可用即重拉，无需状态跟踪。
    幂等。返回 ``{index: addr}``。
    """
    indices = sorted(set(int(i) for i in indices))
    adb = _adb_path()
    _adb_reset(adb, [_idx_to_addr(i) for i in indices])

    # A. 逐台 launch（已跑的跳过）
    info = mumu_info(",".join(map(str, indices)))
    to_launch = [i for i in indices if not info.get(str(i), {}).get("is_process_started")]
    for k, i in enumerate(to_launch):
        if k > 0:
            time.sleep(LAUNCH_STAGGER)  # 相邻实例启动间隔 ≥LAUNCH_STAGGER 秒，避免 5 个模拟器同时拉起压满宿主
        print(f">>> 启动 MuMu 实例 {i}（control launch -pkg {package}）")
        _launch_one(i, package)

    # B. 逐台 double-check adb 可用；不过则重拉该实例
    print(f">>> 逐台双重检查 adb 可用（cool_down={cool_down}s, boot_come_up={boot_come_up}s）")
    failed = []
    for i in indices:
        addr = _idx_to_addr(i)
        t0 = time.time()
        for attempt in range(retries + 1):
            if _double_check(lambda a=addr: _adb_ready(adb, a), cool_down, boot_come_up):
                print(f"    实例 {i}({addr}) adb 就绪（双重检查通过，用时 {int(time.time() - t0)}s）")
                break
            if attempt < retries:
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


def _wait_for(check, timeout, interval=2):
    """轮询 ``check()`` 直到返回真或 ``timeout`` 到期。命中返回 True，超时返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def _double_check(check, cool_down=10, come_up=60):
    """双重检查（ensure_instances / ensure_process 共用）。

    检查①：``_wait_for(check, come_up)``——轮询直到命中（容忍模拟器/游戏慢启动）；
    冷却 ``cool_down`` 秒；检查②：再 ``check()`` 一次——抓"起来又崩 / 闪退"。
    两次都过才返回 True。崩 / 闪退 / 起不来都隐含在此：检查不过 → 上层重拉。
    """
    if not _wait_for(check, come_up):
        return False
    time.sleep(cool_down)
    return check()


def _adb_reset(adb, addresses):
    """清 adb server 的 stale offline 缓存，再 ``connect`` 全部地址（幂等）。"""
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


def ensure_process(addresses, package=PACKAGE, retries=2, cool_down=10, come_up=40):
    """在每个地址上确保游戏进程已启动（**交付界面：游戏进程存活**，建立在 ensure_instances 之上）。

    每个地址：不在则 ``monkey`` 拉起 → 双重检查（检查①轮询到 pidof 命中 ≤``come_up`` →
    冷却 ``cool_down`` → 检查② pidof）。任一未过即重新 monkey，``retries`` 轮仍不过则 raise。

    背景：实测 ``control launch -pkg`` 在本环境**不能可靠起游戏**（120s 未起），monkey 才是可靠
    手段（~2s）。闪退隐含在 double-check 失败 → 重新 monkey。
    """
    adb = _adb_path()
    _adb_reset(adb, addresses)
    print(f">>> 逐台双重检查游戏进程（cool_down={cool_down}s, come_up={come_up}s）")
    failed = []
    for addr in addresses:
        t0 = time.time()
        for attempt in range(retries + 1):
            if not _game_process_alive(adb, addr, package):
                print(f"    [{addr}] 游戏进程不存在，monkey 拉起")
                _start_game_process(adb, addr, package)
            if _double_check(lambda a=addr: _game_process_alive(adb, a, package), cool_down, come_up):
                print(f"    [{addr}] 游戏进程就绪（双重检查通过，用时 {int(time.time() - t0)}s）")
                break
            if attempt < retries:
                print(f"    [{addr}] 游戏进程双重检查未过，重新 monkey（{attempt + 1}/{retries}）")
        else:
            failed.append(addr)
    if failed:
        raise RuntimeError(f"游戏进程在 {failed} 上重试 {retries} 轮仍未就绪")
    print(f"<<< 游戏进程就绪：{list(addresses)}")


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
    found = {d.address: d for d in Toolkit.find_adb_devices()}

    # 清掉 adb server（5037）里的陈旧 offline 缓存：之前任何 adb 使用（手动探测 / GUI / 上一轮
    # 5r 未正常退）都可能给某些端口留下 offline 条目。find_adb_devices 能穿透缓存发现真设备，但
    # 下面 AdbController.post_connection 第一步 ``adb -s <addr> get-state`` 会直接读缓存——命中
    # offline 就秒级失败（"failed to connect" → "Tasker 未就绪"，本次 6130@16448 即此）。kill-server
    # 后下一条 adb 命令会自动重启一个干净 server、重新握手。run_5r 是独占 adb 的进程，无副作用。
    if found:
        _adb = str(next(iter(found.values())).adb_path)
        try:
            subprocess.run([_adb, "kill-server"], capture_output=True, timeout=15)
            print(f">>> adb kill-server（{_adb}）清陈旧 offline 缓存")
        except Exception as _e:
            print(f"!! adb kill-server 失败（忽略继续）：{_e}")
        time.sleep(0.5)

    taskers = {}
    for idx, (role, addr) in enumerate(roles.items(), 1):
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
        taskers[role] = t
        print(f"[{idx}] {role} 已连接 {addr}（独立 Resource）")  # [id] 用于 solo --ids
    return taskers


def run_task(tasker, entry, override=None, timeout=600, label=None):
    """跑一个**原生** entry，带墙钟超时：超时则 ``post_stop`` 中断。返回是否在超时内完成。

    自动合并 ``DEFAULT_OVERRIDES`` 里的项目建议默认值（如运镖跳过活力检测）。
    调用方显式传的同名 node override 优先。
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
    while time.time() < deadline:
        done = job.done  # 读这个属性会进 MaaFw native；若进程在这附近崩，faulthandler 会在 stderr 打栈
        if done:
            print(f"<<< {label} 完成（用时 {int(time.time() - _t0)}s）")
            return True
        time.sleep(1)
    tasker.post_stop().wait()
    print(f"!!! {label} 超时 {timeout}s，已 stop")
    return False


# ---------------- 能力 1：启动一个账号 ----------------

def launch(tasker, package=PACKAGE, timeout=240):
    """任意状态→主界面。

    ``start``（StartApp + 登录流程）+ ``panduan_zhujiemian``（Back 清场到主界面）。

    已登录出口在资源层：``start.json`` 的 ``启动游戏.next`` 首候选是 ``主界面``（定义在
    ``my_task.json``，纯模板检测、无 action、命中即结束）——已登录账号不再空转 timeout。
    这里只剩一个 override：修正 ``启动游戏.package``（start.json 里写的 ``myq`` 是错的）。
    """
    run_task(tasker, "start", override={"启动游戏": {"package": package}},
             timeout=timeout, label="start 启动/登录")
    return run_task(tasker, "panduan_zhujiemian", timeout=120, label="panduan_zhujiemian 清场到主界面")


def launch_parallel(taskers, package=PACKAGE, timeout=240, stagger=10):
    """并行启动多账号，每个相隔 ``stagger`` 秒（错峰 submit）。

    每个账号在独立线程里跑 ``launch``；按 ``taskers`` 顺序 submit，相邻两次 submit 之间
    sleep ``stagger`` 秒。错峰而非齐发：各账号虽各自独立 Resource（已无 OCR 模型并发竞态），
    但 5 个 StartApp + 游戏进程同时拉起会瞬间压满宿主 CPU/磁盘/ADB；错峰 10s 让前一个越过
    StartApp 的 20s post_delay 再起下一个，整体更稳。设 0 即齐发。
    wall-clock 从 N×单账号（顺序）降到 stagger×(N-1) + 单账号。
    """
    items = list(taskers.items())
    print(f">>> 并行启动 {len(items)} 个账号（每个间隔 {stagger}s）")
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        futs = []
        for i, (role, t) in enumerate(items):
            if i > 0:
                time.sleep(stagger)
            futs.append(ex.submit(launch, t, package, timeout))
        for f in futs:
            f.result()  # 任意账号抛异常会在此 re-raise
    print(f"<<< 并行启动完成：{', '.join(r for r, _ in items)}")


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


def team_run(taskers, member_names, fuben_entry=FUBEN_ENTRY, timeouts=None):
    """执行副本 + 解散（能力 2-2）：队长跑 ``fuben_entry`` → ``5R_duizhang_TR``（解散），
    然后停掉所有队员并回收全局 ``_TEAM_MEMBER_FUTS`` / ``_MEMBER_POOL``。

    ``fuben_entry`` 接 ``ZHUOGUI_ROUNDS`` 轮捉鬼：把 ``抓鬼一轮完成.next`` 改写到
    ``抓鬼轮次计算-max-fuben``（max_hit = ZHUOGUI_ROUNDS - 1，实际跑 ZHUOGUI_ROUNDS 轮），
    跑完经 ``捉鬼-fuben结束`` 跳回父节点兜底 ``panduan_zhujiemian``。standalone 不读
    interface.json option，故 base 默认无限循环——必须在此显式接限轮器。
    """
    timeouts = {**TIMEOUTS, **(timeouts or {})}
    L = taskers["队长"]

    fuben_override = {
        "抓鬼一轮完成": {"next": [
            "[JumpBack]抓鬼一轮完成-再次点击确定",
            "抓鬼轮次计算-max-fuben",
            "捉鬼-fuben结束",
        ]},
        "抓鬼轮次计算-max-fuben": {"max_hit": ZHUOGUI_ROUNDS - 1},
    }
    run_task(L, fuben_entry, override=fuben_override,
             timeout=timeouts["fuben"], label=f"队长 {fuben_entry}（+{ZHUOGUI_ROUNDS}轮鬼）")
    run_task(L, "5R_duizhang_TR", timeout=timeouts["duizhang_TR"], label="队长 5R_duizhang_TR 解散")

    _stop_members(taskers, member_names)  # 解散后停掉队员（post_stop 中断 5R_duiyuan）+ 回收 future/池
    print("<<< 副本+解散完成，已停掉队员")


def team_dungeon(taskers, member_names, fuben_entry=FUBEN_ENTRY, timeouts=None):
    """组队副本 = ``team_form`` + ``team_run``（一键入口，供 full/team 模式一次调完）。"""
    team_form(taskers, member_names, timeouts=timeouts)
    team_run(taskers, member_names, fuben_entry=fuben_entry, timeouts=timeouts)


# ---------------- 能力 3：并行单人 pipeline ----------------

def solo_all(taskers, entries=None, ids=None, per_timeout=3600, overall_timeout=None):
    """5 账号**并行**执行单人 pipeline；每个账号内**顺序**跑 ``entries``。

    ids: ``None``=全部账号；或账号 id 集合（1-based，按 ``ROLES`` 顺序，连接日志里有 ``[id] 角色``）。

    两层超时：
      - ``per_timeout``：单个任务墙钟超时（``run_task`` 内 ``post_stop`` 收口）。
      - ``overall_timeout``：**整轮 solo** 墙钟总超时。到点后各账号当前任务被收口、后续任务不再执行。
        实现是"收缩式"：每个任务实际 timeout = ``min(per_timeout, 距整轮截止的剩余时间)``，于是
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
            this_to = per_timeout if left is None else min(per_timeout, left)
            try:
                run_task(t, e, timeout=this_to, label=f"[{role}] 单人 {e}")
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
    print("\n常用单人任务名（solo 可指定）：")
    for k, v in SOLO_TASKS_HELP.items():
        print(f"  {k:18} {v}")


def main(mode="full", solo_tasks=None, solo_ids=None):
    open_log()  # 先开日志：后续所有 print 自动加 [时间戳] 前缀并 tee 到 DEBUG_DIR/run_5r/
    _assert_repo()
    roles = ROLES
    package = PACKAGE
    fuben_entry = FUBEN_ENTRY
    timeouts = TIMEOUTS
    member_names = [r for r in roles if r != "队长"]

    # 生命周期：确保 ROLES 里的实例都启动到 start_finished（幂等；未启动的用
    # ``control launch -pkg`` 拉起并自动开游戏）
    role_idx = _role_indices(roles)
    print("=== 启动/检查 MuMu 实例 ===")
    ensure_instances(sorted(role_idx.values()), package=package)

    # 在交给 Maa 的 start 之前，由 Python 侧显式确认游戏进程稳态存活（双重检查 + 重试）。
    # ensure_instances 的 -pkg 虽顺带拉游戏，但不保证进程稳定；这里 adb 兜底，缺一台即中止整轮。
    print("=== 确认游戏进程存活（双重检查 + 重试）===")
    ensure_process(list(roles.values()), package=package)

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
        launch(taskers["队长"], package=package, timeout=timeouts["start"])
        print("=== 队长无限捉鬼（关闭人员检测-不进入轮次选择）===")
        zhuagui(taskers)

    if mode in ("full", "team", "form"):
        print("=== 组队（建队 + 逐个邀请）===")
        team_form(taskers, member_names, timeouts=timeouts)

    if mode in ("full", "team", "run", "fuben"):
        print("=== 执行副本 + 解散 ===")
        team_run(taskers, member_names, fuben_entry=fuben_entry, timeouts=timeouts)

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
                 per_timeout=timeouts["solo"], overall_timeout=timeouts["solo_overall"])

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
            shutdown_instances(to_stop)

    print("=== 全部完成 ===")


if __name__ == "__main__":
    _args = sys.argv[1:]
    _mode = _args[0] if _args else "full"
    _exit_code = 0
    try:
        if _mode in ("-h", "--help", "help"):
            print_help()
        elif _mode in ("full", "launch", "team", "form", "run", "fuben", "zhuagui"):
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
        # 强制 os._exit：MaaFw 的 C++ 后台线程（Toolkit / 残留 Resource）可能阻塞 Python 解释器
        # 正常退出，导致子进程吊着不退、父进程（autolife）读 stdout 的循环卡死直到 wait_for 超时
        # （本就是这次"4 小时才报超时"的根因）。os._exit 跳过线程 join / atexit，保证父进程立刻
        # 拿到 EOF；日志已逐行 flush（_log_print flush=True + 文件行缓冲），不会丢。
        if _LOG_FILE is not None:
            try:
                _LOG_FILE.flush()
            except Exception:
                pass
        print("=== main 结束，os._exit 强制退出子进程 ===")
        os._exit(_exit_code)
