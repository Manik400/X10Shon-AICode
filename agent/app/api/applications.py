from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core.enums import ApplicationState, VerificationStatus
from ..core.util import now_iso
from ..persistence.repositories import Page
from .deps import Auth, Ctx

router = APIRouter(prefix="/api/applications", tags=["applications"])

_USER_TRANSITIONS = {
    "queue": ApplicationState.QUEUED,
    "skip": ApplicationState.SKIPPED,
    "cancel": ApplicationState.CANCELLED,
    "retry": ApplicationState.QUEUED,
    "mark_eligible": ApplicationState.ELIGIBLE,
}


class TransitionBody(BaseModel):
    action: str = Field(pattern="^(queue|skip|cancel|retry|mark_eligible|reconcile_confirmed|reconcile_failed)$")
    reason: str | None = Field(default=None, max_length=500)
    reference_number: str | None = Field(default=None, max_length=100)


class AnswersBody(BaseModel):
    answers: list[dict[str, Any]] = Field(description="[{label, answer, category?, legally_significant?, save_as_approved?}]")
    requeue: bool = True


class NotesBody(BaseModel):
    notes: str = Field(max_length=4000)


class ClearQueueBody(BaseModel):
    stop_running: bool = Field(default=True, description="Also stop running list passes and scheduled applications (tasks on the user's own tab are never touched)")


@router.get("/queue")
async def queue(ctx: Ctx, _: Auth) -> dict:
    """What is waiting to be applied to: queued / retry-scheduled applications, tasks waiting their
    turn and running queue work."""
    from ..scheduler.queue import queue_summary

    return await queue_summary(ctx.repos)


@router.post("/queue/clear")
async def clear_queue(body: ClearQueueBody | None, ctx: Ctx, _: Auth) -> dict:
    """Empty the queue (dashboard button): cancel every queued application and waiting task."""
    from ..scheduler.queue import clear_queue as _clear
    from ..scheduler.queue import queue_summary

    result = await _clear(ctx.repos, ctx.tasks, ctx.audit, stop_running=body.stop_running if body else True)
    return {**result, **(await queue_summary(ctx.repos))}


@router.get("")
async def list_applications(ctx: Ctx, _: Auth, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), state: str | None = None, search: str | None = None, since: str | None = None, verification: str | None = None, sort: str = "updated_at", order: str = "desc") -> dict:
    items, total = await ctx.repos.applications.list(Page(limit, offset), state=state, search=search, since=since, verification=verification, sort=sort, order=order)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/export.csv")
async def export_csv(ctx: Ctx, _: Auth) -> StreamingResponse:
    items, _ = await ctx.repos.applications.list(Page(500, 0))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "company", "title", "state", "verification_status", "reference_number", "resume", "application_url", "applied_at", "attempt_count", "updated_at", "notes"])
    for a in items:
        w.writerow([a["id"], a.get("company"), a.get("title"), a["state"], a.get("verification_status"), a.get("reference_number"), a.get("resume_label"), a.get("application_url"), a.get("applied_at"), a.get("attempt_count"), a.get("updated_at"), a.get("notes")])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=applications.csv"})


@router.get("/{app_id}")
async def get_application(app_id: str, ctx: Ctx, _: Auth) -> dict:
    app = await ctx.repos.applications.get(app_id)
    if app is None:
        raise HTTPException(404, "application not found")
    job = await ctx.repos.jobs.get(app["job_id"])
    history = await ctx.repos.applications.history(app_id)
    tasks, _ = await ctx.repos.tasks.list(Page(20, 0))
    related = [t for t in tasks if t.get("application_id") == app_id]
    return {"application": app, "job": job, "history": history, "tasks": related}


@router.post("/{app_id}/transition")
async def transition(app_id: str, body: TransitionBody, ctx: Ctx, _: Auth) -> dict:
    app = await ctx.repos.applications.get(app_id)
    if app is None:
        raise HTTPException(404, "application not found")
    try:
        if body.action == "reconcile_confirmed":
            # User verified on the site that the application went through.
            updated = await ctx.repos.applications.transition(app_id, ApplicationState.APPLIED_CONFIRMED, reason=body.reason or "reconciled by user", actor="user", fields={"verification_status": VerificationStatus.CONFIRMED.value, "reference_number": body.reference_number, "applied_at": now_iso(), "evidence": {"source": "user_reconciliation", "at": now_iso(), "note": body.reason}})
        elif body.action == "reconcile_failed":
            updated = await ctx.repos.applications.transition(app_id, ApplicationState.APPLICATION_FAILED, reason=body.reason or "reconciled as failed by user", actor="user", fields={"verification_status": VerificationStatus.REJECTED.value})
        else:
            target = _USER_TRANSITIONS[body.action]
            if target == ApplicationState.QUEUED and app["state"] == ApplicationState.APPLICATION_FAILED:
                failure = app.get("failure_json") or {}
                if failure.get("category") == "SUBMISSION_UNCERTAIN" and (app.get("verification_status") == VerificationStatus.UNCERTAIN.value):
                    raise HTTPException(409, "outcome is uncertain; reconcile it (confirmed/failed) before retrying to avoid a duplicate application")
            updated = await ctx.repos.applications.transition(app_id, target, reason=body.reason or f"{body.action} by user", actor="user", fields={"next_attempt_at": None} if target == ApplicationState.QUEUED else None)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if updated["state"] == ApplicationState.QUEUED:
        ctx.scheduler.kick()
    return updated


@router.post("/{app_id}/answers")
async def provide_answers(app_id: str, body: AnswersBody, ctx: Ctx, _: Auth) -> dict:
    """Store the user's answers to pending questions (as approved answers when requested) and
    optionally re-queue the application."""
    app = await ctx.repos.applications.get(app_id)
    if app is None:
        raise HTTPException(404, "application not found")
    saved = []
    for a in body.answers:
        label = str(a.get("label", "")).strip()
        answer = str(a.get("answer", "")).strip()
        if not label or not answer:
            continue
        if a.get("save_as_approved", True):
            saved.append(await ctx.repos.profile.upsert_answer({"category": a.get("category") or "custom", "question": label, "answer": answer, "provenance": "approved_answer", "legally_significant": bool(a.get("legally_significant", False))}))
    await ctx.repos.applications.update_fields(app_id, {"pending_questions": None})
    await ctx.audit.log("application.answers_provided", application_id=app_id, count=len(saved))
    if body.requeue and app["state"] in (ApplicationState.NEEDS_USER_INPUT, ApplicationState.READY_FOR_REVIEW, ApplicationState.AUTH_REQUIRED):
        await ctx.repos.applications.transition(app_id, ApplicationState.QUEUED, reason="answers provided; re-queued", actor="user")
        ctx.scheduler.kick()
    return {"saved": saved, "application": await ctx.repos.applications.get(app_id)}


@router.put("/{app_id}/notes")
async def set_notes(app_id: str, body: NotesBody, ctx: Ctx, _: Auth) -> dict:
    updated = await ctx.repos.applications.update_fields(app_id, {"notes": body.notes})
    if updated is None:
        raise HTTPException(404, "application not found")
    return updated
