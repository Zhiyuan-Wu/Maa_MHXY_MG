---
name: windows-memory-audit
description: Windows 上"关了程序内存还是很高（50%+）"、"看不出谁在吃内存"时的排查 SOP。用 PowerShell（经 Git Bash 单引号调用）把物理内存拆成 InUse/Standby/Free，判断"是真实占用还是文件缓存"，再把内核非分页池/分页池归因到驱动，按私有提交/工作集排序进程并用命令行确认身份。适用于模拟器 + 多套自动化 + 远程桌面并发吃满内存的场景。
---

# Windows 内存排查 SOP

总结自 2026-07 一次实战排查（`C:\dev\Maa_MHXY_MG`，MuMu 模拟器 + MaaFramework + 多套自动化 + 向日葵远程同时运行，关掉一批程序后内存仍 ~50%）。

## 0. 核心概念（先想清楚再看数字）

物理内存三段，**任务管理器那个"使用率 %"只算第一段**：

```
Total = InUse + Standby + Free
        ├─ InUse    = 进程工作集 + 内核(池/驱动/修改页)   ← 任务管理器 % 的分母里只剩它"在用"
        ├─ Standby  = 文件缓存, 随时可回收, 新进程要内存会被立即顶出  ← 不算"被占", 算"可用(Available)"
        └─ Free     = 完全空闲
```

**关键认知**：
1. **任务管理器 `%` = InUse / Total**，**不含 Standby**。所以 TM 显示 50% 就是真占用 50%，**不是"缓存假装占用"**。（很多人误以为高占用都是缓存——那是另一回事，缓存算 Available，不会拉高 TM%。）
2. **"私有提交(Private Bytes) ≫ 工作集(WS)"** = 进程空闲被换出到页面文件了。它吃 **commit（提交量）** 而非物理 RAM。所以"提交量高、物理占用不那么高"很正常，反过来"提交不高物理高"才反常。
3. **非分页池(Nonpaged Pool) > 1 GB**：几乎必然是**驱动**在吃（屏幕镜像 / 虚拟化 / 杀软过滤 / 网络），**不是**用户进程——按进程归属通常只占个位数 MB。

## 1. Step 1 — 进程速览（先看谁在跑）

```bash
powershell.exe -NoProfile -Command '
$enc=[Console]::OutputEncoding=[Text.Encoding]::UTF8
$os=Get-CimInstance Win32_OperatingSystem
$t=[int]($os.TotalVisibleMemorySize/1024); $f=[int]($os.FreePhysicalMemory/1024); $u=$t-$f
"=== 内存总览(粗, FreePhysicalMemory 口径, 偏高) ==="
"总计 {0} MB | 已用 {1} MB | 空闲 {2} MB | 使用率 {3}%" -f $t,$u,$f,[int]($u/$t*100)
""; "=== Top 15 CPU 累计时间(秒, 非瞬时占用) ==="
Get-Process | Sort-Object CPU -Descending | Select-Object -First 15 Name,Id,@{N="CPU(s)";E={[int]$_.CPU}},@{N="Mem(MB)";E={[int]($_.WorkingSet64/1MB)}} | Format-Table -AutoSize
"=== Top 15 内存工作集(WS) ==="
Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 15 Name,Id,@{N="Mem(MB)";E={[int]($_.WorkingSet64/1MB)}},@{N="CPU(s)";E={[int]$_.CPU}} | Format-Table -AutoSize
'
```

> - **CPU 是累计秒数**（进程开机以来累计），不是实时 %。长跑进程天然高，不代表此刻满载。要实时 % 需两次采样取差。
> - 顶部的"已用%"用的是 `FreePhysicalMemory`，口径偏粗（偏高）。**以 Step 2 计数器为准**。

## 2. Step 2 — 物理内存三分（判"缓存 vs 真占用"，对齐 TM%）

```bash
powershell.exe -NoProfile -Command '
$enc=[Console]::OutputEncoding=[Text.Encoding]::UTF8
$t=[int]((Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize/1024)
$a=[int](Get-Counter "\Memory\Available MBytes").CounterSamples.CookedValue
$fz=[int]((Get-Counter "\Memory\Free & Zero Page List Bytes").CounterSamples.CookedValue/1MB)
$in=$t-$a; $sb=$a-$fz
"物理内存 (总 {0} MB):" -f $t
"  实际占用 InUse    = {0} MB ({1}%)   <- 任务管理器那个 % 就是它" -f $in,[int]($in/$t*100)
"  缓存待命 Standby  = {0} MB ({1}%)   <- 文件缓存, 秒级回收, 不算占用" -f $sb,[int]($sb/$t*100)
"  完全空闲 Free     = {0} MB ({1}%)" -f $fz,[int]($fz/$t*100)
'
```

> 判定：若 **InUse 占比 ≈ TM 显示的 %** → 确实是真占用，继续 Step 3 归因。若 TM% 远小于这里算出的 InUse% → 多半是口径/旧 TM 差异。

## 3. Step 3 — 归因 InUse（进程 + 内核池/驱动/修改页）

```bash
powershell.exe -NoProfile -Command '
$enc=[Console]::OutputEncoding=[Text.Encoding]::UTF8
$t=[int]((Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize/1024)
$in=$t-[int](Get-Counter "\Memory\Available MBytes").CounterSamples.CookedValue
$cb=(Get-Counter "\Memory\Cache Bytes").CounterSamples.CookedValue
$npp=(Get-Counter "\Memory\Pool Nonpaged Bytes").CounterSamples.CookedValue
$pp =(Get-Counter "\Memory\Pool Paged Bytes").CounterSamples.CookedValue
$mod=(Get-Counter "\Memory\Modified Page List Bytes").CounterSamples.CookedValue
$ps=[int](((Get-Process|Measure-Object WorkingSet64 -Sum).Sum)/1MB)
"InUse = {0} MB 的构成:" -f $in
"  进程工作集合计(含共享页重复) = {0} MB" -f $ps
"  非分页池 NonpagedPool        = {0} MB   <- >1GB 基本是驱动吃" -f [int]($npp/1MB)
"  分页池 PagedPool             = {0} MB" -f [int]($pp/1MB)
"  已修改页 ModifiedList        = {0} MB   <- 写回磁盘后释放" -f [int]($mod/1MB)
"  系统/驱动/缓存驻留           = {0} MB" -f [int]($cb/1MB)
'
```

> 进程 WS 合计**会高估**（共享页被多个进程各算一次）。把"进程合计 + 两个池 + 修改页 + 系统驻留"加起来通常仍**小于 InUse**，差额 = Memory Compression 的压缩存储 + 内核线程栈 + 驱动映像驻留 + 系统 PTE，属分散开销，归不到单一进程。**关注非分页池那一项**——它是最常出问题的"隐形大户"。

## 4. Step 4 — 按进程归因（私有提交 + 池归属）

```bash
powershell.exe -NoProfile -Command '
$enc=[Console]::OutputEncoding=[Text.Encoding]::UTF8
"=== Top 15 进程 by 私有提交 PrivateMemorySize ==="
Get-Process | Sort-Object PrivateMemorySize64 -Descending | Select-Object -First 15 Name,Id,`
  @{N="Priv(MB)";E={[int]($_.PrivateMemorySize64/1MB)}},`
  @{N="WS(MB)";E={[int]($_.WorkingSet64/1MB)}},`
  @{N="NPpool(KB)";E={[int]($_.NonpagedSystemMemorySize64/1KB)}},`
  @{N="Ppool(KB)";E={[int]($_.PagedSystemMemorySize64/1KB)}} | Format-Table -AutoSize
"=== Top 12 进程 by 非分页池归属 ==="
Get-Process | Sort-Object NonpagedSystemMemorySize64 -Descending | Select-Object -First 12 Name,Id,`
  @{N="NPpool(KB)";E={[int]($_.NonpagedSystemMemorySize64/1KB)}},`
  @{N="Ppool(KB)";E={[int]($_.PagedSystemMemorySize64/1KB)}},`
  @{N="WS(MB)";E={[int]($_.WorkingSet64/1MB)}} | Format-Table -AutoSize
$p=Get-Process
"私有提交合计 = {0} MB" -f [int](($p|Measure-Object PrivateMemorySize64 -Sum).Sum/1MB)
"非分页池归属合计 = {0} KB" -f [int](($p|Measure-Object NonpagedSystemMemorySize64 -Sum).Sum/1KB)
"分页池归属合计 = {0} KB" -f [int](($p|Measure-Object PagedSystemMemorySize64 -Sum).Sum/1KB)
'
```

> 看两件事：
> 1. **私有提交大、WS 小**（如 1690 MB / 131 MB）→ 进程空闲，大部分已换出页面文件；它拉高 **commit**，但不怎么吃**物理**。
> 2. **"非分页池归属合计"只有个位数 MB，而 Step 3 的 NonpagedPool 是 GB 级** → 池是**驱动级**占用，杀用户进程救不回来，得退掉承载该驱动的设备（模拟器 / 远程 / 杀软）。

## 5. Step 5 — 命令行确认身份（别凭名字瞎猜）

```bash
# 指定 PID 查命令行（把 $pids 换成 Step 4 里可疑的）
powershell.exe -NoProfile -Command '
$enc=[Console]::OutputEncoding=[Text.Encoding]::UTF8
$pids=44212,24540,22708
Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -in $pids } | ForEach-Object {
  $cl=$_.CommandLine; if ($null -ne $cl -and $cl.Length -gt 130) { $cl=$cl.Substring(0,130)+"..." }
  "{0,-20} PID={1,-6} {2}" -f $_.Name,$_.ProcessId,$cl
}
'
```

> 注意 WMI Filter 里的引号在 bash 里转义麻烦，**用 `Where-Object {... -in ...}` 比写 `-Filter` 稳**。

## 6. Step 6 — 解读 & 处置

把 Step 4/5 的表对照角色，常见模式：

| 模式 | 信号 | 含义 | 处置 |
|---|---|---|---|
| **空闲自动化栈占 commit** | `Priv ≫ WS`，命令行是 `python server.py` / `maa_cli.py --server` / `MaaEnd.exe` | 进程在后台但空闲，多数页已换出 | 不用就整组 kill（引擎 + 子进程 + python），释放 commit + 部分驻留 |
| **模拟器吃内存 + 驱动** | `MuMuNxDevice` + 非分页池偏高 | 模拟器钉客户机 RAM（不进 WS）+ 虚拟化驱动吃池 | 退出模拟器，重测非分页池看回落多少 |
| **远程桌面镜像驱动** | `AweSun/SunloginClient` 在跑 + 非分页池高 | 屏幕镜像驱动是非分页池大户 | 没人远程就退出，非分页池常掉一截 |
| **多套自动化并跑** | 多个不同项目目录的 exe（如 `C:\dev\A\...` + `C:\dev\B\...`） | 互不相干的项目同时在跑 | 确认哪些是当前要用的，停掉其余 |
| **Defender** | `MsMpEng` WS 几百 MB | 常驻扫描，正常 | 不用动，杀软没法退 |

**验证驱动是否释放**：处置后**重跑 Step 3**，看非分页池是否回落。若退掉模拟器 + 远程后仍居高不下 → 可能是驱动泄漏，进 Step 7。

## 7. Step 7 — 疑似驱动泄漏（进阶）

非分页池退不掉、且随时间持续增长时，按 pool tag 抓：

- **`poolmon.exe`**（WDK 自带）：`poolmon /a` 按字节数排序，看哪个 4 字节 tag 在涨，再用 `findstr` 在 `*.sys` 里搜该 tag 定位驱动。
- **`RAMMap`**（Sysinternals）：图形化看 Standby/Mapped/Page Table/Pool 等分项，最直观。
- 没有 WDK 时，`driverquery /v` 至少能列出 422+ 个已加载驱动和加载时间，配合"最近装/更新过哪个驱动"排查。

## 8. 命令速查（复制改 PID 即可）

```bash
# 一句话总览（Step 2 精简版，最常用）
powershell.exe -NoProfile -Command '
$enc=[Console]::OutputEncoding=[Text.Encoding]::UTF8
$t=[int]((Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize/1024)
$a=[int](Get-Counter "\Memory\Available MBytes").CounterSamples.CookedValue
"InUse {0}% | Standby+Free(Available) {1} MB / 总 {2} MB" -f [int](($t-$a)/$t*100),$a,$t
'

# 结束一整套进程树（kill 后重测内存）
powershell.exe -NoProfile -Command "Stop-Process -Name MaaEnd,cpp-algo -Force -ErrorAction SilentlyContinue"
taskkill //F //IM MuMuNxDevice.exe //IM MuMuNxMain.exe
```

## 9. 已知大户速查

| 大户 | 典型表现 | 备注 |
|---|---|---|
| **向日葵 / SunLogin (AweSun)** | 非分页池偏高 | 屏幕镜像驱动吃内核池；退掉立省 |
| **MuMu / 安卓模拟器** | 钉客户机 RAM（不进 WS）+ 虚拟化驱动 | 进程 WS 只几百 MB，实际占 GB 级 |
| **Windows Defender (MsMpEng)** | WS 300MB+、持续扫描 | 常驻，正常 |
| **msedgewebview2 / msedge** | 多实例、多 GPU 进程 | 记忆里 `msedgewebview2` 常排前列 |
| **Memory Compression** | WS 1GB+ | 压缩存放其它进程的冷页，正常机制；它大=有进程冷数据多 |

## 10. 排查心法

1. **先判"真占用还是缓存"**：用 Step 2 计数器三分，别被 `FreePhysicalMemory` 粗口径骗。TM% = InUse%。
2. **非分页池高就找驱动**：按进程归属只有个位数 MB 时，方向一定是驱动（镜像 / 虚拟化 / 杀软 / 网络）。
3. **私有提交 ≠ 物理占用**：`Priv ≫ WS` 的进程是"提交大户、物理小户"，杀它们降 commit 不降多少物理。
4. **命令行确认身份**：同名进程可能来自完全不同的项目目录（`C:\dev\A\` vs `C:\dev\B\`），别凭 `python.exe` 瞎猜。
5. **处置后必须重测**：kill 完重跑 Step 3，确认非分页池/InUse 真的回落，否则可能没杀对、或驱动泄漏。
6. **通过 Git Bash 调 PowerShell**：外层用**单引号**包整段脚本（保护 `$_` / `$()` 不被 bash 展开）；中文输出前加 `$enc=[Console]::OutputEncoding=[Text.Encoding]::UTF8` 避免 GBK 乱码。
