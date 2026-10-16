from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.enums import ApplicationState
from ..persistence.repositories import Page
from .deps import Auth, Ctx

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    tab_id: int | None = None
    url: str | None = Field(default=None, max_length=2000)
    application_id: str | None = None


@router.post("")
async def create_task(body: TaskBody, ctx: Ctx, _: Auth) -> dict:
    from ..agent.intent import IntentRouter

    router_ = IntentRouter(ctx.gateway)
    intent = await router_.route(body.instruction, body.url)
    kind = intent.kind
    application_id = body.application_id
    home = (ctx.scheduler.config.home_list_url or "").strip()
    if home and kind in ("apply", "apply_list", "extract_job", "navigate", "general"):
        start_url = body.url or ""
        if not start_url.startswith(home):
            raise HTTPException(400, f"Job Hunt mode: start applications from your list at {home}. Track and contacts work on any page.")
        if kind == "apply":
            kind = "apply_list"  # on the list, "apply" always means: work through the jobs on it
    if body.tab_id is not None:
        busy = [t for t in await ctx.repos.tasks.running() if t.get("tab_id") == body.tab_id and t["status"] in ("pending", "running", "waiting_approval")]
        if busy:
            raise HTTPException(409, f"a {busy[0]['kind']} task is already running on this tab; wait for it or cancel it")
    tab_snap = None
    if kind == "apply" and body.tab_id is not None and ctx.hub.connected:
        try:
            tab_snap = await ctx.extension_driver.snapshot(str(body.tab_id), max_nodes=ctx.settings.snapshot_max_nodes)
        except Exception as exc:
            await ctx.audit.log("task.tab_snapshot_failed", level="warning", error=str(exc)[:200])
    if tab_snap is not None:
        from urllib.parse import urlsplit

        from ..applications.workflow import _NOT_JOB_HOSTS
        from ..browser.page_state import classify_page
        from ..jobs.listing import extract_job_links

        host = urlsplit(tab_snap.url).netloc.lower().split(":")[0]
        if _NOT_JOB_HOSTS.search(host) or (urlsplit(tab_snap.url).port == 8765 and "/fixtures/" not in tab_snap.url):
            raise HTTPException(400, f"{host or 'this page'} is not a job page. Open a job posting or a list of jobs, then try again.")
        cls = classify_page(tab_snap)
        if len(extract_job_links(tab_snap)) >= 3 and cls.kind != "form":
            kind = "apply_list"  # a page full of job links: apply to each of them
    if kind == "apply" and application_id is None and body.url:
        # Importing the page as a job first gives the workflow a persisted application record.
        # From a Chrome tab the tab's own snapshot is used (works on authenticated pages);
        # otherwise the public page is fetched headlessly.
        job = None
        if tab_snap is not None:
            job, _ = await ctx.jobs.import_from_snapshot(tab_snap, source_kind="manual")
        if job is None:
            job, _ = await ctx.jobs.import_url(body.url, source_kind="manual", page_fetcher=None if body.tab_id is not None else ctx.fetch_public_snapshot)
        app = await ctx.repos.applications.get_by_job(job["id"])
        if app is None:
            raise HTTPException(500, "could not create an application record")
        if app["state"] in (ApplicationState.ELIGIBLE, ApplicationState.SKIPPED, ApplicationState.IMPORTED):
            app = await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="queued from task", actor="user")
        application_id = app["id"]
    domain = intent.url or body.url
    task = await ctx.repos.tasks.create(kind, instruction=body.instruction, application_id=application_id, tab_id=body.tab_id, domain=domain)
    if domain and domain.startswith("http"):
        from urllib.parse import urlsplit

        host = urlsplit(domain).netloc
        if not ctx.policy.domain_allowed(domain):
            # Tasks the user explicitly starts on a tab implicitly allow that domain for this task's lifetime.
            ctx.policy.allow_domain(host)
            await ctx.audit.log("policy.domain_allowed_by_user_task", task_id=task["id"], domain=host)
    await ctx.tasks.start(task)
    return {**task, "intent": intent.model_dump()}


@router.get("")
async def list_tasks(ctx: Ctx, _: Auth, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), status: str | None = None, kind: str | None = None) -> dict:
    items, total = await ctx.repos.tasks.list(Page(limit, offset), status=status, kind=kind)
    return {"items": items, "total": total, "running": ctx.tasks.running_ids, "current": ctx.tasks.current()}


@router.get("/{task_id}")
async def get_task(task_id: str, ctx: Ctx, _: Auth) -> dict:
    t = await ctx.repos.tasks.get(task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    return {"task": t, "attempts": await ctx.repos.tasks.attempts(task_id), "live": task_id in ctx.tasks.running_ids}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, ctx: Ctx, _: Auth) -> dict:
    return {"ok": await ctx.tasks.cancel(task_id)}


@router.post("/{task_id}/resume")
async def resume_task(task_id: str, ctx: Ctx, _: Auth) -> dict:
    try:
        return await ctx.tasks.resume_task(task_id)
    except KeyError:
        raise HTTPException(404, "task not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
