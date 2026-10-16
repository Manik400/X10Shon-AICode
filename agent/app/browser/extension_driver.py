"""ExtensionDriver: executes actions in the user's own Chrome through the paired extension."""

from __future__ import annotations

from typing import Any

from ..core.actions import ActionResult, BrowserAction
from ..core.errors import BrowserDisconnectedError
from .driver import BrowserDriver, TabInfo
from .extension_hub import ExtensionHub
from .snapshot import PageSnapshot


class ExtensionDriver(BrowserDriver):
    name = "extension"

    def __init__(self, hub: ExtensionHub, *, request_timeout: float = 30.0):
        self._hub = hub
        self._timeout = request_timeout

    async def is_available(self) -> bool:
        return self._hub.connected

    async def list_tabs(self) -> list[TabInfo]:
        resp = await self._hub.request("list_tabs", {}, timeout=self._timeout)
        return [TabInfo(tab_id=str(t["tab_id"]), url=t.get("url", ""), title=t.get("title", ""), active=bool(t.get("active"))) for t in resp.get("tabs", [])]

    async def open_tab(self, url: str, *, active: bool = True) -> str:
        # The service worker waits up to 30 s for the page load before answering; give it margin.
        resp = await self._hub.request("open_tab", {"url": url, "active": active}, timeout=max(self._timeout, 45.0))
        if not resp.get("ok"):
            raise BrowserDisconnectedError(resp.get("error", "failed to open tab"))
        return str(resp["tab_id"])

    async def activate_tab(self, tab_id: str) -> None:
        version = str(self._hub.status()["extension"].get("version") or "0")
        if tuple(int(p) for p in version.split(".") if p.isdigit()) < (0, 2, 0):
            return  # builds before 0.2.0 do not understand activate_tab; skip instead of timing out
        try:
            await self._hub.request("activate_tab", {"tab_id": int(tab_id)}, timeout=5.0)
        except BrowserDisconnectedError:
            pass  # older extension builds do not support it; cosmetic only

    async def close_tab(self, tab_id: str) -> None:
        await self._hub.request("close_tab", {"tab_id": int(tab_id)}, timeout=self._timeout)

    async def navigate(self, tab_id: str, url: str, timeout_ms: int) -> ActionResult:
        resp = await self._hub.request("navigate", {"tab_id": int(tab_id), "url": url, "timeout_ms": timeout_ms}, timeout=timeout_ms / 1000 + 15)
        return ActionResult.model_validate(resp.get("result", {"ok": False, "action_type": "navigate", "message": "no result"}))

    async def snapshot(self, tab_id: str, *, max_nodes: int) -> PageSnapshot:
        resp = await self._hub.request("snapshot", {"tab_id": int(tab_id), "max_nodes": max_nodes}, timeout=self._timeout)
        if not resp.get("ok", True) and resp.get("error"):
            raise BrowserDisconnectedError(str(resp["error"]))
        return PageSnapshot.model_validate(resp["snapshot"])

    async def execute(self, tab_id: str, action: BrowserAction, *, payload: dict[str, Any] | None = None) -> ActionResult:
        if action.type == "navigate":
            return await self.navigate(tab_id, action.args["url"], action.timeout_ms)
        msg: dict[str, Any] = {"tab_id": int(tab_id), "action": action.model_dump(mode="json", exclude={"reason"})}
        if payload:
            msg["payload"] = payload
        resp = await self._hub.request("action", msg, timeout=action.timeout_ms / 1000 + 5)
        return ActionResult.model_validate(resp.get("result", {"ok": False, "action_type": action.type, "message": "no result"}))
