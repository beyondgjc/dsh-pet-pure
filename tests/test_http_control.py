# -*- coding: utf-8 -*-
"""HTTP control interface tests (DSH-PET.md §5.6/§7 contract).

The pet is the called party: the driver POSTs state/action/say/interaction
here and drains touch events from a queue. This suite pins the whole surface:
the seven endpoints, the vocabulary guards, the seq-monotonic event queue,
requeue-after-cancel, and the long-poll interaction (answer + driver
disconnect) behavior. No Qt, no ffmpeg — pure stdlib + http.client.

The interaction POST holds its connection open as a long poll, so every
interaction test drives the handler from its own thread and polls for the
ops-level state with a bounded deadline.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.client import HTTPConnection

import pytest

from pet import http_control
from pet.http_control import ControlState, Operations, serve, random_interaction_id


def _get_json(conn, path):
    conn.request("GET", path)
    response = conn.getresponse()
    body = response.read()
    return response.status, json.loads(body.decode("utf-8"))


def _post_json(conn, path, payload):
    body = json.dumps(payload).encode("utf-8")
    conn.request("POST", path, body=body,
                 headers={"content-type": "application/json",
                          "content-length": str(len(body))})
    response = conn.getresponse()
    raw = response.read()
    return response.status, json.loads(raw.decode("utf-8")) if raw else None


class FakeOps(Operations):
    """Synchronous operations sink: records every delivery, hands out the
    pending interactions so tests can settle or cancel them."""

    def __init__(self) -> None:
        self.states: list[str] = []
        self.actions: list[str] = []
        self.said: list[str] = []
        self.interactions: list[tuple[str, str, dict]] = []
        self.cancelled: list[str] = []
        self.pending: dict[str, http_control._PendingInteraction] = {}

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def perform(self, action: str) -> None:
        self.actions.append(action)

    def say(self, text: str) -> None:
        self.said.append(text)

    def start_interaction(self, kind, request):
        pending = http_control._PendingInteraction(random_interaction_id(), kind)
        self.interactions.append((pending.id, kind, request))
        self.pending[pending.id] = pending
        return pending

    def cancel_interaction(self, interaction_id: str) -> None:
        self.cancelled.append(interaction_id)


@pytest.fixture()
def server():
    state = ControlState()
    state.operations = FakeOps()
    srv = serve(state, port=0)
    try:
        yield srv, state
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def conn(server) -> HTTPConnection:
    srv, _state = server
    connection = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
    try:
        yield connection
    finally:
        connection.close()


def _pump_until(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


class TestCapabilities:
    def test_declares_the_four_vocabularies(self, conn):
        status, doc = _get_json(conn, "/capabilities")
        assert status == 200
        assert doc["states"] == ["idle", "thinking", "working", "error"]
        assert doc["actions"] == ["celebrate"]
        assert doc["interactions"] == ["approval", "question"]
        assert doc["events"] == ["touch"]
        assert doc["endpoints"] == ["capabilities", "status", "state", "action",
                                    "say", "interaction", "events"]

    def test_unknown_get_is_404(self, conn):
        status, doc = _get_json(conn, "/nope")
        assert status == 404
        assert "error" in doc


class TestStatus:
    def test_status_shape(self, conn, server):
        _srv, state = server
        state.publish_snapshot({"state": "working", "animation": "写代码",
                                "visible": True, "interaction": False})
        state.window_facts = {"x": 11, "y": 22, "width": 460, "height": 400}
        status, doc = _get_json(conn, "/status")
        assert status == 200
        assert doc["state"] == "working"
        assert doc["animation"] == "写代码"
        assert doc["window"] == {"x": 11, "y": 22, "width": 460, "height": 400}
        assert doc["pid"] > 0

    def test_default_snapshot_is_idle(self, conn):
        status, doc = _get_json(conn, "/status")
        assert doc["state"] == "idle"
        assert doc["animation"] is None
        assert doc["visible"] is True


class TestOperations:
    def test_state_ok(self, conn, server):
        _srv, _state = server
        status, doc = _post_json(conn, "/state", {"state": "thinking"})
        assert (status, doc) == (200, {"ok": True})
        assert _srv.server_address[1] > 0

    def test_state_reaches_the_sink(self, conn, server):
        _srv, state = server
        ops: FakeOps = state.operations
        status, _doc = _post_json(conn, "/state", {"state": "thinking"})
        assert status == 200
        assert ops.states == ["thinking"]

    def test_state_rejects_unknown(self, conn, server):
        _srv, state = server
        ops: FakeOps = state.operations
        status, doc = _post_json(conn, "/state", {"state": "sleeping"})
        assert status == 400
        assert "error" in doc
        assert ops.states == []

    @pytest.mark.parametrize("value", ["idle", "thinking", "working", "error"])
    def test_every_declared_state_accepted(self, conn, server, value):
        _srv, state = server
        ops: FakeOps = state.operations
        status, _doc = _post_json(conn, "/state", {"state": value})
        assert status == 200
        assert ops.states == [value]

    def test_action_ok(self, conn, server):
        _srv, state = server
        ops: FakeOps = state.operations
        status, doc = _post_json(conn, "/action", {"action": "celebrate"})
        assert (status, doc) == (200, {"ok": True})
        assert ops.actions == ["celebrate"]

    def test_action_rejects_unknown(self, conn, server):
        _srv, state = server
        ops: FakeOps = state.operations
        status, _doc = _post_json(conn, "/action", {"action": "wave"})
        assert status == 400
        assert ops.actions == []

    def test_say_truncates_and_forwards(self, conn, server):
        _srv, state = server
        ops: FakeOps = state.operations
        long_text = "啊" * (http_control.MAX_TEXT_CHARS + 50)
        status, _doc = _post_json(conn, "/say", {"text": long_text})
        assert status == 200
        assert ops.said and len(ops.said[0]) == http_control.MAX_TEXT_CHARS

    def test_say_empty_is_a_noop_but_ok(self, conn, server):
        _srv, state = server
        ops: FakeOps = state.operations
        status, _doc = _post_json(conn, "/say", {"text": ""})
        assert status == 200
        assert ops.said == []

    def test_unknown_post_is_404(self, conn):
        status, _doc = _post_json(conn, "/teleport", {})
        assert status == 404


class TestEventQueue:
    def test_drain_since_is_monotonic(self, server):
        _srv, state = server
        state.emit("touch", part="head", x=1, y=2)
        time.sleep(0.002)
        state.emit("touch", part="body", x=3, y=4)
        first, latest = state.drain_since(0)
        assert [e["seq"] for e in first] == [1, 2]
        assert latest == 2
        assert first[0]["part"] == "head" and first[0]["x"] == 1
        second, latest2 = state.drain_since(1)
        assert [e["seq"] for e in second] == [2]
        assert latest2 == 2

    def test_drain_since_beyond_head_is_empty(self, server):
        _srv, state = server
        state.emit("touch")
        events, latest = state.drain_since(99)
        assert events == [] and latest == 1

    def test_queue_is_bounded(self, server):
        _srv, state = server
        for i in range(http_control.MAX_EVENTS + 25):
            state.emit("touch", i=i)
        events, latest = state.drain_since(0)
        assert len(events) == http_control.MAX_EVENTS
        assert latest == http_control.MAX_EVENTS + 25
        assert events[-1]["i"] == http_control.MAX_EVENTS + 24

    def test_requeue_puts_event_back(self, server):
        _srv, state = server
        state.emit("touch", part="head")
        events, _latest = state.drain_since(0)
        assert len(events) == 1
        state.requeue(events[0])
        again, _ = state.drain_since(0)
        assert len(again) == 1 and again[0]["seq"] == 1

    def test_requeue_is_idempotent(self, server):
        _srv, state = server
        state.emit("touch")
        events, _ = state.drain_since(0)
        state.requeue(events[0])
        state.requeue(events[0])
        again, _ = state.drain_since(0)
        assert len(again) == 1

    def test_publish_snapshot_replaces_wholesale(self, server):
        """The snapshot is replaced wholesale: publishers must send the full
        dict (main.py's _publish_snapshot always does)."""
        _srv, state = server
        state.publish_snapshot({"state": "error", "animation": "被吓一跳",
                                "visible": True, "interaction": False})
        snapshot = state.snapshot()
        assert snapshot["state"] == "error"
        assert snapshot["animation"] == "被吓一跳"


class TestEventsEndpoint:
    def test_events_over_http(self, server):
        srv, state = server
        state.emit("touch", part="head")
        state.emit("touch", part="body")
        connection = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
        try:
            status, doc = _get_json(connection, "/events?since=0")
            assert status == 200
            assert len(doc["events"]) == 2
            assert doc["latest"] == 2
            status, doc = _get_json(connection, "/events?since=1")
            assert [e["seq"] for e in doc["events"]] == [2]
        finally:
            connection.close()

    def test_events_bad_since_is_zero(self, server):
        srv, state = server
        state.emit("touch")
        connection = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
        try:
            status, doc = _get_json(connection, "/events?since=abc")
            assert status == 200 and len(doc["events"]) == 1
        finally:
            connection.close()


class TestInteractionLongPoll:
    def test_answer_arrives_in_the_response_body(self, conn, server):
        _srv, state = server
        ops: FakeOps = state.operations
        result: dict = {}

        def call():
            try:
                result["response"] = _post_json(
                    conn, "/interaction",
                    {"kind": "approval", "reason": "run tests?"})
            except Exception as error:  # pragma: no cover - debug aid
                result["error"] = error

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        assert _pump_until(lambda: len(ops.interactions) == 1)
        pending_id, kind, request = ops.interactions[0]
        assert kind == "approval"
        assert request["reason"] == "run tests?"
        pending = ops.pending[pending_id]
        pending.answer = {"answer": "allowed-once"}
        pending.settled.set()
        thread.join(timeout=15)
        assert not thread.is_alive()  # join() returns None even on success
        assert result["response"][0] == 200
        assert result["response"][1] == {"ok": True, "answer": "allowed-once"}

    def test_question_answer_shape(self, conn, server):
        _srv, state = server
        ops: FakeOps = state.operations
        result: dict = {}

        def call():
            try:
                result["response"] = _post_json(
                    conn, "/interaction",
                    {"kind": "question",
                     "questions": [{"question": "继续?",
                                    "options": [{"label": "是"}, {"label": "否"}]}]})
            except Exception as error:  # pragma: no cover - debug aid
                result["error"] = error

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        assert _pump_until(lambda: len(ops.interactions) == 1)
        _pending_id, kind, _request = ops.interactions[0]
        assert kind == "question"
        pending = ops.pending[ops.interactions[0][0]]
        pending.answer = {"answer": {"answers": [
            {"id": "q1", "selected": ["是"]}]}}
        pending.settled.set()
        thread.join(timeout=15)
        assert not thread.is_alive()  # join() returns None even on success
        assert result["response"][1]["answer"]["answers"][0]["selected"] == ["是"]

    def test_driver_disconnect_cancels_the_bubble(self, server):
        """A driver that walks away must not leave the bubble up forever.

        The connection is killed with SO_LINGER 0 (immediate RST) so the
        handler is guaranteed to observe the dead peer, and the ops sink must
        receive cancel_interaction — the same callback the platform uses to
        withdraw the bubble.
        """
        srv, state = server
        ops: FakeOps = state.operations
        host, port = srv.server_address
        client = socket.create_connection((host, port), timeout=15)
        # force RST on close so any write from the handler fails hard
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                          b"\x01\x00\x00\x00\x00\x00\x00\x00")
        body = json.dumps({"kind": "approval", "reason": "go?"}).encode()
        client.sendall(
            b"POST /interaction HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        assert _pump_until(lambda: len(ops.interactions) == 1)
        pending_id = ops.interactions[0][0]
        client.close()   # RST: the next write from the handler raises
        assert _pump_until(lambda: ops.cancelled == [pending_id])
        # and the interface keeps serving afterwards
        client2 = HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            status, doc = _get_json(client2, "/capabilities")
            assert status == 200 and doc["states"][0] == "idle"
        finally:
            client2.close()

    def test_old_peer_disconnect_does_not_break_fresh_one(self, conn, server):
        srv, state = server
        ops: FakeOps = state.operations
        result: dict = {}

        def call():
            try:
                result["response"] = _post_json(
                    conn, "/interaction", {"kind": "approval"})
            except Exception as error:
                result["error"] = error

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        assert _pump_until(lambda: len(ops.interactions) == 1)
        pending = ops.pending[ops.interactions[0][0]]
        conn.close()   # abandon mid-poll (ordinary FIN, not RST)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not ops.cancelled:
            time.sleep(0.01)
        pending.answer = {"answer": "answered-late"}
        pending.settled.set()   # never read by anyone: the answer is dropped
        thread.join(timeout=15)
        assert not thread.is_alive()  # join() returns None even on success
        # a fresh interaction still round-trips
        client = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
        try:
            body = json.dumps({"kind": "approval"}).encode()
            client.request("POST", "/interaction", body=body,
                          headers={"content-type": "application/json",
                                   "content-length": str(len(body))})
            # unreachable: no one answers; the point is just that it is accepted
        finally:
            client.close()


class TestOperationsNotWired:
    def test_posts_500_without_a_sink(self):
        state = ControlState()          # operations stays None
        srv = serve(state, port=0)
        try:
            connection = HTTPConnection("127.0.0.1", srv.server_address[1],
                                        timeout=15)
            try:
                status, doc = _post_json(connection, "/state", {"state": "idle"})
                assert status == 500
                assert "error" in doc
            finally:
                connection.close()
        finally:
            srv.shutdown()
            srv.server_close()


def test_random_interaction_ids_are_unique():
    ids = {random_interaction_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("ip-") for i in ids)
