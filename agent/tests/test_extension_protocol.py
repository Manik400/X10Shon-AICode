from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.browser.extension_driver import ExtensionDriver
from app.browser.extension_hub import ExtensionHub
from app.core.actions import BrowserAction, Target
from app.core.errors import BrowserDisconnectedError


class FakeSocket:
    """Behaves like a connected extension: answers requests the way the service worker does."""

    def __init__(self, hub: ExtensionHub, *, answer: bool = True):
        self.hub = hub
        self.sent: list[dict[str, Any]] = []
        self.answer = answer
        self.closed = False

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)
        if not self.answer:
            return
        t = msg.get("type")
        reply: dict[str, Any] | None = None
        if t == "list_tabs":
            reply = {"type": "tabs", "id": msg["id"], "tabs": [{"tab_id": 7, "url": "https://example.com/x", "title": "X", "active": True, "permitted": True}]}
        elif t == "snapshot":
            reply = {"type": "snapshot_result", "id": msg["id"], "ok": True, "snapshot": {"url": "https://example.com/x", "title": "X", "nodes": [{"i": 0, "role": "button", "name": "Apply", "tag": "button"}]}}
        elif t == "action":
            reply = {"type": "action_result", "id": msg["id"], "result": {"ok": True, "action_type": msg["action"]["type"], "value": "typed", "duration_ms": 3}}
        elif t == "navigate":
            reply = {"type": "navigate_result", "id": msg["id"], "result": {"ok": True, "action_type": "navigate", "url": msg["url"]}}
        if reply:
            asyncio.get_running_loop().call_soon(lambda r=reply: asyncio.ensure_future(self.hub.handle_incoming(json.loads(json.dumps(r)))))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_request_response_roundtrip() -> None:
    hub = ExtensionHub()
    ws = FakeSocket(hub)
    await hub.attach(ws, {"extension_id": "abc"})  # type: ignore[arg-type]
    d = ExtensionDriver(hub, request_timeout=2)
    tabs = await d.list_tabs()
    assert tabs[0].tab_id == "7" and tabs[0].active
    snap = await d.snapshot("7", max_nodes=10)
    assert snap.nodes[0].name == "Apply"
    res = await d.execute("7", BrowserAction(type="type", target=Target(strategy="node", value="0"), args={"text": "hi"}))
    assert res.ok and res.value == "typed"
    nav = await d.execute("7", BrowserAction(type="navigate", args={"url": "https://example.com/y"}))
    assert nav.ok and nav.url == "https://example.com/y"
    assert ws.sent[-1]["type"] == "navigate"


@pytest.mark.asyncio
async def test_timeout_and_disconnect_raise_browser_disconnected() -> None:
    hub = ExtensionHub()
    ws = FakeSocket(hub, answer=False)
    await hub.attach(ws, {})  # type: ignore[arg-type]
    d = ExtensionDriver(hub, request_timeout=0.2)
    with pytest.raises(BrowserDisconnectedError):
        await d.list_tabs()
    pending = asyncio.ensure_future(d.list_tabs())
    await asyncio.sleep(0.01)
    await hub.detach(ws)  # type: ignore[arg-type]
    with pytest.raises(BrowserDisconnectedError):
        await pending
    assert not hub.connected
    with pytest.raises(BrowserDisconnectedError):
        await d.snapshot("7", max_nodes=5)


@pytest.mark.asyncio
async def test_new_connection_replaces_old() -> None:
    hub = ExtensionHub()
    a, b = FakeSocket(hub), FakeSocket(hub)
    await hub.attach(a, {})  # type: ignore[arg-type]
    await hub.attach(b, {})  # type: ignore[arg-type]
    assert a.closed and hub.connected
    await hub.detach(a)  # type: ignore[arg-type]  # stale detach must not drop the live socket
    assert hub.connected
