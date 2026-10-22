"""The application queue as the dashboard sees it: what is waiting to be applied to, and how to
empty it in one go (cancel queued applications, waiting tasks and running list passes)."""

from __future__ import annotations

from typing import Any

from ..core.enums import ApplicationState

# Tasks that work through the queue (as opposed to a task the user started on their own tab).
_QUEUE_TASK_STATUSES = ("running", "waiting_approval")


def _works_the_queue(task: dict[str, Any]) -> bool:
    return task["kind"] == "apply_list" or (task["kind"] == "apply" and task.get("tab_id") is None)


async def queue_summary(repos) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    by_state = await repos.applications.counts_by_state()
    live = await repos.tasks.running()
    queued = int(by_state.get(ApplicationState.QUEUED, 0))
    retry = int(by_state.get(ApplicationState.RETRY_SCHEDULED, 0))
    pending = sum(1 for t in live if t["status"] == "pending")
    runs = sum(1 for t in live if t["status"] in _QUEUE_TASK_STATUSES and _works_the_queue(t))
    return {"queued": queued, "retry_scheduled": retry, "pending_tasks": pending, "running_queue_tasks": runs, "total": queued + retry + pending + runs}


async def clear_queue(repos, tasks, audit, *, stop_running: bool = True) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Empty the queue: every QUEUED / RETRY_SCHEDULED application is cancelled, tasks waiting
    their turn are cancelled and (by default) running list passes and scheduled applications are
    stopped. Applications already sent, and tasks the user is running on their own tab, are untouched."""
    cancelled_apps = 0
    for state in (ApplicationState.QUEUED, ApplicationState.RETRY_SCHEDULED):
        for app in await repos.applications.in_state(state):
            try:
                await repos.applications.transition(app["id"], ApplicationState.CANCELLED, reason="queue emptied by user", actor="user")
                cancelled_apps += 1
            except ValueError:
                pass
    cancelled_tasks = 0
    stopped = 0
    for t in await repos.tasks.running():
        if t["status"] == "pending":
            if await tasks.cancel(t["id"]):
                cancelled_tasks += 1
        elif stop_running and t["status"] in _QUEUE_TASK_STATUSES and _works_the_queue(t):
            if await tasks.cancel(t["id"]):
                stopped += 1
    await audit.log("queue.cleared", applications=cancelled_apps, tasks=cancelled_tasks, runs_stopped=stopped)
    return {"applications_cancelled": cancelled_apps, "tasks_cancelled": cancelled_tasks, "runs_stopped": stopped}
