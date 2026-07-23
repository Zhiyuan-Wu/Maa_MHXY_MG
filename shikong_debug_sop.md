# shikong（梦幻西游：时空 桌面端）调试 SOP

> `maa_cli.py` 使用手册与调试策略。所有结论（roi、键码、输入法、组合键写法）均在时空客户端实测验证。

## 0. 这是什么

背景：assets\resource\base下有一套完整的针对ADB controller的自动化控制pipeline，但这些资源无法对win 32 shikong客户端生效（页面布局变化、素材调整）（但基本逻辑相同）。为此，新建了一套资源assets\resource\shikong来在win32下使用。本脚本常驻maa服务并封装maa的相关接口，用于将base资源迁移到shikong的工作中方便对win32客户端的调试。

重要：在执行任何动作前，确保已经完整阅读并理解assets\resource\base下资源和状态机处理逻辑。如果遇到问题，不要猜测，到maa源码和文档中寻找答案。

`maa_cli.py` 是针对**单个游戏窗口**的 MaaFramework 截图 / 识别 / 动作调试 CLI，两种角色在同一脚本里：

- **`--server`**：枚举桌面窗口 → 选一个 → 连 Win32 控制器 + 加载 shikong 资源 + 起 Tasker → 开 HTTP 服务（默认 `http://127.0.0.1:13520`），常驻。
- **`crop` / `reco` / `action` / `windows`**：纯 HTTP 客户端，向已启动的 server 发请求并回显**完整**结果。客户端进程不加载 MaaFw。

解决的核心问题：调参 / 排查时做**单次截图 / 单次识别（看 hit / box / 置信度 / OCR 文本）/ 单次动作（点击 / 按键）**，而不是跑整条 pipeline。配合 `shikong.py`（五开编排）一起用。

---

## 1. 环境与启动

- **Python**：必须用 `C:/Users/zhiyuan/AppData/Local/Programs/Python/Python313/python.exe`（见 `CLAUDE.md`）。
- **前提**：
  1. 游戏窗口（梦幻西游：时空）已打开；
  2. `assets/resource/shikong` 就绪——须含 `pipeline/_base_deps.json`（补齐 base 的 helper 节点，否则资源加载失败；详见该文件头注释）。

**开服务（终端 1）：**

```bash
python maa_cli.py --server                    # 自动选窗口（唯一则用之；多个默认 index=1）
python maa_cli.py --server --keyboard Seize   # 组合键/可靠按键需要 Seize（见 §4）
python maa_cli.py --server --hwnd 0x1B2xxxx   # 直接指定窗口句柄（int 或 0x 十六进制）
python maa_cli.py --server --index 2          # 按枚举序号选（1-based）
python maa_cli.py --server --port 8080        # 换端口；--resource 换资源目录
```

**就绪标志**：日志出现 `<<< 就绪 ... Tasker 已连 hwnd=... HTTP 服务：http://127.0.0.1:13520`。

另开一个终端（终端 2）发请求；所有命令默认连 `http://127.0.0.1:13520`，可用 `--url` 改。

---

## 2. 客户端命令一览（终端 2）

```bash
python maa_cli.py windows                                  # 列出游戏窗口（index/hwnd/pid/rect/title）
python maa_cli.py crop C:/tmp/s.png                        # 截图存盘（缺省路径→debug/maa_cli/）
python maa_cli.py reco OCR '{"roi":[0,0,400,80]}'          # 单次识别，回显完整结果
python maa_cli.py reco TemplateMatch '{"template":["guaji.png"],"roi":[150,0,500,100]}'
python maa_cli.py reco OCR @reco.json                      # PARAM 从文件读
python maa_cli.py reco OCR -                               # PARAM 从 stdin 读
python maa_cli.py action Click '{}' --box 347 14 28 34     # 点击（target 默认取 box 中心）
python maa_cli.py action ClickKey '{"key":[27]}'           # 单键 ESC（Win32 VK=27）
python maa_cli.py action KeyDown '{"key":18}'              # 按下 Alt（不松开，用于组合键）
```

- **PARAM** 是 JSON 对象：内联 / `@file.json` / `-`(stdin) / 缺省 `{}`。
- **识别类型**（`maa.pipeline.JRecognitionType`）：`DirectHit` / `TemplateMatch` / `FeatureMatch` / `ColorMatch` / `OCR` / `NeuralNetworkClassify` / `NeuralNetworkDetect` / `And` / `Or` / `Custom`。
- **动作类型**（`maa.pipeline.JActionType`）：`DoNothing` / `Click` / `LongPress` / `Swipe` / `MultiSwipe` / `Scroll` / `ClickKey` / `LongPressKey` / `KeyDown` / `KeyUp` / `InputText` / `StartApp` / `StopApp` / `StopTask` / `Command` / `Shell` / `Screencap` / `Custom`。
- **输出分流**：摘要打到 stderr（`[reco] hit=True ...`），完整 JSON 打到 stdout——可 `|` 给别的程序。
- **失败分流**：server 报错 → `!! server 返回错误：<msg>`；连不上 → `!! 连不上 server`；JSON 格式错 / 文件找不到 → `!! 输入错误`。

---

## 3. 调试策略：用 reco 判定动作结果（不用图像分析）

**核心思想**：动作执行后，用 `reco`（OCR / TemplateMatch / ColorMatch）在"兴趣点"探测**状态变化**，而不是用图像分析看截图。给每个 UI 状态选一个"只有该状态才出现"的文字 / 模板作探针，动作前后各 reco 一次，对比 `hit` 即可判定。

### 时空客户端实测可用的探针

> roi 为控制器坐标，窗口 1281×720。

| 状态 | 探针 reco | 判定 |
|---|---|---|
| 在主界面（面板关） | OCR `{"expected":["长安"],"roi":[70,12,110,30]}` | `hit=True` = 主界面 |
| 挂机面板已打开 | OCR `{"expected":["自动战斗","原地挂机"],"roi":[200,150,900,450]}` | `hit=True` = 已打开 |
| 挂机按钮位置 | TemplateMatch `{"template":["guaji.png"],"roi":[150,0,500,100],"threshold":[0.6]}` | `hit` 时 box ≈ `[347,14,28,34]` |

### reco 返回的完整结果（排查够用）

```jsonc
{
  "recognition": {
    "hit": true, "algorithm": "OCR", "box": [289,37,30,16],
    "all_results":      [ {"box":[...],"score":0.99,"text":"排行"}, ... ],  // 全部候选（含低分）
    "filtered_results": [ {"box":[...],"score":0.99,"text":"排行"}, ... ],  // 过阈值后
    "best_result":      {"box":[...],"score":0.99,"text":"排行"},           // 最优
    "raw_detail":       {"all":[...],"filtered":[...],"best":{...}}         // 原始
  },
  "ignored_unknown_keys": []   // PARAM 里不被该类型识别的键（防静默丢参，见 §6）
}
```

### bash 判定 helper（stdout 取 JSON、提取 hit）

```bash
panel_open() {
  python maa_cli.py reco OCR '{"expected":["自动战斗","原地挂机"],"roi":[200,150,900,450]}' 2>/dev/null \
    | python -c "import sys,json; print('panel-open='+str(json.load(sys.stdin)['recognition']['hit']).upper())"
}
```

> 注：管道后的 python 默认按 cp936 读 stdin（Windows），中文会乱码；但命中判定只取 `hit`（ASCII）不受影响。若要中文也干净，给消费方设 `PYTHONUTF8=1`（见 §5）。

**辅助手段**：`crop` 存盘人工核对（`python maa_cli.py crop C:/tmp/x.png` 后打开看），但**首选 reco 自动判定**——可脚本化、可断言。

---

## 4. 动作测试要点（实测结论）

### 点击（鼠标）

先 TemplateMatch 定位拿 box，再 Click：

```bash
BOX=$(python maa_cli.py reco TemplateMatch '{"template":["guaji.png"],"roi":[150,0,500,100],"threshold":[0.6]}' 2>/dev/null \
      | python -c "import sys,json;b=json.load(sys.stdin)['recognition']['box'];print(' '.join(map(str,b)))")
python maa_cli.py action Click '{}' --box $BOX      # target 默认=True=取 box 中心点击
```

### 单键

```bash
python maa_cli.py action ClickKey '{"key":[27]}'    # ESC
```

> `ClickKey` 的 list 是**逐个单击序列**（见下）。

### 组合键（Alt+G 等）——重点

`ClickKey` 的 `key` list **不是和弦**！`{"key":[18,71]}` = 单击 Alt、再单击 G（两次独立 down+up），游戏看不到"Alt 按住时按 G"，Alt+G 不触发。

组合键必须用 `KeyDown` / `KeyUp` 显式时序（依据 `deps/docs/zh_cn/3.1-任务流水线协议.md:1121`）：

```bash
python maa_cli.py action KeyDown '{"key":18}'   # Alt 按下
python maa_cli.py action KeyDown '{"key":71}'   # G 按下
python maa_cli.py action KeyUp   '{"key":71}'   # G 松开
python maa_cli.py action KeyUp   '{"key":18}'   # Alt 松开
# 上述四步 = Alt+G，时空客户端实测能打开挂机页
```

### 键码体系（随控制器）

- **Win32** 用虚拟键码 **VK**：`Alt=18`、字母 `A=65…Z=90`（=ASCII）、`ESC=27`、`F1=112…`
  — <https://learn.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes>
- **Adb** 才用 Android KeyEvent：`Alt=57`、`A=29`。
- 本工具默认 Win32。

### 输入法（`--keyboard` / `--mouse`）

- 默认 `SendMessageWithCursorPos`：后台、不抢鼠标；点击与单键可用。
- 两种法单键 ESC 都能送达；组合键可靠的是 **KeyDown/KeyUp**。

---

## 5. 中文乱码：根因与修复（已在 maa_cli.py 修复）

**根因**（字节级证据）：脚本按 UTF-8 输出字节（"已" = `e5 b7 b2`），但中文 Windows 控制台默认代码页是 cp936(GBK)，把它当 GBK 渲染 → 乱码（`e5b7b2` 被解成 `宸蹭`）。原来的 `sys.stdout.reconfigure(encoding="utf-8")` 只让 Python 吐 UTF-8 字节，**没改控制台代码页**。

**修复**（`maa_cli.py` 启动时）：`SetConsoleOutputCP(65001)` 把当前控制台切到 UTF-8 + `stdout/stderr` reconfigure UTF-8。于是**控制台 / 重定向到文件 / 管道给别的进程**三种情形中文都正确。

**消费方注意**：把 `maa_cli` 输出管道给**另一个** Python 进程（如 `maa_cli ... | python -c "..."`）时，那个进程的 stdin 默认按 cp936 读 → 中文乱码（`hit` / `box` 等 ASCII 不受影响）。让消费方也走 UTF-8：`PYTHONUTF8=1 python -c "..."`，或消费方先 `sys.stdin.reconfigure(encoding="utf-8")`。

---

## 6. 常见错误与排查

| 现象 | 原因 / 处理 |
|---|---|
| 资源加载失败 / Tasker 未就绪 | shikong 缺 `pipeline/_base_deps.json`，或模板 / OCR 模型缺失。先 `python tools/ci/check_resource.py ./assets/resource/shikong` 自查；通过则资源 OK。换资源：`--resource <目录>`（**注意 base 在 Win32 下不可用，必须 shikong**）。 |
| server 返回 500 | 看 `error` 字段——常见是未知 type、必填字段缺失（如 `ClickKey` 缺 `key`、`InputText` 缺 `input_text`，会回显 dataclass 的 `TypeError`）。 |
| PARAM 未知键被忽略 | `ignored_unknown_keys` 会列出（防 MaaFw 静默丢参）——拼写错能立刻发现。 |
| 连不上 server | `!! 连不上 server ...` → 先在另一终端 `python maa_cli.py --server`。 |
| 找不到窗口 | `--title` 放宽关键字，或 `--hwnd <句柄>` 直连（用 spy++ 或 `windows` 命令查句柄）。 |

---

## 7. 端到端示例：挂机页 开 / 关 / Alt+G（实测过）

前置：终端 1 已 `python maa_cli.py --server` 并就绪。

```bash
# 判定 helper
panel() { python maa_cli.py reco OCR '{"expected":["自动战斗","原地挂机"],"roi":[200,150,900,450]}' 2>/dev/null \
           | python -c "import sys,json;print('panel-open='+str(json.load(sys.stdin)['recognition']['hit']).upper())"; }

panel                                    # baseline: panel-open=FALSE

# A. 组合键 Alt+G 开挂机页（KeyDown/KeyUp 时序）
python maa_cli.py action KeyDown '{"key":18}'
python maa_cli.py action KeyDown '{"key":71}'
python maa_cli.py action KeyUp   '{"key":71}'
python maa_cli.py action KeyUp   '{"key":18}'
panel                                    # panel-open=TRUE  ✓

# B. ESC 关
python maa_cli.py action ClickKey '{"key":[27]}'
panel                                    # panel-open=FALSE ✓

# C. 点击挂机按钮开（TemplateMatch 定位 → Click）
BOX=$(python maa_cli.py reco TemplateMatch '{"template":["guaji.png"],"roi":[150,0,500,100],"threshold":[0.6]}' 2>/dev/null \
      | python -c "import sys,json;b=json.load(sys.stdin)['recognition']['box'];print(' '.join(map(str,b)))")
python maa_cli.py action Click '{}' --box $BOX
panel                                    # panel-open=TRUE  ✓

# 收尾
python maa_cli.py action ClickKey '{"key":[27]}' && panel   # panel-open=FALSE
```

## 8. 如何迁移base到shikong

原则：
1. base中所有步骤必须完整迁移，严格禁止只迁移部分、把一些节点放在未来去做。没有完全交付=失败。base中的所有节点、动作、判断逻辑必须都在，不能替换成所谓等效节点。仅可以**新增**（不是替换）针对shikong的冗余判断逻辑（例如ocr/模板多路判断，按键/点击多路动作等，但必须与用户确认）
2. base中的所有坐标、模板都可能失效，需要逐个确认，而不是抽样。
3. 所有验证步骤必须使用maa_cli.py做实际测试，禁止语法通过就报告完成。
4. 你需要标记出确认失效的roi/模板，由用户修复资源后重复测试，协助工作直到所有节点检查通过。（严格禁止：某个模板失效、替换为ocr）
5. OCR不可靠。

### 迁移前必须阅读

- base 中被迁移功能的 pipeline JSON 文件（理解原逻辑）
- base 的 `my_task.json` 中 `panduan_zhujiemian` 链路（`amazing_discount` → `panduan_zhujiemian_ks` → `close_exit_confirm` → `zai_zhujiemian`）
- 本 SOP §0 的背景说明
- `CLAUDE.md` 状态机设计模式节（`[JumpBack]` 用法）

### 核心差异速查：base (ADB) vs shikong (Win32)

| 维度 | base (ADB) | shikong (Win32) |
|---|---|---|
| 分辨率 | 1280×720 (模拟器) | 1282×720 (桌面窗口) |
| 键码体系 | Android KeyEvent (Alt=57, A=29, Back=4) | Windows VK (Alt=18, A=65, ESC=27) |
| 组合键 | `ClickKey [57,35]` = Alt+G 和弦（Android 支持） | `ClickKey [18,71]` ≠ Alt+G（是两次独立单击） |
| 模板来源 | ADB 模拟器截图 | 桌面端渲染截图 |
| 布局偏移 | 参考基准 | 画幅更宽：左对齐/居中对齐内容普遍**偏左偏上**（例如领取按钮 x-64 y-44, 挂机按钮 x-115） |

### Step1 创建shikong资源骨架

1. 在 `assets/resource/shikong/pipeline/` 下创建与 base 同名的功能 JSON（如 `shuangbei.json`）
2. 将 base 中该功能的**全部节点**拷贝过来作为起点
3. 确认 `pipeline/_base_deps.json` 包含被引用的共享 helper 节点：
   - `空节点` — 空节点，`default_pipeline.json` 的 `on_error` 引用
   - `panduan_zhujiemian` / `panduan_zhujiemian_ks` / `close_exit_confirm` / `amazing_discount` / `amazing_discount_close` / `zai_zhujiemian` — 主界面判定链
4. 所有 ADB 键码替换为 Win32 VK：`key:[4]`→`key:[27]`, `key:[57,35]`→需拆为 KeyDown/KeyUp 链
5. 将 base 的模板图片复制到 `assets/resource/shikong/image/zonghe/`（后续 Step3 中逐个验证替换）
6. **每步修改后必须运行** `python tools/ci/check_resource.py ./assets/resource/shikong/` 确保资源可被 MaaFramework 加载

### Step2 确认组合键（KeyDown/KeyUp 链替换 ClickKey）

Android 的 `ClickKey [meta, key]` 支持组合按键，Win32 的 `ClickKey` 是**逐个单击序列**。**所有 Android 组合键都必须拆为 4 节点 KeyDown/KeyUp 链**：

```
base:  ClickKey [57,35]  →  Alt+G  (ADB 支持组合)
shikong 必须改为:
  节点1: KeyDown key:18  post_delay:120
  节点2: KeyDown key:71  post_delay:120
  节点3: KeyUp   key:71  post_delay:120
  节点4: KeyUp   key:18  post_delay:800~1000 (等面板出现)
```

**KeyDown/KeyUp 的 `key` 字段是单整数，不是数组**（与 ClickKey 不同）。`KeyDown [18]` 和 `KeyDown [71]` 不是 `KeyDown [18,71]`。

输入模式：默认 `SendMessageWithCursorPos` 下实测 KeyDown/KeyUp 链即可正常发送组合键，无需 `--keyboard Seize`。

已验证可用的组合键方案（SendMessage 模式实测）：

```bash
# Alt+G 打开挂机面板
python maa_cli.py action KeyDown '{"key":18}'     # Alt 按下
python maa_cli.py action KeyDown '{"key":71}'     # G 按下
python maa_cli.py action KeyUp   '{"key":71}'     # G 松开
python maa_cli.py action KeyUp   '{"key":18}'     # Alt 松开
# 四步 = Alt+G 组合键
```

**流水线写法**（4 个连续 DirectHit 节点）：
```jsonc
"打开挂机-按下Alt": {
    "action": "KeyDown", "key": 18, "post_delay": 120,
    "next": ["打开挂机-按下G"]
},
"打开挂机-按下G": {
    "action": "KeyDown", "key": 71, "post_delay": 120,
    "next": ["打开挂机-松开G"]
},
"打开挂机-松开G": {
    "action": "KeyUp", "key": 71, "post_delay": 120,
    "next": ["打开挂机-松开Alt"]
},
"打开挂机-松开Alt": {
    "action": "KeyUp", "key": 18, "post_delay": 1000,
    "next": ["确认面板已打开", "[JumpBack]fallback点击按钮"]
}
```

**单键 ESC** 保持 `ClickKey key:[27]`，无需改为 KeyDown/KeyUp。

### Step3 逐个节点实测验证

**前置：启动服务**（终端1）：
```bash
python maa_cli.py --server
```

#### 3.1 基线确认与状态切换

每次测试前先用已知探针确认当前游戏状态：

```bash
# 主界面判定
python maa_cli.py reco OCR '{"expected":["长安"],"roi":[70,12,110,30]}'
# hit=True -> 在主界面

# 挂机面板已打开
python maa_cli.py reco OCR '{"expected":["自动战斗","原地挂机"],"roi":[200,150,900,450]}'
# hit=True -> 面板已打开
```

**对于未触发的链路，要想办法触发**——禁止静默跳过：
- `panduan_zhujiemian_ks`（`inverse: true`）在主界面不会触发 → 先按 Alt+G 打开子面板，脱离主界面后再测试逆向后能否识别
- `close_exit_confirm` 需要退出确认弹窗 → 在主界面按 ESC 能否触发退出对话框
- `amazing_discount` 是稀有弹窗 → 先在正常状态下确认能否模糊匹配到，标记为"无法触发"

#### 3.2 OCR 节点验证流程

对每个 OCR 识别节点：
```bash
# 1. 用 base 原始 roi 测试
python maa_cli.py reco OCR '{"expected":["领取"],"roi":[954,592,142,103]}'
# hit=False → roi 失效

# 2. 用已知的布局偏移规律缩小搜索范围
# 经验规律：shikong 元素比 base 偏左~64-115px，偏上~0-44px

# 3. 从全屏 OCR 结果中查找目标文字的实际位置（仅用 full_results 观察位置，不替换逻辑）
python maa_cli.py reco OCR '{"expected":[],"roi":[800,450,400,250]}'
# 在结果中找到目标文字的 box

# 4. 用找到的 box 构建新的 roi（适当放宽～80-100px 容错）
# 验证新 roi 是否 hit=True
python maa_cli.py reco OCR '{"expected":["领取"],"roi":[860,510,100,80]}'
# hit=True, score=0.99991 → 更新 roi
```

**已验证的 roi 直接更新到 json 中**，无需标记。

#### 3.3 模板节点验证流程

对每个 TemplateMatch 节点：

```bash
# 1. 用 base 原始模板 + base 原始 roi + 默认 threshold 测试
python maa_cli.py reco TemplateMatch '{"template":["zonghe/jiahao.png"],"roi":[1197,540,78,157]}'
# hit=False → ADB 模板在 Win32 上不兼容（典型 score 0.3-0.43）

# 2. 模糊匹配找大致位置（降低阈值到 0.3，扩大 roi）
python maa_cli.py reco TemplateMatch '{"template":["zonghe/jiahao.png"],"roi":[1100,0,180,720],"threshold":[0.3]}'
# hit=True, box=[1231,664,44,42] score=0.48 → 位置仍在 base roi 内

# 3. 标记失效，交用户修复
# 在 node 的 JSON 中添加 "!shikong实测" 标注：
#   - 哪个模板 miss，score 多少
#   - 模糊匹配找到的近似位置
#   - 建议用户从 shikong 截图、保持相同尺寸
```

**严格禁止**：
- 模板失效→替换为 OCR ✗
- 降低 `threshold` ✗（base 没有的字段不能加）
- 静默跳过未触发的节点 ✗

**正确做法**：
- 标记 `!shikong实测` 注释在对应的 node 字段中
- 标注原因、实测 score、是否需要用户修复
- 模糊匹配确认大致位置后告知用户："图标在 roi 内，只需重截"

#### 3.4 用户更新素材后的验证流程

用户将新素材放到 `assets/resource/shikong/image/` 下后，重复以下步骤直到所有节点通过：

```bash
# 1. 对比新旧模板尺寸（必须与原始完全一致）
python -c "from PIL import Image; print(Image.open('path/new.png').size)"

# 2. 用默认阈值在 base roi 内测试
python maa_cli.py reco TemplateMatch '{"template":["zonghe/xxx_win32.png"],"roi":[1197,540,78,157]}'

# 3. 交叉测试确认多个模板互不干扰
# （A 模板不会在 B 模板的位置误匹配）

# 4. 对于 inverse 节点额外测试：
#    在主界面→模板命中→inverse不触发 ✓
#    在子界面→模板未命中→inverse触发按ESC ✓

# 5. 模板通过后移动到 zonghe/ 目录，加 _win32 后缀
#    更新 pipeline JSON 中的 template 路径
```

#### 3.5 已验证通过的 shikong 布局偏移规律

| 元素 | base roi/box | shikong roi/box | 偏移 |
|---|---|---|---|
| 长安城文字 | 居中 | box=[93,19,45,19] | 左上角，稳定 |
| 挂机按钮 OCR | [457,8,96,79] | [277,5,175,89] | x-115, y+26 |
| 领取按钮 OCR | [954,592,142,103] | [860,510,100,80] | x-64, y-44 |
| 主界面右下图标区 | [1197,540,78,157] | [1197,540,78,157] | **不变**（窗口尺寸接近） |

**推断规则**：shikong 窗口 1282×720 vs ADB 1280×720，游戏内容区域偏左。顶部元素 x 偏移约 0-50px，中部约 60-120px，底部右侧元素位置几乎不变。

推断失效 roi 时可按此规律估算，然后实测验证。验证通过则直接更新，不通过则标记交用户。

#### 3.6 通用验证脚本模板

```bash
# 定义快捷函数（在终端2使用）
PY="C:/Users/zhiyuan/AppData/Local/Programs/Python/Python313/python.exe"

check_ocr() {
  # $1 = expected, $2 = roi_json
  $PY maa_cli.py reco OCR "{\"expected\":[\"$1\"],\"roi\":$2}" 2>/dev/null \
    | $PY -c "import sys,json;d=json.load(sys.stdin)['recognition'];print(f'hit={d[\"hit\"]} box={d[\"box\"]}')"
}

check_tmpl() {
  # $1 = template_name, $2 = roi_json, $3 = threshold (optional, default 0.7)
  local thresh=${3:-0.7}
  $PY maa_cli.py reco TemplateMatch "{\"template\":[\"$1\"],\"roi\":$2,\"threshold\":[$thresh]}" 2>/dev/null \
    | $PY -c "import sys,json;d=json.load(sys.stdin)['recognition'];print(f'hit={d[\"hit\"]} box={d[\"box\"]}')"
}

# 示例
check_ocr "领取" "[860,510,100,80]"
check_tmpl "zonghe/jiahao_win32.png" "[1197,540,78,157]"

# 组合动作验证
do_alt_g() {
  $PY maa_cli.py action KeyDown '{"key":18}' >/dev/null 2>&1
  $PY maa_cli.py action KeyDown '{"key":71}' >/dev/null 2>&1
  $PY maa_cli.py action KeyUp   '{"key":71}' >/dev/null 2>&1
  $PY maa_cli.py action KeyUp   '{"key":18}' >/dev/null 2>&1
}
```

### 迁移产物自检清单

每个功能迁移完成后，逐项确认：

- [ ] `python tools/ci/check_resource.py ./assets/resource/shikong/` 通过（无 `[ERR]`）
- [ ] 所有节点引用可解析（`next`/`on_error` 中的节点名都存在于 pipeline JSON 中）
- [ ] 所有 `ClickKey` 组合键已拆为 KeyDown/KeyUp 链
- [ ] 所有 ADB 键码已替换为 Win32 VK
- [ ] 每个 OCR roi 已实测验证（hit=True, score > 0.9）
- [ ] 每个 TemplateMatch 模板已实测验证（hit=True, score > 0.7, 或无弹窗时标记未触发）
- [ ] action动作结果符合预期（下一个节点能被命中）
- [ ] 每个节点有 `!shikong实测` 注释或无需标注（全部通过）
- [ ] 新增的 `_win32` 模板文件已放在 `zonghe/` 目录，文件名与 pipeline JSON 引用一致
- [ ] 未测试到的链路已标注原因（弹窗未触发 / 模板待截取等）

## 9. 日志

已完成的内容，请参考以下pipeline与base的区别，特别是roi、图片模板的变化：
- panduan_zhujiemian
- shuangbei