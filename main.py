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
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QMouseEvent, QPainter, QRegion
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
    """A rounded self-dismissing speech bubble above the pet."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("pet-speech-bubble")
        from PySide6.QtWidgets import QLabel, QVBoxLayout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(360)
        layout.addWidget(self.label)
        self.setStyleSheet(
            "#pet-speech-bubble { background: rgba(255,255,255,238); border: 1px solid #d8e0ea;"
            " border-radius: 12px; } #pet-speech-bubble QLabel { color: #1c2733; font-size: 13px; }"
        )
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def say(self, text: str) -> None:
        self.label.setText(text)
        self.adjustSize()
        self.show()
        self._timer.start(4000 + min(len(text) * 60, 4000))


class PetWindow(QWidget):
    """The transparent always-on-top shell hosting one sprite + bubbles."""

    def __init__(self, *, zone_size: tuple[int, int] = (900, 560),
                 zone_pos: tuple[int, int] | None = None,
                 drag_moves_zone: bool = True) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 活动区 = 窗口工作区（行为链/物理/拖拽钳制共用），尺寸可配置
        self.resize(max(ZONE_MIN_W, zone_size[0]), max(ZONE_MIN_H, zone_size[1]))
        self._drag_moves_zone = drag_moves_zone

        self.library = MovieLibrary(
            parent=self,
            character_id=catalog.DEFAULT_CHARACTER,
            asset_dir=resolve_assets_root() / catalog.DEFAULT_CHARACTER / "videos",
            prewarm_enabled=True,
        )
        # sprite 初始位 = 活动区居中（默认帧 461x259 的估宽）
        self.sprite = PetSprite(
            self.library,
            pos=QPointF((self.width() - 461) // 2, (self.height() - 259) // 2),
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
        # 旧机同款 30s 动作间隔（治随机动作池"不停做动作"观感；间隔不延迟
        # 聊天动作，待播联动优先于 gap）
        self.behavior.animation_gap_seconds = 30.0
        self.physics = ThrowPhysicsController(self.behavior.bounds)
        self.sprite.set_bounds(QRect(0, 0, self.width(), self.height()))

        self.state = "idle"
        self._one_shot: str | None = None
        self._error_until = 0.0
        # 鼠标会话（旧 overlay 的 grab 协议）：按下=点击候选，过阈值才升级
        self._mouse_grab = False
        self._press_global: QPoint | None = None
        self._drag_committed = False
        self._piercing = False
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
            return
        # Missing animation is non-fatal; the state machine keeps running.
        print(f"[pet-pure] animation not found: {name}", file=sys.stderr)

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
        if action == "celebrate":
            self.play(CELEBRATE_ANIMATION, one_shot=True)

    def _on_say(self, text: str) -> None:
        geometry = self.geometry()
        x = geometry.x() + max(8, (geometry.width() - 380) // 2)
        y = geometry.y() - 90
        self.bubble.move(max(0, x), max(0, y))
        self.bubble.say(text)

    def _on_interaction(self, interaction_id: str, kind: str, payload: object) -> None:
        pending, request = payload
        if kind == "question":
            questions = request.get("questions") or []
            first = questions[0] if questions else {}
            text = first.get("question") or "请选择"
            options = [o.get("label") for o in first.get("options") or [] if o.get("label")]
            if options:
                text = f"{text}\n" + " / ".join(str(o) for o in options)
        else:
            text = request.get("displayReason") or request.get("reason") or "请求执行"
            if isinstance(text, dict):
                text = text.get("zh") or text.get("en") or "请求执行"
        self._active_interaction = (pending, options if kind == "question" else ["允许", "拒绝"])
        self._on_say(f"[{text}] — 按 1/2/… 或点击数字键选择")
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
        if self._one_shot is not None:
            clip = self.sprite._clip
            if clip is None or not getattr(clip, "isRunning", lambda: False)():
                self._one_shot = None
                if self.state != "idle":
                    self.play(STATE_ANIMATION[self.state])
        elif error_active:
            # Rebinding every tick would restart the clip from frame 0
            # (bind_clip stops the old clip unconditionally), freezing the
            # animation — only enter it when something else is playing.
            if self.sprite._clip_name != "被吓一跳":
                self.play("被吓一跳")
        elif self.state != "idle":
            # 状态动画循环持住：被意外打断（clip 停了/被换）就回位
            want = STATE_ANIMATION[self.state]
            clip = self.sprite._clip
            running = clip is not None and bool(getattr(clip, "isRunning", lambda: False)())
            if self.sprite._clip_name != want or not running:
                self.play(want)
        # 窗随宠移只在**拖拽会话内**且 drag_moves_zone 开启时启用：行为链的
        # 移动目标是窗口局部坐标，自主漫游期间挪窗会形成正反馈棘轮（试验版
        # 窗口被拖出屏幕）——拖拽的位置由鼠标驱动，没有那个反馈源。关掉
        # drag_moves_zone 即硬限制模式：宠被锁死在配置的活动区内。
        if self._drag_committed and self._drag_moves_zone:
            self._follow_drag_window()
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
    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        menu = QMenu(self)
        act_quit = menu.addAction("退出")
        chosen = menu.exec(event.globalPos())
        if chosen is act_quit:
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
        control_state.publish_snapshot({
            "state": self.state,
            "animation": self.sprite._clip_name,
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
            pending.answer = {"answer": {"answers": [{"id": "q1", "selected": [answer]}]}}
        else:
            payload = {"allowed-once": "allowed-once", "拒绝": "rejected", "允许": "allowed-once"}
            pending.answer = {"answer": payload.get(answer, answer)}
        pending.settled.set()

    # -- lifecycle ---------------------------------------------------------
    def closeEvent(self, event) -> None:
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
    faulthandler.enable()
    parser = argparse.ArgumentParser(description="dsh-pet-pure: pure-UI desktop pet")
    parser.add_argument("--port", type=int, default=0, help="control interface port (0 = random)")
    parser.add_argument("--assets", type=str, default=None, help="assets root dir")
    parser.add_argument("--state-file", type=str, default=None,
                        help="write the control manifest (port/pid) here; "
                             "default: $DSH_HOME/pet-http.json")
    args = parser.parse_args()

    if args.assets:
        os.environ["DSH_PET_ASSETS"] = args.assets
    print(f"[pet-pure] assets root: {resolve_assets_root()}")

    state_file = Path(args.state_file or os.path.join(
        os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
        "pet-http.json",
    ))
    # 单实例接管：同一清单上已有一只活的 python 宠就终止它（"一端口一宠物"
    # 同源铁律）——否则重启旧壳留双窗，冻结的那只抢走拖拽和视觉。
    try:
        old = json.loads(state_file.read_text(encoding="utf-8"))
        old_pid = int(old.get("pid") or 0)
        if (old.get("pet") == "python" and old_pid and old_pid != os.getpid()
                and _pid_alive(old_pid)):
            os.kill(old_pid, signal.SIGTERM)
            print(f"[pet-pure] took over from previous instance pid={old_pid}", file=sys.stderr)
    except (OSError, ValueError):
        pass  # 没有清单/清单损坏/进程已死：当首例启动

    # 壳层配置（活动区/锚点/拖拽是否连带挪区）：与清单同目录的 pet-pure.json
    config_path = state_file.with_name("pet-pure.json")
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
        drag_moves_zone=shell_config["drag_moves_zone"])

    control_state = ControlState()
    window._control_state = control_state
    control_state.operations = window.operations
    server = http_control.serve(control_state, port=args.port)
    window._control_server = server
    port = server.server_address[1]

    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "port": port,
            "pid": os.getpid(),
            "since": int(__import__("time").time() * 1000),
            "pet": "python",
        }) + "\n", encoding="utf-8")
        print(f"[pet-pure] control interface on 127.0.0.1:{port} (manifest: {state_file})")
    except OSError as error:
        print(f"[pet-pure] could not write the manifest: {error}", file=sys.stderr)

    window.show()
    signal.signal(signal.SIGINT, lambda *_a: app.quit())
    # A timer keeps the interpreter responsive to SIGINT under Qt.
    keeper = QTimer()
    keeper.setInterval(200)
    keeper.timeout.connect(lambda: None)
    keeper.start()

    try:
        return app.exec()
    finally:
        try:
            state_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
