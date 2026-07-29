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
   - 系统化做全 5 号 × 全任务横向对比 + 基线参照，见 [§3](#3-任务耗时矩阵与典型基线)。
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

## 3. 任务耗时矩阵与典型基线

Step 2「耗时异常」的系统化做法：把当天 5 号 × 全部单人任务拉成耗时矩阵横向比，是挖「假成功 / 卡循环 / 整轮预算被吃」最快的办法（呼应心法 3：耗时是最强信号）。**判读永远以同批横向对比为准，绝对值仅供参考。**

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
# 自动生成耗时矩阵（行=任务，列=5号；`X`前缀=墙钟超时，`-`=该号整轮超时后未跑到；详见 §3）
"$PY" -c "
import re,sys,collections
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass
log=open(sys.argv[1],encoding='utf-8').read()
data=collections.defaultdict(dict)
for line in log.splitlines():
    m=re.search(r'<<< \[([^\]]+)\] 单人 (\S+) 完成（用时 ([\d.]+)s）', line)
    if m: data[m.group(2)][m.group(1)]=int(float(m.group(3))); continue
    m=re.search(r'!!! \[([^\]]+)\] 单人 (\S+) 超时 ([\d.]+)s', line)
    if m: data[m.group(2)][m.group(1)]=-int(float(m.group(3)))   # 负数=超时
roles=['队长','渣中','6130','缤纷','晚风']
print('%-18s'%'任务'+''.join('%-9s'%r for r in roles))
for t in data:
    cells=[]
    for r in roles:
        v=data[t].get(r)
        cells.append('-' if v is None else (('X'+str(-v)) if v<0 else str(v)))
    print('%-18s'%t+''.join('%-9s'%c for c in cells))
" "$(ls -t debug/run_5r/run_5r_<YYYYMMDD>_*.log | head -1)"
# 注：中文列宽不齐不影响判读；Windows cmd 中文乱码可 `chcp 65001`，数字列才是重点。
#   同名任务（如两次「打开大地图_69副本」）只记最后一次。
```

> **用 Python 解析 maafw 里某行的 JSON 缓存**（grep 正则遇到嵌套 `]` 会断，用 python 最稳）：
> ```bash
> "$PY" -c "import sys,json; ..." # 见 [[maafw-ocr-expected-and-only-rec]] 里的实例
> ```

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
- **相关代码**：`agent/run_5r.py`（编排 + ROLES + TIMEOUTS）、`agent/custom/recognition/ocrNum.py`（活力/活跃度识别）、`assets/resource/base/pipeline/{mijing_renwu,yunbiao_renwu2,huoli}.json`、`assets/resource/base/default_pipeline.json`（全局 on_error→空节点）。

## 7. 排查心法

1. **先分清层**：编排日志说"谁/哪个任务/多久"，原生日志说"为什么"。先编排定位、再原生取证。
2. **先判生死**：有 `=== 全部完成 ===` → 5r 没死，问题在调度层或某任务的"假成功"；没有 → 5r 中途崩/超时。
3. **耗时是最强信号**：同批 5 个号横向比，异常短=静默失败，异常长=卡循环/冲关。
4. **on_error 不等于失败**：很多是设计内出口（运镖）或自恢复（秘境瞬态），按节点计数 + 对照已知表再下结论。
5. **maafw 日志巨大**：永远 grep，永不整读；用 task_id/uuid 把 5 路交错线程拆开。
6. **"完成却超时"先查调度**：5r 正常退 + autolife 超时 = 几乎必然是管道/退出/部署问题，不要去 5r 任务里找原因。
