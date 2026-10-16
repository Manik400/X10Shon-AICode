"""WebSocket endpoint for the paired extension."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import __version__
from ..browser.extension_hub import MAX_MESSAGE_BYTES

router = APIRouter(tags=["extension"])
log = logging.getLogger("lfa.ws")

_last_reload_request: dict[str, float] = {}


def dist_version() -> str | None:
    """Version of the extension build on disk (extension/dist/manifest.json)."""
    from ..security.extension_id import repo_extension_dist

    try:
        return str(json.loads((repo_extension_dist() / "manifest.json").read_text(encoding="utf-8")).get("version"))
    except (OSError, ValueError):
        return None


async def _maybe_request_reload(ctx, running: str) -> None:  # type: ignore[no-untyped-def]
    """If the connected extension runs an older build than the one on disk, ask it to reload
    itself (at most once per 5 minutes, so a build that cannot reload never loops)."""
    import time

    on_disk = dist_version()
    if not on_disk or not running or on_disk == running:
        return
    now = time.monotonic()
    if now - _last_reload_request.get(on_disk, -1e9) < 300:
        return
    _last_reload_request[on_disk] = now
    await ctx.audit.log("extension.reload_requested", running=running, on_disk=on_disk)
    await ctx.hub.push("reload_extension", {"reason": f"{running} -> {on_disk}"})


@router.websocket("/ws/extension")
async def extension_socket(ws: WebSocket) -> None:
    ctx = ws.app.state.ctx
    token = ws.query_params.get("token", "")
    auth_header = ws.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    pairing = await ctx.repos.pairings.find_by_token(token) if token else None
    origin = ws.headers.get("origin", "")
    if pairing is None:
        await ws.close(code=4001, reason="invalid token")
        return
    if origin and not origin.startswith("chrome-extension://"):
        await ws.close(code=4003, reason="origin not allowed")
        return
    if origin and pairing.get("extension_id") and origin != f"chrome-extension://{pairing['extension_id']}":
        await ws.close(code=4003, reason="origin does not match pairing")
        return
    await ws.accept()
    log.info("extension socket accepted (conn %s) from %s ua=%s", id(ws) % 10000, origin, (ws.headers.get("user-agent") or "")[:80])
    await ctx.hub.attach(ws, {"pairing_id": pairing["id"], "extension_id": pairing.get("extension_id"), "origin": origin})
    await ctx.repos.pairings.touch(pairing["id"])
    await ctx.audit.log("extension.connected", pairing_id=pairing["id"])
    try:
        await ws.send_json({"type": "welcome", "agent_version": __version__})
        pending = await ctx.repos.approvals.pending()
        await ws.send_json({"type": "status_update", "status": {"paused_all": ctx.policy.paused, "current_task": ctx.tasks.current(), "pending_approvals": len(pending), "waiting_user": None}})
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE_BYTES:
                await ws.send_json({"type": "error", "error": "message too large"})
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") not in ("pong", "tab_event"):
                log.info("extension message %s (conn %s)", msg.get("type"), id(ws) % 10000)
            if msg.get("type") == "hello":
                ctx.hub._info.update({"version": msg.get("version"), "granted_origins": msg.get("granted_origins", [])})
                await _maybe_request_reload(ctx, str(msg.get("version") or ""))
                continue
            await ctx.hub.handle_incoming(msg)
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - network errors
        log.exception("extension socket error")
    finally:
        await ctx.hub.detach(ws)
        await ctx.audit.log("extension.disconnected", pairing_id=pairing["id"])
