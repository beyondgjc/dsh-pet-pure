"""Local HTTP control interface for the pure pet (driver's operations).

The pet is the CALLED party: the driver in the dsh runtime posts operations
here and consumes touch events from a queue, exactly like the Electron
carrier's interface (DSH-PET.md §5.6/§7). Same six endpoints, same shapes —
that is what makes pets interchangeable.

Endpoints:
  GET  /capabilities   states / actions / interactions / events the pet declares
  GET  /status         the mirror: current state, animation, window, visible
  GET  /events?since=  touch queue for the driver's poll (seq-ordered)
  POST /state          set semantic state {state: idle|thinking|working|error}
  POST /action         one-shot animation {action: celebrate}
  POST /say            fire-and-forget speech bubble {text}
  POST /interaction    approval/question; the response IS the user's choice
                       (empty body + connection close = canceled)

Design notes (WSGI-free on purpose):
  - BaseHTTPRequestHandler gives zero-dependency HTTP on the stdlib.
  - The queue is bounded and seq-monotonic; a restart re-winds `latest`,
    which the driver detects and re-pulls from 0.
  - The interaction POST holds its connection open as a long poll; an
    aborted driver fetch closes the connection, which cancels the bubble.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable

STATE_VOCABULARY = ("idle", "thinking", "working", "error")
ACTION_VOCABULARY = ("celebrate",)
INTERACTION_VOCABULARY = ("approval", "question")
EVENT_VOCABULARY = ("touch",)

MAX_TEXT_CHARS = 200
MAX_EVENTS = 200


class ControlState:
    """Shared state between the Qt shell (producer) and HTTP handlers (consumers)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._seq = 0
        self._events: list[dict] = []
        self._snapshot: dict = {"state": "idle", "animation": None, "visible": True}
        # Delivered from the HTTP thread to the GUI thread via Qt signals;
        # the shell wires `handler.operations` into a QObject's slots.
        self.operations: "Operations | None" = None
        self.window_facts: dict = {"x": 0, "y": 0, "width": 460, "height": 400}

    # -- mirror ----------------------------------------------------------
    def publish_snapshot(self, snapshot: dict) -> None:
        with self.lock:
            self._snapshot = dict(snapshot)

    def snapshot(self) -> dict:
        with self.lock:
            facts = dict(self.window_facts)
            return {**self._snapshot, "window": facts, "pid": _pid()}

    # -- event queue ------------------------------------------------------
    def emit(self, event_type: str, **fields) -> None:
        with self.lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "type": event_type, "at": int(time.time() * 1000), **fields})
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]

    def drain_since(self, since: int) -> tuple[list[dict], int]:
        with self.lock:
            return ([e for e in self._events if e["seq"] > since], self._seq)

    def requeue(self, event: dict) -> None:
        """Put a consumed event back (a canceled driver must not swallow it)."""
        with self.lock:
            if event not in self._events:
                self._events.insert(0, event)


class Operations:
    """GUI-thread sink interface; the shell implements it."""

    def set_state(self, state: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def perform(self, action: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def say(self, text: str) -> None:  # pragma: no cover - interface
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
            from urllib.parse import urlparse
            parsed = urlparse(self.path)
            if parsed.path == "/capabilities":
                self._json(200, {
                    "states": list(STATE_VOCABULARY),
                    "actions": list(ACTION_VOCABULARY),
                    "interactions": list(INTERACTION_VOCABULARY),
                    "events": list(EVENT_VOCABULARY),
                })
                return
            if parsed.path == "/status":
                self._json(200, state.snapshot())
                return
            if parsed.path == "/events":
                from urllib.parse import parse_qs
                try:
                    since = int(parse_qs(parsed.query).get("since", ["0"])[0])
                except ValueError:
                    since = 0
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
                    self._ops().perform(want)
                    self._json(200, {"ok": True})
                    return
                if path == "/say":
                    text = str(body.get("text", ""))[:MAX_TEXT_CHARS]
                    if text:
                        self._ops().say(text)
                    self._json(200, {"ok": True})
                    return
                if path == "/interaction":
                    self._long_poll_interaction(body)
                    return
            except Exception as error:  # never kill the server thread on one bad call
                self._json(500, {"error": str(error)})
                return
            self._json(404, {"error": "unknown endpoint"})

        # -- long poll -----------------------------------------------------
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
            # Connection: close). The periodic space is body padding and the
            # dead-peer probe in one: a vanished driver makes the write raise,
            # which withdraws the bubble.
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("connection", "close")
            self.end_headers()
            try:
                while not pending.settled.wait(1.0):
                    self.wfile.write(b" ")  # keepalive; a dead peer raises here
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
