"""ExtensionHub: server side of the extension WebSocket.

One paired extension connects at a time (a reconnect replaces the previous socket). Requests are
correlated by id with futures and bounded by timeouts. The hub also pushes status updates to the
extension for its side panel.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket

from ..core.errors import BrowserDisconnectedError
from ..core.util import new_id, now_iso

log = logging.getLogger("lfa.extension")

MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class ExtensionHub:
    def __init__(self) -> None:
        self._ws: WebSocket | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._info: dict[str, Any] = {}
        self._connected_at: str | None = None
        self._send_lock = asyncio.Lock()
        self._on_event: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        self.active_tab: dict[str, Any] | None = None
        self.created_tabs: list[dict[str, Any]] = []  # recent tab_event 'created' records

    # ------------------------------------------------------------------ state
    @property
    def connected(self) -> bool:
        return self._ws is not None

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connected_at": self._connected_at,
            "extension": self._info,
            "active_tab": self.active_tab,
            "pending_requests": len(self._pending),
        }

    def on_event(self, fn: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._on_event.append(fn)

    # ------------------------------------------------------------------ connection lifecycle
    async def attach(self, ws: WebSocket, info: dict[str, Any]) -> None:
        old = self._ws
        self._ws = ws
        self._info = info
        self._connected_at = now_iso()
        if old is not None and old is not ws:
            try:
                await old.close(code=4000, reason="replaced by a newer connection")
            except Exception:
                pass

    async def detach(self, ws: WebSocket) -> None:
        if self._ws is ws:
            self._ws = None
            self._connected_at = None
            self.active_tab = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BrowserDisconnectedError("extension disconnected"))
            self._pending.clear()

    async def handle_incoming(self, msg: dict[str, Any]) -> None:
        mid = msg.get("id")
        mtype = msg.get("type")
        if mid and mid in self._pending:
            fut = self._pending.pop(mid)
            if not fut.done():
                fut.set_result(msg)
            return
        if mtype == "tab_event":
            if msg.get("active"):
                self.active_tab = {"tab_id": msg.get("tab_id"), "url": msg.get("url"), "title": msg.get("title")}
            if msg.get("event") == "created":
                self.created_tabs.append({"tab_id": msg.get("tab_id"), "opener_tab_id": msg.get("opener_tab_id"), "url": msg.get("url"), "at": now_iso()})
                del self.created_tabs[:-50]
        if mtype == "pong":
            return
        for fn in list(self._on_event):
            try:
                await fn(msg)
            except Exception:
                log.exception("extension event handler failed")

    # ------------------------------------------------------------------ requests
    async def request(self, mtype: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        ws = self._ws
        if ws is None:
            raise BrowserDisconnectedError("extension is not connected")
        mid = new_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[mid] = fut
        try:
            async with self._send_lock:
                await ws.send_json({"type": mtype, "id": mid, **payload})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(mid, None)
            raise BrowserDisconnectedError(f"extension did not answer '{mtype}' within {timeout:.0f}s") from exc
        except BrowserDisconnectedError:
            raise
        except Exception as exc:
            self._pending.pop(mid, None)
            raise BrowserDisconnectedError(f"failed to send '{mtype}' to the extension: {exc}") from exc

    async def push(self, mtype: str, payload: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            async with self._send_lock:
                await ws.send_json({"type": mtype, **payload})
            return True
        except Exception:
            return False
