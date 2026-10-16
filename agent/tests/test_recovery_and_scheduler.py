from __future__ import annotations

from datetime import datetime

import pytest

from app.agent.recovery import RecoveryManager, backoff_seconds
from app.core.enums import ApplicationState, ErrorCategory
from app.core.errors import AgentError, RateLimitedError, classify_exception
from app.scheduler import SchedulerConfig
from app.security.redaction import redact, redact_text


def test_backoff_grows_with_jitter_and_honours_retry_after() -> None:
    a, b = backoff_seconds(1), backoff_seconds(4)
    assert 1 <= a <= 2 and 8 <= b <= 16
    assert backoff_seconds(1, retry_after=42) == 42
    assert backoff_seconds(50) <= 300


def test_recovery_decisions() -> None:
    rm = RecoveryManager(max_attempts=3)
    f = AgentError("x", category=ErrorCategory.NAVIGATION_FAILED).to_failure()
    assert rm.decide(f, 1).retry and rm.decide(f, 3).retry is False
    assert rm.decide(AgentError("x", category=ErrorCategory.AUTH_REQUIRED).to_failure(), 1).strategy == "handoff"
    assert rm.decide(AgentError("x", category=ErrorCategory.SUBMISSION_UNCERTAIN).to_failure(), 1).retry is False
    assert rm.decide(AgentError("x", category=ErrorCategory.ELEMENT_NOT_FOUND).to_failure(), 1).strategy == "resnapshot"
    rl = RateLimitedError("slow down", retry_after_seconds=30).to_failure()
    assert rm.decide(rl, 1).wait_seconds == 30


def test_classify_exception_never_leaks_traceback() -> None:
    f = classify_exception(RuntimeError("secret token=abc123"))
    assert f.category == ErrorCategory.UNKNOWN and "RuntimeError" in f.message


def test_redaction() -> None:
    out = redact({"password": "p", "profile": {"email": "a@b.co", "note": "call +1 555 010 0100 token=zzz"}, "Authorization": "Bearer abcdefghijkl"})
    assert out["password"] == "[REDACTED]" and out["Authorization"] == "[REDACTED]"
    assert out["profile"]["email"] == "[PII]"
    assert "[PHONE]" in out["profile"]["note"]
    assert "Bearer [REDACTED]" in redact_text("Authorization: Bearer abcdefghijklmnop")


@pytest.mark.asyncio
async def test_scheduler_respects_enabled_hours_limits_and_domains(ctx, sample_profile) -> None:
    sched = ctx.scheduler
    await ctx.repos.profile.save(sample_profile)
    job, _ = await ctx.jobs.import_url("https://jobs.example.com/a/1", prefill={"title": "Backend Engineer", "company": "A"})
    app = await ctx.repos.applications.get_by_job(job["id"])
    await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="t")

    assert (await sched.tick())["reason"] == "scheduler disabled"
    cfg = SchedulerConfig(enabled=True, active_hours_start=0, active_hours_end=24, active_days=list(range(7)), allowed_domains=[], daily_application_limit=5)
    await sched.save(cfg)
    r = await sched.tick()
    assert r["started"] == 0 and r["reason"] == "nothing schedulable"  # domain not allowed → stays queued

    await sched.save(cfg.model_copy(update={"active_hours_start": 23, "active_hours_end": 24}))
    assert sched.within_active_hours(datetime(2026, 10, 14, 12, 0)) is False
    assert sched.within_active_hours(datetime(2026, 10, 14, 23, 30)) is True

    await sched.save(cfg.model_copy(update={"daily_application_limit": 0}))
    assert "daily limit" in (await sched.tick())["reason"]

    ctx.policy.paused = True
    await sched.save(cfg)
    assert (await sched.tick())["reason"] == "paused"


@pytest.mark.asyncio
async def test_scheduler_retry_limit_fails_application(ctx) -> None:
    job, _ = await ctx.jobs.import_url("https://jobs.example.com/a/2", prefill={"title": "X", "company": "A"})
    app = await ctx.repos.applications.get_by_job(job["id"])
    await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="t")
    await ctx.repos.applications.transition(app["id"], ApplicationState.IN_PROGRESS, reason="t", fields={"attempt_count": 9})
    await ctx.repos.applications.transition(app["id"], ApplicationState.RETRY_SCHEDULED, reason="t")
    await ctx.scheduler.save(SchedulerConfig(enabled=True, active_hours_start=0, active_hours_end=24, active_days=list(range(7)), allowed_domains=["jobs.example.com"], max_retries=3))
    await ctx.scheduler.tick()
    assert (await ctx.repos.applications.get(app["id"]))["state"] == "APPLICATION_FAILED"


@pytest.mark.asyncio
async def test_restart_marks_running_tasks_interrupted_and_reconciles(settings, sample_profile) -> None:
    from app.core.enums import TaskStatus
    from app.state import build_context

    c1 = await build_context(settings)
    job, _ = await c1.jobs.import_url("https://jobs.example.com/a/3", prefill={"title": "X", "company": "A"})
    app = await c1.repos.applications.get_by_job(job["id"])
    await c1.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="t")
    await c1.repos.applications.transition(app["id"], ApplicationState.IN_PROGRESS, reason="t")
    task = await c1.repos.tasks.create("apply", application_id=app["id"])
    await c1.repos.tasks.set_status(task["id"], TaskStatus.RUNNING)
    await c1.db.close()  # simulate crash: no graceful shutdown
    c2 = await build_context(settings)
    t = await c2.repos.tasks.get(task["id"])
    assert t["status"] == "failed" and t["failure_json"]["category"] == "BROWSER_DISCONNECTED"
    a = await c2.repos.applications.get(app["id"])
    assert a["state"] == "RETRY_SCHEDULED"
    await c2.db.close()
