# -*- coding: utf-8 -*-
"""全回路冒烟：真实壳层 + 真实 HTTP 接线（非 pytest 收集，手动运行）。

与单测的区别：单测用 FakeOps 验协议，这里起真的 PetWindow——Qt 信号
跨线程投递（HTTP 线程 -> GUI 线程）、真素材库、真 WebM 解码、GUI 线程
按键应答路径全部走真。默认 offscreen（无窗口、可无人值守）；想肉眼看
一圈就临时解掉环境变量运行。

Run:  .venv/Scripts/python.exe tests/smoke_shell.py
看见窗口跑:  QT_QPA_PLATFORM=windows .venv/Scripts/python.exe tests/smoke_shell.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as petmain  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from pet.http_control import ControlState, serve  # noqa: E402

app = QApplication([])
# 定时器/信号槽里的异常不会打断事件循环也不会让进程退出——冒烟必须显式收集
slot_errors: list[str] = []
_prev_hook = sys.excepthook
def _hook(t, v, tb):
    slot_errors.append(f"{t.__name__}: {v}")
    _prev_hook(t, v, tb)
sys.excepthook = _hook
window = petmain.PetWindow()
cs = ControlState()
window._control_state = cs
cs.operations = window.operations
server = serve(cs, port=0)
window._control_server = server
port = server.server_address[1]
window.show()
print(f"[smoke] server on 127.0.0.1:{port} (platform: {os.environ['QT_QPA_PLATFORM']})", flush=True)

results: dict = {}


def driver() -> None:
    """Worker thread: drives all endpoints; the long poll blocks until the
    GUI thread answers (exactly the driver-side view of the contract)."""
    conn = HTTPConnection("127.0.0.1", port, timeout=25)

    def get(path: str):
        conn.request("GET", path)
        response = conn.getresponse()
        return json.loads(response.read())

    def post(path: str, body: dict):
        raw = json.dumps(body).encode()
        conn.request("POST", path, body=raw, headers={
            "content-type": "application/json", "content-length": str(len(raw))})
        response = conn.getresponse()
        return json.loads(response.read())

    try:
        results["caps"] = get("/capabilities")
        time.sleep(0.3)  # let the GUI event loop publish its first snapshot
        results["status0"] = get("/status")
        results["state"] = post("/state", {"state": "thinking"})
        time.sleep(0.3)
        results["status1"] = get("/status")
        results["action"] = post("/action", {"action": "celebrate"})
        results["say"] = post("/say", {"text": "冒烟测试"})
        results["interaction"] = post("/interaction", {"kind": "approval", "reason": "允许冒烟?"})
        time.sleep(0.6)
        events = get("/events?since=0")
        results["events"] = [e["type"] for e in events["events"]]
        results["ok"] = True
    except Exception as error:
        results["error"] = repr(error)
    finally:
        conn.close()


thread = threading.Thread(target=driver, daemon=True)
thread.start()

phase = {"answered": False}


def touch_pet() -> None:
    window._handle_touch(QPoint(200, 200))


def finish() -> None:
    timer.stop()
    # 合成拖拽（不经鼠标）：on_press 设偏移 → begin_drag → on_move →
    # advance 积分应移动 sprite——grab 协议断了（如漏 on_press）在这里抓
    pos0 = QPointF(window.sprite.pos)
    window.sprite.on_press(QPointF(pos0.x() + 5, pos0.y() + 5))
    window.sprite.begin_drag()
    window.sprite.on_move(QPointF(pos0.x() + 60, pos0.y() + 40))
    for _ in range(15):
        window.sprite.advance(1 / 60)
    results["drag_moved"] = window.sprite.pos != pos0
    window.sprite.on_release(QPointF(window.sprite.pos))
    # 像素级验收：grab 强制走一遍 paintEvent → sprite.paint → _rebuild_pixmap，
    # 采样非透明像素——正是"协议全绿但窗口全透明"这类断链的唯一探测器。
    image = window.grab().toImage()
    opaque = sum(
        1 for y in range(0, image.height(), 8) for x in range(0, image.width(), 8)
        if image.pixelColor(x, y).alpha() > 0
    )
    results["opaque_samples"] = opaque
    server.shutdown()
    checks = {
        "caps.states": results.get("caps", {}).get("states") == ["idle", "thinking", "working", "error"],
        "status0.idle+anim": (results.get("status0", {}).get("state") == "idle"
                              and results.get("status0", {}).get("animation") is not None),
        "post.state ok": results.get("state") == {"ok": True},
        "status1.thinking+anim": (results.get("status1", {}).get("state") == "thinking"
                                  and results.get("status1", {}).get("animation") == "深度思考碎碎念"),
        "post.action ok": results.get("action") == {"ok": True},
        "post.say ok": results.get("say") == {"ok": True},
        "interaction.answer": results.get("interaction") == {"ok": True, "answer": "allowed-once"},
        "touch event": "touch" in results.get("events", []),
        "answered on GUI thread": phase["answered"],
        "drag moves sprite": results.get("drag_moved") is True,
        "sprite pixels composited": opaque > 50,
        "no slot exceptions": not slot_errors,
        "no driver error": "error" not in results,
    }
    failed = [key for key, ok in checks.items() if not ok]
    print(f"[smoke] results: {json.dumps(results, ensure_ascii=False, default=str)[:600]}", flush=True)
    print("[smoke] VERDICT:", "ALL PASS" if not failed else f"FAILED: {failed}", flush=True)
    window.close()
    app.quit()


def tick() -> None:
    if not phase["answered"] and window._active_interaction is not None:
        phase["answered"] = True
        window._answer_interaction("允许")
        QTimer.singleShot(100, touch_pet)
    if phase["answered"] and not thread.is_alive():
        finish()


timer = QTimer()
timer.setInterval(50)
timer.timeout.connect(tick)
timer.start()
QTimer.singleShot(30000, lambda: (print("[smoke] TIMEOUT", flush=True), finish()))
rc = app.exec()
print(f"[smoke] exit rc={rc}", flush=True)
sys.exit(0 if rc == 0 and results.get("ok") else 1)
