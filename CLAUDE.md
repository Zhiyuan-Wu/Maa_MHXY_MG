# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本仓库中工作时提供指引。

## 项目概览

`Maa_MHXY_MG` 基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 实现梦幻西游手游（“梦手”）日常与周常任务的自动化。它通过识别屏幕区域（模板匹配/OCR）并执行点击来驱动安卓模拟器（ADB）或 Mac PlayCover。代码库为双语结构：pipeline 节点名及大部分标识符为中文，Python 代码则中英文混用。

任何改动都涉及两半：
- **资源层**（`assets/`）—— 由 MaaFramework 执行的声明式 JSON 流水线。
- **Agent 层**（`agent/`）—— 用 Python 扩展 MaaFramework 的自定义识别/动作逻辑。

## 首次配置

1. 递归克隆 —— `assets/MaaCommonAssets` 子模块提供 OCR 模型：
   `git clone --recursive ...`，或 `git submodule update --init --recursive`。
2. 将 MaaFramework 放入 `deps/`（已被 gitignore）。可从 [release](https://github.com/MaaXYZ/MaaFramework/releases) 下载并解压，使 `deps/bin`、`deps/share`、`deps/tools` 存在；或运行 `python tools/ci/download_deps.py`（自动检测平台/架构）。
3. `python tools/configure.py` —— 把子模块中的 OCR 模型拷贝到 `assets/resource/base/model/ocr`。安装脚本也会调用它。
4. `pip install -r requirements.txt` —— 固定 `MaaFw==5.11.0`。`zai-sdk`（智谱 AI）用于 AI 答题。

## 常用命令

**重要**：需要使用的python路径必须是 C:/Users/zhiyuan/AppData/Local/Programs/Python/Python313/python.exe

```bash
# 校验资源包能否被 MaaFramework 加载（CI 即运行此命令）
python tools/ci/check_resource.py ./assets/resource/base/

# 将可运行的 GUI 包构建到 install/（MFAAvalonia / MaaPiCli 结构）
python tools/install.py [<版本号>] [<平台标签>]

# 将可运行的 GUI 包构建到 install-mxu/（MXU 结构）
python tools/install_mxu.py [<版本号>]

# 把子模块中的 OCR 模型配置到资源树中
python tools/configure.py
```

本仓库没有单元测试套件。唯一的自动化校验是 `check_resource.py`（CI：`.github/workflows/check.yml`）。本地验证改动的方式：运行某个安装脚本，然后启动生成的 GUI（`install/MaaPiCli.exe` 或 MXU 版本）对模拟器进行测试。

发版：CI（`.github/workflows/install.yml`）由 git tag 触发，产出**两种** GUI 变体 —— **MFAA**（MFAAvalonia，“阿瓦隆”，标签 `MFAA_VERSION`）和 **MXU**（“棉絮”，标签 `MXU_VERSION`）。发版时需在 `install.yml` 中同时更新这两个版本号环境变量。

## 架构

### 资源层 —— 流水线（`assets/resource/`）

一个 *resource* 是按游戏服务器/控制器选择的叠加资源集合。`base/` 为公共资源；`NeteaseServer/`（官服）、`9game/`（九游）、`mac/`（PlayCover）等叠加目录补充服务器专属节点。resource 名称 → 叠加路径 + 控制器的映射声明在 `assets/interface.json` 的 `resource` 段。

实际自动化逻辑位于 `base/pipeline/*.json`（每个文件对应一个功能，如 `shimen_renwu.json`、`mijing_renwu.json`）。格式（MaaFramework pipeline schema 见 `deps/tools/pipeline.schema.json`）：
- 每个顶层 key 是一个具名**节点**。
- 节点包含 `recognition`（如何定位目标：`TemplateMatch`、`OCR`、`ColorMatch`、`Custom` 等）、`action`（`Click`、`MultiSwipe`、`Custom` 等）、`roi` `[x,y,w,h]`（控制器坐标系下的绝对区域）、延迟（`pre_delay`/`post_delay`），以及一个按顺序尝试的候选节点列表 `next`。
- `next` 支持 `[JumpBack]名称` 以返回已保存的断点。`default_pipeline.json` 设置全局默认值，包括 `on_error` → "空节点"。
- 自定义逻辑通过字符串名称串联：`"custom_action": "count"` 或 `"recognition": "Custom", "custom_recognition": "OCRNum"` 指向以这些名称注册的 Python 类（见 Agent 层）。参数经 `custom_action_param`（一个 JSON 对象）传递。

图片/模板位于 `assets/resource/base/image/<功能>/`。

### MaaFramework 文档参考

改 pipeline 前应先查权威文档（本项目固定 `MaaFw==5.11.0`）：
- **在线中文文档站**：<https://maafw.com/docs/>；Pipeline 协议专页 <https://maafw.com/docs/3.1-PipelineProtocol/>
- **GitHub**：<https://github.com/MaaXYZ/MaaFramework>；旧 `is_sub`/`interrupt` 迁移脚本 `tools/migrate_pipeline_v5.py`
- **本地随包文档**（`deps/` 下载后可用，最权威）：`deps/docs/zh_cn/3.1-任务流水线协议.md`（执行逻辑、全部字段、节点属性、算法/动作类型）
- **JSON Schema**：`deps/tools/pipeline.schema.json`、`interface.schema.json` 等，已接入 `.vscode/settings.json` 提供编辑时校验
- **项目内文档**：[`docs/description/状态机设计模式.md`](docs/description/状态机设计模式.md)（pipeline 循环/状态机的 7 种写法与选型）；`docs/description/` 下另有各任务说明（kejuxiangshi、zhuoguirenwu、gongfang_kaogu）

### interface.json —— GUI 契约（`assets/interface.json`）

GUI（MFAAvalonia 或 MXU）读取的唯一事实来源。关键段落：
- `controller`（ADB、PlayCover）、`resource`（服务器变体）、`agent`（如何启动 Python agent）。
- `task[]` —— 面向用户的任务，每个 `entry` 是一个 pipeline 节点名；`option` 引用某个具名选项。
- `option{}`（**字典**，以选项名为 key）—— `type:"select"` 带 `cases`；每个 case 携带一个 `pipeline_override`，在选择该选项时修补指定节点（例如替换某个 `next` 目标）。
- `preset[]` —— 为一键日常预设的任务链。
- `advanced`（在子 JSON 如 `docs/废弃接口,json` 中）定义用户自由文本输入（如账号/角色名），通过 `{field}` 插值覆盖节点字段。

这些 JSON 的 schema 已接入 `.vscode/settings.json`（位于 `deps/tools/`），在 VS Code 中编辑可获得校验。`install.py`/`install_mxu.py` 会改写 `agent.child_exec`/`child_args` 并在拷贝出的 `interface.json` 中写入 `version`，因此**不要**手动编辑安装产物中的这些字段。

### Agent 层 —— 自定义 Python（`agent/`）

`agent/main.py` 是 GUI 启动的入口：在 Linux 上确保存在 `.venv`，按 `interface.json` 的 `version` 做版本门控的 pip 依赖安装（使用国内 PyPI 镜像并带回退），随后启动 `AgentServer`（MaaFramework 用于自定义代码的 RPC 服务）。socket id 作为最后一个命令行参数传入。

自定义类通过装饰器注册，并被 pipeline JSON 用完全相同的字符串引用：
```python
@AgentServer.custom_action("count")
class count(CustomAction): ...
@AgentServer.custom_recognition("OCRNum")
class OCRNum(CustomRecognition): ...
```
目录结构：
- `agent/custom/action/` —— `CustomAction` 子类（计数器、OCR 返回器等）。
- `agent/custom/recognition/` —— `CustomRecognition` 子类；基于 OCR 的判断逻辑、三界奇缘/科举题库查询（`searchAnswer.py` + `tiku.txt`）以及智谱 AI 答题（`AIAnswer.py`）。
- `agent/custom/sink/` —— 消息 sink（宽高比检查；渲染模式检查器目前在 `__init__.py` 中被注释禁用）。
- 每个包的 `__init__.py` 重导出全部内容（`from .x import *`）；`agent/custom/__init__.py` 聚合三者，`main.py` 通过 `import custom` 触发注册。**新自定义类若未在此处重导出，MaaFramework 将无法感知。**
- `agent/utils/`：`logger.py`（loguru，写入 `debug/custom/*.log`）、`utils.py`（`LocalStorage` —— `agent/data/mnma_storage.json` 的 JSON 持久化，用于跨任务计数器）、`SendKingsoftDocs.py`。

自定义识别常通过 `context.run_recognition(..., pipeline_override={...})` 对某个硬编码 `roi` 执行一次性 OCR —— 这些内联的 `roi`/`expected` 值正是游戏 UI 变更时最容易出问题的地方。

## 双机部署（Win + Mac）

`agent/run_5r.py`（五开编排）支持两种部署，**同一份代码**靠 `main()` 顶部 `_detect_backend` 自动切换：WIN_IP 是本机网卡 → **local**（Win 永远 local，同机保护）；WIN_IP 非本机 + `mumu_server` healthy → **remote**。

### 分工与连接

| 机器 | 角色 | 跑什么 | 局域网 IP（当前）| Tailscale IP（备用） |
|---|---|---|---|---|
| **Win** | MuMu 主机 | MuMu 5 实例 + `mumu_server`（专用 adb server `0.0.0.0:5038` + MuMu 生命周期 HTTP API `:5080`，**不加载 Maa 资源**） | `192.168.5.5` | `100.77.236.94` |
| **Mac** | 执行机 | `cli_server`（HTTP API `:5090`，**Maa 资源仅在此加载**；每个 `/run` 起独立子进程跑 `main()`）。把 OCR/资源开销从 Win（~28GB 内存）卸到 Mac | `192.168.5.148` | `100.116.176.34`（`imacmac-studio`） |

两机同一局域网（也可经 [Tailscale](https://tailscale.com) 互联备用）。**2026-08-19 起 `run_5r.py`/`team2.py` 配置默认走局域网 IP**（Tailscale 链路不稳时切换；两 IP 都可用，改 `WIN_IP`/`MAC_IP` 两行即可）。Mac 经 `192.168.5.5:5038` adb 驱动 Win 的设备、经 `:5080` API 管 MuMu 生命周期（launch/shutdown/restart/ensure）。**关键简化**：adb shell 探针透明打到 Win 设备，只有 `MuMuManager.exe` 生命周期走 HTTP（Win 二进制）。

### run_5r.py 模式

- **local（Win 单机）**：Win 上 `python run_5r.py full`。本机 MuMu + 本机 adb，逐字节沿用旧行为。
- **remote（Mac 执行 + Win MuMu）**：Mac 上 `python run_5r.py <mode>`，或 Win 上 `python run_5r.py remote <mode>`（POST Mac `cli_server:5090/run`）。Mac 探测到远端 → 经 Win:5038 adb + Win:5080 生命周期 API 驱动 Win 的 MuMu。
- 服务/客户端模式：`mumu_server`（Win 跑，adb:5038+API:5080）、`cli_server`（Mac 跑，API:5090）、`remote <mode>`（Win→Mac 提交，含 `remote cancel`/`remote show`）。完整说明见 `run_5r.py` 顶部文档注释。

### ssh 到 Mac

```bash
ssh imac@192.168.5.148           # 局域网（当前默认）；备用 ssh imac@100.116.176.34（Tailscale）
```

- 用户名是 **`imac`**（不是 `zhiyuan`/`wuzy14`——后两者 `publickey denied`）。
- 用 **IP** 连，别用 Tailscale 主机名——`known_hosts` 登记的是 IP，用主机名会 `Host key verification failed`。
- Mac 仓库路径：`/Users/imac/dev/Maa_MHXY_MG`。

### 仓库同步（Win ↔ Mac）

两机 git remote **不同源**：
- **Win**：`origin` = GitHub `gitlihang/Maa_MHXY_MG`（上游），`fork` = GitHub `Zhiyuan-Wu/Maa_MHXY_MG`（个人 fork，日常 push 目标）。
- **Mac**：`origin` = **本地 git bundle** `/Users/imac/mhg2.bundle`（**不联 GitHub**），只 fetch 不 push。

**Win → Mac 同步代码**走 git bundle（用户称之为"同步 bundle"）：

```bash
# 1. Win: 增量 bundle（^<mac-head> = Mac 当前 HEAD，如 d8e7aab；先 ssh 查 git rev-parse HEAD）
git bundle create /tmp/mhg2.bundle zhiyuan-dev ^d8e7aab
# 2. scp 覆盖 Mac 的 bundle
scp -o BatchMode=yes /tmp/mhg2.bundle imac@100.116.176.34:/Users/imac/mhg2.bundle
# 3. Mac: fetch + fast-forward
ssh imac@100.116.176.34 'cd /Users/imac/dev/Maa_MHXY_MG && git fetch origin && git merge --ff-only origin/zhiyuan-dev'
```

Mac 常年 `ahead N`（相对旧 bundle），但只要 Win 的 `zhiyuan-dev` 把 Mac HEAD 当祖先，`merge --ff-only` 就能直接推进；工作区的 `agent/custom/recognition/search_log.txt`（运行时日志，已 gitignore）不受影响。

> `cli_server` 每个 `/run` 起独立子进程，**代码同步后无需重启 cli_server**——下次 `/run` 自动加载新代码 + 新 resource bundle。

## 流水线节点列表（`next`）处理逻辑与技巧

这是本项目（以及所有 MaaFramework 项目）最需要吃透的部分。以下均依据 `deps/docs/zh_cn/3.1-任务流水线协议.md`（行号为该文档）。

### 评估规则

- `next` 是**有序候选列表**：每轮从头到尾逐个识别，**首个命中者即中断本轮检测**并执行其 `action`，其余候选本轮不再尝试（doc:204、32-38）。
- 本轮全未命中 → 休眠至 `rate_limit`（默认 1000ms）后再开下一轮，如此循环直到 `timeout`（默认 20s）。
- 可近似理解为 `while(!hit && !timeout){ foreach(next); sleep_until(rate_limit); }`（doc:223）。

### 单节点生命周期

`pre_wait_freezes → pre_delay → action（可 `repeat` 多次）→ post_wait_freezes → post_delay → 截图 → 识别 next`；命中进新节点，`next` 超时或 action 失败进 `on_error`（doc:58-77）。

### 关键字段速查

| 字段 | 作用 |
|---|---|
| `next` | 有序候选；首个命中即执行 |
| `on_error` | 本节点 `next` 超时、或 action 失败时进入的列表 |
| `timeout` | **上一节点**留给自己被识别的等待时长（要调本节点的识别等待，改的是上个节点的 `timeout`）；`-1` 永不超时（v5.5） |
| `rate_limit` | 每轮识别最小间隔（默认 1000ms） |
| `inverse` | 反转命中：识别不到才算命中（注意此时 Click 等动作会失效，需单独设 `target`） |
| `enabled` / `max_hit` | 为 false / 命中次数用尽后，该节点在他人 `next` 中被跳过 |
| `order_by` + `index` | 多结果排序（Horizontal/Vertical/Score/Random）后取第几个，`index` 支持负数 |
| `anchor` | v5.1 运行时动态锚点，可被 `[Anchor]名` 引用 |

### 节点属性：两种等价写法

前缀形式 `"next": ["[JumpBack]X", "[Anchor]Y"]` 与对象形式 `"next": [{"name":"X","jump_back":true}]` 等价，可混用：
- **`[JumpBack]X`**：执行 X 及其子链后，**跳回引用它的父节点**，父节点从头重扫 `next`。处于 `on_error` 路径时不回跳。取代旧 `is_sub`/`interrupt`。
- **`[Anchor]名`**：引用最近一次设置该锚点的节点；锚点未设置/已清除则该候选被跳过。

### 范例：用 `[JumpBack]` 构造反应式状态机

> 📄 **完整的循环/状态机模式 catalog 见 [`docs/description/状态机设计模式.md`](docs/description/状态机设计模式.md)**——归纳了 7 种模式（反应式事件状态机、两级 JumpBack 兜底、`count`/`countGlobal` 计数器循环、`max_hit` 限量、`inverse` 完成判定、答题循环、按键双保险），含选型速查与源码依据。本节只展开最常用的反应式状态机。

这是本项目最核心、也最值得仿写的模式。在副本、抓鬼等**非确定性**场景里，下一秒可能出现战斗、剧情动画、弹窗或完成结算中的任意一种——无法用线性脚本覆盖。解法是把"当前所处的宏状态"写成一个节点，用它的 `next` 列出所有可能事件，靠 `[JumpBack]` 反应式循环。

**一个节点 = 一个状态；`next` 列表 = 该状态下的事件/转移表。**

真实例子取自 `assets/resource/base/pipeline/fuben69.json:272`（"在 50 侠士副本内"状态），精简后：

```jsonc
"进入50级侠士副本-成功": {
  "recognition": "OCR", "expected": "因缘绘",   // 确认"我在侠士副本里"
  "next": [
    "50侠士-副本完成-退出",                       // ① 退出门：识别到"副本已完成" → 离开状态机
    "[JumpBack]点击副本-跳过剧情动画",             // ② 回旋门：处理事件后回到本节点
    "[JumpBack]战斗中-等待10秒",
    "[JumpBack]点击副本-开始战斗",
    "[JumpBack]跳过剧情动画-快进键",
    "[JumpBack]推荐好友信息",
    "[JumpBack]关闭福利弹窗",
    "[JumpBack]关闭师门弹窗"
  ]
}
```

`next` 里的候选按**有无 `[JumpBack]` 前缀**分两类，这是状态机的灵魂：

| 写法 | 命中后行为 | 角色 |
|---|---|---|
| `"X"`（无前缀） | 进入 X 沿其 `next` 走，**不再回本状态** | **退出门**（离开状态机） |
| `"[JumpBack]X"` | 执行 X，子链结束后**弹栈回到本状态**，从头重扫 `next` | **回旋门**（处理事件、留在状态机内） |

**单步 trace（副本内一个 cycle）**：框架轮询 `next` → ① 判"副本已完成"？否 → ②③… 依次试，命中"战斗中-等待10秒" → 压栈父状态 → 执行等待 → 子链空 → 弹栈回到 `进入50级侠士副本-成功` → 又一轮从头扫，直到某轮 ① 命中"副本完成"走退出门离开。

**顺序即优先级**：`recognize_list` 顺序识别、首个命中即停，所以把"退出条件"放最前（每轮先问"结束了吗"），其余按重要性排。

**超时与保活（源码 `source/MaaFramework/Task/PipelineTask.cpp`）**：`run_next` 每次被调用都在入口重置计时（`:134`）；命中 `[JumpBack]` 走的是显式 `jumpback_stack`（命中压栈 `:62`、子链空弹栈 `:84-95`）。结论——**每次 `[JumpBack]` 回到父状态都会重置 20s 超时**，所以只要每隔 <20s 有任意事件被识别到，状态机就一直转；只有连续 20s 全部候选都 miss 才走 `on_error`。这也解释了为何副本内要放 `战斗中-等待10秒/100秒`——战斗时屏幕变化少，靠它们"命中一次"喂活状态机、避免误超时。

**抽象模板**（写任何反应式任务都可套用）：

```jsonc
"STATE_在X状态": {
  "recognition": "…",                 // 确认宏状态
  "next": [
    "EXIT_离开状态机",                 // 无前缀 = 退出门（可多个）
    "[JumpBack]HANDLER_事件A",         // 有前缀 = 回旋门
    "[JumpBack]HANDLER_事件B",
    "[JumpBack]panduan_zhujiemian"     // 兜底（见下节）
  ]
}
```

`fuben69.json` 里其实有一串同款状态机（导航 `:8`、开地图 `:26`/`:131`、进本 `:272`/`:547`/`:681`、抓鬼 `:870`），都是"父状态 + 退出门 + 若干 `[JumpBack]` 回旋门"。**无前缀=走出去、`[JumpBack]`=处理完回来**，记住这一句即可读懂全项目大半逻辑。

### 本项目常用技巧

1. **兜底回主界面**：任务 `next` 末尾放 `[JumpBack]panduan_zhujiemian`。前面业务节点全 miss 时命中它 → 内层反复按 Back 回主界面 → JumpBack 回父任务从头重试。这是全项目容错的核心（`my_task.json:86`）。
2. **临时弹窗 / 打断处理**：把"识别并关闭弹窗"的节点写成 `[JumpBack]关闭某弹窗` 放在 `next` 中，命中→关闭→跳回父节点继续主流程，无需改动主链。
3. **条件分支（"不存在才做"）**：用 `inverse:true`。`panduan_zhujiemian_ks`（`my_task.json:95`）即 `inverse`+`ClickKey key:[4]` 实现"不在主界面就按返回键"。
4. **循环 / 翻页计数**：`max_hit` 限制某节点命中次数；复杂计数走自定义 action `count`/`countGlobal`（`agent/custom/action/count.py`，通过 `custom_action_param` 传 `target_count`/`nextTask`/`LoopNode`，用 `context.override_pipeline` 写回计数）。
5. **永久等待某状态出现**：`timeout:-1`。
6. **错误兜底**：`on_error` 列表 + `assets/resource/base/default_pipeline.json` 全局 `on_error:["空节点"]`，避免任务因偶发识别失败而中断。
7. **运行时动态改写**：两类入口都会在运行时替换节点字段——
   - `interface.json` 的 option → `pipeline_override`（用户选选项时改 `next`/`expected` 等）；
   - custom code 的 `context.override_pipeline({...})` 与 `context.run_recognition(..., pipeline_override={...})`（见各 `CustomRecognition`，常临时覆盖 `roi`/`expected` 做一次性 OCR）。
8. **让节点被"跳过"的几种方式**：`enabled:false`、`inverse` 未命中、超过 `max_hit`、引用了未设置的 `[Anchor]`——可据此实现条件忽略。
9. **少用 delay**：官方明确建议多用中间过程节点、少用 `pre/post_delay`，"既慢还不稳定"（doc:256）。优先用 `*_wait_freezes`（等画面静止）代替固定延迟。

## 约定

- **约定式提交**（Conventional Commits）用于生成更新日志（`.github/cliff.toml`）：`feat`、`fix`、`perf`、`refactor`、`style`、`test`、`docs`、`chore`、`ci`、`revert`。追加 `[skip changelog]` 可跳过该提交。现有提交历史为中文（如 `fix:修复家园整理适配新UI`）。
- pipeline 节点名采用中文且具描述性；新增节点保持同一风格。功能文件名与任务 `entry` 对应（如 entry `shimen_renwu` → `shimen_renwu.json`）。
- `MaaCommonAssets` 子模块来自上游 —— 不要在其中修改；那里的改动应回到其原仓库。游戏专属资源放入 `assets/resource/base/...`。
- `agent/custom/recognition/search_log.txt` 是运行时日志，已被 gitignore —— 不要提交答题搜索日志。
- 低等级（≤69）账号游戏内流程不同；许多任务针对较高等级调校，在小号上可能异常（README 与 `docs/功能列表.md` 中已说明）。
