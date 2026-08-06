"""队伍2 配置（覆盖 run_5r.py 默认）。

由 ``python run_5r.py --config team2 <mode>`` 加载。机制：run_5r 在其 ``globals()``
命名空间里 exec 本文件，故下方同名变量的赋值会**直接覆盖** run_5r 的对应全局；**未列出
的字段自动保持 run_5r 默认**；亦可直接引用 run_5r 已定义的名字（如 ``os`` / ``datetime``
/ ``KEJU_AI``）做条件或派生。

**初始内容 = run_5r.py 头部配置区的当前值**（行为与不传 ``--config`` 完全一致）。
之后按需修改某项即可分化队伍2（如换一批角色、改副本计划、增删单人任务）。

可覆盖的常用字段（见 run_5r.py 配置区全文）：
  ROLES / PACKAGE / FUBEN69NEW_PLAN / ZHUOGUI_ROUNDS / ZHUAGUI_OVERRIDE /
  MUMU_MANAGER / LAUNCH_STAGGER / TIMEOUTS / SOLO_ENTRIES / KEJU_AI /
  WIN_IP / MAC_IP / ADB_SERVER_PORT / MUMU_API_PORT / CLI_API_PORT /
  REMOTE_PROBE_TIMEOUT / SHARED_TOKEN
（DEFAULT_OVERRIDES 是派生量，run_5r 会基于本文件的 KEJU_AI 自动重算，无需在此写。）
"""

# 五开角色 → MuMu 实例 ADB 地址。MuMu12 约定 adb_port = 16384 + 32*index，
# 故 index = (port - 16384) // 32；ROLES 的顺序即账号编号顺序（solo --ids 用，从 1 起）。
ROLES = {
    "队长": "127.0.0.1:16512",  # id=4 title=3
    "渣中": "127.0.0.1:16576",  # id=6 title=5
    "6130": "127.0.0.1:16544",  # id=5 title=4
    "欧阳": "127.0.0.1:16448",  # id=2 title=1
    "晚风": "127.0.0.1:16480",  # id=3 title=2
}
PACKAGE = "com.netease.my"

# 组队副本 = 5 本 fuben69new 串联（每本一个 entry，override 选目标）+ ZHUOGUI_ROUNDS 轮捉鬼。
# (是否侠士, 第几个)：侠士 idx∈{1,2}=50侠士/70侠士；普通 idx∈{1,2,3}=50普通-1/2、70普通。
FUBEN69NEW_PLAN = [
    (True, 1),   # 50侠士
    (True, 2),   # 70侠士
    (False, 1),  # 50普通-1
    (False, 2),  # 50普通-2
    (False, 3),  # 70普通
]
# 副本完成、桥接捉鬼后跑几轮鬼。注意 **实际轮数 = max_hit + 1**（详见 run_5r.py 注释）。
ZHUOGUI_ROUNDS = 4

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
    "solo": 2400, "solo_overall": 7200, "zhuagui": 14400,
}

# 单人任务列表。先整体赋值（覆盖 run_5r 默认 + 其 weekday 调整结果），再做与 run_5r.py
# 一致的星期调整（此处赋值后列表是"基础列表"，insert 不会与 run_5r 的调整重复）。
SOLO_ENTRIES = ["shuangbei", "huoli", "fuli_qiandao", "shimen_renwu_new", "yunbiao_renwu2",
                "baotu_renwu", "wabaotu_qingli", "打开大地图_69副本",
                "mijing_renwu", "打开大地图_69副本", "sanjieqiyuan", "huoyue_lingqu",
                "zhengli_baibao", "jiayuan_zhengli", "huoli", "zhanghao_xinxi"]
if datetime.now().weekday() == 3:   # 周四
    SOLO_ENTRIES.insert(0, "bangpai_renwu")
if datetime.now().weekday() < 5:    # 工作日
    SOLO_ENTRIES.insert(0, "kejuxiangshi")

# 科举乡试 AI 答题凭证（deepseek，openai 兼容端点）。apikey 从 .env 的 OPENAI_KEY 读
# （run_5r 的 _load_dotenv 已加载进 os.environ）。留空则走 pipeline 默认普通答题。
KEJU_AI = {
    "apikey": os.environ.get("OPENAI_KEY", ""),
    "url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
}

# ---- 远端/服务模式配置（双机部署，一般无需改）----
WIN_IP = "100.77.236.94"   # Win（MuMu 主机）Tailscale IP
MAC_IP = "100.116.176.34"  # Mac（资源/执行）  Tailscale IP
ADB_SERVER_PORT = 5038
MUMU_API_PORT = 5080
CLI_API_PORT = 5090
REMOTE_PROBE_TIMEOUT = 2.5
SHARED_TOKEN = os.environ.get("MAA_5R_TOKEN", "")
