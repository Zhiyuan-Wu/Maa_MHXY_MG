"""队伍2 配置（覆盖 run_5r.py 默认）。

由 ``python run_5r.py --config team2 <mode>`` 加载。机制：run_5r 在其 ``globals()``
命名空间里 exec 本文件，故下方同名变量的赋值会**直接覆盖** run_5r 的对应全局；**未列出
的字段自动保持 run_5r 默认**；亦可直接引用 run_5r 已定义的名字（如 ``os`` / ``datetime``
/ ``KEJU_AI``）做条件或派生。

**初始内容 = run_5r.py 头部配置区的当前值**（行为与不传 ``--config`` 完全一致）。
之后按需修改某项即可分化队伍2（如换一批角色、改副本计划、增删单人任务）。

可覆盖的常用字段（见 run_5r.py 配置区全文）：
  ROLES / PACKAGE / FUBEN69NEW_PLAN / ZHUOGUI_ROUNDS / ZHUAGUI_OVERRIDE /
  MUMU_MANAGER / LAUNCH_STAGGER / TIMEOUTS / SOLO_ENTRIES / SOLO_MAP / KEJU_AI /
  DEFAULT_OVERRIDES /
  WIN_IP / MAC_IP / ADB_SERVER_PORT / MUMU_API_PORT / CLI_API_PORT /
  REMOTE_PROBE_TIMEOUT / SHARED_TOKEN
（本文件已拷贝 run_5r 全部可改配置，行为与不传 ``--config`` 一致。DEFAULT_OVERRIDES 是**整体
替换**非合并——run_5r 用 ``globals().get(...) or {...}`` 取本文件这份，故改一项须把要保留的也列上；
其中 kejuxiangshi（AI 答题）仍由 run_5r 据 KEJU_AI.apikey 自动补，无需也勿在此写。）
"""
from datetime import datetime
import os

# 五开角色 → MuMu 实例 ADB 地址。MuMu12 约定 adb_port = 16384 + 32*index，
# 故 index = (port - 16384) // 32；ROLES 的顺序即账号编号顺序（solo --ids 用，从 1 起）。
ROLES = {
    # "队长": "127.0.0.1:16608",  # id=7 title=6 龙宫
    "队长": "127.0.0.1:16736",  # id=11 title=10 方寸
    "梦蝶": "127.0.0.1:16672",  # id=9 title=8 地府
    "离歌": "127.0.0.1:16704",  # id=10 title=9 魔王
    "六仔": "127.0.0.1:16640",  # id=8 title=7 大唐
}
PACKAGE = "com.netease.my"

# 组队副本 = 5 本 fuben69new 串联（每本一个 entry，override 选目标）+ ZHUOGUI_ROUNDS 轮捉鬼。
# (是否侠士, 第几个)：侠士 idx∈{1,2}=50侠士/70侠士；普通 idx∈{1,2,3}=50普通-1/2、70普通。
FUBEN69NEW_PLAN = [
    # (True, 1),   # 50侠士
    # (True, 2),   # 70侠士
    (False, 1),  # 50普通-1
    (False, 2),  # 50普通-2
    (False, 3),  # 70普通
]
if datetime.now().weekday() in [0]:
    FUBEN69NEW_PLAN = [(True, 1), (True, 2)] + FUBEN69NEW_PLAN

# 副本完成、桥接捉鬼后跑几轮鬼。注意 **实际轮数 = max_hit + 1**（详见 run_5r.py 注释）。
ZHUOGUI_ROUNDS = 3#5 if datetime.now().weekday() not in [3, 4] else 4

# 队长单人无限捉鬼模式的 override（``python run_5r.py --config team2 zhuagui`` 用）。
ZHUAGUI_OVERRIDE = {
    "抓鬼一轮完成": {"next": [
        "钟馗-捉鬼任务-循环",
        "[JumpBack]抓鬼一轮完成-再次点击确定",
    ]},
}

MUMU_MANAGER = r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"
LAUNCH_STAGGER = 20                    # 启动错峰间隔秒数（0=齐发）
TIMEOUTS = {                           # 各步墙钟超时（秒）
    "start": 600, "chuangjianduiwu": 180, "duizhang": 600,
    "fuben_per": 1800, "zhuogui": 1800,
    "duizhang_TR": 300, "duiyuan": 14400,
    "solo": 2400, "solo_overall": 10800,
    "bangpai_renwu": 2400, "zhuagui": 28800,
}

# 单人任务列表（任务间由 run_5r.solo_all 自动插 barrier：sleep5 + 打开大地图重置位置，
# 故这里不手动插"打开大地图_69副本"）。先整体赋值（覆盖 run_5r 默认），再做与 run_5r.py
# 一致的星期调整（此处赋值后列表是"基础列表"，insert 不会与 run_5r 的调整重复）。
SOLO_ENTRIES = ["5R_duiyuan_tuichuduiwu", "shuangbei", "huoli", "fuli_qiandao", "shimen_renwu_new", "yunbiao_renwu2",
                "baotu_renwu", "wabaotu_qingli",
                "mijing_renwu", "sanjieqiyuan",
                "zhengli_baibao", "jiayuan_zhengli", "zhanghao_xinxi", "jialan", "baitanchushou"]
if datetime.now().weekday() in [3, 4]:
    SOLO_ENTRIES.insert(1, "bangpai_qiandao")
    SOLO_ENTRIES.insert(1, "bangpai_renwu")
    
if datetime.now().weekday() < 5:    # 工作日
    # SOLO_ENTRIES.insert(0, "kejuxiangshi")
    SOLO_ENTRIES.append("kejuxiangshi")

SOLO_MAP = {
    "队长": ["huoli_linshifu"] + SOLO_ENTRIES,
    # "欢喜": SOLO_ENTRIES,
    "梦蝶": SOLO_ENTRIES,
    "六仔": SOLO_ENTRIES,
    "离歌": ["huoli_linshifu"] + SOLO_ENTRIES,
}
# SOLO_MAP 支持每账号定制 solo 任务列表（key=角色名，同上方 ROLES）；未列出的角色/空列表
# 回退 SOLO_ENTRIES。命令行显式给任务名（solo <任务名...>）时 SOLO_MAP 不生效，全员同一列表。

# 科举乡试 AI 答题凭证（deepseek，openai 兼容端点）。apikey 从 .env 的 OPENAI_KEY 读
# （run_5r 的 _load_dotenv 已加载进 os.environ）。留空则走 pipeline 默认普通答题。
KEJU_AI = {
    "apikey": os.environ.get("OPENAI_KEY", ""),
    "url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
}

# 各 solo 任务的建议 pipeline_override（对照 interface.json option 默认值 + 各 pipeline 裸默认）。
# **整体替换** run_5r 默认（run_5r 用 ``globals().get("DEFAULT_OVERRIDES") or {...}`` 取本文件这份，
# 非合并）——故此处的值就是全部生效值，删一项等于关掉那项 override；改一项须把要保留的也列上。
# kejuxiangshi（AI 答题）**故意不写在这**：run_5r 检测到 KEJU_AI.apikey 非空且此处无 kejuxiangshi
# 时自动补上（run_5r.py:317），所以改上面 KEJU_AI 即可联动启停 AI 答题。逐项依据见 run_5r.py 注释。
DEFAULT_OVERRIDES = {
    "yunbiao_renwu2": {"活动-运镖": {"next": ["活动-运镖-点击日常活动"]}},
    "mijing_renwu": {
        "秘境降妖-选择模式": {"next": ["秘境降妖-选择海底秘境"]},
        "海底秘境-指定关卡结束任务": {"expected": ["第21关", "第22关", "第23关", "第24关", "第25关", "第36关"]},
        "秘境-点击进入战斗前重新确认关卡数": {"expected": ["第21关", "第22关", "第23关", "第24关", "第25关", "第36关"]},
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

# ---- 远端/服务模式配置（双机部署，一般无需改）----
WIN_IP = "100.77.236.94"   # Win（MuMu 主机）Tailscale IP
MAC_IP = "100.116.176.34"  # Mac（资源/执行）  Tailscale IP
ADB_SERVER_PORT = 5038
MUMU_API_PORT = 5080
CLI_API_PORT = 5090
REMOTE_PROBE_TIMEOUT = 2.5
SHARED_TOKEN = os.environ.get("MAA_5R_TOKEN", "")
