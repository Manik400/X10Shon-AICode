"""Queue emptying, bounded retries, and tasks the user starts on their own tab not waiting behind
the scheduler's queue."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent.recovery import RecoveryManager
from app.agent.runner import TaskManager
from app.browser.page_state import ineligible_notice
from app.browser.snapshot import PageSnapshot, SnapshotNode
from app.core.enums import ApplicationState, ErrorCategory, TaskStatus
from app.core.errors import AgentError
from app.scheduler import SchedulerConfig
from app.scheduler.queue import clear_queue, queue_summary

from .conftest import wait_task


# ------------------------------------------------------------------ settings bounds
def test_runaway_limits_are_clamped_when_loaded() -> None:
    cfg = SchedulerConfig.model_validate({"max_concurrent_tasks": 100, "max_retries": 10})
    assert (cfg.max_concurrent_tasks, cfg.max_retries) == (5, 3)
    assert SchedulerConfig().max_retries == 1
    assert SchedulerConfig.model_validate({"max_retries": -2}).max_retries == 0


def test_recovery_follows_the_retry_setting() -> None:
    rm = RecoveryManager()
    transient = AgentError("x", category=ErrorCategory.NAVIGATION_FAILED).to_failure()
    rm.set_max_retries(0)
    assert rm.max_retries == 0 and rm.decide(transient, 1).retry is False  # a failure is final
    rm.set_max_retries(1)
    assert rm.decide(transient, 1).retry is True and rm.decide(transient, 2).retry is False
    # A job that fails for its own reasons is never retried, whatever the setting.
    rm.set_max_retries(3)
    assert rm.decide(AgentError("x", category=ErrorCategory.SITE_UNSUPPORTED).to_failure(), 1).retry is False


# ------------------------------------------------------------------ the site says "not eligible"
def _page(text: str = "", notices: list[str] | None = None) -> PageSnapshot:
    nodes = [SnapshotNode(i=i, role="alert", name=n, tag="div", visible=True) for i, n in enumerate(notices or [])]
    return PageSnapshot(url="https://jobs.example.com/j/1", text_excerpt=text, nodes=nodes)


@pytest.mark.parametrize(
    "text",
    [
        "Senior Engineer, Bengaluru. You are not eligible for this job based on your profile.",
        "Sorry, your profile does not match the requirements for this role.",
        "You do not meet the minimum requirements for this position.",
        "We are not accepting applications from your location at this time.",
        "Your experience doesn't match the job criteria",
        "This job is not available in your country.",
    ],
)
def test_site_verdict_is_detected(text: str) -> None:
    assert ineligible_notice(_page(text))


@pytest.mark.parametrize(
    "text",
    [
        "Please note that you are not eligible for visa sponsorship in this role.",
        "If you do not meet the requirements listed above, please do not apply.",
        "Candidates who do not meet the minimum requirements will not be contacted.",
        "Are you authorized to work in the US? You are eligible to apply from anywhere.",
        "Responsibilities: build APIs. Requirements: 3+ years of Python.",
    ],
)
def test_descriptions_and_hypotheticals_are_not_verdicts(text: str) -> None:
    assert ineligible_notice(_page(text)) is None


def test_verdict_in_an_alert_counts() -> None:
    assert ineligible_notice(_page("Job description...", ["Your profile is not a match for this job"])) == "Your profile is not a match"


# ------------------------------------------------------------------ user tasks do not wait for the queue
class _StubRunner:
    """Stands in for a TaskRunner: records when each task started and blocks until released."""

    def __init__(self, log: list[str], gate: asyncio.Event, repos: Any):
        self._log, self._gate, self._repos = log, gate, repos
        self.cancel_event = asyncio.Event()
        self.resume_cb: Any = None
        self.browser_unavailable_cb: Any = None
        self.task: dict[str, Any] = {}
        self.action_count = 0
        self.message = ""
        self.driver = None

    async def run(self, task: dict[str, Any]) -> dict[str, Any]:
        self.task = task
        self._log.append(task["id"])
        await self._repos.tasks.set_status(task["id"], TaskStatus.RUNNING)
        await self._gate.wait()
        await self._repos.tasks.set_status(task["id"], TaskStatus.SUCCEEDED, result={})
        return {"status": "succeeded"}


@pytest.mark.asyncio
async def test_tab_task_runs_while_background_queue_is_full(ctx) -> None:
    started: list[str] = []
    gate = asyncio.Event()
    tm = TaskManager(ctx.settings, ctx.repos, ctx.audit, ctx.policy, lambda: _StubRunner(started, gate, ctx.repos))
    tm.set_concurrency(1)
    bg1 = await ctx.repos.tasks.create("apply", application_id=None, domain="https://a.example")
    bg2 = await ctx.repos.tasks.create("apply", application_id=None, domain="https://b.example")
    mine = await ctx.repos.tasks.create("apply_list", instruction="apply to all", tab_id=42, domain="https://me.example")
    await tm.start(bg1)
    await tm.start(bg2)
    await tm.start(mine)
    await asyncio.sleep(0.1)
    # The first background task holds the single slot and the second waits; the user's own-tab task runs anyway.
    assert started == [bg1["id"], mine["id"]]
    assert (await ctx.repos.tasks.get(bg2["id"]))["status"] == "pending"
    # Cancelling a task that is still waiting its turn never runs it.
    assert await tm.cancel(bg2["id"])
    gate.set()
    await asyncio.sleep(0.2)
    assert (await ctx.repos.tasks.get(bg2["id"]))["status"] == "cancelled"
    assert started == [bg1["id"], mine["id"]]
    await tm.shutdown()


# ------------------------------------------------------------------ no browser: no attempt burnt, scheduler holds
@pytest.mark.asyncio
async def test_browser_unavailable_keeps_the_job_queued_and_holds_the_scheduler(ctx) -> None:
    job, _ = await ctx.jobs.import_url("https://jobs.example.com/a/9", prefill={"title": "X", "company": "A"})
    app = await ctx.repos.applications.get_by_job(job["id"])
    await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="t")
    task = await ctx.repos.tasks.create("apply", application_id=app["id"], job_id=job["id"])
    runner = ctx.tasks._factory()
    runner.browser_unavailable_cb = ctx.tasks.browser_unavailable_cb
    failure = AgentError("max concurrent pages (2) reached", category=ErrorCategory.BROWSER_DISCONNECTED).to_failure()
    await runner._mark_application_after_failure(task, failure)
    after = await ctx.repos.applications.get(app["id"])
    assert after["state"] == "QUEUED" and int(after.get("attempt_count") or 0) == 0 and after["next_attempt_at"]
    await ctx.scheduler.save(SchedulerConfig(enabled=True, active_hours_start=0, active_hours_end=24, active_days=list(range(7))))
    assert (await ctx.scheduler.tick())["reason"].startswith("waiting for a browser")


@pytest.mark.asyncio
async def test_scheduler_does_not_start_work_without_a_browser(ctx) -> None:
    job, _ = await ctx.jobs.import_url("https://jobs.example.com/a/10", prefill={"title": "X", "company": "A"})
    app = await ctx.repos.applications.get_by_job(job["id"])
    await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="t")
    await ctx.scheduler.save(SchedulerConfig(enabled=True, active_hours_start=0, active_hours_end=24, active_days=list(range(7))))
    ctx.scheduler.browser_ready = lambda: (False, "extension not connected")
    r = await ctx.scheduler.tick()
    assert r["started"] == 0 and "extension not connected" in r["reason"]
    assert (await ctx.repos.applications.get(app["id"]))["state"] == "QUEUED"


# ------------------------------------------------------------------ empty the queue
@pytest.mark.asyncio
async def test_clear_queue_cancels_waiting_applications_and_tasks(ctx) -> None:
    apps = []
    for i in range(3):
        job, _ = await ctx.jobs.import_url(f"https://jobs.example.com/q/{i}", prefill={"title": f"Job {i}", "company": "Q"})
        a = await ctx.repos.applications.get_by_job(job["id"])
        await ctx.repos.applications.transition(a["id"], ApplicationState.QUEUED, reason="t")
        apps.append(a)
    await ctx.repos.applications.transition(apps[2]["id"], ApplicationState.IN_PROGRESS, reason="t")
    await ctx.repos.applications.transition(apps[2]["id"], ApplicationState.RETRY_SCHEDULED, reason="t")
    # A finished application is never touched.
    job, _ = await ctx.jobs.import_url("https://jobs.example.com/q/done", prefill={"title": "Done", "company": "Q"})
    done = await ctx.repos.applications.get_by_job(job["id"])
    for st in (ApplicationState.QUEUED, ApplicationState.IN_PROGRESS, ApplicationState.SUBMISSION_PENDING, ApplicationState.APPLIED_CONFIRMED):
        await ctx.repos.applications.transition(done["id"], st, reason="t")
    pending = await ctx.repos.tasks.create("apply", application_id=apps[0]["id"])
    summary = await queue_summary(ctx.repos)
    assert (summary["queued"], summary["retry_scheduled"], summary["pending_tasks"], summary["total"]) == (2, 1, 1, 4)
    result = await clear_queue(ctx.repos, ctx.tasks, ctx.audit)
    assert result["applications_cancelled"] == 3 and result["tasks_cancelled"] == 1
    for a in apps:
        assert (await ctx.repos.applications.get(a["id"]))["state"] == "CANCELLED"
    assert (await ctx.repos.applications.get(done["id"]))["state"] == "APPLIED_CONFIRMED"
    assert (await ctx.repos.tasks.get(pending["id"]))["status"] == "cancelled"
    assert (await queue_summary(ctx.repos))["total"] == 0


def test_clear_queue_endpoint(client) -> None:
    from .conftest import DASH_HEADERS

    assert client.get("/api/applications/queue", headers=DASH_HEADERS).json()["total"] == 0
    r = client.post("/api/applications/queue/clear", json={"stop_running": True}, headers=DASH_HEADERS)
    assert r.status_code == 200 and r.json()["applications_cancelled"] == 0 and r.json()["total"] == 0


@pytest.mark.asyncio
async def test_track_task_still_runs_through_the_manager(ctx) -> None:
    task = await ctx.repos.tasks.create("track", instruction="track my applications")
    await ctx.tasks.start(task)
    assert (await wait_task(ctx, task["id"]))["status"] == "succeeded"
