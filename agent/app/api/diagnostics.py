from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from ..core.enums import ApplicationState
from ..core.util import now_dt
from ..persistence.repositories import Page
from ..security.redaction import redact
from .deps import Auth, Ctx

router = APIRouter(prefix="/api", tags=["diagnostics"])


@router.get("/overview")
async def overview(ctx: Ctx, _: Auth) -> dict:
    jobs = await ctx.repos.jobs.counts()
    by_state = await ctx.repos.applications.counts_by_state()
    confirmed = by_state.get(ApplicationState.APPLIED_CONFIRMED, 0)
    failed = by_state.get(ApplicationState.APPLICATION_FAILED, 0)
    attempted = confirmed + failed + by_state.get(ApplicationState.RETRY_SCHEDULED, 0)
    eligible = by_state.get(ApplicationState.ELIGIBLE, 0)
    recent, _ = await ctx.repos.audit.list(Page(15, 0))
    return {
        "jobs_discovered": jobs["total"],
        "jobs_duplicates": jobs["duplicates"],
        "eligible": eligible,
        "queued": by_state.get(ApplicationState.QUEUED, 0) + by_state.get(ApplicationState.RETRY_SCHEDULED, 0),
        "in_progress": by_state.get(ApplicationState.IN_PROGRESS, 0),
        "confirmed": confirmed,
        "awaiting_review": by_state.get(ApplicationState.READY_FOR_REVIEW, 0) + by_state.get(ApplicationState.NEEDS_USER_INPUT, 0),
        "failed": failed,
        "auth_required": by_state.get(ApplicationState.AUTH_REQUIRED, 0),
        "skipped": by_state.get(ApplicationState.SKIPPED, 0),
        "by_state": by_state,
        "rates": {
            "success_rate": round(confirmed / attempted, 3) if attempted else None,
            "failure_rate": round(failed / attempted, 3) if attempted else None,
            "denominator": "applications that reached a terminal attempt outcome (confirmed + failed + retry scheduled)",
            "attempted": attempted,
        },
        "recent_activity": recent,
        "paused": ctx.policy.paused,
        "extension_connected": ctx.hub.connected,
        "pending_approvals": len(await ctx.repos.approvals.pending()),
    }


@router.get("/activity")
async def activity(ctx: Ctx, _: Auth, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), task_id: str | None = None, application_id: str | None = None, level: str | None = None, since: str | None = None, search: str | None = None) -> dict:
    items, total = await ctx.repos.audit.list(Page(limit, offset), task_id=task_id, application_id=application_id, level=level, since=since, search=search)
    return {"items": items, "total": total}


@router.get("/diagnostics")
async def diagnostics(ctx: Ctx, _: Auth) -> dict:
    since = (now_dt() - timedelta(hours=24)).isoformat()
    return {
        "resources": ctx.resources.snapshot().to_dict(),
        "model_metrics": await ctx.repos.metrics.summary(since),
        "model_gateway": {"calls": ctx.gateway.calls, "cache_hits": ctx.gateway.cache_hits, "failures": ctx.gateway.failures},
        "extension": ctx.hub.status(),
        "scheduler": ctx.scheduler.status(),
        "running_tasks": ctx.tasks.running_ids,
        "current_task": ctx.tasks.current(),
        "db": {"schema_version": await ctx.db.schema_version(), "path": str(ctx.db.path)},
        "errors_24h": (await ctx.repos.audit.list(Page(20, 0), level="error", since=since))[0],
    }


@router.get("/diagnostics/export")
async def export_diagnostics(ctx: Ctx, _: Auth) -> Response:
    """Redacted diagnostic bundle: recent audit events, model metrics, settings, resources."""
    events, _ = await ctx.repos.audit.list(Page(500, 0))
    tasks, _ = await ctx.repos.tasks.list(Page(50, 0))
    bundle = {
        "exported_at": now_dt().isoformat(),
        "settings": redact({"scheduler": ctx.scheduler.config.model_dump(), "model_roles": {r: c.model_dump() for r, c in ctx.settings.model_roles.items()}}),
        "resources": ctx.resources.snapshot().to_dict(),
        "model_metrics": await ctx.repos.metrics.summary(),
        "extension": redact(ctx.hub.status()),
        "tasks": redact(tasks),
        "events": redact(events),
    }
    return Response(content=json.dumps(bundle, indent=2, default=str), media_type="application/json", headers={"Content-Disposition": "attachment; filename=lfa-diagnostics.json"})


class DeleteBody(BaseModel):
    confirm: str


@router.get("/data/export")
async def export_data(ctx: Ctx, _: Auth) -> Response:
    """Full data export (unredacted; it is the user's own data)."""
    jobs, _ = await ctx.repos.jobs.list(Page(500, 0))
    apps, _ = await ctx.repos.applications.list(Page(500, 0))
    data = {
        "profile": await ctx.repos.profile.get(),
        "answers": await ctx.repos.profile.list_answers(),
        "resumes": await ctx.repos.profile.list_resumes(),
        "jobs": jobs,
        "applications": apps,
        "settings": ctx.scheduler.config.model_dump(),
    }
    return Response(content=json.dumps(data, indent=2, default=str), media_type="application/json", headers={"Content-Disposition": "attachment; filename=lfa-export.json"})


@router.post("/data/delete")
async def delete_all(body: DeleteBody, ctx: Ctx, principal: Auth) -> dict:
    if principal.kind != "dashboard" or body.confirm != "DELETE":
        raise HTTPException(400, "send {confirm: 'DELETE'} from the dashboard to erase all data")
    await ctx.tasks.pause_all()
    async with ctx.db.transaction():
        for table in ("execution_attempts", "approvals", "application_transitions", "tasks", "applications", "jobs", "job_sources", "approved_answers", "resumes", "candidate_profile", "audit_events", "model_metrics", "daily_counters", "settings"):
            await ctx.db.execute(f"DELETE FROM {table}")
    for p in (ctx.settings.data_dir / "resumes").glob("*"):
        try:
            p.unlink()
        except OSError:
            pass
    await ctx.tasks.resume_all()
    await ctx.audit.log("data.deleted")
    return {"ok": True}
