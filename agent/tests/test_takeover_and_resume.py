"""Tab takeover endpoints and automatic resume after the user handles a login."""

from __future__ import annotations

import asyncio

import pytest

from app.core.enums import ApplicationState, TaskStatus

from .conftest import DASH_HEADERS, fixture_url, requires_browser, seed_profile, wait_task
from .test_extension_protocol import FakeSocket


def test_allow_domain_persists_and_updates_policy(client) -> None:
    r = client.post("/api/control/allow-domain", json={"url": "https://Jobs.Example.com:443/apply?x=1"}, headers=DASH_HEADERS)
    assert r.status_code == 200 and "jobs.example.com" in r.json()["allowed_domains"]
    assert "jobs.example.com" in client.get("/api/settings", headers=DASH_HEADERS).json()["scheduler"]["allowed_domains"]
    assert client.post("/api/control/allow-domain", json={}, headers=DASH_HEADERS).status_code == 400


def test_approval_policy_endpoint(client) -> None:
    r = client.post("/api/control/approval-policy", json={"policy": "never"}, headers=DASH_HEADERS)
    assert r.status_code == 200
    assert client.get("/api/settings", headers=DASH_HEADERS).json()["scheduler"]["approval_policy"] == "never"
    assert client.post("/api/control/approval-policy", json={"policy": "sometimes"}, headers=DASH_HEADERS).status_code == 422


@pytest.mark.asyncio
async def test_approval_policy_reaches_workflow(ctx) -> None:
    await ctx.scheduler.save(ctx.scheduler.config.model_copy(update={"approval_policy": "never"}))
    assert ctx.workflow._submit_policy == "never"
    assert ctx.policy._submit_policy == "never"


@pytest.mark.asyncio
@requires_browser
async def test_unattended_mode_submits_without_approval(ctx, sample_profile, resume_file) -> None:
    from .conftest import approve_all, register_resume

    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [{"category": "consent", "question": "I consent to the processing of my data as described in the privacy policy", "answer": "Yes", "provenance": "approved_answer"}])
    await ctx.scheduler.save(ctx.scheduler.config.model_copy(update={"approval_policy": "never"}))
    job, _ = await ctx.jobs.import_url(fixture_url("forms/simple_application.html"), prefill={"title": "SE", "company": "Acme"})
    app = await ctx.repos.applications.get_by_job(job["id"])
    await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="t")
    task = await ctx.repos.tasks.create("apply", application_id=app["id"], job_id=job["id"], domain=app["application_url"])
    await ctx.tasks.start(task)
    t = await wait_task(ctx, task["id"])
    assert t["status"] == "succeeded", t["failure_json"]
    assert (await ctx.repos.applications.get(app["id"]))["state"] == "APPLIED_CONFIRMED"
    assert await ctx.repos.approvals.pending() == [] and await ctx.repos.approvals.list() == []
    assert await approve_all(ctx, timeout=0.1) == []


@pytest.mark.asyncio
async def test_auto_resume_after_login_detected(ctx) -> None:
    """A task waiting on AUTH_REQUIRED for tab 7 resumes itself when the next page load of that
    tab no longer looks like a login page."""
    hub = ctx.hub
    pages = {"login": True}

    class LoginAwareSocket(FakeSocket):
        async def send_json(self, msg):  # type: ignore[no-untyped-def]
            self.sent.append(msg)
            if msg.get("type") == "snapshot":
                snap = (
                    {"url": "https://x/login", "title": "Sign in", "nodes": [{"i": 0, "role": "textbox", "name": "Password", "tag": "input", "type": "password"}], "signals": {"has_password_field": True}}
                    if pages["login"]
                    else {"url": "https://x/apply", "title": "Apply", "nodes": [{"i": 0, "role": "textbox", "name": "First name", "tag": "input"}], "signals": {"logout_cta_present": True}}
                )
                reply = {"type": "snapshot_result", "id": msg["id"], "ok": True, "snapshot": snap}
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.hub.handle_incoming(reply)))
            elif msg.get("type") in ("action", "open_tab", "navigate", "list_tabs", "close_tab"):
                reply = {"type": "action_result", "id": msg["id"], "result": {"ok": True, "action_type": "wait"}}
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.hub.handle_incoming(reply)))

    ws = LoginAwareSocket(hub)
    await hub.attach(ws, {"extension_id": "abc"})  # type: ignore[arg-type]
    task = await ctx.repos.tasks.create("read", instruction="read", tab_id=7, domain="https://x/apply")
    await ctx.repos.tasks.set_status(task["id"], TaskStatus.WAITING_USER, failure={"category": "AUTH_REQUIRED", "message": "log in", "retryable": False, "recommended_action": "log in"})

    # Still a login page → nothing happens.
    await hub.handle_incoming({"type": "tab_event", "tab_id": 7, "url": "https://x/login", "title": "Sign in", "active": True, "event": "updated"})
    await asyncio.sleep(0.05)
    assert (await ctx.repos.tasks.get(task["id"]))["status"] == "waiting_user"

    # User logged in; the page is a form now → the task resumes as a new task on the same tab.
    pages["login"] = False
    await hub.handle_incoming({"type": "tab_event", "tab_id": 7, "url": "https://x/apply", "title": "Apply", "active": True, "event": "updated"})
    for _ in range(50):
        await asyncio.sleep(0.05)
        old = await ctx.repos.tasks.get(task["id"])
        if old["status"] != "waiting_user":
            break
    assert old["status"] == "succeeded" and old["result_json"]["resumed_as"]
    new = await ctx.repos.tasks.get(old["result_json"]["resumed_as"])
    assert new is not None and new["tab_id"] == 7
    events, _ = await ctx.repos.audit.list(__import__("app.persistence.repositories", fromlist=["Page"]).Page(20, 0), search="task.auto_resumed")
    assert events
    await ctx.tasks.shutdown()
    await hub.detach(ws)  # type: ignore[arg-type]
