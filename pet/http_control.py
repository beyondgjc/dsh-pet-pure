"""Local HTTP control interface for the pure pet (driver's operations).

The pet is the CALLED party: the driver in the dsh runtime posts operations
here and consumes touch/chat events from a queue, exactly like the Electron
carrier's interface (DSH-PET.md §5.6/§7). Same eight endpoints, same shapes —
that is what makes pets interchangeable.

Endpoints (§6 contract v4):
  GET  /capabilities   {contractVersion, states, actions, interactions}
  GET  /status         the mirror: state, animation, location, world, visible
  GET  /events?since=&hold=  event queue (touch + chat), v4 long poll
  POST /state          set semantic state {state: idle|thinking|working|error}
  POST /action         one-shot animation {action: celebrate}; responds with
                       a requestId that matches /status.lastAction.id
  POST /say            fire-and-forget speech bubble {text}
  POST /interaction    approval/question; the response IS the user's choice
                       (empty body + connection close = canceled)
  POST /world          set the roaming area {x,y,width,height} or {reset:true}

Design notes (WSGI-free on purpose):
  - BaseHTTPRequestHandler gives zero-dependency HTTP on the stdlib.
  - The queue is bounded and seq-monotonic; a restart re-winds `latest`,
    which the driver detects and re-pulls from 0.
  - The interaction POST holds its connection open as a long poll; an
    aborted driver fetch closes the connection, which cancels the bubble.
"""

from __future__ import annotations

import json
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable

STATE_VOCABULARY = ("idle", "thinking", "working", "error")
ACTION_VOCABULARY = ("celebrate",)
INTERACTION_VOCABULARY = ("approval", "question")

MAX_TEXT_CHARS = 200
MAX_CHAT_CHARS = 2000
MAX_EVENTS = 200
# 长轮询心跳间隔：防 Node undici 300s bodyTimeout（TS 载体 60s 同款）
INTERACTION_HEARTBEAT_SECONDS = 60.0
# /events hold 服务端钳制（契约值 ≤30s）
MAX_EVENT_HOLD_SECONDS = 30.0


class ControlState:
    """Shared state between the Qt shell (producer) and HTTP handlers (consumers)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._changed = threading.Condition(self.lock)
        self._seq = 0
        self._events: list[dict] = []
        # chat 模态（§6 2026-10-31 修订）：每宠至多一条在途——该事件被
        # /events 响应实际携出前不收新输入；携出即发送成功。
        self._pending_chat_seq: int | None = None
        self._snapshot: dict = {"state": "idle", "animation": None, "visible": True}
        # Delivered from the HTTP thread to the GUI thread via Qt signals;
        # the shell wires `handler.operations` into a QObject's slots.
        self.operations: "Operations | None" = None
        # 当前活动区（pure 的窗=区：窗口矩形即 world）。GUI 线程每 tick 直写。
        self.world_facts: dict = {"x": 0, "y": 0, "width": 900, "height": 560}

    # -- mirror ----------------------------------------------------------
    def publish_snapshot(self, snapshot: dict) -> None:
        with self.lock:
            self._snapshot = dict(snapshot)

    def snapshot(self) -> dict:
        with self.lock:
            body = dict(self._snapshot)
            body["pid"] = _pid()
            # §6 v3 恒定输出：无记录 = null（不是缺席——条件缺席是消费方
            # `=== null` 判定的陷阱）。
            body.setdefault("lastAction", None)
            # v2 三态：本实现恒支持活动区（窗=区），world 恒为当前窗矩形；
            # location 由 shell 合成（窗位置 + sprite 窗内偏移），缺席时
            # 退回窗矩形近似（防御：合成链路异常不炸 /status）。
            if not isinstance(body.get("location"), dict):
                body["location"] = dict(self.world_facts)
            body["world"] = dict(self.world_facts)
            return body

    # -- event queue ------------------------------------------------------
    def emit(self, event_type: str, **fields) -> None:
        with self._changed:
            self._seq += 1
            self._events.append({"seq": self._seq, "type": event_type, "at": int(time.time() * 1000), **fields})
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]
            self._changed.notify_all()

    def emit_chat(self, text: str) -> tuple[bool, int]:
        """chat 上行（§6 2026-10-31 修订）：每宠至多一条在途，上一条被
        /events 携出前拒绝新输入（输入卡模态）。返回 (accepted, seq)。"""
        with self._changed:
            if self._pending_chat_seq is not None:
                return (False, self._pending_chat_seq)
            self._seq += 1
            seq = self._seq
            self._events.append({
                "seq": seq, "type": "chat", "text": text[:MAX_CHAT_CHARS],
                "at": int(time.time() * 1000),
            })
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]
            self._pending_chat_seq = seq
            self._changed.notify_all()
            return (True, seq)

    def drain_since(self, since: int) -> tuple[list[dict], int]:
        with self._changed:
            events = [e for e in self._events if e["seq"] > since]
            if self._pending_chat_seq is not None and any(e["seq"] == self._pending_chat_seq for e in events):
                self._pending_chat_seq = None  # 携出即发送成功
            return (events, self._seq)

    def wait_for_events(self, since: int, timeout: float) -> None:
        """Park a holding /events reader until backlog appears (or the pet
        restarts: latest < since) or the budget lapses. Callers re-drain."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._changed:
            while True:
                events = [e for e in self._events if e["seq"] > since]
                if events or self._seq < since:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._changed.wait(remaining)

    def requeue(self, event: dict) -> None:
        """Put a consumed event back (a canceled driver must not swallow it)."""
        with self._changed:
            if event not in self._events:
                self._events.insert(0, event)


class Operations:
    """GUI-thread sink interface; the shell implements it."""

    def set_state(self, state: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def perform(self, action: str, request_id: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def say(self, text: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def set_world(self, rect: dict | None, done: threading.Event, applied: dict) -> None:  # pragma: no cover - interface
        """Apply the roaming area (None = reset to defaults). Sets `done`
        once applied; `applied` carries {"error": str | None, "moved": bool}
        — the shell owns the screen-intersection verdict (422 source)."""
        raise NotImplementedError

    def start_interaction(self, kind: str, request: dict) -> tuple[str, threading.Event, dict]:
        """Present one interaction; returns (id, settled, answer-holder)."""
        raise NotImplementedError

    def cancel_interaction(self, interaction_id: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class _PendingInteraction:
    def __init__(self, interaction_id: str, kind: str) -> None:
        self.id = interaction_id
        self.kind = kind
        self.settled = threading.Event()
        self.answer: dict = {"answer": None}


def _pid() -> int:
    import os
    return os.getpid()


def _rect_of(value: object) -> dict | None:
    """A {x,y,width,height} rect from a loose payload; None unless all four
    members are finite real numbers (bool excluded) — the /world gate."""
    if not isinstance(value, dict):
        return None
    numbers: dict = {}
    for key in ("x", "y", "width", "height"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if item != item or item in (float("inf"), float("-inf")):  # NaN / inf
            return None
        numbers[key] = item
    return numbers


def make_handler(state: ControlState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server: "ThreadingHTTPServer"

        def log_message(self, *_args) -> None:  # keep the console quiet
            return

        # -- helpers -------------------------------------------------------
        def _json(self, status: int, body: object) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                parsed = json.loads(raw.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}

        def _ops(self) -> Operations:
            ops = state.operations
            if ops is None:
                raise RuntimeError("pet control: the GUI operations sink is not wired")
            return ops

        # -- routes --------------------------------------------------------
        def do_GET(self) -> None:
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(self.path)
            if parsed.path == "/capabilities":
                # v4：endpoints/events 词表退役——端点存在性由 contractVersion
                # 承接，事件类型容忍由事件体 type 判别字段承接（§6 修订记录）。
                self._json(200, {
                    "contractVersion": 4,
                    "states": list(STATE_VOCABULARY),
                    "actions": list(ACTION_VOCABULARY),
                    "interactions": list(INTERACTION_VOCABULARY),
                })
                return
            if parsed.path == "/status":
                self._json(200, state.snapshot())
                return
            if parsed.path == "/events":
                query = parse_qs(parsed.query)
                try:
                    since = int(query.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                try:
                    raw_hold = float(query.get("hold", ["0"])[0])
                except ValueError:
                    raw_hold = 0.0
                hold = min(MAX_EVENT_HOLD_SECONDS, max(0.0, raw_hold))
                events, latest = state.drain_since(since)
                # 回卷（重启）与已有积压恒立即返回，挂或不挂都一样。
                if hold <= 0 or latest < since or events:
                    self._json(200, {"events": events, "latest": latest})
                    return
                state.wait_for_events(since, hold)
                events, latest = state.drain_since(since)
                self._json(200, {"events": events, "latest": latest})
                return
            self._json(404, {"error": "unknown endpoint"})

        def do_POST(self) -> None:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            body = self._body()
            try:
                if path == "/state":
                    want = str(body.get("state", ""))
                    if want not in STATE_VOCABULARY:
                        self._json(400, {"error": f"state must be one of {list(STATE_VOCABULARY)}"})
                        return
                    self._ops().set_state(want)
                    self._json(200, {"ok": True})
                    return
                if path == "/action":
                    want = str(body.get("action", ""))
                    if want not in ACTION_VOCABULARY:
                        self._json(400, {"error": f"action must be one of {list(ACTION_VOCABULARY)}"})
                        return
                    # v3 wait 关联键：requestId 与 /status.lastAction.id 同值，
                    # driver 据此轮询完成回填（durationMs/endedAt）。
                    request_id = f"a{int(time.time() * 1000):x}{random_interaction_id()[3:9]}"
                    self._ops().perform(want, request_id)
                    self._json(200, {"ok": True, "action": want, "requestId": request_id})
                    return
                if path == "/say":
                    text = str(body.get("text", ""))[:MAX_TEXT_CHARS]
                    if text:
                        self._ops().say(text)
                    self._json(200, {"ok": True})
                    return
                if path == "/world":
                    self._handle_world(body)
                    return
                if path == "/interaction":
                    self._long_poll_interaction(body)
                    return
            except Exception as error:  # never kill the server thread on one bad call
                self._json(500, {"error": str(error)})
                return
            self._json(404, {"error": "unknown endpoint"})

        # -- long poll -----------------------------------------------------
        def _handle_world(self, body: dict) -> None:
            """§6 v2：设置活动区，立即生效（宠在区外即被钳入）。{"reset":true}
            回落默认。200 返回**应用后的生效值**（done Event 等应用完成再取
            world_facts——非请求回声）；形状非法 422（与显示器的交集判定由
            shell 槽做——屏幕信息在 GUI 线程）。"""
            rect = None if body.get("reset") is True else _rect_of(body)
            if rect is None and body.get("reset") is not True:
                self._json(422, {"ok": False, "error": 'invalid world: expected {x,y,width,height} or {"reset":true}'})
                return
            applied: dict = {"error": None, "moved": False}
            done = threading.Event()
            self._ops().set_world(rect, done, applied)
            if not done.wait(2.0):
                self._json(504, {"ok": False, "error": "world apply timed out"})
                return
            if applied.get("error") is not None:
                self._json(422, {"ok": False, "error": applied["error"]})
                return
            snapshot = state.snapshot()
            self._json(200, {
                "ok": True,
                "world": snapshot.get("world"),
                "location": snapshot.get("location"),
                "moved": bool(applied.get("moved", False)),
            })

        def _long_poll_interaction(self, body: dict) -> None:
            kind = "question" if str(body.get("kind", "approval")) == "question" else "approval"
            try:
                pending = self._ops().start_interaction(kind, body)
            except Exception as error:
                self._json(500, {"error": f"cannot start interaction: {str(error)}"})
                return
            # Streaming holder: the headers go out immediately so the driver's
            # fetch resolves; the body IS the user's answer, delimited by the
            # connection close (no content-length is legal together with
            # Connection: close).
            #
            # Disconnect detection rides select() on the socket: the driver's
            # abort closes the socket (FIN or RST), which turns it readable
            # with EOF/an error — detected within the 50ms loop tick. The old
            # write-probe (keepalive byte failing) only noticed on its 1s
            # cadence, and that delay WAS the losing pet's bubble retraction
            # lag in the multi-pet race (TS retracts instantly).
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("connection", "close")
            self.end_headers()
            try:
                # 每 60s 往响应体写一个空格（TS 载体 main.ts:651 同款）：喂
                # Node undici 的 300s bodyTimeout——提问挂 5 分钟以上（用户
                # 走开很常见）没有它，driver 那条臂超时阵亡，用户回来再点，
                # 答案写进死 socket 静默丢失。断连探测与此无关（下面的
                # select 在 abort/FIN/RST 的 50ms 内触发）。
                last_beat = time.monotonic()
                while not pending.settled.wait(0.05):
                    ready, _, _ = select.select([self.connection], [], [], 0)
                    if ready and not self.rfile.read(1):
                        raise ConnectionError("driver closed the long poll")
                    if time.monotonic() - last_beat >= INTERACTION_HEARTBEAT_SECONDS:
                        self.wfile.write(b" ")
                        last_beat = time.monotonic()
            except OSError:
                self._ops().cancel_interaction(pending.id)
                return
            if not pending.settled.is_set():
                # Driver disconnected: withdraw the bubble and stop.
                self._ops().cancel_interaction(pending.id)
                return
            payload = json.dumps({"ok": True, "answer": pending.answer.get("answer")}).encode("utf-8")
            try:
                self.wfile.write(payload)
            except OSError:
                pass  # the answer is recorded even if the peer is gone

    return Handler


def serve(state: ControlState, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, name="pet-control", daemon=True)
    thread.start()
    return server


def random_interaction_id() -> str:
    import uuid
    return f"ip-{uuid.uuid4().hex[:12]}"
