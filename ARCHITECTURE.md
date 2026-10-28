# dsh-pet-pure 技术实现文档

> 面向维护者的实现说明（2026-10-28）。配套的用户文档见仓库 README；多宠的产品语义见 Obsidian《DSH-PET 多宠使用指南》。
> 一句话定位：**纯 UI 桌宠终端**——零 DSH 知识的独立进程，通过本地 HTTP 控制面被任意 driver 驱动，"换宠物不改 driver" 的可替换性证明与载体。

---

## 1. 设计原则

1. **零 DSH 知识**：本体不理解会话/审批/任务语义，只认七端点定义的控制面契约（DSH-PET.md §5.6/§7）。driver 在运行时插件内做语义收敛，宠物只做渲染与交互回报。
2. **控制面单向请求 + 事件队列反向轮询**：driver 发起全部 HTTP 操作；宠物侧事件（触摸等）入队，driver 5s 轮询拉走。
3. **可替换性**：与 TS 载体（Electron 宠）实现同一契约，行为等价即可互换——已实测闭环。
4. **渲染闭包整体搬运**：动画/物理/行为引擎从旧 Qt 仓库（dsh-pet-indesktop）原样剥离（`pet/` 包，27 模块），壳层（`main.py`）是唯一新代码。

## 2. 进程模型与生命周期

```
start-pet.cmd / restart-pet.cmd
   └─ pythonw main.py                # 监督父进程（无窗口，只管生死）
       └─ pythonw main.py --supervised --supervisor-pid <N>   # GUI 真身
            ├─ Qt 主循环（900×560 透明置顶窗 = 活动区）
            ├─ 控制面 HTTP 服务（127.0.0.1 随机端口）
            └─ 注册表发布 pet-http.d/<pet-id>.json {port,pid,since,supervisor}
```

- **监督父进程**：GUI 真身原生崩溃（faulthandler 转储落 `pet-pure.log`）后 2s 重生；正常退出（code 0）与退出旗标（`pet-pure-exit.flag`，closeEvent 落痕）不重生。**重生前让位检查**：注册表条目的 `supervisor` 已换成别的 pid = 新实例接管了坑位，让位退出——否则两条监督链互杀成战争。
- **单实例接管（同 id）**：启动时读自己的注册表条目，**先杀旧监督者、再杀旧真身**（顺序颠倒 = 旧监督者重生真身互杀）。别的 pet-id 不动（多宠共存）。
- venv 的 pythonw 是"桩+真身"双层（桩 spawn 真身后等待）——进程列表看到的两层 pythonw 是正常的；接管与重启都按注册表 pid 打真身。
- 观察窗口：崩溃循环期一切怪象（弹窗消失/状态卡住/时序错乱）的第一解释是"她刚崩过一次，这是新身体"。

## 3. 控制面协议（pet/http_control.py，纯标准库）

| 端点 | 方法 | 语义 |
|---|---|---|
| /capabilities | GET | 四词表 + `endpoints` 路由声明（TS 载体同形） |
| /status | GET | 镜像：state/animation(.webm)/override/lastAction 三件套/window/visible/pid |
| /events?since=N | GET | 触摸等事件队列（seq 单调、有界 200 条） |
| /state | POST | idle/thinking/working/error（词表校验，400 拒绝） |
| /action | POST | 一次性动画（celebrate）；记录 lastAction 见证三件套 |
| /say | POST | 气泡文本（截断 200 字）；交互期间让位（见 §6 守卫） |
| /interaction | POST | **长轮询**：响应头立即返回，正文 = 用户选择；断连 = 撤气泡 |

### 长轮询实现要点（最容易做错的三件事）

1. **头先发、正文延迟**：`Connection: close` + 无 content-length，正文由连接关闭定界——driver 的 fetch 立刻 resolve，正文等用户作答。
2. **断连探测走 select()**：50ms 粒度探测 socket 可读（driver abort = FIN/RST → 可读 + EOF/错误 → `rfile.read(1)` 判定）。**不要用周期写失败探测**——1s keepalive 写的感知延迟就是双宠竞速里败者气泡的收回延迟（实测 60ms vs ≤1s）。
3. **60s 心跳空格**：与断连探测**正交**的另一职责——喂 Node undici 的 300s bodyTimeout。没有它，提问挂 5 分钟以上（用户走开常态）driver 臂超时阵亡，答案写进死 socket 静默丢失。间隔常量 `INTERACTION_HEARTBEAT_SECONDS` 可 monkeypatch 测试。

### 契约对齐（与 TS 载体逐字段一致，driver 的 pet_state 工具原样透传 /status 给模型）

- `animation` 报**文件名**（`.webm` 后缀）；`override` = 一次性动作覆盖（播完清 null）
- `lastAction/lastActionAt/lastActionDone` 恒在场（null 起步）——`/action` 的可观察见证；`override` 单 tick 内可能设/清，**不是可靠见证**
- `lastActionDone.durationMs` 在 one-shot finished 时填实际墙钟

## 4. 渲染管线（pet/ 闭包核心）

```
MovieLibrary（素材库：manifest + 路线决策，启动时扫描）
   └─ bind_clip(name) → FrameSeqClip（帧序列，首选）或 WebMClip（实时解码，兜底）
        └─ PetSprite（几何/合成/命中/拖拽模型，纯 QObject）
             └─ PetWindow.paintEvent → sprite.paint(painter)（SmoothPixmapTransform）
```

- **两条动画路线在库扫描时固化**：`FrameSeqClip` 读 `frameseq/` 无损帧缓存（共享预取线程异步解码，起播 ~2.5ms）；`WebMClip` 现场起 ffmpeg reader（有退役泄漏病史，实际已退役）。**帧缓存变更后必须重启宠物**——路线是启动时定的，运行时不重扫。
- 帧缓存由 `frameseq_provision` 供给器生成（webm → 无损 webp 序列 + 白底反解，世代目录带源哈希）；全库 106 段已就位，新素材入库后跑 provision 补齐。
- **DPR**：showEvent 时 `sprite.set_dpr(screen.devicePixelRatio())`（D2 契约）——150% 屏不喂 = 按逻辑像素解码再被合成器放大 = 模糊。物理像素 = CANVAS × scale × dpr，pixmap 标 `setDevicePixelRatio` 后 1:1 上屏。
- 完成通知统一走 **finished 信号**：`FrameSeqClip` 没有 `isRunning` 方法，任何"轮询 isRunning 判断播没播完"的写法恒 False——状态动画会被钉死在第 0 帧、one-shot 会在 16ms 内被切走（历史上最大的"定住"事故）。播完循环：idle 由行为链续，语义状态（working/thinking/error）由壳层 `_on_state_clip_finished` 重播。

## 5. 语义状态机（main.py 壳层）

- driver 词表 → 动画映射：idle/working/thinking/error → 待机呼吸休闲/工作状态-忙碌点按/深度思考碎碎念/被吓一跳；working→idle 转换播开心跃动（one-shot）；error 持 6s。
- **one-shot**（celebrate/点击反应）：置 `_one_shot`、行为链 `forget`，finished 信号回落状态动画（idle 回行为链掷骰）。
- **状态持住**：非 idle 状态下 clip 名被换才回位；**one-shot 播放期间让位**（否则 16ms 内被覆盖）。
- 交互气泡决策期间：普通 `/say` 让位（TS renderer:470 同款守卫）——否则按钮被清、自灭计时启动、pending 挂死。options 非空的调用是交互呈现本身，不拦。

## 6. 交互体系（鼠标/触摸）

- **拖拽 grab 协议**（旧 overlay 同款）：按下 = 点击候选（`on_press` 记偏移），位移过 `DRAG_THRESHOLD × scale` 才 `begin_drag` 升级（防点击闪悬空姿态）；`on_move` 跟随（弹簧或直写）；松手 `on_release` 按末段轨迹判**甩出**（thrown → 抛掷物理接管）或原地放下。
- **触摸**：点击候选未升级 = 触摸。部位判定映射到视频帧坐标（HEAD_BOX/BODY_BOX），即时播 click 池反应 + 事件入队（driver 轮询取走进好感账本）。
- **逐像素穿透**：60ms 轮询光标位置，`sprite.alpha_at` 判定——光标不在宠的不透明像素上时整窗 `WA_TransparentForMouseEvents`，窗口罩住的桌面照常可点。
- **气泡跟随**：`_place_bubble` 每 tick 重定位（贴 sprite 上缘 +30px 入帧顶透明带、水平对齐 sprite 中心、屏内钳制）——pet 游走/被拖时气泡实时跟随。
- **活动区**：透明窗口工作区 = 行为链掷骰目标/抛掷物理/拖拽钳制共用的边界；`drag_moves_zone: true` 时拖到窗缘整窗跟随（仅拖拽会话内挪窗——自主漫游期间挪窗与行为链的局部坐标目标形成正反馈棘轮，实测出屏事故）；`false` = 硬限制锁死区内。

## 7. 行为链（pet/sprite_behavior.py，旧机语义移植）

待机时每 tick 掷骰：30% 呆立 / 10% 转向 / 20% 走路（步幅曲线驱动防脚滑）/ 40% 随机动作池（素材 `random/` 目录 40+ 动作）。动作播完 30s 强制间隔（`animation_gap_seconds`，旧机同款防"不停做动作"）。拖拽/抛掷期间行为链只接管画面（悬空/飞行动画），位置归鼠标与物理控制器；语义状态切换与 one-shot 播放触发 `forget` 作废旧计划。预测预热已禁用（后台解码线程与主线程 bind 的竞态嫌疑，帧序列起播本就 ~2.5ms 无收益）。

## 8. 多宠协作（driver 侧实现，pure 按协议发布）

- **注册表**：`~/.dsh/pet-http.d/<pet-id>.json` = `{port, pid, since, supervisor}`。每宠一个文件、唯一写者是自己（机制上无覆盖竞态）；driver 读全表，**pid 存活 + 控制面 800ms 应答**双检判在线（pid 活 ≠ 在线：冻结的事件循环留着内核 socket）。
- **路由**：会话绑定宠在线优先，否则默认宠（第一个启动的条目）；审批/提问**全员通知**——每只健康宠各挂一条长轮询臂 + web 卡片，先答者赢，败者臂被 abort → 各宠气泡 ≤100ms 自动收回（§3 的 select 探测就是为这一刻）。
- **legacy 折叠**：旧单文件发布（pet-http.json + pet:"python"）被 driver 折叠为 `id="pure"` 条目——pure 现已升级原生注册表，legacy 路径保留给旧版。
- **账本**：driver 单写 `pet-affection.json`（摸头 +2 摸身 +1，跨 10 点自主庆祝——driver 直接 POST /action，无模型参与）；多宠下每宠独立记账。

## 9. 守护与可观测性

- **faulthandler** → `pet-pure.log`（512KB 轮转）：pythonw 下 stdout/stderr 为 None，一切诊断（含原生崩溃的 Python 栈转储）靠它。**日志里有 `Current thread ... in main` 转储 = 发生过原生崩溃**。
- **心跳自愈**：driver 10s 重写自己的注册表条目（last-writer-wins 自愈）；pure 宠启动写一次 + 退出删一次（注册表无覆盖竞态，无再主张循环）。
- **崩溃转储**：HKCU LocalDumps 已为 pythonw.exe 开启（mini ×10 → `%LOCALAPPDATA%\CrashDumps`）——原生崩溃的真栈在这里。
- **driver 日志**：`pet-driver.log`（事件/竞速/投递）；**设置页** → 桌宠：每宠在线状态/账本/CDP 调试开关/重置窗口位置。

## 10. 配置（pet-pure.json，dsh home 下，首启落默认盘）

| 键 | 默认 | 说明 |
|---|---|---|
| zone_width / zone_height | 900 / 560 | 活动区尺寸（下限随 scale 联动：帧 640×360×scale + 余量） |
| zone_x / zone_y | null | 活动区锚点（null = 屏左上 120,120；配了钳屏内） |
| drag_moves_zone | true | 拖到边界是否连带挪活动区；false = 硬限制锁死 |
| animation_gap_seconds | 30.0 | 随机动作播完后的间隔（0 = 关闭） |
| scale | 0.72 | 宠物缩放（0.4–1.5；联动活动区下限与 sprite 初始居中位） |
| no_move | false | true = 禁用游走（移动桶并入动作池） |

### 设置的归属分层（设计决策）

宠物成为终端后，设置天然分两层、归属不同的 UI：

- **driver/语义层**（宠物选择、好感重置、自主表达、调试开关）归 **driver 的桌宠设置页**——桌面端侧栏已有且多宠化，pet 本体零 UI。
- **pet 本体层**（本表全部键）归 **pet-pure.json 文件**——当前量级（8 键）不值得任何 GUI；外部改文件 + 重启生效。
- **扩展路径**：配置项增长到需要 GUI 时，正确形态是控制面加 `/config` GET/PATCH 端点（契约扩展）+ driver 设置页远程渲染——**一个 UI 管所有宠**，而非移植旧 Qt 面板（每宠自带独立 UI，与"终端"定位相悖）。旧面板值得取的只有配置热重载 watcher 模式与设置项语义清单。

## 11. 已知问题与权衡

| 项 | 状态 | 说明 |
|---|---|---|
| **原生崩溃循环** | 未根因，取证管线已布 | GUI 线程 app.exec 内确定性崩溃（36 dumps / 37 launches），LocalDumps 已开启待真栈；疑点指向新壳独有路径（ToolTip 顶窗气泡快闪 / 穿透属性动态切换）。监督链把影响面压成"每几分钟换一具身体"。 |
| WebM reader 泄漏 | 影响面≈0 | 退役池累积，帧缓存全覆盖后 WebM 路线基本退役；根因未查。 |
| 多问题提问 | MVP 差距 | 只答 questions[0]（回带真实 id）；TS 支持多问题全答。 |
| 动作词表 | celebrate only | 模型 pet_do 白名单单一；TS 有 26 动作——扩词表需素材路线核对。 |
| 审批丢失窗口 | 固有 | 宠物崩溃瞬间挂起的交互随进程消失（driver 竞速回落 web 卡片兜底）；重生宠不重放历史请求。 |

## 12. 测试策略

- **契约单测**（37 例，无 Qt）：七端点形状/词表/长轮询（断连 RST+FIN 双路、心跳空格）/注册表解析/事件队列。
- **全回路冒烟**（`tests/smoke_shell.py`，offscreen）：真壳层 + 真信号接线 + 真解码 + 像素采样（grab 非透明像素）+ **槽异常收集**（定时器槽异常不打断事件循环，必须显式收集）。
- **进程内探针模式**：clip 帧号/pixmap cacheKey/气泡尺寸等内部状态断言。**屏幕帧差法永久弃用**——动态壁纸污染全数假阳性；GUI 验证只信进程内状态与窗口 grab。
- offscreen 平台的局限：量不出真实 DPR、CJK 字体渲染成豆腐、量不了真实屏幕几何——实机验收仍需人眼 + 全屏 CopyFromScreen（裁剪框必须罩住目标）。

## 13. 踩坑全集（按子系统）

- **Qt/PySide6**：wordWrap QLabel 的 sizeHint 给单行理想宽（塌竖条）——固定列宽 + `sizePolicy.setHeightForWidth(True)` 让布局算高（DPR≠1 下手动 heightForWidth+setFixedHeight 仍失准）；裸函数 + UniqueConnection 每连必警（函数幂等就去掉标志）；`FrameSeqClip` 无 isRunning；动画绑定完成检测只用 finished 信号。
- **布局坐标**：气泡/窗口定位一律 Qt 逻辑坐标（DIP），CopyFromScreen/GetWindowRect 是物理像素——150% 屏混用必错；屏内钳制统一走 `screen.availableGeometry()`。
- **Windows 进程**：taskkill 强杀不留退出旗标（监督会复活——设计）；venv pythonw 桩+真身双层；接管/重启按注册表 pid 打真身。
- **编码**：Git Bash curl 发中文 JSON 会损毁（宠物 json.loads 失败走回退）——测试用英文 body 或 Python 客户端；.cmd 文件注释用 ASCII（GBK 控制台 + UTF-8 中文注释错位成命令）。
- **Edit 工具删行**：会吞相邻行换行（import 粘连）——删行后编译/收集验证。
- **帧缓存**：变更后重启宠物（路线启动时固化）；junction 对 git/npm 均不可靠——素材已真实入库（webm 随仓库，帧缓存首启自生成）。
