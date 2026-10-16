from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core.enums import ApplicationState
from ..jobs.matching import JobFilters
from ..persistence.repositories import Page
from .deps import Auth, Ctx

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class ImportBody(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    source_kind: str = Field(default="url", max_length=32)
    tab_id: int | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None


class SourceBody(BaseModel):
    kind: str = Field(pattern="^(greenhouse|lever|csv|url|manual)$")
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)


@router.get("")
async def list_jobs(
    ctx: Ctx,
    _: Auth,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: str | None = None,
    state: str | None = None,
    source_kind: str | None = None,
    min_score: float | None = None,
    since: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    include_duplicates: bool = True,
) -> dict:
    items, total = await ctx.repos.jobs.list(Page(limit, offset), search=search, state=state, source_kind=source_kind, min_score=min_score, since=since, sort=sort, order=order, include_duplicates=include_duplicates)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/filters")
async def get_filters(ctx: Ctx, _: Auth) -> dict:
    return (await ctx.jobs.filters()).model_dump()


@router.put("/filters")
async def put_filters(body: JobFilters, ctx: Ctx, _: Auth) -> dict:
    await ctx.jobs.save_filters(body)
    n = await ctx.jobs.rescreen_all()
    return {"ok": True, "rescreened": n, "filters": body.model_dump()}


@router.get("/export.csv")
async def export_csv(ctx: Ctx, _: Auth) -> StreamingResponse:
    items, _ = await ctx.repos.jobs.list(Page(500, 0))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "company", "title", "location", "remote_type", "source_kind", "match_score", "state", "posted_at", "canonical_url", "created_at"])
    for j in items:
        w.writerow([j["id"], j.get("company"), j.get("title"), j.get("location"), j.get("remote_type"), j.get("source_kind"), j.get("match_score"), j.get("application_state"), j.get("posted_at"), j.get("canonical_url"), j.get("created_at")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=jobs.csv"})


@router.post("/import")
async def import_job(body: ImportBody, ctx: Ctx, principal: Auth) -> dict:
    """Import by URL. When the request comes from the extension with a tab id, the page snapshot
    from that tab is used (so authenticated pages work); otherwise adapters/APIs are used."""
    extraction = None
    prefill = {"title": body.title, "company": body.company, "location": body.location}
    if body.tab_id is not None and ctx.hub.connected:
        try:
            snap = await ctx.extension_driver.snapshot(str(body.tab_id), max_nodes=ctx.settings.snapshot_max_nodes)
            if snap.url.split("#")[0] == body.url.split("#")[0]:
                job, dup = await ctx.jobs.import_from_snapshot(snap, source_kind=body.source_kind)
                return {"job": job, "duplicate": dup}
        except Exception as exc:  # fall back to URL import
            await ctx.audit.log("job.import_snapshot_failed", level="warning", error=str(exc)[:200])
    job, dup = await ctx.jobs.import_url(body.url, source_kind=body.source_kind, extraction=extraction, prefill={k: v for k, v in prefill.items() if v}, page_fetcher=ctx.fetch_public_snapshot)
    return {"job": job, "duplicate": dup}


@router.post("/import-csv")
async def import_csv(ctx: Ctx, _: Auth, file: UploadFile) -> dict:
    raw = await file.read()
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "CSV too large (2 MB max)")
    src = await ctx.repos.jobs.add_source("csv", file.filename or "upload.csv", {"rows": raw.count(b"\n")})
    return await ctx.jobs.import_csv(raw.decode("utf-8", errors="replace"), source_id=src["id"])


@router.get("/sources")
async def list_sources(ctx: Ctx, _: Auth) -> dict:
    return {"items": await ctx.repos.jobs.list_sources(), "adapters": ctx.adapters.describe()}


@router.post("/sources")
async def add_source(body: SourceBody, ctx: Ctx, _: Auth) -> dict:
    return await ctx.repos.jobs.add_source(body.kind, body.name, body.config)


@router.post("/sources/{source_id}/run")
async def run_source(source_id: str, ctx: Ctx, _: Auth) -> dict:
    try:
        return await ctx.jobs.run_source(source_id)
    except KeyError:
        raise HTTPException(404, "source not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"source run failed: {str(exc)[:200]}")


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str, ctx: Ctx, _: Auth) -> dict:
    return {"ok": await ctx.repos.jobs.delete_source(source_id)}


@router.get("/{job_id}")
async def get_job(job_id: str, ctx: Ctx, _: Auth) -> dict:
    job = await ctx.repos.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    app = await ctx.repos.applications.get_by_job(job_id)
    return {"job": job, "application": app}


@router.post("/{job_id}/screen")
async def screen(job_id: str, ctx: Ctx, _: Auth) -> dict:
    try:
        return await ctx.jobs.screen(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")


@router.post("/{job_id}/queue")
async def queue(job_id: str, ctx: Ctx, _: Auth) -> dict:
    app = await ctx.repos.applications.get_by_job(job_id)
    if app is None:
        raise HTTPException(404, "no application record; screen the job first")
    try:
        app = await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="queued by user", actor="user")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    ctx.scheduler.kick()
    return app


@router.delete("/{job_id}")
async def delete_job(job_id: str, ctx: Ctx, _: Auth) -> dict:
    return {"ok": await ctx.repos.jobs.delete(job_id)}
