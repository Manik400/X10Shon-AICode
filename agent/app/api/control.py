from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .deps import Auth, Ctx

router = APIRouter(prefix="/api", tags=["control"])


class DecisionBody(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")


class AllowDomainBody(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    domain: str | None = Field(default=None, max_length=253)


class ApprovalPolicyBody(BaseModel):
    policy: str = Field(pattern="^(always|never|per_site)$")


@router.post("/control/allow-domain")
async def allow_domain(body: AllowDomainBody, ctx: Ctx, _: Auth) -> dict:
    """Add one domain to the agent's persisted allow-list. Called by the extension when the user
    hands a tab to the agent, and by the dashboard."""
    from urllib.parse import urlsplit

    host = (body.domain or urlsplit(body.url or "").netloc).lower().split(":")[0].strip()
    if not host or "/" in host:
        raise HTTPException(400, "a url or domain is required")
    cfg = ctx.scheduler.config
    if host not in cfg.allowed_domains:
        await ctx.scheduler.save(cfg.model_copy(update={"allowed_domains": [*cfg.allowed_domains, host]}))
    ctx.policy.allow_domain(host)
    await ctx.audit.log("policy.domain_allowed", domain=host)
    return {"allowed_domains": ctx.policy.allowed_domains}


@router.post("/control/approval-policy")
async def set_approval_policy(body: ApprovalPolicyBody, ctx: Ctx, _: Auth) -> dict:
    cfg = ctx.scheduler.config
    await ctx.scheduler.save(cfg.model_copy(update={"approval_policy": body.policy}))
    await ctx.audit.log("settings.approval_policy", policy=body.policy)
    return {"approval_policy": body.policy}


@router.post("/control/pause")
async def pause(ctx: Ctx, _: Auth) -> dict:
    await ctx.tasks.pause_all()
    return {"paused": True}


@router.post("/control/resume")
async def resume(ctx: Ctx, _: Auth) -> dict:
    await ctx.tasks.resume_all()
    ctx.scheduler.kick()
    return {"paused": False}


@router.get("/control/status")
async def status(ctx: Ctx, _: Auth) -> dict:
    return {"paused": ctx.policy.paused, "running": ctx.tasks.running_ids, "current": ctx.tasks.current(), "scheduler": ctx.scheduler.status(), "extension": ctx.hub.status(), "pending_approvals": len(await ctx.repos.approvals.pending())}


@router.get("/control/tabs")
async def list_tabs(ctx: Ctx, _: Auth) -> dict:
    """Tabs the connected extension can see (url/title/permission only, never content)."""
    if not ctx.hub.connected:
        return {"connected": False, "tabs": []}
    from dataclasses import asdict

    try:
        tabs = await ctx.extension_driver.list_tabs()
    except Exception as exc:
        raise HTTPException(503, f"extension did not answer: {str(exc)[:200]}")
    return {"connected": True, "tabs": [asdict(t) for t in tabs]}


@router.post("/control/tabs/{tab_id}/activate")
async def activate_tab(tab_id: int, ctx: Ctx, _: Auth) -> dict:
    """Bring a tab to the front. Answered only by extension builds >= 0.2.0 (also a probe)."""
    try:
        resp = await ctx.hub.request("activate_tab", {"tab_id": tab_id}, timeout=5.0)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": bool(resp.get("ok")), "reply": resp.get("type")}


@router.post("/control/extension/reload")
async def reload_extension(ctx: Ctx, _: Auth) -> dict:
    """Ask the connected extension to reload itself (picks up a rebuilt extension/dist)."""
    from .extension_ws import dist_version

    ok = await ctx.hub.push("reload_extension", {"reason": "requested from the dashboard"})
    return {"sent": ok, "running": ctx.hub.status()["extension"].get("version"), "on_disk": dist_version()}


@router.get("/control/tabs/{tab_id}/snapshot")
async def tab_snapshot(tab_id: int, ctx: Ctx, _: Auth) -> dict:
    """Compact snapshot of one tab (diagnostics: what the agent sees on that page)."""
    try:
        snap = await ctx.extension_driver.snapshot(str(tab_id), max_nodes=ctx.settings.snapshot_max_nodes)
    except Exception as exc:
        raise HTTPException(503, f"snapshot failed: {str(exc)[:200]}")
    from ..jobs.listing import extract_job_links

    return {"url": snap.url, "title": snap.title, "nodes": [n.model_dump(exclude_none=True) for n in snap.nodes], "job_links": extract_job_links(snap)}


@router.post("/control/tabs/{tab_id}/close")
async def close_tab(tab_id: int, ctx: Ctx, _: Auth) -> dict:
    await ctx.extension_driver.close_tab(str(tab_id))
    return {"ok": True}


@router.post("/control/scheduler/tick")
async def tick(ctx: Ctx, _: Auth) -> dict:
    return await ctx.scheduler.tick()


@router.get("/approvals")
async def list_approvals(ctx: Ctx, _: Auth, status: str | None = None) -> dict:
    items = await ctx.repos.approvals.pending() if status == "pending" else await ctx.repos.approvals.list()
    return {"items": items}


@router.post("/approvals/{approval_id}/decide")
async def decide(approval_id: str, body: DecisionBody, ctx: Ctx, _: Auth) -> dict:
    updated = await ctx.approvals.decide(approval_id, body.decision)
    if updated is None:
        raise HTTPException(404, "approval not found")
    return updated
