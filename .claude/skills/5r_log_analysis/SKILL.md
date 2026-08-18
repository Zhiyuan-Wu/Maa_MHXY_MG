---
name: 5r-log-analysis
description: 排查"梦幻西游五开"（agent/run_5r.py）运行日志的 SOP。当需要分析 debug/run_5r/*.log（Python 编排层）或 debug/debug/maafw*.log（MaaFw 原生层）来定位任务失败/超时/静默成功/on_error/自定义回调崩溃，或诊断"autolife 4h 假超时"时使用。排查完成后须归档：把 Mac 侧日志/on_error 拷到 debug/mac log/<YYYYMMDD>/ 并把报告写入该目录 report.md（见 §5.2）。
---

# 5r 日志排查 SOP

`agent/run_5r.py` 是五开编排（单进程、每账号独立 Resource、原生 pipeline + 墙钟超时）。日志分**两层**，排查时要分清看哪层。本 SOP 总结自 2026-07 的几次实战排查。

## ⚠ 硬性规则（每次排查必须遵守，不可跳过）

以下六条是用户反复强调的底线，违反任一条都算排查不到位：

1. **三份报告全出 + 三份都必须以完整表格呈现**：耗时矩阵（报告一）、账号金币·银币变化量（报告二）、科举答题质量（报告三）三份缺一不可，且**每一份都必须以表格形式贴出，禁止用文字总结糊弄**：
   - **报告一（耗时矩阵）**：必须贴出「行=任务、列=5 个号、单元格=耗时(秒)」的完整矩阵，`X`=墙钟超时、`*`=超基线、`—`=未跑到、`⚠`=异常短疑似假成功 全部标出，并把含异常标记的单元格在表下点名。禁止只说"都正常/某号慢"。
   - **报告二（账号变化量）**：必须贴出每号「账号(port) / 上次金币 / 本次金币 / Δ金币 / 上次银币 / 本次银币 / Δ银币 / 本次时间」**全部 8 列**（§4 account 脚本输出原样贴）。禁止用"5 个号金币都小幅正"一句总结糊弄；负值/大额波动必须在表下逐号说明。
   - **报告三（科举）**：**cache 命中统计必须给数字**（答题事件总数 / cache 命中（含独立题数）/ AI 新答 / 命中率，§4 命中统计脚本输出原样贴）；形式门结果要给数字（门1/门2/门3 各几条）；逐题校验必须贴**错题清单表**（题干 / AI 错答 / 正解 / 依据 / 是否已改），无错也要明确写"0 错"。

2. **科举知识性逐题校验是必做步骤，不要询问用户是否执行**：报告三除 cache 命中统计 + 三道形式门外，**第四道"逐题知识性人工校验"强制必做**——当天有新入库题就必须逐题判 ✅/❌/⚠，错的给正解并改 cache。**禁止问"要不要我人工校验"——直接做，做完在报告里给错题清单 + 修正结果。** 详见 §3 报告三。

3. **多个 job / 多次 run 必须全部给出结果**：当天或查询时段内有多个 job（如 155000 done + 195959 cancelled）时，**必须逐个 job 出三份报告**，不能只详报一个、略提另一个；cancelled / failed 的 job 也要说明跑到哪、为何中止（去 maafw 看最后状态）。

4. **有异常（超时 / on_error / 异常耗时）必须定位根因到节点级**：看到 `!!! 超时`、异常短的"完成"、异常多的 on_error 截图时，**必须去原生日志追"卡在哪个节点"**——按号（端口 / Tx 线程）+ 时段筛 maafw bak，看该任务终态节点命中、OCR raw text、最后 sleep / 循环状态，给出"卡在节点 X，因为 Y"的结论，不能只报"某号某任务超时"了事。截图（timeout / on_error PNG）必看——用图像分析工具读"卡在哪一帧"。详见 §2 Step 4 + §4.1。

5. **全队/组队任务必须核实"真完成"，不能只看编排日志的"完成"**：team 阶段（副本、抓鬼、组队任务）编排日志只报 `<<< ... 完成（用时 Ns）` + maafw 也常报 `Tasker.Task.Succeeded`，但**副本/抓鬼链路是已知⑫/⑬的假成功重灾区**（空节点 Succeeded）——编排说"完成"≠真打完。**每次排查必须对 team 阶段单独核实真完成**，在报告里给一张「team 阶段任务核实表」（步骤 / 耗时 / 编排状态 / 真实命中证据 / 真假），不能只抄编排的"全部完成"。
   - **核实方法（节点级，必走）**：
     1. 编排日志锁定 team 阶段每个步骤的**时间窗口**（`>>> 队长 fuben69new XX（≤Ns）` 到 `<<< ... 完成（用时 Ns）`）。
     2. 找覆盖该时段的 maafw bak（bak 时间戳 = 上一份 log 滚动时刻，覆盖该时间戳之前的时段）。
     3. 每个副本/步骤 post 时会拿到一个 **task_id**（`task start: [... "entry":"fuben69new" ... "task_id":<id>]`），5 本副本就是 5 个 task_id——**逐一追终态**，不能合在一起数。
        > ⚠ **task_id 归属陷阱（08-15 实证）**：`reco hit [result.name=...]` 行里**不含 task_id 字段**（只有 `NextList.Starting` 的 details JSON 里带）——按 `"task_id":<id>` 字符串去 grep 命中行会得到 **0**，然后误判"全假"。**追终态（Succeeded/Failed 事件）可用 task_id，数命中请改用「时间窗 + 节点名」**：从编排日志拿每本的起止时刻，在该时间窗内 grep `reco hit [result.name=fuben69new-副本完成-退出]` 的时刻列表，5 本窗口各得 1 hit = 全真完成（08-15 两 job 即用此法核实 10/10）。
     4. **判真假的两条硬证据，二选一即可**：
        - **真完成标志节点命中**：如副本的 `fuben69new-副本完成-退出`、抓鬼的 `抓鬼一轮完成`（4 轮鬼应 hit 4 次）+ `捉鬼-结束`。5 本副本→完成退出节点应 hit **5 次**；<5 必有假成功。
        - **进本+战斗命中**：`fuben69new-进入-XX-成功` / `副本内状态机` / `点击副本-开始战斗` / `战斗中-等待N秒`。真打完的副本这几项 >0；假成功那本**战斗命中 = 0**、且 trace 末尾是 `空节点` → `Task.Succeeded`。
     5. **耗时是最强初筛**：team 阶段同类型步骤横向比，某本异常短（如同批副本 523/616/372/411s，唯独一本 44s）= 几乎必然假成功——但要**回 maafw 用上面的命中证据坐实**，不能只凭耗时下结论。
     6. **看到 on_error 截图必须读画面**：假成功那一刻 maafw 会落一张 `save_on_error`（文件名 = 卡住的节点，如 `fuben69new-点击地图-百晓仙子.png`），用图像分析工具读"点击后画面切到了什么"——往往能直接看到根因（如点错相邻 NPC 弹出错误对话框）。
   - **典型范式（08.13 实证，普通-1 假成功）**：5 本副本 `fuben69new-副本完成-退出` 只 hit 4 次（少 1）→ 锁定普通-1 task_id=200000037（耗时 44s 异常短）→ trace：`点击地图-百晓仙子`(click) → 22s 全 miss → `NODE_FAILED` → `空节点` → `Task.Succeeded`；战斗命中 = 0 → 假成功坐实。on_error 截图读画面：click 落点偏到相邻 NPC"乌巢禅师"、弹出错误对话框，"选择副本"面板永不出现。根因 = 该节点 next 缺 `[JumpBack]panduan_zhujiemian` 兜底（已知⑫）。
   - **team 阶段步骤从编排日志识别**：grep `全队|team|副本|抓鬼|zhuogui|fuben|5R_duizhang|barrier 打开大地图|5本副本`，起止行夹出每步时间窗口。注意 team 阶段**只有队长在驱动**（队员跟随），所以节点命中按队长端口/Tx 归号即可。

6. **排查完成后必须归档**：把 Mac 侧当天 `run_5r_*.log` + **过滤后的** `on_error/` 截图（只拷异常取证，设计内退出门/已知重复错误不拷，名单见 §5.2）拷到 Win 本地 `debug/mac log/<YYYYMMDD>/`，并把**当天的三份报告 + team 核实表 + 根因结论写成该目录下的 `report.md`**（当天多 job 时写一份 report.md、分章节逐 job 全覆盖）。归档命令与目录约定见 §5.2。不归档 = 排查未完成。

## 0. 日志在哪

| 层 | 路径 | 内容 | 注意 |
|---|---|---|---|
| 编排层（Python） | `debug/run_5r/run_5r_<YYYYMMDD>_<HHMMSS>.log` | 每步墙钟耗时、`>>>`/`<<<` 起止、超时 `!!!`、异常 traceback | **UTF-8、行缓冲、已带时间戳**。直接 Read 即可，文件小 |
| 原生层（MaaFw） | `debug/debug/maafw.log` + `maafw.bak.<时间戳>.log` | 每个 recognition/action 的命中/失败、OCR 文本、on_error、节点 trace | **单文件 5 路线程交错、文件巨大（16MB+ 轮转）**。**只 grep，不要整读** |
| on_error 截图 | `debug/debug/on_error/<时间戳>_<节点名>.png` | 每次 on_error 落一张截图（文件名=触发节点） | 按文件名直接统计节点出现次数最省事 |

> maafw.log 在 ~16MB 时轮转成 `maafw.bak.<时间戳>.log`。多 Tasker 并发时偶尔会轮转出 **5 份相同 bak**（日志轮转竞态，无害）。

> **remote 模式（Mac cli_server 执行，8/6 起为常态）**：日志/截图全在 Mac（`100.116.176.34`，ssh 用户 `imac`，仓库 `/Users/imac/dev/Maa_MHXY_MG`），本地没有。分析当天的先归档到 `debug/mac log/<日期>/`（§5.2，编排日志+过滤后 on_error 一起拉回）；要现查 maafw 原生层才 ssh。Mac 侧 `agent/data/account_info.log`（账号变化，报告二数据源）与 `agent/data/keju_ai_cache.json` 也在 Mac。
> ```bash
> curl -s http://100.116.176.34:5090/jobs    # job 列表 + exit（exit=0 ≠ 成功，见 ③）
> curl -s http://100.116.176.34:5090/health  # current_job 是否还在
> ```
> **坑**：`ssh mac 'grep 中文 file'` 远程 grep 中文 **0 命中**（locale 不通）——改用 `ssh mac 'python3' <<'PY' ... PY` 读文件（`repr`/`os.listdir` 输出避开终端 GBK）。详见 [[mac-5r-remote-logs-lookup]]。

## 1. 账号 / 实例对照表（排查必备）

编排层日志里是角色名（队长/渣中/6130/缤纷/晚风）；原生层 + PowerShell 里是端口/MuMu 索引。**team1（默认配置）**对照（来自 `run_5r.py::ROLES`，端口约定 `16384+32*idx`）：

| 角色 | adb 端口 | MuMu idx |
|---|---|---|
| 缤纷/欧阳 | 16448 | 2 |
| 晚风 | 16480 | 3 |
| 队长 | 16512 | 4 |
| 6130 | 16544 | 5 |
| 渣中 | 16576 | 6 |

**team2（`--config team2`，08-15 起使用）**是另一组账号、另一组端口（16608–16736，**不满足 16384+32*idx 约定**）：

| 角色 | adb 端口 |
|---|---|
| 队长 | 16608 |
| 六仔 | 16640 |
| 梦蝶 | 16672 |
| 离歌 | 16704 |
| 欢喜 | 16736 |

> ⚠ **端口/角色永远从编排日志动态读**：连接行 `[N] <角色> 已连接 127.0.0.1:<port>`（每 job 开头 5 行）是唯一可靠来源——配置可换（team1/team2）、角色会改名、端口约定会破。§1 两张表只作速查，**写死端口或硬套 `16384+32*idx` 都会归错号**。
>
> **渣中（idx6）一贯最慢**——实例特性，不是 bug（每次排查都会看到它的 shimen/baotu/mijing 耗时偏高）。
>
> **角色名会变，脚本勿硬编码**：`run_5r.py::ROLES` 改名后（如 07.30 把 4 号位「缤纷」改成「欧阳」，端口 16448 / MuMu idx2 不变），编排日志的角色名随之改变。耗时矩阵脚本必须**从日志动态读 roles**（见 §4），写死 `['队长','渣中','6130','缤纷','晚风']` 会让改名号整列变 `-`，造成"该号一个任务都没跑"的误判。

### 1.1 原生层 Tx/uuid → 角色归号（`[tag]` 显式打标，08-15 起）

**maafw 原生日志里没有任何角色名**，5 个号按 `[Tx<tid>]` 线程交错。三个实测事实：①每条 `task start:` 行带 `uuid`（Tasker 实例指纹，5 号各一、全程不变）；②**Tx 线程号 per-uuid 全程稳定**（08-15 job2 全场 216 条 task start 各自 100% 落同一 Tx）；③uuid→角色原本只能靠"首批 start 提交时刻对齐"间接推断（脆弱）。

**已修（run_5r `_RoleTagSink`）**：`connect_all` 每建好一个 tasker 就 `add_sink(_RoleTagSink(role, addr))`，该号**首个** `Tasker.Task.Starting` 事件在编排日志落一行显式标记（之后静默）：

```
[tag] 欢喜(127.0.0.1:16736) uuid=9fcb6c12df310cb3   # maafw 按 Tx/uuid 归号用
```

**排查用法**（08-15 之后的日志）：编排日志 grep `\[tag\]` 拿 5 行 uuid 表 → maafw 里 `task start: .*"uuid":"<uuid>"` 所在行的 `[TxNNNNN]` 即该号线程号 → 之后按 Tx 筛 reco hit / trace 全程有效。一行命令建表：

```bash
"$PY" -c "
import re,sys,glob
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass
log=open(sys.argv[1],encoding='utf-8').read()
uu2role={m[2]:m[0] for m in re.findall(r'\[tag\] (\S+?)\(\S+?\) uuid=(\w+)',log)}
uu2tx={}
for f in glob.glob('debug/debug/maafw*.log'):
    for line in open(f,encoding='utf-8',errors='replace'):
        m=re.search(r'\[Tx(\d+)\].*?task start: .*?\"uuid\":\"(\w+)\"',line)
        if m and m.group(2) in uu2role and m.group(2) not in uu2tx: uu2tx[m.group(2)]=m.group(1)
for u,r in uu2role.items(): print(f'{r:6s} uuid={u} Tx={uu2tx.get(u,\"?\")}')" debug/run_5r/run_5r_<YYYYMMDD>_*.log
```

> **08-15 及更早的日志没有 `[tag]` 行**——回溯用旧法：编排日志拿该号某独占任务的时间窗（如 solo 段 wabaotu），在 maafw 找该窗口 `task start entry=<任务>` 的 Tx/uuid，反查该 uuid 其它 task start 时刻对齐首批 start（每 20s 一个、按 ROLES 序）。
>
> **L3 自愈重建的 tasker**（`_reconnect`）会换新 uuid——若 job 中途某号重连过，其 `[tag]` 的 uuid 只覆盖重连前；重连后新 uuid 需按旧法（时间窗）补一次归号。

## 2. 排查流程（按顺序）

### Step 1 — 确认这次 run 的边界
> 先 `curl :5090/jobs`（remote）或 `ls debug/run_5r/`（local）拉出当天 / 查询时段内**全部** job。**有几个 job 就按本 SOP 走几遍**（硬性规则 3）——cancelled / failed 也要查清跑到哪、为何中止，不能只详报 done 的那个。

读编排日志头尾：`=== 日志写入 ... ===`（起点）、`=== 全部完成 ===`（正常终点）、`=== main 结束，TerminateProcess ... ===`（进程退出）。
- **有 `=== 全部完成 ===`** → 5r 本体跑完了；若调度器仍报超时，跳到 [Step 6 autolife 专项](#step-6--autolife-4h-假超时专项)。
- **没有** → 5r 中途崩/超时，继续 Step 2。

### Step 2 — 编排日志速扫
对当天日志做三件事：
1. **关键词**：`超时 | 异常 | 错误 | !!! | Traceback | Error`（有就逐条看上下文）。
2. **耗时异常**：
   - **异常短**（如某任务 10~50s，明显短于同批其它号）= **疑似静默失败**（on_error→空节点 假成功，见已知问题 ③）。重点盯 `mijing_renwu`（曾出现缤纷 47s）。
   - **异常长/贴着超时**（如 `≤2400s` 的任务跑了 2300s+）= 可能卡循环/冲关（盯 `mijing`、`wabaotu`）。
   - 系统化做全 5 号 × 全任务横向对比 + 基线参照，见 [§3](#3-量化报告每次排查必出三份)。耗时矩阵只是其中一份——**每次排查必出的三份量化报告**（耗时矩阵+异常标记 / 账号金币·银币变化量 / 新入库 AI 答题质量）见 §3，务必**全部产出再下结论**。
3. **全员完成校验**：SOLO 列表最后一个任务（当前是 `baitanchushou`；列表以编排日志「并行单人（每账号顺序跑 [...]」行为准）5 个号是否都 `<<< 完成`。缺谁谁就在那个任务或上一个出了问题。

### Step 3 — 原生层 on_error / 回调异常统计
on_error 不一定致命（很多是设计内或自恢复），但要**按节点计数**看分布：
```bash
cd debug/debug
# on_error 截图按节点计数（最省事、最准）
ls on_error/ | grep "2026.07.22" | sed -E 's/^[0-9._]+_//; s/\.png$//' | sort | uniq -c | sort -rn
# maafw 里自定义回调崩溃（MaaFw 会"忽略"并当 no-hit，非致命但会少干活）
cat maafw.bak.2026.07.<DD>-*.log maafw.log 2>/dev/null | grep -E "Exception ignored on calling ctypes callback|AttributeError|TypeError" | head
```
对照 [已知问题表](#5-已知问题速查表) 判定每类 on_error 是"设计内 / 自恢复 / 真 bug"。

### Step 4 — 深挖可疑任务（编排→原生联动）
编排日志给"哪个号哪个任务多长"，原生日志给"为什么"。联动方法：
1. 在编排日志锁定可疑号+任务+时间段（如"缤纷 mijing，19:02→19:03 仅 47s"）。
2. 找对应的 maafw bak（按轮转时间戳覆盖该时段的那份）。
3. 在该 bak 里 grep 该任务的 `post_task`/`task start` 拿 **task_id + uuid**（同号同任务的标识）：
   ```bash
   grep -nE "post_task.*mijing_renwu|task start.*mijing_renwu" maafw.bak.<覆盖该时段>.log
   ```
4. 用 task_id 追该任务全程：`grep "task_id\":<id>"` 或 `grep "<uuid>"`。
5. 看终态节点命中：`reco hit [result.name=<终态节点>]`、`Node.PipelineNode.Failed`、`save_on_error to ...png`。

### Step 5 — 对照已知问题（见下表）

### Step 6 — autolife 4h 假超时专项
**症状**：autolife（`C:\Users\zhiyuan\Desktop\autolife`）报 `任务执行超时 (>14400秒)`，但 5r 编排日志里有 `=== 全部完成 ===` + `TerminateProcess`。
**判定**：5r 本体已正常退出，autolife 没感知 → 不是 5r 任务失败，是**子进程的 stdout 管道被遗留子进程持有 / 或进程卡在退出**。
**确诊三连**：
```bash
# 1) autolife server 启动时间（若早于最近 jobs.py 改动 → 跑的是旧代码，需重启）
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -match 'server\.py' } | Select ProcessId,CreationDate | Format-Table -AutoSize"
# 2) 遗留 run_5r python（有 → 进程没死，卡在退出；无 → 已死，管道被子进程持有）
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -match 'run_5r' } | Select ProcessId,CreationDate | Format-Table -AutoSize"
# 3) adb server 孤儿（run 时段起的、父进程已死的 adb.exe = 管道持有者）
powershell.exe -NoProfile -Command "\$live=(Get-CimInstance Win32_Process).ProcessId; Get-CimInstance Win32_Process | Where-Object { \$_.CreationDate -gt (Get-Date '2026-07-22 17:00') -and \$_.CreationDate -lt (Get-Date '2026-07-22 21:05') -and (\$live -notcontains \$_.ParentProcessId) } | Select ProcessId,CreationDate,Name | Format-Table -AutoSize"
```
**处置**：重启 autolife server（加载新 jobs.py：`stdout=DEVNULL`+`stderr→文件`+`taskkill /T` 收尾）；run_5r 侧已用 `TerminateProcess` 跳过 DLL detach。机制详见 [[on-error-kongjiedian-false-success]] 同侧笔记与 git 历史 `fix(run_5r): os._exit 改 TerminateProcess` / autolife `feat(jobs): 新增 run_5r_full`。

## 3. 量化报告（每次排查必出三份）

排查不仅定位故障，还要量化"这轮到底跑得怎么样"。**每次排查必须产出以下三份报告并贴进结论，缺一不可；当天 / 时段内有多个 job 时，每个 job 各产一份**（硬性规则 3）：

| 报告 | 回答的问题 | 命令 |
|---|---|---|
| 一：耗时矩阵 + 异常标记 | 每号每个任务多久？哪些偏离典型基线 / 超时？ | §4 矩阵脚本 |
| 二：账号金币·银币变化量 | 这轮跑完，每号钱是涨是跌、涨跌多少？ | §4 account 脚本 |
| 三：科举答题质量（cache 命中统计 + 3 形式门 + **第 4 道知识性人工校验，均强制、不问用户**） | 命中统计：当天答了多少题、cache 命中多少、AI 新答多少；形式门：answer∉options / 同题冲突 / 与 tiku 不符；第 4 道：逐题判知识对错，错的改 cache | §4 命中统计脚本 + 新题质量脚本 + §3 第四道 |

> **SOLO 列表无 kejuxiangshi 的跑（08-15 起）**：报告三直接写"列表无 keju → 当日 0 新题，三门对象为空集，第四道 N/A"，**不要**为凑报告去翻旧日期。sanjieqiyuan 的答题走 reco_sjqy（题库+日志"匹配度：100"），可顺带在报告三提一句当日无错答。

一键命令见 [§4](#4-诊断命令速查复制改日期即可)。

### 报告一：耗时矩阵与典型基线

把当天 5 号 × 全部单人任务拉成耗时矩阵横向比，是挖「假成功 / 卡循环 / 整轮预算被吃」最快的办法（呼应心法 3：耗时是最强信号）。**判读永远以同批横向对比为准，绝对值仅供参考。脚本会自动给超过典型基线「留意线」的单元格标 `*`、墙钟超时标 `X`；但「异常短」（假成功）脚本不标，需人工结合 §5 ⑨/③ 留意（如 keju<60s、mijing<200s）。**

### 建矩阵

行 = 任务，列 = 5 个号，单元格填耗时（秒）；`X` 前缀 = 墙钟超时被 stop；`—` = 该号整轮超时后未跑到。一键生成命令见 [§4](#4-诊断命令速查复制改日期即可) 末条。

### 三类异常信号

| 信号 | 含义 | 重点查 |
|---|---|---|
| **贴单任务上限**（`≤2400s` 跑了 2000s+） | 卡循环 / 冲关 / 接近假成功 | `yunbiao`（靠 on_error 出口，贴上限要核验是否真跑完）、`mijing` |
| **异常短**（同任务其它号几百秒，它几十秒内） | 疑似假成功（on_error→空节点，已知③） | `mijing`（曾出现缤纷 47s） |
| **某号后半段全 `—`** | 整轮预算（7200s）被前序任务吃光，该号整轮停 | 找该号耗时畸高的元凶（往往就是贴上限那个任务） |

辅助判读：

- **`≤Ns` 逐任务递减**：编排日志里单任务上限变成 `≤999.1135s` 这种带小数的递减值，就是整轮预算在压缩——本身即「前面有任务超时」的信号。
- **扣除渣中基线偏移**：渣中（idx6）各项一贯偏慢（已知⑧），判读时把它当 +N% 基线，不要误判为故障。

### 典型正常耗时基线

基于 07.22–07.24 健康号，**随账号等级 / 活动 / 欧非浮动，仅供找不到历史数据时参照**：

| 任务 | 典型正常 | 留意线 | 备注 |
|---|---|---|---|
| shuangbei | 20–33s | >60s | 几乎恒定 |
| fuli_qiandao | 30–60s | >120s | 个别号 ~114s 仍正常（08-15 欧阳） |
| shimen_renwu / **shimen_renwu_new** | 250–900s | >1000s / 贴 2400 | 08.15 起入口换 `shimen_renwu_new`（带主界面回退前置，⑰ 修后）；team1 全员 644–882s、team2 650–1016s 均真干活（1016s 已核 357 命中无 on_error）。<60s 仍按 ⑰ 判假 |
| yunbiao_renwu2 | 90–580s | >1000s / 贴 2400 | ⚠ **"完成"≠跑满 3 镖**，必须按号校验 `点击押送普通镖_确定` 命中 = 每号 3（见 ⑩）。07.24 渣中 2352/晚风 2222（卡循环）；07.26 渣中/缤纷/6130 ~480–510s"完成"但 `确定×1` 假完成 |
| baotu_renwu | 400–820s | >1000s | 波动较大 |
| wabaotu_qingli | 30–570s | — | 取决于背包宝图数量；**<30s = 假成功（见 ⑲）** |
| 打开大地图_69副本 (barrier) | 7–15s | >30s | 纯导航；29s+ 说明有一次传送 miss 重试（自愈，无害） |
| mijing_renwu | 1024–1680s | >2100 / 贴 2400 | 走关数决定；07.24 缤纷 2257 偏长 |
| sanjieqiyuan | 94–126s | >200s | |
| huoyue_lingqu | 14–20s | >60s | 07.24 晚风 65 偏长 |
| zhengli_baibao | 58–139s | >200s | 08-15 实测比旧基线（25–70）长，新任务链含更多页 |
| jiayuan_zhengli | 76–106s | >150s | 同上 |
| huoli | 22–73s | >120s | 22–24s（无活力可领）与 45–73s（有）都正常 |
| zhanghao_xinxi | 17–23s | >60s | |
| jialan | 56–93s | >150s | |
| baitanchushou | 72–140s | >240s | 摆摊扫描+上架（08-14 新增）；`出售阵法` on_error 是流程内出口 |
| 5R_duiyuan_tuichuduiwu | 17–25s | >60s | 退队 |
| kejuxiangshi | 90–100s（**真答**）| >300s | ⚠ **~50s 往往=没答（假成功，见 ⑨）**，并非"答得快"；07.24 缤纷 620 = 题库/AI 卡题。**08-15 起 SOLO 列表已移除 keju**——报告三按"列表无 keju → N/A"处理 |

> **SOLO 任务列表本身会变**（08-15 已比 07 底多 `jialan`/`baitanchushou`、少 `kejuxiangshi`）——排查第一步先从编排日志「并行单人（每账号顺序跑 [...]」行读**当轮实际列表**，不要拿旧列表对照。耗时矩阵脚本已按日志动态收集任务行，天然适配。

### 实例（异常样本精选，秒）

**07.24（UI 变更 + 级联）**：

| 任务 | 队长 | 渣中 | 6130 | 缤纷 | 晚风 |
|---|---|---|---|---|---|
| shimen | X2400 | X2400 | X2400 | X2400 | X2400 |
| yunbiao | 354 | 2352⚠ | 110 | 91 | 2222⚠ |
| mijing | 1301 | X999 | 1626 | 2257⚠ | 1362 |
| keju | 94 | — | 92 | 620⚠ | 95 |

> 渣中 mijing 的 `X999` 是整轮预算压缩后的小上限（**非 mijing 本身卡**）——「`≤Ns` 递减 + 后半段 `—`」组合：先看哪个号后半段全 `—`，再回溯哪个任务贴上限吃掉预算。

**08.06（⑬–⑯ 四类新信号集中爆发）**：

| 任务 | 队长 | 渣中 | 6130 | 欧阳 | 晚风 |
|---|---|---|---|---|---|
| fuben69new(5本) | 506/541/362/**34⚠**/**34⚠** | (队员跟随队长) | | | |
| zhuogui(4轮) | **31⚠** | | | | |
| bangpai | 1494 | X2400 | X2400 | 2226 | X2400 |
| wabaotu | 625 | 501 | 392 | **X2400** | 397 |
| mijing | 1401 | 1667 | X预算 | **41⚠** | 1156 |
| 欧阳后续7项 | | | | sanjie/huoyue/zhengli/jiayuan/huoli/zhanghao **全 41⚠** | |

> 副本后几本骤降 30s 级 = ⑬ 连续进本 UI 时序假完成；某号超时后整串恒定 ~41s = ⑮ 连锁假完成；bangpai 全员 1500–2400s = ⑯；shimen 单号超时 = ⑭ 验证码弹窗。⑬⑮ 已修（`_barrier_reset` + sentinel，commit `93badad`/`a3add01`）；⑭⑯ 待修。

**07.25 / 08.15 = 全绿样板**（详见 `debug/mac log/` 对应日期 report.md）——判读时拿它们当健康基线。

### 报告二：账号金币·银币变化量

> ⚠ **必须贴完整表格（每号 6 列：上次金币 / 本次金币 / Δ金币 / 上次银币 / 本次银币 / Δ银币），禁止用一句"都小幅正"总结糊弄**（硬性规则 1）。§4 account 脚本输出原样贴进结论。

`agent/data/account_info.log` 每个 `zhanghao_xinxi` 节点写一行（每号每次 run 一条）：`[时间] 账号: 127.0.0.1:<port> | 金币: <n> | 银币: <n>`。按端口取**最近两条**算 Δ，即"本次 run 相对上一次 run"的净变化（本跑产出 + 期间消耗/收入）。判读：

- **金币 Δ 正常小幅为正**：日常产出 + 押镖/活跃度收入，扣挖宝/修炼等消耗；银币通常缓涨。
- **Δ 异常**：暴负 → 大额消耗（买道具/点技能）；为 0 → zhanghao_xinxi 的 OCR 没刷新/读到旧值（留意已知 ⑪ 复发）；暴正 → 上次没跑、跨多天累计。
- **任一行出现 `账号ID: (未知账号)`**（旧格式）→ 已知 ⑪ 复发，停；正常应为 `账号: 127.0.0.1:<port>`（新格式，07.27 起）。
- **银币大负的归因排除法（08-15 实战）**：不需要尝试解释金币和银币的大幅减少，完全可能是玩家自行操作/消费。如实报告即可
- **account_info.log 在 Mac 侧**（remote 模式 zhanghao_xinxi 由 Mac 子进程写）——本地 `agent/data/account_info.log` 是旧的（08-13 止），跑 §4 account 脚本前先 ssh 拉 Mac 的。

### 报告三：科举新入库答题质量

> **08-15 起 SOLO 列表已移除 kejuxiangshi**——当天列表无 keju 时本报告直接写"0 新题，三门空集，第四道 N/A"（见 §3 开头注）。以下内容在 keju 回归列表或回溯历史日期（≤08-14）时使用。

> ⚠ 题库链路认准（用户专门纠正过，别搞混）：**`agent/data/keju_ai_cache.json`（Mac 侧）= 5r 科举实际使用的运行时题库**（`开始答题API` → `AIAnswer.py` 只加载它，命中即点、miss 调 AI 后存回）；`tiku.txt` 是别的任务（三界等）用的，仅作交叉参照；`question_bank.json` 是遗留未接入文件，忽略。结构 `{题干key: {answer, options, question, ts}}`，"新入库"按 `ts[:10]==当天` 界定。

**第 0 道门：cache 命中统计（必出数字，08-17 起强制）**——回答"今天答的题里多少是 cache 直接命中的、多少走了 AI"。数据源 = Mac 侧 loguru 日志 `debug/custom/<YYYY-MM-DD>.log`（`AIAnswer.py::_cache_lookup`/`_cached_or_ask` 每题落一行标记）：

- `缓存精确命中：《Q》→ X` = cache 命中该题（同一题多个号答会产生多行，去重得"独立命中题数"）
- `缓存未命中，调用AI：《Q》` = AI 新答（≈ 当天新入库条数，可交叉验证报告三的"新入库 N"）
- 校验恒等式：**命中事件数 + AI 调用数 = 答题事件总数**（应等于 `开始答题API` 每号 hit 数之和；对不上=有号答题链路异常，回 maafw 查）。缓存模糊命中已被禁用（`_FUZZY_THRESHOLD=1.01`），正常不会出现；若日志里见到"缓存模糊命中"按异常上报。
- 命中率走势还能当**题库健康度**指标：逐日走高 = cache 在积累生效；某天骤降 = 题库被清/换 key 归一化规则变了。
- 一键命令见 §4「cache 命中统计」（ssh python3 远读，绕开中文 grep 0 命中坑）。报告里按号拆分不必要（loguru 无角色标记），给**总量 + 独立题数 + 按小时分布**即可；按号核答题次数另有 `开始答题API` per-Tx 法（§4.1）。

**四道质量门**（前三道脚本 = §4 新题质量脚本、第四道人工，**均强制**，见硬性规则 2）：

1. **answer∉options（硬错误）**：AI 幻觉 → 必错必报；只能删该 cache 条重答（回填 tiku 无效）。
2. **同题多答案冲突**（题干归一化后全量查）。
3. **与 tiku.txt 交叉不符**。
4. **知识性逐题校验**：三门只查形式、**抓不出知识性错答**——deepseek 会把事实记错且稳定重复（08.10 实证：80 题三门全过，人工查出 8 错，删了重答还错）。**只能人工判 ✅/❌/⚠ + 直接改 cache**，禁止问用户"要不要校验"。存疑题 web 搜（`mcp__web-search-prime` location=cn，优先教材/百科）；仍存学术争议则保留 AI 答案+备注。

**修正方式**：直接编辑 `keju_ai_cache.json` 的 answer 为正确答案**文本**（cache 存文本，不是字母）。**批量改的坑**：短关键词匹配会误伤语义相近旧题（08.10 "百戏之师" 同键命中两题、把本来正确的旧题改成不是）；必须用完整 question 文本匹配 + 改完逐条 diff 核对 + 多改即回滚。

**08.10 错例（deepseek 高频知识性错点，遇到直接改 cache）**：亚洲耕地最大=印度（非中国）；最大海湾=孟加拉湾（非几内亚）；退避三舍=九十里（一舍三十里×3）；高屋建瓴"瓴"=盛水瓶（非屋檐）；"百戏之师"=昆曲（非京剧）；百家姓有萧无肖（肖是二简字后分出）；不受寒潮影响=雅鲁藏布江地区（非海南岛）；横跨两洲首都西半球=美国（非土耳其）。

## 4. 诊断命令速查（复制改日期即可）

```bash
PY="C:/Users/zhiyuan/AppData/Local/Programs/Python/Python313/python.exe"
cd "C:/dev/Maa_MHXY_MG"

# 当天编排日志 + 关键词扫
F=$(ls -t debug/run_5r/run_5r_<YYYYMMDD>_*.log | head -1)
grep -nE "超时|异常|错误|!!!|Traceback|Error|未识别|没有识别" "$F"

# 当天 on_error 截图按节点计数
ls debug/debug/on_error/ | grep "<YYYY.MM.DD>" | sed -E 's/^[0-9._]+_//; s/\.png$//' | sort | uniq -c | sort -rn

# 秘境结局分布（第25关停 / 失败退 / 通关 / 最大关卡）
cd debug/debug
BAKS="maafw.bak.<YYYY.MM.DD>-18*.log maafw.bak.<YYYY.MM.DD>-19*.log maafw.log"
echo -n "第25关停: "; cat $BAKS 2>/dev/null | grep -c "reco hit \[result.name=海底秘境-指定关卡结束任务\]"
echo -n "shibai(失败退): "; cat $BAKS 2>/dev/null | grep -c "reco hit \[result.name=mijing_shibai\]"
echo -n "tongguan(通关): "; cat $BAKS 2>/dev/null | grep -c "reco hit \[result.name=mijing_tongguan\]"
echo "OCR 到过的最大关卡: "; cat $BAKS 2>/dev/null | grep -oE '"text":"第[0-9]+关' | grep -oE '[0-9]+' | sort -n | tail -1
```

```bash
# 报告一：耗时矩阵（动态读 roles + 超基线标 *、超时标 X；同名任务记最后一次）
"$PY" -c "
import re,sys,collections
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass
log=open(sys.argv[1],encoding='utf-8').read()
data=collections.defaultdict(dict); order=[]
for line in log.splitlines():
    m=re.search(r'<<< \[([^\]]+)\] 单人 (\S+) 完成（用时 ([\d.]+)s）', line)
    if m:
        t=m.group(2)
        if t not in data: order.append(t)
        data[t][m.group(1)]=int(float(m.group(3))); continue
    m=re.search(r'!!! \[([^\]]+)\] 单人 (\S+) 超时 ([\d.]+)s', line)
    if m:
        t=m.group(2)
        if t not in data: order.append(t)
        data[t][m.group(1)]=-int(float(m.group(3)))   # 负数=超时
# 动态读 roles：单人阶段开头那一行「对象: 1:队长, 2:渣中, 3:6130, 4:欧阳, 5:晚风」
# ⚠ 必须行内扫描——[^，;] 字符类含换行，跨行 search 会吞掉后续所有时间戳行(17:43:05] 等)误当 roles
roles=[]
for line in log.splitlines():
    if '对象:' in line or '对象：' in line:
        seg=line.split('对象:')[-1] if '对象:' in line else line.split('对象：')[-1]   # 只取"对象:"之后，避开行首时间戳 17:43:05]
        for _,n in re.findall(r'(\d+):([^\s,，;：]+)', seg):
            if n not in roles: roles.append(n)
        break
if not roles:   # 兜底：从完成行抓角色名
    for line in log.splitlines():
        m=re.search(r'<<< \[([^\]]+)\] 单人 \S+ 完成', line)
        if m and m.group(1) not in roles: roles.append(m.group(1))
    roles=roles[:5] or ['?']
# 典型基线「留意线」(秒)，超过即标 *；未列=波动大不判绝对值（如 wabaotu_qingli）
LIM={'shuangbei':60,'fuli_qiandao':120,'shimen_renwu':900,'shimen_renwu_new':1000,
     'yunbiao_renwu2':1000,'baotu_renwu':1000,'打开大地图_69副本':30,'mijing_renwu':2100,
     'sanjieqiyuan':200,'zhengli_baibao':200,'jiayuan_zhengli':150,'huoli':120,
     'zhanghao_xinxi':60,'jialan':150,'baitanchushou':240,'kejuxiangshi':300}
print('%-16s'%'任务'+''.join('%-9s'%r for r in roles))
for t in order:
    cells=[]
    for r in roles:
        v=data[t].get(r)
        if v is None: cells.append('-')
        elif v<0: cells.append('X'+str(-v))
        else: cells.append(str(v)+('*' if (LIM.get(t) and v>LIM[t]) else ''))
    print('%-16s'%t+''.join('%-9s'%c for c in cells))
print('(* = 超过典型基线留意线，X = 墙钟超时；异常短=疑似假成功，人工留意见 §5 ③/⑨)')
" "$(ls -t debug/run_5r/run_5r_<YYYYMMDD>_*.log | head -1)"
# 注：中文列宽不齐不影响判读；Windows cmd 中文乱码可 `chcp 65001`，数字列才是重点。
#   roles 从日志动态读取（§1：角色名会变，勿硬编码）。基线表见 §3「典型正常耗时基线」。
```

> **用 Python 解析 maafw 里某行的 JSON 缓存**（grep 正则遇到嵌套 `]` 会断，用 python 最稳）：
> ```bash
> "$PY" -c "import sys,json; ..." # 见 [[maafw-ocr-expected-and-only-rec]] 里的实例
> ```

```bash
# 报告二：账号金币/银币变化量（⚠ account_info.log 在 Mac 侧——remote 模式由 Mac 子进程写，
#   本地文件是旧的。先 ssh 拉回: scp imac@100.116.176.34:/Users/imac/dev/Maa_MHXY_MG/agent/data/account_info.log ./agent/data/）
# （每端口取最近两条算 Δ = 本次 − 上次）
"$PY" -c "
import re,sys,collections
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass
rows=[]
for line in open(sys.argv[1],encoding='utf-8'):
    m=re.search(r'账号:\s*(127\.0\.0\.1:\d+)\s*\|\s*金币:\s*(-?\d+)\s*\|\s*银币:\s*(-?\d+)', line)
    if m: rows.append((m.group(1),int(m.group(2)),int(m.group(3))))
by=collections.defaultdict(list)
for port,g,s in rows: by[port].append((g,s))
print('%-16s'%'账号(port)'+''.join('%-11s'%x for x in ['上次金币','本次金币','Δ金币','上次银币','本次银币','Δ银币']))
for port in sorted(by):
    recs=by[port]
    if len(recs)<2: print('%-16s'%port+'  (仅 %d 条，无法算 Δ)'%len(recs)); continue
    (g0,s0),(g1,s1)=recs[-2],recs[-1]
    d=lambda n:('+'+str(n)) if n>=0 else str(n)
    print('%-16s'%port+''.join('%-11s'%x for x in [g0,g1,d(g1-g0),s0,s1,d(s1-s0)]))
print('Δ = 本次(最近一条) − 上次；负=净消耗。任一行含 (未知账号) = 已知 ⑪ 复发，停。')
" agent/data/account_info.log
```

```bash
# 报告三·第0道：keju cache 命中统计（⚠ loguru 日志在 Mac 侧 debug/custom/<日期>.log；
#   AIAnswer.py 每题落一行「缓存精确命中：《Q》→X」或「缓存未命中，调用AI：《Q》」。
#   远程 grep 中文 0 命中，用 ssh python3 读。改日期。）
ssh imac@100.116.176.34 'python3' <<'PY'
import re
from collections import Counter
day="2026-08-17"                                   # ← 改日期
f=f"/Users/imac/dev/Maa_MHXY_MG/debug/custom/{day}.log"
hit_q, miss_q, fuzzy = [], [], []
try:
    for line in open(f, encoding="utf-8", errors="replace"):
        m = re.search(r"缓存精确命中：《(.+?)》", line)
        if m: hit_q.append(m.group(1)); continue
        m = re.search(r"缓存未命中，调用AI：《(.+?)》", line)
        if m: miss_q.append(m.group(1)); continue
        if "缓存模糊命中" in line: fuzzy.append(line.strip()[:80])
except FileNotFoundError:
    print(f"日志不存在：{f}（当天没跑 keju？）"); raise SystemExit
tot = len(hit_q) + len(miss_q)
print(f"答题事件总数: {tot}  |  cache 命中: {len(hit_q)}（独立题 {len(set(hit_q))}）"
      f"  |  AI 新答: {len(miss_q)}（独立题 {len(set(miss_q))}）")
print(f"命中率(按事件): {len(hit_q)/tot*100:.0f}%" if tot else "命中率: N/A")
if fuzzy:
    print(f"!! 模糊命中 {len(fuzzy)} 次（已禁用功能不应出现，按异常上报）:")
    for l in fuzzy[:5]: print("  ", l)
# 校验：命中+AI 应 == 各号「开始答题API」hit 之和（maafw per-Tx 数，见 §4.1/§1.1）
PY

# 报告三：科举题库(keju_ai_cache.json)新入库答题质量（⚠ 文件在 Mac 侧，先 scp 拉回；
#   当天 SOLO 列表无 kejuxiangshi 时跳过本脚本。默认查当天 ts；传第 2 参指定日期如 2026-07-30）
"$PY" -c "
import json,sys,re,collections,ast
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass
path=sys.argv[1]; day=sys.argv[2] if len(sys.argv)>2 else None
d=json.load(open(path,encoding='utf-8'))
norm=lambda q: re.sub(r'[\s　，。、,.?!:;（）()【】]+','',q or '')
new={k:v for k,v in d.items() if (not day) or v.get('ts','')[:10]==day}
print('keju_ai_cache 总条数:',len(d),'| 本次检查(ts[:10]=='+str(day)+'):',len(new))
hard=[(k,v) for k,v in new.items() if v.get('answer') not in list(v.get('options',{}).values())]
print('\n[门1] answer 不在四选项(硬错误/AI幻觉):',len(hard))
for k,v in hard[:10]: print('   ! answer=',repr(v.get('answer')),'opts=',v.get('options'),'Q:',(v.get('question') or k)[:50])
qn=collections.defaultdict(set)
for k,v in d.items():
    a=v.get('answer')
    if a: qn[norm(v.get('question',k))].add(a)
conf={q:a for q,a in qn.items() if len([x for x in a if x])>1}
print('\n[门2] 同题多答案冲突(全量):',len(conf))
for q,a in list(conf.items())[:10]: print('   ?',list(a),'Q:',q[:50])
tk={}
for line in open('agent/custom/recognition/tiku.txt',encoding='utf-8'):
    mm=re.match(r'\x22(.+?)\x22:\s*(\[.+\])', line.strip())
    if mm:
        try: tk[norm(mm.group(1))]=set(ast.literal_eval(mm.group(2)))
        except Exception: pass   # 跳过含 nbsp 等非标准行
diff=[(k,v.get('answer'),tk[norm(v.get('question',k))]) for k,v in new.items() if norm(v.get('question',k)) in tk and v.get('answer') not in tk[norm(v.get('question',k))]]
print('\n[门3] tiku 已有该题但 AI 答案不符:',len(diff),'(tiku 解析',len(tk),'题)')
for k,a,t in diff[:10]: print('   != AI:',repr(a),' tiku:',t,'Q:',k[:40])
print('\n三门全 0 = 本次新题干净。门1 硬错误应删该 cache 条让其重答（keju 不读 tiku，回填 tiku 无效）。')
" agent/data/keju_ai_cache.json 2026-07-30
```

### 4.1 节点级追踪 + 截图视觉分析（8/6 实战补充）

```bash
# A) 按时段筛 on_error（定位副本段/某任务段的 on_error 分布）——远程 grep 中文 0 命中，用 python
ssh imac@100.116.176.34 'python3' <<'PY'
import os
from collections import Counter
oe="/Users/imac/dev/Maa_MHXY_MG/debug/debug/on_error"
fs=sorted(f for f in os.listdir(oe) if f.startswith("2026.08.06"))
print("by name:", Counter(f.split("_",1)[1].replace(".png","") for f in fs).most_common(15))
for f in fs:
    if "17.08" <= f[11:16] <= "17.35": print("fuben段:", f)   # 按时段筛
PY

# B) 按线程(Tx)追踪单账号节点序列——5 账号在 maafw 按 Tx 线程交错，
#    先 grep "task start" 拿各账号 Tx（队长从副本段起固定某 Tx），再筛该 Tx 的 reco hit
"$PY" -c "
import re,collections
lines=open(r'debug/debug/maafw.bak.<覆盖该时段>.log',encoding='utf-8',errors='replace').read().splitlines()
hits=[(i,l[11:19],m.group(1)) for i,l in enumerate(lines)
      if '[Tx45157]' in l and (m:=re.search(r'reco hit \[result\.name=([^\]]+)\]',l))]
seg=[h for h in hits if h[0]>=<任务起始line>]      # 例：队长 bangpai 段
print('命中:',len(seg)); [print(f'{c:5} {n}') for n,c in collections.Counter(h[2] for h in seg).most_common(20)]
for h in seg[:12]+seg[-15:]: print('L%d %s %s'%h)  # 头12/尾15 看入口与终态（真完成 vs 假成功）
"
#   .bak 选哪份：maafw.bak.<时间戳> 的时间戳=滚动时刻（上一份 log 结束写入），
#   覆盖该时间戳之前的时段。例：18.00.27 的 bak 含 17:25–18:00（副本+队长 bangpai）。

# D) 【08-15 实战补充】maafw 时间覆盖自检——别猜 bak 覆盖哪段，先读首行时间戳。
#    maafw.log 的起点=最后一次滚动时刻（不是任务时刻！），21:07:02 滚动的 log
#    首行就是 21:07:02，拿它查 21:07:12 的窗口必然 0 行（白白怀疑设备断连）。
"$PY" -c "
f=open(r'debug/debug/maafw.log',encoding='utf-8',errors='replace')
print('first:',f.readline()[:40])
f.seek(0,2); sz=f.tell(); f.seek(max(0,sz-3000))
print('tail :',f.read().splitlines()[-1][:40])
"
#    然后按 [起点,终点] 选 bak 链。选错 bak 的典型症状：目标窗口内 0 行/0 命中。

# E) 【08-15 实战补充】单任务假成功三步定位法（编排窗口 → task start 拿 Tx → 该 Tx 的命中序列）：
#    ① 编排日志夹出可疑窗口（如 21:07:12–21:07:40）；② 在覆盖窗口的 bak 里找
#    'task start' + 'entry=<任务名>' 拿该任务的 Tx 线程号；③ 筛 [TxNNNNN] 打印
#    命中序列 + save_on_error + Task.Succeeded——假成功的形态一目了然：
#    入口 hit → (DirectHit 也 hit) → N×20s 全 miss → on_error → 空节点 → Succeeded。
#    同时把该窗口 NextList.Starting 的 details JSON 里 "list":[...] 打出来，
#    即可看到"卡住时它在等什么候选"（如 wabaotu 在等 [打开背包, 兜底点击图标]，
#    而画面停在大地图两者必 miss —— ⑲）。
#    注意行内时间戳格式是 [2026-08-15 21:07:19.637]，匹配用
#    re.match(r'\[2026-08-15 (\d{2}:\d{2}:\d{2})', line) 取 group(1) 比窗口，
#    别用 line[:26] 之类的切片（08-15 踩过：maafw.log 行首是 '['，切片对不上 0 命中）。

# C) 截图视觉分析：timeout/on_error 截图用图像分析工具读"卡在哪一帧"
#    prompt 要点：当前界面（主界面/战斗/挖宝/弹窗）、可见中文、角色是否卡异常状态、为何超时。
#    例：8/6 shimen 截图→"装备上交"弹窗+验证码（⑭）；wabaotu→"挖宝中"没回主界面（⑮）。
```

## 5. 已知问题速查表

| # | 症状 / 日志特征 | 判定 | 处置 / 说明 |
|---|---|---|---|
| ① | `活动-运镖-开始-点击参加` on_error ×5（每号一张），yunbiao 仍"完成" | **设计内出口，但"完成"≠"押满3镖"** | `yunbiao_renwu2.json` 该节点 `on_error:[运镖完成onerror]` 是正常出口（3 镖跑完后 `参加` 的 `next` 找不到押送选项）。**但必须按号校验 `点击押送普通镖_确定` 命中数 = 每号 3 次**；某号 <3 = 假完成（见 ⑩）。循环设计：`参加`(Click) → `[JumpBack]押送`(`max_hit:3`) → `确定` → 弹栈回 `参加` 重扫 next，3 次后押送按钮消失改走 `运镖中` 等待，全 miss → on_error |
| ② | `海底秘境-点击关卡` on_error ×1（偶发，随后自恢复） | **瞬态、自恢复** | 秘境关卡间面板 OCR 短暂 miss → 20s 超时 → 空节点弹栈回继续；非 bug |
| ③ | 某任务"完成"但耗时异常短（如 mijing 47s）；maafw 里该任务走 `on_error→空节点` | **静默成功（假成功）** | 全局 `on_error:["空节点"]` 让失败也报 `Tasker.Task.Succeeded`。`run_5r.run_task` 只看 `job.done` 无法察觉。详见 [[on-error-kongjiedian-false-success]]。要堵需加 sentinel 校验 |
| ④ | mijing 某号跑到第 26~28 关、贴 2400s 超时 | **第25关停止门漏判** | `海底秘境-指定关卡结束任务`(expected `["第25关"]`) 靠单帧 OCR；MaaFw `only_rec` 重识别偶发把"第25关·挑"读成乱码 → 漏停 → 冲关。用计数器停止更稳。详见 [[maafw-ocr-expected-and-only-rec]] |
| ⑤ | `debug/debug/` 出现 5 份相同 `maafw.bak.<同时间戳>.log` | **日志轮转竞态（无害）** | 5 Tasker 并发越过 size 阈值各转一份；只占磁盘，不影响自动化 |
| ⑥ | `Exception ignored on calling ctypes callback ... AttributeError/TypeError in ocrNum.py` | **自定义识别/动作回调崩** | MaaFw 吞掉异常、当 no-hit（非致命，但该步少干活）。定位：traceback 里的文件:行号。曾修过 `OCRVitality`（`re.match` 返回 None 未守卫）、`OCRNum.convert_to_int`（返回 AnalyzeResult 致 `num>=50` TypeError） |
| ⑦ | autolife 报 `>14400秒` 超时，但 5r 日志有 `=== 全部完成 ===` | **autolife 假超时**（非 5r 失败） | 见 Step 6。一般是 server 没重启跑旧 jobs.py + 遗留 adb server 持有 stdout 管道 |
| ⑧ | 某号全程各项都最慢 | **实例慢**（渣中 idx6 常见） | MuMu 实例特性，非脚本问题；但要留意它会最接近超时上限 |

| ⑨ | keju 全员 ~50s"完成"，但原生层 `进入答题界面`/`活动-科举乡试-开始`/`开始答题` 命中 **0**；`活动-科举乡试-向上滑动到顶端` on_error ×N | **科举假成功** | 科举没开门（或已答过 / 非活动日）→ 找不到"开始"按钮 → `向上滑动到顶端`(上滑) ↔ `[JumpBack]baotu_huodong_down`(`max_hit:6` 下滑 helper) 循环滚屏 → 耗尽 → 该节点 `next` 20s 超时 → 全局 `空节点` → 报"完成"。`未到开始时间` 模板没兜住（面板状态不匹配）。`run_5r.run_task` 只看 `job.done` 察觉不到。**堵法**：sentinel（见 [[on-error-kongjiedian-false-success]]），或给 `向上滑动到顶端` 显式 `on_error:[panduan_zhujiemian]` 并排查 keju 时间窗 / 把 keju 排到其活动时段内 |

| ⑩ | yunbiao 某号"完成"且耗时不算很短（~480–510s），但 `点击押送普通镖_确定` 只命中 1~2 次（应 3）；trace：`押送1→确定1→运镖中×N→[5~7分钟 点击押送普通镖银(三次) OCR 全 miss]→活动-运镖-开始-点击参加 on_error→运镖完成onerror` | **运镖假完成（只跑 1~2 镖）** | `活动-运镖-开始-点击参加` 的 next 只有 `[押送, 运镖中, 战斗中-等待20秒]`，**无兜底回主界面/重开面板的回旋门**；且 JumpBack 回 `参加` 后**不重新 Click 参加**（只重扫 next）。第 1 镖运到后押送面板若没回到可识别状态（被奖励/奇遇弹窗遮挡，或需重点"参加"），押送 OCR（roi `[933,262,307,237]`）持续 miss → 干等到 on_error 假完成。07.26 渣中/缤纷/6130 均 `确定×1`，队长/晚风 `确定×3` 正常。**判别**：Python 解析 maafw 按号（Tx→角色见 §1）数 `点击押送普通镖_确定`，<3 即中。**堵法**：参加节点 next 末尾加 `[JumpBack]panduan_zhujiemian` 或"重开运镖面板"回旋门；或 run_5r 加 sentinel 校验押送次数 |
| ⑪ | `agent/data/account_info.log` 全行 `账号ID: (未知账号)`，但金币/银币数字正常；loguru（`debug/custom/<日期>.log`）里同号 `[logOcr] 暂存账号ID: <数字>` 与 `[logOcr] 已写入: ...(未知账号)` **共存**，0 异常；maafw 里 `账号信息-记录账号ID`/`记录金币银币` 节点均命中、`_logOcr_probe`(logOcr 内部 OCR 探针，经 `context.run_recognition` 调用，日志格式是 `[MaaContextRunRecognition]` **不是** `reco hit`) 命中 >0 | **logOcr `_PENDING` key 失配（5 开并发）** | `agent/custom/action/logOcr.py:74,84` 用 `id(context.tasker)` 做跨节点暂存 key。5 开并发下 MaaFw 每次 Custom Action 调用传入的 tasker 代理对象不同 → `id()` 在 mode=id 与 mode=coins 两次不同 → `pop` 拿不到写入的条目 → 返回 `"(未知账号)"`（`logOcr.py:84`）。**单开测试不复现**（07.26 凌晨 00:17 单开正常）。`(未知账号)`≠`(未识别)`：前者=跨节点 key 丢了，后者=单次 ROI OCR 失败（`logOcr.py:53`，会写成 `账号ID: (未识别)`）。**堵法（已实施 07.27）**：logOcr 改用 `context.tasker.controller.info["adb_serial"]`（如 `127.0.0.1:16576`，通过 controller 的 C handle 查、**不依赖 Python wrapper 对象身份**，跨节点稳定）做账号标识；pipeline 删掉 mode=id 整条人物界面链（`zhanghao_xinxi.json` 7 节点→4 节点），只留 `打开背包→记录金币银币` 单 logOcr 节点。**渣中(16576) 单跑实测通过**：`账号: 127.0.0.1:16576 | 金币: 246537 | 银币: 5865248`，不再 `(未知账号)`。判此 bug 是否复现：看 account_info.log 是否出现 `账号: <ip:port>`（新）而非 `账号ID: (未知账号)`（旧） |
| ⑫ | fuben115 报"完成"但**最后一个副本(50普通-3)没打 + 2 轮鬼没抓**；maafw 里 `115点击地图-百晓仙子-70普通-3` 走 `on_error→空节点`(on_error 截图 OCR 到"师门任务/张百忍来信"而非"选择副本")，`捉鬼-fuben桥接`/`抓鬼轮次计算-max`/`捉鬼-结束` 全 0 命中 | **副本链式假成功(导航节点无兜底)** | `fuben115.json` 是 6 副本线性链 + `[JumpBack]捉鬼-fuben桥接`：`50侠士①→70侠士→50侠士②→50普通-1→50普通-2→50普通-3→捉鬼桥接(2轮鬼)`，每个"完成-退出"后 next 到下个副本的导航。**导航节点(如 `115点击地图-百晓仙子-70普通-3`)next 只有 `[JumpBack]再次点击地图百晓仙子, 选择副本-X]`，无 `[JumpBack]panduan_zhujiemian` 回主界面兜底**。前副本退出时画面若没回长安城小地图(被师门/活动弹窗带偏)，小地图模板 + "选择副本"OCR 连续 miss → 20s 超时 → 该节点无 on_error → 全局空节点 → `PipelineNode.Succeeded` → **该副本及后续整链(含 2 轮鬼)全跳过**，编排层只看 job.done 报"完成"。07.30 实证：6 个"完成-退出"节点里前 5 个都 hit=1，第 6 个(50普通-3)卡在导航 on_error(17:42:35)，`捉鬼-fuben桥接` 0 命中。**判别**：fuben115 用时正常(~2000s) 不可信，须去 maafw 数 6 个"副本完成-退出"节点(`115-50侠士-`/`115-70级-`/`115-50普通-1/2/3-副本完成-退出`)是否全 hit + `捉鬼-fuben桥接` 是否 hit。**堵法**：①每个导航节点 next 末尾加 `[JumpBack]panduan_zhujiemian`(回主界面重开小地图，全项目通用兜底，CLAUDE.md 技巧1)；或②run_5r 加 sentinel 校验"6 副本完成-退出 + 捉鬼桥接"命中数 |
| ⑬ | 副本 5 本里**后几本骤降到 30s 级**（506/541/362→**34/34**s）+ 捉鬼 31s；maafw `fuben69new-打开小地图-百晓仙子`/`打开小地图-钟馗` on_error；on_error 截图显示**小地图没打开**（角色长安城主界面、小地图面板关） | **副本连续进本 UI 时序假完成** | `打开小地图-百晓仙子` 是 DirectHit+ClickKey 61（小地图快捷键），action 报 success 但前一本刚结算退出、长安城界面未稳定 → 按键被吞 → 后续识别 NPC 20s 全 miss → 空节点假完成。前几本间隔够不受影响。**堵法（已实施 93badad）**：run_5r.team_run 每本前插 `_barrier_reset`（sleep5+打开大地图）让 UI 稳定 |
| ⑭ | shimen 某号贴 2400s 超时；超时截图是**"装备上交"弹窗**（"您的'金缕羽衣'是价值较高的装备…输入验证码 2243"+数字键盘） | **师门高价值装备上交验证码弹窗未处理** | 上交高价值装备时游戏弹验证码确认，pipeline 无对应节点 → 卡死。**随机性**：仅上交高价值装备才触发。**堵法**：识别弹窗→OCR 验证码→输入→确认（或跳过高价值装备上交） |
| ⑮ | 某号某任务超时后，**后续 N 个任务全 ~41s"完成"**（非 0s、非超时）；on_error 集中在那些后续任务的入口节点 | **前一任务卡死污染后续（连锁假成功）** | 前任务（如 wabaotu 卡"挖宝中"）超时后角色没回主界面；后续任务入口都先识别"主界面"，在错误界面 miss → 空节点 done=True（41s ≈ 2×20s next 超时）。判别：一串任务恒定 ~41s。**堵法（已实施 93badad/a3add01）**：①solo_all 每个 entry 前插 `_barrier_reset`；②run_task 加 `sentinel=_sentinel_main`（done 后校验主界面，假完成计失败） |
| ⑯ | bangpai_renwu 全员 1500–2400s（远超 typical）；maafw 队长 `帮派任务单次的链`×46 + `战斗-等待20秒`×60；部分账号超时截图停**长安城"自动寻路中(藏宝图)"**（没进帮派） | **帮派战斗爆炸 + 寻路异常** | ①单次链未收敛/完成判断不灵，反复接-打-提交；②部分账号寻路目标异常（藏宝图而非帮派 NPC）。**堵法**：待定（查单次链完成判断、寻路目标） |
| ⑰ | shimen 某号 30~40s"完成"（08.03 全员 35s / 08.07 队长 34s+渣中 33s，正常应 250~670s）；maafw trace `师门任务开始-v2` next 候选（OCR"师门任务"）全 miss→on_error→空节点→Task.Succeeded；**on_error 截图显示角色卡在大地图**（南赡部洲、任务面板折叠，OCR"师门任务"必然 miss） | **shimen 入口缺主界面前置 + barrier 传送留大地图** | `师门任务开始-v2` **无 recognition 永远命中**，next 直接找师门任务；barrier（`打开大地图_69副本`→`传送长安城_69副本` Click tab + post_delay 仅 1000ms）传送不稳定把角色留大地图时，入口 OCR 全 miss。**其余 8 个 SOLO entry 入口均有主界面图标前置**（TM `chenghao/jiahao/baoguo`，大地图上 miss→`[JumpBack]panduan_zhujiemian` 兜底回主界面），容忍了 barrier 传送不稳定，**唯 shimen 缺前置**。判别：shimen 用时 <60s + on_error 截图非主界面。**堵法（已实施 20856c1）**：`师门任务开始-v2` next 开头加 `[JumpBack]panduan_zhujiemian_ks`（仿 bangpai 入口：inverse+TM 主界面图标+ClickKey back，不在主界面就按 back 关大地图，JumpBack 回入口重扫）。注意 `_sentinel_main`（主界面模板）抓不住此假成功——角色停在大地图时 `zai_zhujiemian` 模板仍宽松命中 |
| ⑱ | 副本进入"失败重试"基于 stale 帧误点；maafw trace `fuben69new-进入-侠士-1-失败重试` HIT（OCR"进入" box[351,579]）后 action Click[360,602]（左下角"进入"按钮位），但同窗口下一帧 OCR 已是因缘绘——**队员配对完毕后会短暂回"进入"界面再切因缘绘**，此间隙命中失败重试，action Click 时画面已切因缘绘，**误点因缘绘界面左下角** | **recognition↔action 时间差 + 画面切换（stale 帧）** | MaaFw 节点生命周期：识别（帧 A）→ pre_delay → action（帧 B，画面可能已切）。配对间隙识别到"进入"按钮（帧 A=进入界面），Click 时画面已切因缘绘（帧 B）→ 点到因缘绘左下角无关区域。判别：进本点击坐标落在"进入"按钮 roi 但该次点击后副本未进、反而在因缘绘界面产生异常点击副作用。**堵法（已实施 052bb5f）**：`失败重试` 改**过渡节点**（OCR"进入" + DoNothing + post_delay 1000），next 指向 `[失败重试-确认, 空节点]`；新增 `-确认`（再 OCR"进入" + Click）= **double check**：第二次识别（≥1s 后，stale 帧已过）命中才点（真还在进入界面），miss（已切因缘绘）走空节点不点，子链结束 JumpBack 回侠士-1。**通用模式**：重要点击若依赖易变画面，拆"识别过渡→再识别点击"两节点，两次都命中才点；`[确认, 空节点]` 结构让 miss 时优雅跳过而非误点 |
| ⑲ | wabaotu 某号 ~28s"完成"（正常 263–818s）；maafw trace：`wabaotu_qingli`→`清理主界面使用-关闭完成`→`wabaotu`(DirectHit 命中) → 该节点 next `[挖宝图-打开背包-成功-并向上滑动到顶端, [JumpBack]背包界面-按键打开失败-点击图标]` 连续 ~22s 全 miss → `on_error 落图 wabaotu.png` → `空节点` → `Task.Succeeded`；**on_error 截图显示角色卡在南赡部洲大地图**（非主界面，背包打不开、图标形态不同） | **wabaotu 入口缺主界面前置（⑰ 同族）** | barrier「打开大地图」传送不稳定把角色留在大地图时，`wabaotu`（DirectHit，永远命中）照样进入，但后续"打开背包"在大地图必然 miss，唯一兜底 `[JumpBack]背包界面-按键打开失败-点击图标` 也 miss（大地图上无背包图标）→ 干等超时 → 空节点假成功，**背包宝图 0 张挖**。08-15 job2 离歌 28s 实证（job1 同任务全员正常 → 偶发，barrier 时序决定）。**堵法**：`wabaotu` 节点 next 开头加 `[JumpBack]panduan_zhujiemian_ks`（⑰ 同款：inverse 主界面检查 + ClickKey back 关大地图回主界面），或显式 `on_error:[panduan_zhujiemian]`。判别：wabaotu 用时 <30s + on_error 截图 = 大地图 |

### 5.1 每轮"必触发"的 on_error（看到别慌，逐个对号）

健康跑也会**稳定产出**这些 on_error 截图——多为设计内出口，**不是 bug**。但其中混着假成功（⑨），所以判定真假的关键 = **去原生层 grep 该任务"真干活"的节点是否命中**（耗时短只是信号，节点命中才是证据）：

| on_error 节点 | 频率 | 出口去向 | 验"真活了"的 grep（命中应 >0） | 判定 |
|---|---|---|---|---|
| `活动-运镖-开始-点击参加` | ×5（每号1） | `运镖完成onerror`（确认主界面） | **`点击押送普通镖_确定` 应每号 3 次**（⚠ 不是 `点击押送普通镖银(三次)`——后者带 `(三次)` 是**节点名**不是命中次数，它 `max_hit:3` 限制自身最多命中 3 次） | ⚠ 出口设计内，但**确定×3 才是真跑完**；某号 `点击押送普通镖_确定` <3 = 假完成（见 ⑩） |
| `藏宝图-背包使用` | ×5（每号1） | `挖宝图完成判断`→`挖宝图完成`（空背包） | `藏宝图-主界面使用` 应数十次（真挖图） | ✅ 设计内：背包图挖完后找不到"使用"→ on_error 复查；**但靠 20s timeout 判完成，略糙**（可清理） |
| `活动-科举乡试-向上滑动到顶端` | ×5（每号1） | 全局 `空节点` | `进入答题界面` / `开始答题_普通` / `开始-点击参加` 命中应 >0 | ❌ **假成功**（见 ⑨）：没答就报完成 |
| `点击副本-开始战斗` | ×1 偶发 | 副本状态机内 | 副本正常完成即可 | ✅ 瞬态自恢复（`[JumpBack]` 回旋门重扫即过） |

> 口诀：**「每号恰好 1 张 + 该任务耗时正常」通常是设计内出口；但耗时异常短（<60s 的答题/挖图类）必查节点命中，提防空节点假成功。**

### 5.2 排查后归档（硬性规则 6 的落地）

每次排查完（remote 模式日志宿主在 Mac），把当天日志拷到 Win 本地统一归档 + 写报告。目录约定（已建，见 `debug/mac log/README.md` 索引）：

```
debug/mac log/
├── README.md                # 归档索引（日期/文件数/有无 report/当天要事）
└── <YYYYMMDD>/
    ├── run_5r_<YYYYMMDD>_*.log   # 当天全部编排日志
    ├── on_error/                 # 当天【过滤后】的 on_error 截图（只留异常取证）
    └── report.md                 # 当天分析报告
```

**report.md 必含**（即硬性规则 1/3/5 的产出落盘）：每个 job 一章的三份量化报告（耗时矩阵/账号变化/科举——含 cache 命中统计）+ team 阶段核实表 + 异常根因（节点级 trace + 截图画面结论）+ on_error 分布表 + 结论与待办。当天多 job 写一份 report.md 分章节，不拆多份。

**⚠ on_error 只归档"异常取证"截图——设计内退出门 / 已知重复错误不拷贝**（08-15 用户要求；当日实测过滤后 44→4 张，整档 710MB→52MB）。过滤 = 不在下列 ROUTINE 名单内的才拷：

```python
ROUTINE = {  # 设计内出口/瞬态自恢复/已知问题截图，report 里计数即可、不留图
  '活动-运镖-开始-点击参加',            # ① 运镖 3 镖跑完的设计内出口
  '宝图完成判断-再次检查','藏宝图-背包使用',  # 空背包复查的设计内出口
  '出售阵法',                            # baitanchushou 摆摊流程内出口
  '师门任务-任务分支-装备提交确认-输入验证码',  # ⑭ 已知验证码弹窗（同日大量重复，留 1 张也只在 report 引用）
  '点击打工','队长踢人-选人','点击副本-开始战斗',  # 瞬态自恢复
}
# 注意：过滤是"拷贝时排除"，不是"分析时忽略"——report 的 on_error 分布表仍要给 ROUTINE 节点的计数。
```

**拷贝命令**（08-16 修正版；on_error 在 Mac 侧先过滤再传，全量日志仍留 Mac 原地）：

```bash
# 1) 编排日志：scp 通配（文件名纯 ASCII，直接拷）
scp -q "imac@100.116.176.34:/Users/imac/dev/Maa_MHXY_MG/debug/run_5r/run_5r_<YYYYMMDD>_*.log" "debug/mac log/<YYYYMMDD>/"

# 1b) loguru 日志（报告三·cache 命中统计的数据源，跑过 keju 的日期才需要）
scp -q "imac@100.116.176.34:/Users/imac/dev/Maa_MHXY_MG/debug/custom/<YYYY-MM-DD>.log" "debug/mac log/<YYYYMMDD>/"

# 2) on_error（中文文件名）：分两步——先远端 unicode_escape 列清单（纯 ASCII 回传，绕开终端 GBK），
#    本地过滤 ROUTINE 后逐个 scp（单引号包 remote spec，scp 认 UTF-8）。
#    ⚠ 08-16 踩坑：把中文名单经 ssh stdin/命令行传给远端 python 做过滤**不可靠**——
#    转义层级随传递方式漂移（`{set!r}` 里 '\\u6d3b' 一层变两层），症状是**静默全过滤（keep=[]）**
#    而非报错。结论：**过滤逻辑放本地**（远端只负责 ASCII 通道传输），绝不内联中文进远端脚本。
PY="C:/Users/zhiyuan/AppData/Local/Programs/Python/Python313/python.exe"
"$PY" - <<'PY'
import subprocess,os
d='20260816'                                              # 改日期
ROUTINE={'活动-运镖-开始-点击参加','宝图完成判断-再次检查','藏宝图-背包使用',
         '出售阵法','师门任务-任务分支-装备提交确认-输入验证码',
         '点击打工','队长踢人-选人','点击副本-开始战斗'}
# 2a) 远端列文件：节点名按 unicode_escape 输出（纯 ASCII，稳定跨编码）
script=("import os\n"
        "oe='/Users/imac/dev/Maa_MHXY_MG/debug/debug/on_error'\n"
        "for f in sorted(os.listdir(oe)):\n"
        "    if f.startswith(%r):\n" % ('2026.'+d[4:6]+'.'+d[6:],) +
        "        print(f.split('_',1)[0], f.split('_',1)[1].rsplit('.',1)[0].encode('unicode_escape').decode())\n")
p=subprocess.run(['ssh','imac@100.116.176.34','python3'],input=script.encode('ascii'),capture_output=True)
rows=[l.split(' ',1) for l in p.stdout.decode('ascii').splitlines() if ' ' in l]
# 2b) 本地过滤（escape 后的节点名反转义回中文再比）
keep=[(ts,n.encode('ascii').decode('unicode_escape')) for ts,n in rows
      if n.encode('ascii').decode('unicode_escape') not in ROUTINE]
# 2c) 逐个 scp（数量少——过滤后通常 0~5 张；文件名含中文用单引号包 remote spec）
for ts,node in keep:
    fname=f"{ts}_{node}.png"
    dst=rf'debug\mac log\{d}\on_error\{fname}'
    subprocess.run(['scp','-q',
        f"imac@100.116.176.34:'/Users/imac/dev/Maa_MHXY_MG/debug/debug/on_error/{fname}'",dst],
        check=True)
    print('copied',fname)
print('total kept',len(keep))
PY
```

**不归档 maafw 原生日志**（16MB+ 轮转、总量 GB 级）——需要时 ssh 到 Mac 现查；report.md 里注明关键 trace 所在的 bak 文件名 + 时间窗，便于回查。

## 6. 关联

- **记忆**（`~/.claude/projects/.../memory/`）：`maafw-ocr-expected-and-only-rec`、`on-error-kongjiedian-false-success`、`maafw-resource-not-thread-safe`、`maa-pipeline-task-entry-pattern`、`mac-5r-remote-logs-lookup`、`custom-recognition-singleton-shared-5r`。
- **项目文档**：`CLAUDE.md`（pipeline 节点/next/on_error 机制）、`docs/description/状态机设计模式.md`。
- **相关代码**：`agent/run_5r.py`（编排 + ROLES + TIMEOUTS + `_RoleTagSink` 角色打标）、`agent/custom/recognition/{AIAnswer.py,tiku.txt,searchAnswer.py}`、`agent/custom/action/{logOcr.py,shopScan.py}`、`assets/resource/base/pipeline/`（各任务 JSON）、`assets/resource/base/default_pipeline.json`（全局 on_error→空节点）。
- **归档**：`debug/mac log/<日期>/report.md`（历史报告，`README.md` 为索引）。

## 7. 排查心法

1. **先分清层**：编排日志说"谁/哪个任务/多久"，原生日志说"为什么"。先编排定位、再原生取证。
2. **先判生死**：有 `=== 全部完成 ===` → 5r 没死，问题在调度层或某任务的"假成功"；没有 → 5r 中途崩/超时。
3. **耗时是最强信号**：同批 5 个号横向比，异常短=静默失败，异常长=卡循环/冲关。
4. **on_error 不等于失败**：很多是设计内出口（运镖）或自恢复（秘境瞬态），按节点计数 + 对照已知表再下结论；耗时短只是信号，**节点命中才是证据**。
5. **maafw 日志巨大**：永远 grep，永不整读；先 grep `\[tag\]`（编排日志）建 Tx→角色 表（§1.1），再按 Tx/时间窗把 5 路交错线程拆开。
6. **"完成却超时"先查调度**：5r 正常退 + autolife 超时 = 几乎必然是管道/退出/部署问题，不要去 5r 任务里找原因。
