"""End-to-end application workflow against local fixtures through the TaskRunner."""

from __future__ import annotations

import asyncio

import pytest

from app.core.enums import ApplicationState

from .conftest import DASH_HEADERS, approve_all, fixture_url, register_resume, requires_browser, seed_profile, wait_task

pytestmark = [requires_browser, pytest.mark.asyncio]

CONSENT = {"category": "consent", "question": "I consent to the processing of my data as described in the privacy policy", "answer": "Yes", "provenance": "approved_answer"}
ACCURATE = {"category": "consent", "question": "I confirm the information above is accurate", "answer": "Yes", "provenance": "approved_answer"}
SPONSOR = {"category": "sponsorship", "question": "Will you require sponsorship now or in the future?", "answer": "No", "provenance": "approved_answer", "legally_significant": True}
AUTOMATION = {"category": "custom", "question": "Do you have test automation experience?", "answer": "Yes", "provenance": "approved_answer"}
FRAMEWORKS = {"category": "custom", "question": "Which frameworks?", "answer": "Playwright, pytest", "provenance": "approved_answer"}
SALARY = {"category": "salary", "question": "What are your salary expectations (EUR)?", "answer": "95000", "provenance": "approved_answer", "legally_significant": True}
SOURCE = {"category": "custom", "question": "How did you hear about this job?", "answer": "Company website", "provenance": "approved_answer"}


async def queue_fixture(ctx, rel: str, *, title: str = "Software Engineer", company: str = "Fixture Co"):
    job, dup = await ctx.jobs.import_url(fixture_url(rel), prefill={"title": title, "company": company})
    app = await ctx.repos.applications.get_by_job(job["id"])
    if app["state"] != ApplicationState.QUEUED:
        app = await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="test")
    return job, app


async def run_apply(ctx, app, *, approve: bool = True, timeout: float = 60.0):
    task = await ctx.repos.tasks.create("apply", application_id=app["id"], job_id=app["job_id"], domain=app["application_url"])
    await ctx.tasks.start(task)
    approver = asyncio.ensure_future(approve_all(ctx, timeout=timeout)) if approve else None
    t = await wait_task(ctx, task["id"], timeout=timeout)
    if approver:
        await approver
    return t, await ctx.repos.applications.get(app["id"])


async def test_simple_application_confirmed_with_evidence(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [CONSENT])
    job, app = await queue_fixture(ctx, "forms/simple_application.html")
    t, app = await run_apply(ctx, app)
    assert t["status"] == "succeeded", t["failure_json"]
    assert app["state"] == "APPLIED_CONFIRMED"
    assert app["verification_status"] == "confirmed"
    assert app["reference_number"] and app["reference_number"].startswith("ACME-")
    assert app["evidence_json"]["page_kind"] == "confirmation" and app["applied_at"]
    steps = app["fill_plan_json"]["steps"]
    filled = {f["label"] for f in steps[0]["filled"]}
    assert {"First name *", "Last name *", "Email *", "Resume / CV *", "Are you legally authorized to work in the United States? *", "Will you now or in the future require sponsorship for employment visa status? *", "Years of experience *"} <= filled
    provenance = {f["label"]: f["provenance"] for f in steps[0]["filled"]}
    assert provenance["First name *"] == "verified_fact"
    history = [h["to_state"] for h in await ctx.repos.applications.history(app["id"])]
    assert history[-4:] == ["IN_PROGRESS", "READY_FOR_REVIEW", "SUBMISSION_PENDING", "APPLIED_CONFIRMED"]
    approvals = await ctx.repos.approvals.list()
    assert approvals and approvals[0]["kind"] == "submit" and approvals[0]["status"] == "approved"
    attempts = await ctx.repos.tasks.attempts(t["id"])
    assert attempts and all(a["action_json"]["type"] != "navigate" or a["ok"] for a in attempts)
    assert (await ctx.repos.audit.list.__self__.db.fetch_value("SELECT COUNT(*) FROM audit_events WHERE event='policy.evaluated'")) >= len(attempts)


async def test_duplicate_application_is_prevented(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [CONSENT])
    job, app = await queue_fixture(ctx, "forms/simple_application.html")
    await run_apply(ctx, app)
    # Re-importing the same URL is a duplicate and cannot create a second application.
    job2, dup = await ctx.jobs.import_url(fixture_url("forms/simple_application.html"))
    assert dup and job2["id"] == job["id"]
    assert (await ctx.repos.applications.get(app["id"]))["state"] == "APPLIED_CONFIRMED"
    with pytest.raises(Exception):
        await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="again")


async def test_missing_required_answers_pause_for_user_input(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, {**sample_profile, "legal": {}}, resume["id"], [CONSENT])  # no work-auth / sponsorship facts
    job, app = await queue_fixture(ctx, "forms/simple_application.html")
    t, app = await run_apply(ctx, app)
    assert t["status"] == "waiting_user"
    assert app["state"] == "NEEDS_USER_INPUT"
    labels = {q["label"] for q in app["pending_questions_json"]}
    assert any("authorized to work" in l for l in labels) and any("sponsorship" in l for l in labels)
    assert all(q.get("legally_significant") for q in app["pending_questions_json"] if "label" in q and ("authorized" in q["label"] or "sponsorship" in q["label"]))
    # No submit approval was ever requested.
    assert not [a for a in await ctx.repos.approvals.list() if a["kind"] == "submit"]
    # Providing answers re-queues and the next run completes.
    for q in app["pending_questions_json"]:
        ans = "Yes" if "authorized" in q["label"] else "No"
        await ctx.repos.profile.upsert_answer({"category": "legal", "question": q["label"], "answer": ans, "provenance": "approved_answer", "legally_significant": True})
    app = await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="answers provided", actor="user")
    t2, app = await run_apply(ctx, app)
    assert t2["status"] == "succeeded" and app["state"] == "APPLIED_CONFIRMED"


async def test_unapproved_consent_pauses_for_review(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [])
    job, app = await queue_fixture(ctx, "forms/simple_application.html")
    t, app = await run_apply(ctx, app)
    assert app["state"] == "READY_FOR_REVIEW" and t["status"] == "waiting_user"
    assert any("consent" in q["label"].lower() for q in app["pending_questions_json"])


async def test_login_wall_triggers_auth_required(ctx, sample_profile) -> None:
    await seed_profile(ctx, sample_profile)
    job, app = await queue_fixture(ctx, "auth/login_required.html")
    t, app = await run_apply(ctx, app)
    assert t["status"] == "waiting_user" and t["failure_json"]["category"] == "AUTH_REQUIRED"
    assert app["state"] == "AUTH_REQUIRED"
    # Nothing was typed into the login form.
    assert all(a["action_json"]["type"] != "type" for a in await ctx.repos.tasks.attempts(t["id"]))


@pytest.mark.parametrize("rel", ["auth/captcha.html", "auth/otp.html"])
async def test_challenges_trigger_manual_handoff(ctx, sample_profile, rel: str) -> None:
    await seed_profile(ctx, sample_profile)
    job, app = await queue_fixture(ctx, rel)
    t, app = await run_apply(ctx, app)
    assert t["status"] == "waiting_user" and t["failure_json"]["category"] == "HUMAN_VERIFICATION_REQUIRED"
    assert app["state"] == "AUTH_REQUIRED"


async def test_resume_after_auth_handoff(ctx, sample_profile, client) -> None:
    await seed_profile(ctx, sample_profile)
    job, app = await queue_fixture(ctx, "auth/login_required.html")
    t, app = await run_apply(ctx, app)
    assert app["state"] == "AUTH_REQUIRED"
    new = await ctx.tasks.resume_task(t["id"])
    assert (await ctx.repos.tasks.get(t["id"]))["status"] == "succeeded"
    t2 = await wait_task(ctx, new["id"])
    assert t2["status"] == "waiting_user"  # still a login page: handed off again, never bypassed


async def test_rejected_submission_is_recorded_not_claimed(ctx, sample_profile) -> None:
    await seed_profile(ctx, sample_profile)
    job, app = await queue_fixture(ctx, "forms/rejects_submission.html")
    t, app = await run_apply(ctx, app)
    assert t["status"] == "failed" and t["failure_json"]["category"] == "SUBMISSION_REJECTED"
    assert app["state"] == "APPLICATION_FAILED" and app["verification_status"] == "rejected"
    assert "no longer accepting" in app["evidence_json"]["error_messages"][0]


async def test_ambiguous_outcome_is_uncertain_and_blocks_retry(ctx, sample_profile, client) -> None:
    await seed_profile(ctx, sample_profile)
    job, app = await queue_fixture(ctx, "forms/ambiguous_outcome.html")
    t, app = await run_apply(ctx, app)
    assert t["status"] == "failed" and t["failure_json"]["category"] == "SUBMISSION_UNCERTAIN"
    assert app["state"] == "APPLICATION_FAILED" and app["verification_status"] == "uncertain"
    # The API refuses a blind retry until the outcome is reconciled.
    r = client.post(f"/api/applications/{app['id']}/transition", json={"action": "retry"}, headers=DASH_HEADERS)
    assert r.status_code == 409
    r = client.post(f"/api/applications/{app['id']}/transition", json={"action": "reconcile_confirmed", "reference_number": "MANUAL-1"}, headers=DASH_HEADERS)
    assert r.status_code == 200 and r.json()["state"] == "APPLIED_CONFIRMED" and r.json()["evidence_json"]["source"] == "user_reconciliation"


async def test_submission_rejected_when_user_declines_approval(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [CONSENT])
    job, app = await queue_fixture(ctx, "forms/simple_application.html")
    task = await ctx.repos.tasks.create("apply", application_id=app["id"], job_id=job["id"], domain=app["application_url"])
    await ctx.tasks.start(task)
    for _ in range(300):
        pending = await ctx.repos.approvals.pending()
        if pending:
            await ctx.approvals.decide(pending[0]["id"], "rejected")
            break
        await asyncio.sleep(0.1)
    t = await wait_task(ctx, task["id"])
    app = await ctx.repos.applications.get(app["id"])
    assert t["status"] == "waiting_user" and app["state"] == "READY_FOR_REVIEW"
    assert app["verification_status"] == "not_attempted"


async def test_greenhouse_like_form(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [SALARY, SOURCE, {"category": "legal", "question": "Are you legally authorized to work in Germany?", "answer": "Yes", "provenance": "approved_answer", "legally_significant": True}])
    job, app = await queue_fixture(ctx, "forms/greenhouse_like.html", title="Backend Engineer", company="Nimbus Cloud")
    t, app = await run_apply(ctx, app)
    assert t["status"] == "succeeded", t["failure_json"]
    assert app["state"] == "APPLIED_CONFIRMED"
    filled = {f["label"] for f in app["fill_plan_json"]["steps"][0]["filled"]}
    assert {"First Name *", "Last Name *", "Email *", "Resume/CV *", "What are your salary expectations (EUR)? *"} <= filled
    skipped = {s["label"]: s["status"] for s in app["fill_plan_json"]["steps"][0]["skipped"]}
    assert skipped.get("Gender") == "needs_input"  # optional EEOC question: reported, left blank, never blocks


async def test_lever_like_form_with_radio_group_and_hidden_upload(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [SPONSOR])
    job, app = await queue_fixture(ctx, "forms/lever_like.html", title="Data Engineer", company="Orbit Labs")
    t, app = await run_apply(ctx, app)
    assert t["status"] == "succeeded", t["failure_json"]
    assert app["state"] == "APPLIED_CONFIRMED"
    assert any("Application submitted" in m for m in app["evidence_json"]["success_messages"])
    filled = {f["label"]: f for f in app["fill_plan_json"]["steps"][0]["filled"]}
    assert filled["Full name✱"]["observed"] == "Ada Testperson"
    assert "Will you require sponsorship now or in the future?✱" in filled or "Will you require sponsorship now or in the future?" in filled


async def test_multi_step_wizard_with_dynamic_field(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [ACCURATE, AUTOMATION, FRAMEWORKS])
    job, app = await queue_fixture(ctx, "forms/multi_step.html", title="QA Engineer", company="Stepwise")
    t, app = await run_apply(ctx, app)
    assert t["status"] == "succeeded", t["failure_json"]
    assert app["state"] == "APPLIED_CONFIRMED" and app["reference_number"] == "SW-4471"
    steps = app["fill_plan_json"]["steps"]
    assert len(steps) >= 3
    all_filled = {f["label"] for s in steps for f in s["filled"]}
    assert "Which frameworks? *" in all_filled and "Resume *" in all_filled


async def test_pause_all_stops_running_task_promptly(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [CONSENT])
    job, app = await queue_fixture(ctx, "forms/simple_application.html")
    task = await ctx.repos.tasks.create("apply", application_id=app["id"], job_id=job["id"], domain=app["application_url"])
    await ctx.tasks.start(task)
    await asyncio.sleep(0.3)
    await ctx.tasks.pause_all()
    t = await wait_task(ctx, task["id"], timeout=15)
    assert t["status"] == "cancelled"
    assert (await ctx.repos.applications.get(app["id"]))["state"] in ("APPLICATION_FAILED", "RETRY_SCHEDULED")
    await ctx.tasks.resume_all()


async def test_extract_task_imports_and_scores_job(ctx, sample_profile) -> None:
    await seed_profile(ctx, sample_profile)
    task = await ctx.repos.tasks.create("extract_job", instruction="import this job", domain=fixture_url("jobs/jsonld_posting.html"))
    await ctx.tasks.start(task)
    t = await wait_task(ctx, task["id"])
    assert t["status"] == "succeeded", t["failure_json"]
    job = await ctx.repos.jobs.get(t["result_json"]["job_id"])
    assert job["title"] == "Senior Python Developer" and job["match_score"] is not None
    assert (await ctx.repos.applications.get_by_job(job["id"]))["state"] == "ELIGIBLE"


async def test_read_task_uses_bounded_general_loop(ctx) -> None:
    task = await ctx.repos.tasks.create("read", instruction="summarise this page", domain=fixture_url("jobs/plain_posting.html"))
    await ctx.tasks.start(task)
    t = await wait_task(ctx, task["id"])
    assert t["status"] == "succeeded" and "Lumen Health" in t["result_json"]["extracted"]["text"]
    assert t["action_count"] <= 3
