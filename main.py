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

from PySide6.QtCore import QObject, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pet import catalog, http_control  # noqa: E402
from pet.http_control import ControlState, Operations, random_interaction_id  # noqa: E402
from pet.library import MovieLibrary  # noqa: E402
from pet.pet_sprite import PetSprite  # noqa: E402

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

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(460, 400)

        self.library = MovieLibrary(
            parent=self,
            character_id=catalog.DEFAULT_CHARACTER,
            asset_dir=resolve_assets_root() / catalog.DEFAULT_CHARACTER / "videos",
            prewarm_enabled=True,
        )
        self.sprite = PetSprite(self.library, pos=QPointF(120, 40), parent=self)
        self.bubble = SpeechBubble(self)

        self.state = "idle"
        self._one_shot: str | None = None
        self._error_until = 0.0
        self._mouse_down_at: tuple[float, float, float] | None = None
        self._active_interaction: tuple[http_control._PendingInteraction, list[str]] | None = None

        self.operations = PetOperations(self)
        self._wire_operations()
        # Standalone/manual runs see the pet before the driver's first
        # /state: start on the idle animation instead of an empty sprite.
        self.play(STATE_ANIMATION[self.state])

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.left() + 120, screen.top() + 120)

        tick = QTimer(self)
        tick.setInterval(16)
        tick.timeout.connect(self._tick)
        tick.start()

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
        if self.sprite.bind_clip(name):
            return
        # Missing animation is non-fatal; the state machine keeps running.
        print(f"[pet-pure] animation not found: {name}", file=sys.stderr)

    def _on_state(self, state: str) -> None:
        finished_work = self.state == "working" and state == "idle"
        self.state = state
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
    @staticmethod
    def _monotonic() -> float:
        import time
        return time.monotonic()

    def _tick(self) -> None:
        self.sprite.advance(1 / 60)
        if self._one_shot is not None:
            clip = self.sprite._clip
            if clip is None or not getattr(clip, "isRunning", lambda: False)():
                self._one_shot = None
                self.play(STATE_ANIMATION[self.state])
        elif self._error_until > self._monotonic():
            # Rebinding every tick would restart the clip from frame 0
            # (bind_clip stops the old clip unconditionally), freezing the
            # animation — only enter it when something else is playing.
            if self.sprite._clip_name != "被吓一跳":
                self.play("被吓一跳")
        self._publish_snapshot()

    def _publish_snapshot(self) -> None:
        clip_name = self.sprite._clip_name
        self._control_state.publish_snapshot({
            "state": self.state,
            "animation": clip_name,
            "visible": self.isVisible(),
            "interaction": self._active_interaction is not None,
        })

    # -- input -------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._mouse_down_at = (event.globalPosition().x(), event.globalPosition().y(), self._monotonic())
            self.sprite.on_move(event.scenePosition().toPoint() if hasattr(event, "scenePosition") else event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._mouse_down_at is not None:
            self.sprite.on_move(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._mouse_down_at is not None:
            down_x, down_y, down_at = self._mouse_down_at
            self._mouse_down_at = None
            distance = (event.globalPosition().x() - down_x) ** 2 + (event.globalPosition().y() - down_y) ** 2
            if distance <= 25 and self._monotonic() - down_at <= 0.5:
                self._handle_touch(event.position().toPoint())
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

    app = QApplication(sys.argv)
    app.setApplicationName("dsh-pet-pure")

    window = PetWindow()

    control_state = ControlState()
    window._control_state = control_state
    control_state.operations = window.operations
    server = http_control.serve(control_state, port=args.port)
    window._control_server = server
    port = server.server_address[1]

    state_file = Path(args.state_file or os.path.join(
        os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
        "pet-http.json",
    ))
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
