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

    async def capabilities(self) -> set[str]:
        """Features of the running service worker. Old (cached) workers do not answer: empty set.
        Cached per connection."""
        cached = self._hub._info.get("_features")
        if cached is not None:
            return set(cached)
        try:
            resp = await self._hub.request("capabilities", {}, timeout=3.0)
            features = list(resp.get("features") or [])
        except BrowserDisconnectedError:
            features = []
        self._hub._info["_features"] = features
        return set(features)

    async def open_tab(self, url: str, *, active: bool = True, opener_tab_id: str | None = None, home_url: str | None = None) -> str:
        payload: dict[str, Any] = {"url": url, "active": active}
        if opener_tab_id and opener_tab_id.isdigit():
            payload["opener_tab_id"] = int(opener_tab_id)
        if home_url:
            payload["home_url"] = home_url  # the extension opens next to the job list's window when nothing else is known
        # The service worker waits up to 30 s for the page load before answering; give it margin.
        # Chrome briefly refuses tab changes while the user drags a tab ("Tabs cannot be edited
        # right now"): wait and retry instead of failing the job.
        import asyncio as _asyncio

        for attempt in range(20):
            try:
                resp = await self._hub.request("open_tab", payload, timeout=max(self._timeout, 45.0))
            except BrowserDisconnectedError as exc:
                resp = {"ok": False, "error": str(exc)}
            if resp.get("ok"):
                return str(resp["tab_id"])
            err = str(resp.get("error", "failed to open tab"))
            if "cannot be edited right now" not in err.lower() and "dragging" not in err.lower():
                raise BrowserDisconnectedError(err)
            await _asyncio.sleep(min(1.0 + attempt * 0.5, 5.0))
        raise BrowserDisconnectedError("Chrome kept refusing to open a tab (a tab is being dragged?)")

    async def protect_tab(self, tab_id: str) -> None:
        """Keep a tab that waits for the user out of Chrome's Memory Saver (no discarding)."""
        if not str(tab_id).isdigit():
            return
        try:
            await self._hub.request("protect_tab", {"tab_id": int(tab_id)}, timeout=5.0)
        except Exception:  # noqa: BLE001 - older extension builds do not know the message
            return

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

    async def notify(self, title: str, message: str, *, tab_id: str | None = None, key: str | None = None) -> None:
        """Chrome desktop notification; clicking it focuses ``tab_id``. Fire-and-forget."""
        payload: dict[str, Any] = {"title": title, "message": message}
        if tab_id and str(tab_id).isdigit():
            payload["tab_id"] = int(tab_id)
        if key:
            payload["key"] = key
        try:
            await self._hub.push("notify", payload)
        except Exception:  # noqa: BLE001 - an alert must never break a task
            pass

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
