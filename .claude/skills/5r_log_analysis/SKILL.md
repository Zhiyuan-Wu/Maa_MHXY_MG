---
name: 5r-log-analysis
description: 排查"梦幻西游五开"（agent/run_5r.py）运行日志的 SOP。当需要分析 debug/run_5r/*.log（Python 编排层）或 debug/debug/maafw*.log（MaaFw 原生层）来定位任务失败/超时/静默成功/on_error/自定义回调崩溃，或诊断"autolife 4h 假超时"时使用。
---

# 5r 日志排查 SOP

`agent/run_5r.py` 是五开编排（单进程、每账号独立 Resource、原生 pipeline + 墙钟超时）。日志分**两层**，排查时要分清看哪层。本 SOP 总结自 2026-07 的几次实战排查。

## 0. 日志在哪

| 层 | 路径 | 内容 | 注意 |
|---|---|---|---|
| 编排层（Python） | `debug/run_5r/run_5r_<YYYYMMDD>_<HHMMSS>.log` | 每步墙钟耗时、`>>>`/`<<<` 起止、超时 `!!!`、异常 traceback | **UTF-8、行缓冲、已带时间戳**。直接 Read 即可，文件小 |
| 原生层（MaaFw） | `debug/debug/maafw.log` + `maafw.bak.<时间戳>.log` | 每个 recognition/action 的命中/失败、OCR 文本、on_error、节点 trace | **单文件 5 路线程交错、文件巨大（16MB+ 轮转）**。**只 grep，不要整读** |
| on_error 截图 | `debug/debug/on_error/<时间戳>_<节点名>.png` | 每次 on_error 落一张截图（文件名=触发节点） | 按文件名直接统计节点出现次数最省事 |

> maafw.log 在 ~16MB 时轮转成 `maafw.bak.<时间戳>.log`。多 Tasker 并发时偶尔会轮转出 **5 份相同 bak**（日志轮转竞态，无害）。

> **remote 模式（Mac cli_server 执行，8/6 起为常态）**：日志/截图全在 Mac（`100.116.176.34`，ssh 用户 `imac`，仓库 `/Users/imac/dev/Maa_MHXY_MG`），本地没有。流程：①curl cli_server 确认 job → ②ssh/scp 拉到本地 `debug/` → ③按下列 SOP 分析。
> ```bash
> curl -s http://100.116.176.34:5090/jobs    # job 列表 + exit（exit=0 ≠ 成功，见 ③）
> curl -s http://100.116.176.34:5090/health  # current_job 是否还在
> ssh imac@100.116.176.34 'ls -lt /Users/imac/dev/Maa_MHXY_MG/debug/run_5r/run_5r_<YYYYMMDD>_*.log | head'
> scp imac@100.116.176.34:/Users/imac/dev/Maa_MHXY_MG/debug/run_5r/run_5r_<...>.log ./debug/run_5r/
> # 截图（中文文件名：单引号包整个 remote spec，scp 认 UTF-8）
> scp 'imac@100.116.176.34:/Users/imac/dev/Maa_MHXY_MG/debug/debug/timeout/<中文>.png' ./shot.png
> ```
> **坑**：`ssh mac 'grep 中文 file'` 远程 grep 中文 **0 命中**（locale 不通）——改用 `ssh mac 'python3' <<'PY' ... PY` 读文件（`repr`/`os.listdir` 输出避开终端 GBK）。详见 [[mac-5r-remote-logs-lookup]]。

## 1. 账号 / 实例对照表（排查必备）

编排层日志里是角色名（队长/渣中/6130/缤纷/晚风）；原生层 + PowerShell 里是端口/MuMu 索引。对照（来自 `run_5r.py::ROLES`，端口约定 `16384+32*idx`）：

| 角色 | adb 端口 | MuMu idx |
|---|---|---|
| 缤纷 | 16448 | 2 |
| 晚风 | 16480 | 3 |
| 队长 | 16512 | 4 |
| 6130 | 16544 | 5 |
| 渣中 | 16576 | 6 |

> **渣中（idx6）一贯最慢**——实例特性，不是 bug（每次排查都会看到它的 shimen/baotu/mijing 耗时偏高）。
>
> **角色名会变，脚本勿硬编码**：`run_5r.py::ROLES` 改名后（如 07.30 把 4 号位「缤纷」改成「欧阳」，端口 16448 / MuMu idx2 不变），编排日志的角色名随之改变。耗时矩阵脚本必须**从日志动态读 roles**（见 §4），写死 `['队长','渣中','6130','缤纷','晚风']` 会让改名号整列变 `-`，造成"该号一个任务都没跑"的误判。端口→MuMu idx 的 `16384+32*idx` 约定不变，原生层按**端口 / Tx 线程**归号最稳。

## 2. 排查流程（按顺序）

### Step 1 — 确认这次 run 的边界
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
3. **全员完成校验**：最后一项（通常 `kejuxiangshi`）5 个号是否都 `<<< 完成`。缺谁谁就在那个任务或上一个出了问题。

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

排查不仅定位故障，还要量化"这轮到底跑得怎么样"。**每次排查必须产出以下三份报告并贴进结论**，缺一不可：

| 报告 | 回答的问题 | 命令 |
|---|---|---|
| 一：耗时矩阵 + 异常标记 | 每号每个任务多久？哪些偏离典型基线 / 超时？ | §4 矩阵脚本 |
| 二：账号金币·银币变化量 | 这轮跑完，每号钱是涨是跌、涨跌多少？ | §4 account 脚本 |
| 三：科举新入库答题质量 | keju_ai_cache.json 本次新存的 AI 答案有没有不在选项里 / 自相矛盾 / 与 tiku.txt 不符？ | §4 新题质量脚本 |

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
| shuangbei | 20–25s | >60s | 几乎恒定 |
| fuli_qiandao | 30–50s | >120s | |
| shimen_renwu | 250–430s | >600s / 贴 2400 | 07.24 全员超时 = UI 变更（见下实例） |
| yunbiao_renwu2 | 90–360s | >1000s / 贴 2400 | ⚠ **"完成"≠跑满 3 镖**，必须按号校验 `点击押送普通镖_确定` 命中 = 每号 3（见 ⑩）。07.24 渣中 2352/晚风 2222（卡循环）；07.26 渣中/缤纷/6130 ~480–510s"完成"但 `确定×1` 假完成 |
| baotu_renwu | 470–820s | >1000s | 波动较大 |
| wabaotu_qingli | 30–570s | — | 取决于背包宝图数量 |
| 打开大地图_69副本 | 7–25s | >60s | 纯导航 |
| mijing_renwu | 1300–1700s | >2100 / 贴 2400 | 走关数决定；07.24 缤纷 2257 偏长 |
| sanjieqiyuan | 85–110s | >200s | |
| huoyue_lingqu | 14–20s | >60s | 07.24 晚风 65 偏长 |
| zhengli_baibao | 25–70s | >150s | |
| jiayuan_zhengli | 60–70s | >150s | |
| huoli | 45–55s | >120s | |
| kejuxiangshi | 90–100s（**真答**）| >300s | ⚠ **~50s 往往=没答（假成功，见 ⑨）**，并非"答得快"；07.24 缤纷 620 = 题库/AI 卡题 |

### 实例（07.24，秒，节选关键行）

| 任务 | 队长 | 渣中 | 6130 | 缤纷 | 晚风 |
|---|---|---|---|---|---|
| shimen | X2400 | X2400 | X2400 | X2400 | X2400 |
| yunbiao | 354 | 2352⚠ | 110 | 91 | 2222⚠ |
| mijing | 1301 | X999 | 1626 | 2257⚠ | 1362 |
| keju | 94 | — | 92 | 620⚠ | 95 |

> 渣中 mijing 的 `X999` 是整轮预算压缩后的小上限（**非 mijing 本身卡**）——原生层显示它已推进到第 25 关、停止门命中×3，是 shimen 超时级联的受害者。这是「`≤Ns` 递减 + 后半段 `—`」组合判读的典型案例：先看哪个号后半段全 `—`，再回溯该号哪个任务贴上限吃掉了预算。

### 实例（07.25，秒——全绿，07.24 的问题全部消失）

| 任务 | 队长 | 渣中 | 6130 | 缤纷 | 晚风 |
|---|---|---|---|---|---|
| shimen | 209 | 362 | 243 | 337 | 239 |
| yunbiao | 352 | 524 | 265 | 243 | 466 |
| baotu | 564 | 677 | 639 | 884 | 542 |
| mijing | 1352 | 1661 | 1644 | 1711 | 1154 |
| keju | 50⚠ | 51⚠ | 52⚠ | 51⚠ | 51⚠ |

- **shimen / yunbiao / mijing 全部回归正常区间**：07.24 的 shimen 全 X2400（UI 变更）、yunbiao 渣中 2352 / 晚风 2222（卡循环）、mijing 缤纷 2257（冲关）今天都没了。整跑 0 超时、0 回调崩溃。
- **mijing 0 冲关 = 停止门修复见效**：`pipeline_override` 里 `海底秘境-指定关卡结束任务.expected` 已由 `["第25关"]` 扩成 `["第25关"…"第30关"]`，结局 = 4×第25关停 + 1×shibai（晚风 1154s 失败退，合法出口）。**这是 07.24 已知④的直接修复证据。**
- **keju 全员 ~50s ⚠ 但要警惕**：50s **不是"答得快"，是没答**——全员走 `向上滑动到顶端` 假成功出口，见 §5 ⑨。
- **zhuogui「方案二」限轮器实跑验证通过**：fuben115(+2轮鬼) 阶段 `捉鬼-fuben桥接`→`抓鬼轮次计算-max`(=max_hit)→`捉鬼-结束` 整链各命中 1 次，全日志 0 处旧节点名（`抓鬼轮次计算-max-fuben`/`捉鬼-fuben结束`）。

### 实例（08.06，秒——副本后半 + 连锁假成功 + bangpai 爆炸）

| 任务 | 队长 | 渣中 | 6130 | 欧阳 | 晚风 |
|---|---|---|---|---|---|
| fuben69new(5本) | 506/541/362/**34⚠**/**34⚠** | (队员跟随队长) | | | |
| zhuogui(4轮) | **31⚠** | | | | |
| bangpai | 1494 | X2400 | X2400 | 2226 | X2400 |
| shimen | 833 | 739 | X2400 | 1088 | 421 |
| wabaotu | 625 | 501 | 392 | **X2400** | 397 |
| mijing | 1401 | 1667 | X预算 | **41⚠** | 1156 |
| 欧阳后续7项 | | | | sanjie/huoyue/zhengli/jiayuan/huoli/zhanghao **全 41⚠** | |

> 8/6 集中爆发的**四类新信号**（均见 §5 ⑬–⑯）：
> - **副本后几本骤降到 30s 级**（34/34s）+ 捉鬼 31s = 连续进本 UI 时序假完成（⑬，ClickKey 小地图键被吞）。
> - **某号超时后整串恒定 ~41s**（欧阳 wabaotu 超时→后续 7 任务全 41s）= 连锁假完成（⑮，前一任务卡死没回主界面，污染后续入口）。
> - **bangpai 全员 1500–2400s** = 战斗爆炸 + 寻路异常（⑯）。
> - **shimen 单号超时**（6130）= 装备上交验证码弹窗（⑭，随机触发）。
> - **已修**：⑬⑮ 用 `run_5r` 的 `_barrier_reset`（任务间 sleep5+打开大地图）+ `run_task.sentinel`（done 后校验主界面）堵（commit `93badad`/`a3add01`）；⑭⑯ 待修。

### 报告二：账号金币·银币变化量

`agent/data/account_info.log` 每个 `zhanghao_xinxi` 节点写一行（每号每次 run 一条）：`[时间] 账号: 127.0.0.1:<port> | 金币: <n> | 银币: <n>`。按端口取**最近两条**算 Δ，即"本次 run 相对上一次 run"的净变化（本跑产出 + 期间消耗/收入）。判读：

- **金币 Δ 正常小幅为正**：日常产出 + 押镖/活跃度收入，扣挖宝/修炼等消耗；银币通常缓涨。
- **Δ 异常**：暴负 → 大额消耗（买道具/点技能）；为 0 → zhanghao_xinxi 的 OCR 没刷新/读到旧值（留意已知 ⑪ 复发）；暴正 → 上次没跑、跨多天累计。
- **任一行出现 `账号ID: (未知账号)`**（旧格式）→ 已知 ⑪ 复发，停；正常应为 `账号: 127.0.0.1:<port>`（新格式，07.27 起）。

### 报告三：科举新入库答题质量

> ⚠ 5r 科举答题链路要认准（用户专门纠正过，别搞混）：
> - **`agent/data/keju_ai_cache.json`** = **5r 科举实际使用的题库**（本报告对象）。keju 答题节点 `活动-科举乡试-开始答题API` → `custom_recognition:"AIAnswer"` → `AIAnswer.py` **只加载这一个文件**：`_cache_lookup` 先查、命中即点；miss 才调智谱 AI，答完 `_cache_store` 存回。名义是"AI 缓存"，**事实上就是科举运行时题库**，每跑一次增量。
> - `agent/custom/recognition/tiku.txt` = `searchAnswer.py` 加载的题库，给**别的任务**用（**不是** keju 的 `开始答题API` 节点），这里仅作交叉参照。
> - `agent/custom/recognition/question_bank.json` = **遗留未接入文件**（全仓库 `grep question_bank\.json` 无代码引用），排查时**忽略它，别挂错**。

结构 `{题干key: {answer, options{A,B,C,D}, question, ts}}`，`ts` 形如 `2026-07-30 17:43:28`。**"新入库"按 `ts[:10] == 当天日期` 界定**（最准；也可 `git diff HEAD` 交叉印证）。三道质量门（脚本见 §4）：

1. **answer∉options（硬错误）**：AI 幻觉，答案不在四选项里 → 该题必错、必报（pipeline 命中 cache 即点选，会直接丢分）。
2. **同题多答案冲突**：题干归一化（去标点/空格）后，全量 cache 里同一题出现多个不同 answer → AI 两次答得不一致，存疑。
3. **与 `tiku.txt` 交叉**：tiku 已收录该题的标准答案，但 AI 答案不符 → AI 可能答错（或 tiku 用的是另一可接受答案，结合 options 人工确认）。

**三门全 0 = 本次新题干净**；任一非 0 → 列明细人工复核。门 1 硬错误**只能删该 cache 条让其下次重答**（keju 不读 tiku，回填 tiku 对它无效）。

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
LIM={'shuangbei':60,'fuli_qiandao':120,'shimen_renwu':600,'yunbiao_renwu2':1000,
     'baotu_renwu':1000,'打开大地图_69副本':60,'mijing_renwu':2100,'sanjieqiyuan':200,
     'huoyue_lingqu':60,'zhengli_baibao':150,'jiayuan_zhengli':150,'huoli':120,
     'kejuxiangshi':300,'zhanghao_xinxi':60}
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
# 报告二：账号金币/银币变化量（每端口取最近两条算 Δ = 本次 − 上次）
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
# 报告三：科举题库(keju_ai_cache.json)新入库答题质量（默认查当天 ts；传第 2 参指定日期如 2026-07-30）
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

### 5.1 每轮"必触发"的 on_error（看到别慌，逐个对号）

健康跑也会**稳定产出**这些 on_error 截图——多为设计内出口，**不是 bug**。但其中混着假成功（⑨），所以判定真假的关键 = **去原生层 grep 该任务"真干活"的节点是否命中**（耗时短只是信号，节点命中才是证据）：

| on_error 节点 | 频率 | 出口去向 | 验"真活了"的 grep（命中应 >0） | 判定 |
|---|---|---|---|---|
| `活动-运镖-开始-点击参加` | ×5（每号1） | `运镖完成onerror`（确认主界面） | **`点击押送普通镖_确定` 应每号 3 次**（⚠ 不是 `点击押送普通镖银(三次)`——后者带 `(三次)` 是**节点名**不是命中次数，它 `max_hit:3` 限制自身最多命中 3 次） | ⚠ 出口设计内，但**确定×3 才是真跑完**；某号 `点击押送普通镖_确定` <3 = 假完成（见 ⑩） |
| `藏宝图-背包使用` | ×5（每号1） | `挖宝图完成判断`→`挖宝图完成`（空背包） | `藏宝图-主界面使用` 应数十次（真挖图） | ✅ 设计内：背包图挖完后找不到"使用"→ on_error 复查；**但靠 20s timeout 判完成，略糙**（可清理） |
| `活动-科举乡试-向上滑动到顶端` | ×5（每号1） | 全局 `空节点` | `进入答题界面` / `开始答题_普通` / `开始-点击参加` 命中应 >0 | ❌ **假成功**（见 ⑨）：没答就报完成 |
| `点击副本-开始战斗` | ×1 偶发 | 副本状态机内 | 副本正常完成即可 | ✅ 瞬态自恢复（`[JumpBack]` 回旋门重扫即过） |

> 口诀：**「每号恰好 1 张 + 该任务耗时正常」通常是设计内出口；但耗时异常短（<60s 的答题/挖图类）必查节点命中，提防空节点假成功。**

## 6. 关联

- **记忆**（`~/.claude/projects/.../memory/`）：`maafw-ocr-expected-and-only-rec`（OCR expected 子串匹配 + only_rec 乱码）、`on-error-kongjiedian-false-success`（假成功 + sentinel）、`maafw-resource-not-thread-safe`（每账号独立 Resource）、`maa-pipeline-task-entry-pattern`（next/JumpBack 状态机）。
- **项目文档**：`CLAUDE.md`（pipeline 节点/next/on_error 机制）、`docs/description/状态机设计模式.md`。
- **相关代码**：`agent/run_5r.py`（编排 + ROLES + TIMEOUTS）、`agent/custom/recognition/{ocrNum.py,searchAnswer.py,AIAnswer.py,question_bank.json,tiku.txt}`（活力识别 + 科举题库 question_bank + 三界/科举题库 tiku + AI 缓存 keju_ai_cache）、`assets/resource/base/pipeline/{fuben115,mijing_renwu,yunbiao_renwu2,huoli}.json`、`assets/resource/base/default_pipeline.json`（全局 on_error→空节点）。

## 7. 排查心法

1. **先分清层**：编排日志说"谁/哪个任务/多久"，原生日志说"为什么"。先编排定位、再原生取证。
2. **先判生死**：有 `=== 全部完成 ===` → 5r 没死，问题在调度层或某任务的"假成功"；没有 → 5r 中途崩/超时。
3. **耗时是最强信号**：同批 5 个号横向比，异常短=静默失败，异常长=卡循环/冲关。
4. **on_error 不等于失败**：很多是设计内出口（运镖）或自恢复（秘境瞬态），按节点计数 + 对照已知表再下结论。
5. **maafw 日志巨大**：永远 grep，永不整读；用 task_id/uuid 把 5 路交错线程拆开。
6. **"完成却超时"先查调度**：5r 正常退 + autolife 超时 = 几乎必然是管道/退出/部署问题，不要去 5r 任务里找原因。
