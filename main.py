"""dsh-pet-pure: a pure-UI desktop pet driven over its local HTTP control
interface (DSH-PET.md §5.6/§7). No DSH knowledge lives here: the driver
plugin inside the runtime pushes semantic state, actions, speech, and
interaction requests; the pet reports touches through the queue.

Rendering (webm/frame-sequence playback, sprite compositing, drag physics,
behavior chain) comes from the retired standalone pet's closure — vendored
whole under `pet/` because that layer never touched DSH. This file is the
new part: the shell.

Run:  .venv/Scripts/python.exe main.py [--port 0] [--state PORT_FILE]
(assets: junction to dsh-pet-indesktop/assets, or DSH_PET_ASSETS env)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QIcon, QMouseEvent, QPainter,
                           QPixmap, QRegion)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pet import catalog, http_control  # noqa: E402
from pet.http_control import ControlState, Operations, random_interaction_id  # noqa: E402
from pet.library import MovieLibrary  # noqa: E402
from pet.pet_sprite import PetSprite  # noqa: E402
from pet.sprite_behavior import BehaviorController  # noqa: E402
from pet.sprite_physics import ThrowPhysicsController  # noqa: E402

# Semantic state -> animation name (the pet's own vocabulary).
STATE_ANIMATION = {
    "idle": "待机呼吸休闲",
    "thinking": "深度思考碎碎念",
    "working": "工作状态-忙碌点按",
    "error": "被吓一跳",
    "waiting": "东张西望",
}
CELEBRATE_ANIMATION = "点击回应-开心跃动"
TOUCH_REACTIONS = [
    "点击回应-开心跃动",
    "点击回应-元气挥手",
    "点击回应-害羞惊讶",
    "点击回应-傲娇生气",
    "点击回应-挠痒咯咯笑",
]

# manifest.json's boxes in the 640x360 video frame.
HEAD_BOX = (185, 60, 436, 195)
BODY_BOX = (212, 60, 428, 330)
# 逐像素命中阈值（旧 overlay_window.ALPHA_HIT_THRESHOLD 同值）。
ALPHA_HIT_THRESHOLD = 16


def resolve_assets_root() -> Path:
    configured = os.environ.get("DSH_PET_ASSETS") or os.environ.get("DSH_PET_ASSETS_DIR")
    if configured:
        return Path(configured)
    # The pure pet sits inside the agents workspace; the shared character
    # assets live in the retired standalone pet's checkout (a junction when
    # staged next to a driver run).
    candidates = [
        HERE / "assets" / "characters",
        HERE.parent / "dsh-pet-indesktop" / "assets" / "characters",
    ]
    for candidate in candidates:
        if (candidate / "shenshen" / "videos").is_dir():
            return candidate
    return candidates[-1]


class PetOperations(QObject, Operations):
    """GUI-thread operations: the HTTP layer calls, Qt signals deliver."""

    request_state = Signal(str)
    request_action = Signal(str)
    request_say = Signal(str)
    interaction_present = Signal(str, str, object)
    interaction_cancel = Signal(str)

    def __init__(self, window: "PetWindow") -> None:
        QObject.__init__(self)
        self.window = window

    # Operations (http thread -> signal -> GUI thread)
    def set_state(self, state: str) -> None:
        self.request_state.emit(state)

    def perform(self, action: str) -> None:
        self.request_action.emit(action)

    def say(self, text: str) -> None:
        self.request_say.emit(text)

    def start_interaction(self, kind: str, request: dict) -> http_control._PendingInteraction:
        pending = http_control._PendingInteraction(random_interaction_id(), kind)
        self.interaction_present.emit(pending.id, kind, (pending, request))
        return pending

    def cancel_interaction(self, interaction_id: str) -> None:
        self.interaction_cancel.emit(interaction_id)


class SpeechBubble(QWidget):
    """A rounded bubble above the pet: plain speech, or option buttons when
    an interaction is active (buttons + number keys both answer).

    投影阴影自绘（TS 气泡 box-shadow 的 Qt 等价）：窗口比内容大一圈留出
    阴影扩散区，paintEvent 先画多层递减 alpha 的圆角矩形再画主体。
    """

    # 内容矩形相对窗口四边的留边（下边略大：阴影向下偏移，TS 的 0 6px 同款）
    M_LEFT, M_TOP, M_RIGHT, M_BOTTOM = 18, 14, 18, 24

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("pet-speech-bubble")
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(self.M_LEFT, self.M_TOP, self.M_RIGHT, self.M_BOTTOM)
        self._outer.setSpacing(10)
        self.title = QLabel(self)
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.RichText)
        self.title.setObjectName("bubble-title")
        self.title.setVisible(False)
        self._outer.addWidget(self.title)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        self.label.setObjectName("bubble-body")
        self._outer.addWidget(self.label)
        self._row = QHBoxLayout()
        self._row.setSpacing(10)
        self._row.setAlignment(Qt.AlignRight)
        self._outer.addLayout(self._row)
        self.setStyleSheet(
            "#pet-speech-bubble { background: transparent; }"
            "#pet-speech-bubble QLabel { color: #1c2733; font-size: 13px;"
            " font-family: 'Microsoft YaHei', 'Segoe UI', sans-serif; }"
            "#pet-speech-bubble QLabel#bubble-title { font-size: 14px; font-weight: 600; }"
            "#pet-speech-bubble QPushButton { border-radius: 8px; padding: 7px 20px;"
            " font-size: 13px; font-family: 'Microsoft YaHei', 'Segoe UI', sans-serif; }"
            "#pet-speech-bubble QPushButton[role='primary'] { background: #4d6bfe;"
            " color: white; border: none; }"
            "#pet-speech-bubble QPushButton[role='primary']:hover { background: #3d5af0; }"
            "#pet-speech-bubble QPushButton[role='secondary'] { background: #ffffff;"
            " color: #1c2733; border: 1px solid #d8e0ea; }"
            "#pet-speech-bubble QPushButton[role='secondary']:hover { background: #f2f5f9; }"
        )
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self._on_option = None  # callable(str) set by the window

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        from PySide6.QtGui import QColor, QPainter, QPainterPath
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        body = self.rect().adjusted(self.M_LEFT, self.M_TOP,
                                    -self.M_RIGHT, -self.M_BOTTOM)
        # 多层扩张矩形近似高斯投影（TS: 0 6px 24px rgba(30,40,60,.18)）
        for grow, alpha in ((1, 26), (3, 18), (6, 11), (10, 6), (14, 3)):
            halo = body.adjusted(-grow, -grow // 2, grow, grow + 4)
            path = QPainterPath()
            path.addRoundedRect(halo, 12 + grow, 12 + grow)
            painter.fillPath(path, QColor(30, 40, 60, alpha))
        path = QPainterPath()
        path.addRoundedRect(body, 12, 12)
        painter.fillPath(path, QColor(255, 255, 255, 247))
        painter.setPen(QColor(216, 224, 234))
        painter.drawPath(path)

    def say(self, text: str, *, title: str | None = None,
            options: list[str] | None = None) -> None:
        import html as _html
        # 标题（approval 的 toolName / question 的问题行）——有才显示
        self.title.setText(f"<p style='margin:0;'>{_html.escape(title)}</p>"
                           if title else "")
        self.title.setVisible(bool(title))
        safe = _html.escape(text).replace("\n", "<br>")
        self.label.setText(f"<p style='line-height:155%; margin:0;'>{safe}</p>")
        while self._row.count():
            item = self._row.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        if options:
            from PySide6.QtWidgets import QPushButton
            for index, option in enumerate(options, start=1):
                button = QPushButton(option, self)
                # 第一个选项 = 主操作（TS 同款：允许主蓝、拒绝次白）
                button.setProperty("role", "primary" if index == 1 else "secondary")
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(lambda _=False, opt=option: self._pick(opt))
                self._row.addWidget(button, 0, Qt.AlignRight)
        # QLabel 带 wordWrap 的尺寸陷阱（DPR 1.5 实锤）：sizeHint 给"单行理想
        # 宽"、手动 heightForWidth+setFixedHeight 在非 1.0 DPR 下失准 → 正文
        # 高度塌 0。标准解 = 固定列宽 + sizePolicy 声明 heightForWidth，让
        # 布局自己用换行高度（宽 372 = TS #bubble 400 - padding 14×2）。
        content_width = 372
        for label in ((self.title,) if title else ()) + (self.label,):
            policy = label.sizePolicy()
            policy.setHeightForWidth(True)
            label.setSizePolicy(policy)
            label.setFixedWidth(content_width)
        self.adjustSize()
        self.show()
        # 审批/提问不自动消失（用户在决策）；普通说话按长度定时
        if options is None:
            self._timer.start(4000 + min(len(text) * 60, 4000))
        else:
            self._timer.stop()

    def _pick(self, option: str) -> None:
        callback = self._on_option
        self.hide()
        if callback is not None:
            callback(option)


class PetWindow(QWidget):
    """The transparent always-on-top shell hosting one sprite + bubbles."""

    def __init__(self, *, zone_size: tuple[int, int] = (900, 560),
                 zone_pos: tuple[int, int] | None = None,
                 drag_moves_zone: bool = True,
                 manifest_path: Path | None = None,
                 scale: float = catalog.DEFAULT_SCALE,
                 animation_gap: float = 30.0,
                 no_move: bool = False) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 活动区 = 窗口工作区（行为链/物理/拖拽钳制共用），尺寸可配置；
        # 缩放联动下限：帧 640×360×scale + 余量，区比帧小宠会被裁
        frame_w, frame_h = int(640 * scale), int(360 * scale)
        self.resize(max(ZONE_MIN_W, int(zone_size[0]), frame_w + 40),
                    max(ZONE_MIN_H, int(zone_size[1]), frame_h + 40))
        self._drag_moves_zone = drag_moves_zone

        self.library = MovieLibrary(
            parent=self,
            character_id=catalog.DEFAULT_CHARACTER,
            asset_dir=resolve_assets_root() / catalog.DEFAULT_CHARACTER / "videos",
            prewarm_enabled=True,
        )
        # sprite 初始位 = 活动区居中（帧 640×360×scale）
        self.sprite = PetSprite(
            self.library,
            pos=QPointF((self.width() - frame_w) // 2, (self.height() - frame_h) // 2),
            scale=scale,
            parent=self)
        # PetSprite 是纯 QObject 模型：像素要靠宿主的 paintEvent 画（旧
        # overlay_window 的分工）。挂脏回调——位移/帧到达直驱局部重绘，
        # 不经 tick（V-3/V-4 同款）。
        def _on_sprite_dirty(old, new) -> None:
            try:
                self.update(QRegion(old) | QRegion(new))
            except RuntimeError:
                pass  # 窗口 C++ 侧已销毁（退出竞态）
        self.sprite._dirty_cb = _on_sprite_dirty
        self.bubble = SpeechBubble(self)

        # 仿真控制器组（screen_host 的单宠版）：行为链（游走/转向/随机动作）
        # + 抛掷物理，域 = 窗口局部工作区（宠 confined 在窗内，见 _tick 注）。
        # 碰撞世界单宠无意义，不装。
        self.behavior = BehaviorController(QRect(0, 0, self.width(), self.height()))
        # 动作间隔（config 可调；0 = 关闭间隔）与游走开关
        self.behavior.animation_gap_seconds = animation_gap
        self.behavior.no_move = no_move
        # 预测预热禁用：后台解码线程与主线程 bind 竞态（idle 时 one-shot
        # bind 间歇性原生崩，working/forget 后不崩的实测模式）。帧序列
        # 起播本就 ~2.5ms，预热收益远小于稳定性。
        self.behavior.predict_enabled = False
        self.physics = ThrowPhysicsController(self.behavior.bounds)
        self.sprite.set_bounds(QRect(0, 0, self.width(), self.height()))

        self.state = "idle"
        self._one_shot: str | None = None
        self._error_until = 0.0
        # /action 的可观察见证（TS 载体同款字段，恒在场——条件缺席是
        # JSON.stringify 的 undefined 陷阱）
        self._last_action: str | None = None
        self._last_action_at: int | None = None
        self._last_action_done: dict | None = None
        # 鼠标会话（旧 overlay 的 grab 协议）：按下=点击候选，过阈值才升级
        self._mouse_grab = False
        self._press_global: QPoint | None = None
        self._drag_committed = False
        self._piercing = False
        self._manifest_path = manifest_path
        self._active_interaction: tuple[http_control._PendingInteraction, list[str]] | None = None

        self.operations = PetOperations(self)
        self._wire_operations()
        # Standalone/manual runs see the pet before the driver's first
        # /state: start on the idle animation instead of an empty sprite.
        self.play(STATE_ANIMATION[self.state])

        screen = QApplication.primaryScreen().availableGeometry()
        if zone_pos is not None:
            # 配置锚点钳进屏内（窗口整体不出屏）
            self.move(
                max(screen.left(), min(int(zone_pos[0]), screen.right() - self.width() + 1)),
                max(screen.top(), min(int(zone_pos[1]), screen.bottom() - self.height() + 1)))
        else:
            self.move(screen.left() + 120, screen.top() + 120)

        tick = QTimer(self)
        tick.setInterval(16)
        tick.timeout.connect(self._tick)
        tick.start()

        # 逐像素穿透轮询（旧 overlay 穿透控制的轻量版）：光标不在宠的不透明
        # 像素上时整窗穿透——窗内透明区域不再吞桌面点击
        pierce = QTimer(self)
        pierce.setInterval(60)
        pierce.timeout.connect(self._update_pierce)
        pierce.start()

    # -- operations wiring ------------------------------------------------
    def _wire_operations(self) -> None:
        ops = self.operations
        ops.request_state.connect(self._on_state)
        ops.request_action.connect(self._on_action)
        ops.request_say.connect(self._on_say)
        ops.interaction_present.connect(self._on_interaction)
        ops.interaction_cancel.connect(self._on_cancel)

    # -- state machine ----------------------------------------------------
    def play(self, name: str, one_shot: bool = False) -> None:
        if one_shot:
            self._one_shot = name
            # 一次性播放打断行为链：内部状态作废，播完由 tick 重新掷骰
            self.behavior.forget(self.sprite)
        if self.sprite.bind_clip(name):
            # 完成检测统一走 finished 信号（FrameSeqClip 无 isRunning 方法，
            # tick 轮询恒 False：one_shot 16ms 内被切走、状态动画被钉在
            # 第 0 帧——两类"定住/闪没"都是它）。UniqueConnection 防同一
            # clip 对象（library 缓存复用）重复连接。
            finished = getattr(self.sprite._clip, "finished", None)
            if finished is not None:
                slot = (self._on_one_shot_finished if one_shot
                        else self._on_state_clip_finished)
                if self.state != "idle" or one_shot:
                    finished.connect(slot, Qt.UniqueConnection)
            return
        # Missing animation is non-fatal; the state machine keeps running.
        print(f"[pet-pure] animation not found: {name}", file=sys.stderr)

    def _on_one_shot_finished(self) -> None:
        # 完成收据（TS 载体同款）：durationMs = 从推入到播完的实际墙钟
        if self._last_action_done is not None and self._last_action_done["durationMs"] is None:
            self._last_action_done["durationMs"] = int(time.time() * 1000) - self._last_action_done["at"]
        self._one_shot = None
        # 播完回落：语义状态回状态动画；idle 回行为链（tick 掷骰接管）
        if self.state != "idle":
            self.play(STATE_ANIMATION[self.state])

    def _on_state_clip_finished(self) -> None:
        if self._one_shot is None and self.state != "idle":
            self.play(STATE_ANIMATION[self.state])

    def _on_state(self, state: str) -> None:
        finished_work = self.state == "working" and state == "idle"
        self.state = state
        # 语义状态切换：行为链内部状态作废（working/thinking 期间它不 tick，
        # 旧计划留着会在回 idle 时瞬间执行）
        self.behavior.forget(self.sprite)
        if state == "error":
            self._error_until = self._monotonic() + 6.0
        if finished_work:
            self.play(CELEBRATE_ANIMATION, one_shot=True)
        else:
            self.play(STATE_ANIMATION[state])

    def _on_action(self, action: str) -> None:
        # /action 的可观察见证（TS 载体同款契约）：override 在单 tick 内可能
        # 设/清，不是可靠见证——消费方轮询 lastAction/lastActionAt。
        self._last_action = action
        self._last_action_at = int(time.time() * 1000)
        self._last_action_done = {"action": action, "durationMs": None, "at": self._last_action_at}
        if action == "celebrate":
            self.play(CELEBRATE_ANIMATION, one_shot=True)

    def _place_bubble(self) -> None:
        """气泡跟随宠物：贴 sprite 头顶、水平对齐 sprite 中心，钳在屏内。

        每 tick 调用——pet 会游走/被拖，say 时刻的定位立即过时（TS 宠的
        气泡长在宠物窗口里天然跟随，pure 的活动窗大，必须显式跟）。
        垂直锚点下移 30px：视频帧顶部是透明边距（头部约从帧高 1/6 处
        开始），按矩形顶定位会在头顶留出一条可见空隙。
        """
        if not self.bubble.isVisible():
            return
        geometry = self.geometry()
        center = self.sprite.rect().center()
        bx = geometry.x() + center.x() - self.bubble.width() // 2
        by = geometry.y() + self.sprite.rect().top() + 30 - self.bubble.height()
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen is not None:
            bx = max(screen.left() + 4, min(bx, screen.right() - self.bubble.width() - 4))
            by = max(screen.top() + 4, min(by, screen.bottom() - self.bubble.height() - 4))
        self.bubble.move(bx, by)

    def _on_say(self, text: str, *, title: str | None = None,
                options: list[str] | None = None) -> None:
        if options is None and self._active_interaction is not None:
            # 交互气泡决策中：普通 say 让位（TS 渲染层同款守卫，renderer:470）。
            # 不让位的后果 = 按钮被清、自灭计时启动、pending 挂死到 driver
            # abort——模型"边说话边请求工具"是常态路径。options 非空的调用
            # 是交互呈现本身，不拦。
            print("[pet-pure] /say ignored: interaction bubble is up", file=sys.stderr)
            return
        self.bubble.say(text, title=title, options=options)  # 先定尺寸再定位
        self.bubble._on_option = self._answer_interaction
        self._place_bubble()

    def _on_interaction(self, interaction_id: str, kind: str, payload: object) -> None:
        pending, request = payload
        title: str | None
        if kind == "question":
            questions = request.get("questions") or []
            first = questions[0] if questions else {}
            # 平台问题的真实 id（应答回带它；写死 "q1" 会被丢弃）。
            # MVP 差距：只支持 questions[0]，多问题全答是 TS 已有的能力。
            self._active_question_id = str(first.get("id") or "q1")
            title = str(first.get("question") or "请选择")
            options = [str(o.get("label")) for o in first.get("options") or [] if o.get("label")]
            text = "请选择一个选项"
        else:
            tool_name = request.get("toolName")
            title = f"请求执行：{tool_name}" if tool_name else None
            text = request.get("displayReason") or request.get("reason") or "请求执行"
            if isinstance(text, dict):
                text = text.get("zh") or text.get("en") or "请求执行"
            options = ["允许", "拒绝"]
        self._active_interaction = (pending, options)
        self._on_say(str(text), title=title, options=options)
        pending.settled.clear()

    def _on_cancel(self, _interaction_id: str) -> None:
        self._active_interaction = None
        self.bubble.hide()

    # -- frame loop -------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().showEvent(event)
        # DPR 归一化（D2，旧 overlay._feed_sprite_dpr 同款）：窗口 realize 后
        # 按所在屏喂——150% 屏不喂 = 按逻辑像素解码再被合成器放大 → 模糊
        screen = self.screen()
        if screen is not None:
            self.sprite.set_dpr(float(screen.devicePixelRatio()))

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if getattr(self.sprite, "visible", True):
            self.sprite.paint(painter)

    @staticmethod
    def _monotonic() -> float:
        import time
        return time.monotonic()

    def _tick(self) -> None:
        dt = 1 / 60
        error_active = self._error_until > self._monotonic()
        if self.state == "idle" and self._one_shot is None and not error_active:
            # 行为链只在待机跑（working/thinking 持状态动画、error 持受惊）；
            # drag/thrown 时它内部只接管画面，位置归鼠标/物理
            self.behavior.tick([self.sprite], dt)
        self.physics.tick([self.sprite], dt)
        self.sprite.advance(dt)
        # one_shot 的完成检测走 finished 信号（_on_one_shot_finished）——
        # 不在 tick 里轮询 isRunning（FrameSeqClip 无该方法，恒 False 会让
        # one_shot 在 16ms 内被切走，点击反应/celebrate 全部只见一闪）
        if self._one_shot is None and error_active:
            # Rebinding every tick would restart the clip from frame 0
            # (bind_clip stops the old clip unconditionally), freezing the
            # animation — only enter it when something else is playing.
            if self.sprite._clip_name != "被吓一跳":
                self.play("被吓一跳")
        elif self.state != "idle":
            # 状态动画持住：clip 名被别的路径换掉才回位。绝不能按
            # "isRunning" 判断——FrameSeqClip 没有该方法（getattr 恒 False），
            # 每 tick 重绑会把动画永远钉在第 0 帧（实机"定住"的根因）；
            # 循环播放由 finished 信号驱动（_on_state_clip_finished）。
            # one_shot 播放期间不持住——否则 one_shot 在 16ms 内被本分支
            # 覆盖回状态动画（celebrate/点击反应"不会做"的根因）。
            want = STATE_ANIMATION[self.state]
            if self._one_shot is None and self.sprite._clip_name != want:
                self.play(want)
        # 窗随宠移只在**拖拽会话内**且 drag_moves_zone 开启时启用：行为链的
        # 移动目标是窗口局部坐标，自主漫游期间挪窗会形成正反馈棘轮（试验版
        # 窗口被拖出屏幕）——拖拽的位置由鼠标驱动，没有那个反馈源。关掉
        # drag_moves_zone 即硬限制模式：宠被锁死在配置的活动区内。
        if self._drag_committed and self._drag_moves_zone:
            self._follow_drag_window()
        self._place_bubble()
        self._publish_snapshot()

    # -- 窗随宠移（仅拖拽会话）与穿透 --------------------------------------
    def _follow_drag_window(self) -> None:
        """拖拽中宠到窗缘就挪窗跟随，活动范围由此跨出窗口。

        窗口移动 + sprite 局部坐标反向补偿 = 宠的屏幕位置本 tick 不动，下一
        个鼠标事件继续追光标。窗口钳在屏内——绝不重蹈出屏覆辙。
        """
        rect = self.sprite.rect()
        margin = 140
        dx = dy = 0
        if rect.left() < margin:
            dx = rect.left() - margin
        elif rect.right() > self.width() - margin:
            dx = rect.right() - (self.width() - margin)
        if rect.top() < margin:
            dy = rect.top() - margin
        elif rect.bottom() > self.height() - margin:
            dy = rect.bottom() - (self.height() - margin)
        if dx == 0 and dy == 0:
            return
        geo = self.geometry()
        screen = self.screen().availableGeometry() if self.screen() else None
        nx, ny = geo.x() + dx, geo.y() + dy
        if screen is not None:
            nx = max(screen.left(), min(nx, screen.right() - self.width() + 1))
            ny = max(screen.top(), min(ny, screen.bottom() - self.height() + 1))
        if nx == geo.x() and ny == geo.y():
            return
        self.move(nx, ny)
        self.sprite.set_pos(self.sprite.pos - QPointF(nx - geo.x(), ny - geo.y()))

    def _update_pierce(self) -> None:
        """逐像素穿透轮询：光标不在宠身上且未拖拽 → 整窗让出鼠标。"""
        if self._drag_committed or self._mouse_grab:
            self._set_pierce(False)
            return
        if QApplication.activePopupWidget() is not None:
            return  # 右键菜单开着，别动穿透态
        if not self.isVisible():
            return
        gpos = QCursor.pos()
        origin = self.geometry().topLeft()
        local = QPoint(int(gpos.x() - origin.x()), int(gpos.y() - origin.y()))
        inside = 0 <= local.x() < self.width() and 0 <= local.y() < self.height()
        self._set_pierce(not (inside and self._hit_sprite(local)))

    def _set_pierce(self, pierce: bool) -> None:
        if self._piercing == pierce:
            return
        self._piercing = pierce
        self.setAttribute(Qt.WA_TransparentForMouseEvents, pierce)

    # -- 右键菜单 ----------------------------------------------------------
    # -- 托盘与菜单（右键/托盘共用一份动作） --------------------------------
    def _fill_pet_menu(self, menu) -> None:
        act_summon = menu.addAction("召唤到屏幕中央")
        act_log = menu.addAction("打开日志文件夹")
        act_restart = menu.addAction("重启宠物")
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_summon.triggered.connect(self._summon)
        act_log.triggered.connect(self._open_logs)
        act_restart.triggered.connect(lambda: os._exit(3))  # 无旗标硬退 → 监督 2s 重生
        act_quit.triggered.connect(self.close)

    def _summon(self) -> None:
        """宠物游走/意外出屏时的找回：窗口回屏中央、sprite 回活动区中央。"""
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen is not None:
            self.move(screen.center().x() - self.width() // 2, screen.top() + 120)
        self.sprite.set_pos(QPointF((self.width() - self.sprite.rect().width()) // 2,
                                    (self.height() - self.sprite.rect().height()) // 2))
        self.show()
        self.raise_()
        self.activateWindow()

    def _open_logs(self) -> None:
        if self._manifest_path is not None:
            os.startfile(str(self._manifest_path.parent.parent))

    def _tray_icon(self) -> QIcon:
        """托盘图标 = 待机首帧裁出角色主体方块（视频帧带透明边距，整帧缩放
        会小成一个点）；多尺寸预缩入 QIcon（托盘按 DPI 取最接近的）。
        无缓存回退纯色圆标。"""
        import glob
        icon = QIcon()
        frames = sorted(glob.glob(str(
            resolve_assets_root() / "shenshen" / "frameseq" / "idle" / "待机呼吸休闲.g*" / "f_0001.webp")))
        if frames:
            source = QPixmap(frames[0])
            if not source.isNull():
                # 角色主体方块：视频帧内以 BODY_BOX 中心 (320,195) 取 270×270
                body_square = source.copy(185, 60, 270, 270)
                for size in (16, 24, 32, 48, 64, 128):
                    icon.addPixmap(body_square.scaled(
                        size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return icon
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor("#4d6bfe"))
        return QIcon(pixmap)

    def _setup_tray(self) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return  # 无托盘环境（offscreen/裁剪）：静默跳过
        self.tray = QSystemTrayIcon(self._tray_icon(), self)
        tray_menu = QMenu()
        self._tray_state = tray_menu.addAction("")
        self._tray_state.setEnabled(False)
        tray_menu.addSeparator()
        self._fill_pet_menu(tray_menu)
        self.tray.setContextMenu(tray_menu)
        self.tray.setToolTip("dsh-pet-pure")
        self.tray.show()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        menu = QMenu(self)
        self._fill_pet_menu(menu)
        chosen = menu.exec(event.globalPos())
        if chosen is not None and chosen.text() == "退出":
            self.close()

    def _publish_snapshot(self) -> None:
        control_state = getattr(self, "_control_state", None)
        if control_state is None:  # main() 接线完成前 tick 先到：静默跳过
            return
        geometry = self.geometry()
        # window_facts 走 snapshot() 的合并通道；每 tick 直写（dict 换引用，
        # HTTP 线程读到的是某一代完整值）。TS 载体的 /status 同样回报实况。
        control_state.window_facts = {
            "x": geometry.x(), "y": geometry.y(),
            "width": geometry.width(), "height": geometry.height(),
        }
        # 字段对齐 TS 载体的 /status（pet_state 工具原样透传给模型）：
        # animation 报文件名（.webm 后缀），override = 一次性动作覆盖
        animation = self.sprite._clip_name
        if getattr(self, "tray", None) is not None:
            self.tray.setToolTip(f"dsh-pet-pure: {self.state} / {animation}")
            self._tray_state.setText(f"状态：{self.state}　动画：{animation or '无'}")
        control_state.publish_snapshot({
            "state": self.state,
            "animation": f"{animation}.webm" if animation else animation,
            "override": f"{self._one_shot}.webm" if self._one_shot else None,
            # TS 载体同款见证字段（恒在场）：/action 的可观察痕迹
            "lastAction": self._last_action,
            "lastActionAt": self._last_action_at,
            "lastActionDone": self._last_action_done,
            "visible": self.isVisible(),
            "interaction": self._active_interaction is not None,
        })

    # -- input -------------------------------------------------------------
    def _hit_sprite(self, local: QPoint) -> bool:
        """旧 overlay.sprite_at 的单宠版：绘制外接矩形粗筛 + alpha 细判。"""
        bounds = (self.sprite.paint_bounds()
                  if hasattr(self.sprite, "paint_bounds") else self.sprite.rect())
        if not bounds.contains(local):
            return False
        rect = self.sprite.rect()
        alpha = self.sprite.alpha_at(QPoint(local.x() - rect.x(), local.y() - rect.y()))
        return alpha >= ALPHA_HIT_THRESHOLD

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._hit_sprite(event.position().toPoint()):
            # 按下只是点击候选（不置拖拽态）：过了阈值才 begin_drag 升级，
            # 否则每次点击都会闪悬空姿态（旧 overlay 同款协议）
            self._mouse_grab = True
            self._press_global = event.globalPosition().toPoint()
            self._drag_committed = False
            self.sprite.on_press(QPointF(event.position()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._mouse_grab:
            if not self._drag_committed and self._press_global is not None:
                threshold = catalog.DRAG_THRESHOLD * self.sprite.scale
                delta = event.globalPosition().toPoint() - self._press_global
                if delta.manhattanLength() >= threshold:
                    self._drag_committed = True
                    self.sprite.begin_drag()
                    self.behavior.on_drag_started(self.sprite)
            self.sprite.on_move(QPointF(event.position()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._mouse_grab:
            self._mouse_grab = False
            committed = self._drag_committed
            self._drag_committed = False
            self._press_global = None
            if committed:
                # 真拖拽松手：按轨迹判甩出（thrown→物理接管）或原地放下
                self.sprite.on_release(QPointF(event.position()))
                self.behavior.on_drag_released(self.sprite)
            else:
                self._handle_touch(event.position().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        active = self._active_interaction
        if active is not None and event.text().isdigit():
            index = int(event.text()) - 1
            _pending, options = active
            if 0 <= index < len(options):
                self._answer_interaction(options[index])
                return
        if event.key() == Qt.Key_Escape:
            self.close()
        super().keyPressEvent(event)

    def _handle_touch(self, local: QPoint) -> None:
        import random
        # 点击反应走行为链的 click 池（链管理：播完自动回掷骰；working 等
        # 非 normal 态返回 False）；池不可用再退一次性播放
        if not self.behavior.on_sprite_clicked(self.sprite):
            self.play(random.choice(TOUCH_REACTIONS), one_shot=True)
        # Translate the click into the 640x360 video frame.
        rect = self.sprite.rect()
        scale = min(rect.width() / 640, rect.height() / 360)
        video_x = (local.x() - rect.x() - (rect.width() - 640 * scale) / 2) / scale
        video_y = (local.y() - rect.y() - (rect.height() - 360 * scale) / 2) / scale
        part = "unknown"
        if HEAD_BOX[0] <= video_x <= HEAD_BOX[2] and HEAD_BOX[1] <= video_y <= HEAD_BOX[3]:
            part = "head"
        elif BODY_BOX[0] <= video_x <= BODY_BOX[2] and BODY_BOX[1] <= video_y <= BODY_BOX[3]:
            part = "body"
        if getattr(self, "_control_state", None) is not None:
            self._control_state.emit("touch", part=part)
        print(f"[pet-pure] touch ({part})")

    def _answer_interaction(self, answer: str) -> None:
        active = self._active_interaction
        if active is None:
            return
        pending, options = active
        self._active_interaction = None
        self.bubble.hide()
        if pending.kind == "question":
            qid = getattr(self, "_active_question_id", "q1")
            pending.answer = {"answer": {"answers": [{"id": qid, "selected": [answer]}]}}
        else:
            payload = {"allowed-once": "allowed-once", "拒绝": "rejected", "允许": "allowed-once"}
            pending.answer = {"answer": payload.get(answer, answer)}
        pending.settled.set()

    # -- lifecycle ---------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self._manifest_path is not None:
            try:
                # 退出留痕：监督父进程看到旗标即不重生（用户主动退出的判定
                # 依据，退出路径上的原生崩溃会让退出码非 0）。旗标放 home
                # 根（注册表目录保持只有条目文件）。
                self._manifest_path.parent.parent.joinpath(
                    "pet-pure-exit.flag").write_text("quit", encoding="utf-8")
            except OSError:
                pass
        self.library.shutdown()
        self._control_server.shutdown()
        super().closeEvent(event)


# 壳层配置（pet-pure.json，与清单同目录）。zone = 活动区（透明窗口工作区），
# 行为链/抛掷物理/拖拽钳制共用；zone_x/y 缺省 = 屏幕左上 (120,120) 起步。
# 硬下限：sprite 帧 461x259 + 余量——区比帧还小宠会被裁，不准配。
ZONE_MIN_W, ZONE_MIN_H = 500, 330
SHELL_CONFIG_DEFAULTS = {
    "zone_width": 900,
    "zone_height": 560,
    "zone_x": None,
    "zone_y": None,
    "drag_moves_zone": True,
    "animation_gap_seconds": 30.0,
    "scale": 0.72,
    "no_move": False,
}


def load_shell_config(path: Path) -> dict:
    """读壳层配置；文件缺失/损坏/键非法逐项回退默认（绝不因配置挂壳）。"""
    config = dict(SHELL_CONFIG_DEFAULTS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return config
    if not isinstance(raw, dict):
        return config
    for key in ("zone_width", "zone_height"):
        try:
            value = int(raw.get(key, config[key]))
        except (TypeError, ValueError):
            continue
        if key == "zone_width":
            config[key] = max(ZONE_MIN_W, value)
        else:
            config[key] = max(ZONE_MIN_H, value)
    for key in ("zone_x", "zone_y"):
        try:
            config[key] = int(raw[key])
        except (TypeError, ValueError, KeyError):
            config[key] = None
    config["drag_moves_zone"] = bool(raw.get("drag_moves_zone", True))
    # 行为参数：动作间隔（0 = 关闭间隔）、宠物缩放（钳在可读区间）、游走开关
    for key, lo, hi in (("animation_gap_seconds", 0.0, 3600.0), ("scale", 0.4, 1.5)):
        try:
            value = float(raw.get(key, config[key]))
        except (TypeError, ValueError):
            continue
        config[key] = max(lo, min(hi, value))
    config["no_move"] = bool(raw.get("no_move", False))
    return config


def save_default_shell_config(path: Path, config: dict) -> None:
    """首次运行落一份默认配置（用户可编辑；之后绝不覆盖用户改动）。"""
    try:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError:
        pass  # 配置落盘失败不拦启动


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # CPython 在 Windows 上特判 0 号信号 = 存在性探测
        return True
    except OSError:
        return False


def main() -> int:
    import faulthandler
    parser = argparse.ArgumentParser(description="dsh-pet-pure: pure-UI desktop pet")
    parser.add_argument("--port", type=int, default=0, help="control interface port (0 = random)")
    parser.add_argument("--assets", type=str, default=None, help="assets root dir")
    parser.add_argument("--state-file", type=str, default=None,
                        help="legacy single-file manifest anchor; its parent "
                             "dir is the dsh home (default ~/.dsh). The pet "
                             "publishes to pet-http.d/<pet-id>.json there")
    parser.add_argument("--pet-id", type=str, default="pure",
                        help="registry entry id = pet-http.d/<pet-id>.json; "
                             "multi-pet routing, default宠 selection and the "
                             "affection ledger all key on it")
    parser.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--no-supervise", action="store_true",
                        help="run once; do not auto-restart after a crash")
    args = parser.parse_args()

    if args.assets:
        os.environ["DSH_PET_ASSETS"] = args.assets
    print(f"[pet-pure] assets root: {resolve_assets_root()}")

    # dsh home 锚点：--state-file 只用来定位 home（兼容旧调用习惯），
    # 发布目标是注册表 pet-http.d/<pet-id>.json（多宠架构，per-pet 文件
    # 从机制上消灭 last-writer-wins 竞态）。
    anchor = Path(args.state_file or os.path.join(
        os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
        "pet-http.json",
    ))
    home = anchor.parent
    registry_file = home / "pet-http.d" / f"{args.pet_id}.json"
    # 单实例接管（同 id）：注册表文件天然只含本 id 的 pid——旧实例活着
    # 就终止它（用户手动重启 = 换血，直觉正确）；别的 id 不动（多宠共存）。
    # **先杀旧监督者再杀旧真身**：监督者若活着，2s 内会重生真身，与本实例
    # 的接管互杀成战争（TerminateProcess 不留退出旗标，旧监督者分不清）。
    try:
        old = json.loads(registry_file.read_text(encoding="utf-8"))
        old_pid = int(old.get("pid") or 0)
        old_sup = int(old.get("supervisor") or 0)
        if old_pid and old_pid != os.getpid() and _pid_alive(old_pid):
            if old_sup and old_sup != os.getpid() and _pid_alive(old_sup):
                os.kill(old_sup, signal.SIGTERM)
                print(f"[pet-pure] terminated previous supervisor pid={old_sup}", file=sys.stderr)
            os.kill(old_pid, signal.SIGTERM)
            print(f"[pet-pure] took over previous '{args.pet_id}' pid={old_pid}", file=sys.stderr)
    except (OSError, ValueError):
        pass  # 没有注册表条目/损坏/进程已死：当首例启动
    # 旧单清单文件退役：本宠过去发布的 pet-http.json 若还在，删掉——
    # 驱动 legacy 折叠会把死 pid 条目忽略，但留着是垃圾。
    if args.state_file is None:
        try:
            anchor.unlink(missing_ok=True)
        except OSError:
            pass
    # pythonw 下 stdout/stderr 是 None——诊断与 faulthandler 全部落盘，
    # "启动没效果"从此打开 pet-pure.log 就能查（超 512KB 重开截断）。
    log_path = home / "pet-pure.log"
    try:
        size = log_path.stat().st_size if log_path.exists() else 0
        log_file = open(log_path, "w" if size > 512_000 else "a",
                        buffering=1, encoding="utf-8", errors="replace")
        if sys.stdout is None or sys.stderr is None:
            sys.stdout = log_file
            sys.stderr = log_file
        faulthandler.enable(log_file)
        print(f"[pet-pure] ---- launch pid={os.getpid()} registry={registry_file} ----", flush=True)
    except OSError:
        pass
    exit_flag_path = home / "pet-pure-exit.flag"
    if not args.supervised and not args.no_supervise:
        # 监督模式（默认）：app.exec 里偶发原生崩溃（faulthandler 只留转储、
        # 无细节）自动重生——换宠连续性优先，尸检照常落盘。正常退出
        # （右键退出/Esc → code 0）不重生。
        import subprocess
        import time as _time
        supervisor_pid = os.getpid()
        child_argv = [os.path.abspath(__file__),
                      *(a for a in sys.argv[1:] if a != "--supervised"),
                      "--supervised", "--supervisor-pid", str(supervisor_pid)]
        while True:
            code = subprocess.call([sys.executable, *child_argv])
            print(f"[pet-pure] child exited code={code}", flush=True)
            if exit_flag_path.exists():
                # 子进程 closeEvent 留了痕 = 用户主动退出（退出路径上的原生
                # 崩溃会让 code 非 0，不能据此重生违背用户意志）
                exit_flag_path.unlink(missing_ok=True)
                print("[pet-pure] graceful exit requested", flush=True)
                return 0
            if code == 0:
                return 0
            # 让位检查：注册表条目的 supervisor 已换成别的 pid = 新实例接管
            # 了本宠的坑位——重生就是互杀战争的第一枪，让位退出。
            try:
                entry = json.loads(registry_file.read_text(encoding="utf-8"))
                owner = int(entry.get("supervisor") or 0)
                if owner and owner != supervisor_pid and _pid_alive(owner):
                    print(f"[pet-pure] registry owned by supervisor {owner} — yielding", flush=True)
                    return 0
            except (OSError, ValueError):
                pass
            print("[pet-pure] respawning in 2s", flush=True)
            _time.sleep(2)
    # 壳层配置（活动区/锚点/拖拽是否连带挪区）：dsh home 下的 pet-pure.json。
    config_path = home / "pet-pure.json"
    shell_config = load_shell_config(config_path)
    save_default_shell_config(config_path, shell_config)

    app = QApplication(sys.argv)
    app.setApplicationName("dsh-pet-pure")

    zone_pos = None
    if shell_config["zone_x"] is not None and shell_config["zone_y"] is not None:
        zone_pos = (shell_config["zone_x"], shell_config["zone_y"])
    window = PetWindow(
        zone_size=(shell_config["zone_width"], shell_config["zone_height"]),
        zone_pos=zone_pos,
        drag_moves_zone=shell_config["drag_moves_zone"],
        manifest_path=registry_file,
        scale=shell_config["scale"],
        animation_gap=shell_config["animation_gap_seconds"],
        no_move=shell_config["no_move"])

    control_state = ControlState()
    window._control_state = control_state
    control_state.operations = window.operations
    server = http_control.serve(control_state, port=args.port)
    window._control_server = server
    port = server.server_address[1]

    try:
        registry_file.parent.mkdir(parents=True, exist_ok=True)
        registry_file.write_text(json.dumps({
            "port": port,
            "pid": os.getpid(),
            "since": int(__import__("time").time() * 1000),
            # 监督者 pid：接管者先杀它再杀真身（防重生互杀战争），重生循环
            # 据此让位。driver 只读 port/pid/since，多出的字段被忽略。
            "supervisor": args.supervisor_pid or None,
        }) + "\n", encoding="utf-8")
        print(f"[pet-pure] control interface on 127.0.0.1:{port} "
              f"(registry: {registry_file})")
    except OSError as error:
        print(f"[pet-pure] could not write the registry entry: {error}", file=sys.stderr)

    window.show()
    signal.signal(signal.SIGINT, lambda *_a: app.quit())
    # A timer keeps the interpreter responsive to SIGINT under Qt.
    window._setup_tray()
    keeper = QTimer()
    keeper.setInterval(200)
    keeper.timeout.connect(lambda: None)
    keeper.start()

    try:
        return app.exec()
    finally:
        try:
            registry_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
