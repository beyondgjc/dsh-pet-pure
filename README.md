# dsh-pet-pure

纯 UI 桌面宠物——DSH 运行时 driver 的**可替换终端**。本体零 DSH 知识：
driver（运行时内的插件）通过本地 HTTP 控制接口下发语义操作，宠物只负责
渲染与交互回报；触摸等宠物侧事件由 driver 轮询取走。

```
dsh 运行时 driver 插件 ──HTTP 六端点──▶ 本进程（Qt 壳 + 渲染闭包）
        ▲   语义状态 / 动作 / 说话 / 审批·提问
        └── GET /events 轮询触摸等事件 ◀──
```

- `main.py` — 壳层：透明置顶窗、语义状态→动画映射、气泡、触摸部位判定、
  审批/提问（数字键作答）、控制接口接线。这是唯一的新代码。
- `pet/` — 从 `dsh-pet-indesktop` 剥离的渲染闭包（webm 帧播放器、sprite
  合成、拖拽物理、素材库、ffmpeg 子进程管理等），原样搬运、无 DSH 耦合。
- 契约文档：DSH-PET.md §5.6/§7（与 Electron 载体控制接口同形——这是
  "换宠物不改 driver" 的验证标本，行为一致即可互换）。

## 控制接口（127.0.0.1，一端口一宠物）

| 端点 | 方向 | 说明 |
|---|---|---|
| `GET /capabilities` | 查询 | 四词表：states / actions / interactions / events |
| `GET /status` | 查询 | 镜像：state / animation / window / visible / pid |
| `GET /events?since=N` | 轮询 | 事件队列（seq 单调、有界；driver 重启从 0 重拉） |
| `POST /state` | 驱动 | `idle` / `thinking` / `working` / `error` |
| `POST /action` | 驱动 | 一次性动画（`celebrate`），播完回落状态动画 |
| `POST /say` | 驱动 | 气泡文本（截断 200 字） |
| `POST /interaction` | 驱动 | 审批/提问**长轮询**：响应头立即返回，正文即用户选择；驱动断连 = 撤气泡 |

`/interaction` 细节：headers 先到（fetch 立刻 resolve），正文由连接关闭
定界；宠物每秒写一个空格保活兼探测死连接——对端消失即触发
`cancel_interaction` 撤气泡，先答者赢由驱动侧裁决。

## 运行

依赖：Python 3.14 + `PySide6` + `imageio-ffmpeg==0.6.0`
（imageio-ffmpeg 是 webm 帧播放器的解码底座，缺它所有动画 `start()`
被静默拒绝——症状是宠物什么都不播）。

```bash
.venv/Scripts/python.exe -m pip install PySide6 "imageio-ffmpeg==0.6.0"
.venv/Scripts/python.exe main.py [--port 0] [--state-file PATH]
```

- **素材**：默认借用旧仓库 `dsh-pet-indesktop/assets/characters`
  （也可设 `DSH_PET_ASSETS` 环境变量，或在项目下建 `assets` junction——
  已 gitignore）。
- **发现**：端口与 pid 写入 `$DSH_HOME/pet-http.json`（默认
  `~/.dsh/pet-http.json`），driver 据此找到宠物；`--port 0` = 随机端口。
  注意该清单与 Electron 载体共用，双方只验 pid 存活、后写者赢。
- **本地交互**：左键点触摸（头/身部位判定，即时反应动画 + 事件入队）、
  拖拽移动、审批/提问按数字键作答、Esc 退出。

## 测试

```bash
# 契约单测（无 Qt 依赖，30 例）
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest -q tests/

# 全回路冒烟：真壳层 + 真信号接线 + 真解码（默认 offscreen）
.venv/Scripts/python.exe tests/smoke_shell.py
# 想肉眼看一遍：QT_QPA_PLATFORM=windows .venv/Scripts/python.exe tests/smoke_shell.py
```
