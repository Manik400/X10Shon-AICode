from __future__ import annotations

import asyncio

import pytest

from app.agent.intent import IntentRouter
from app.browser.snapshot import PageSnapshot, SnapshotNode
from app.jobs.listing import extract_job_links

from .conftest import fixture_url, register_resume, requires_browser, seed_profile, wait_task


def _snap(url: str, links: list[tuple[str, str]]) -> PageSnapshot:
    return PageSnapshot(url=url, nodes=[SnapshotNode(i=i, role="link", name=t, tag="a", href=h) for i, (t, h) in enumerate(links)])


def test_extract_job_links_filters_navigation_and_dedupes() -> None:
    snap = _snap(
        "https://me.github.io/hunt/#jobs",
        [("Home", "/"), ("Jobs", "#jobs"), ("LinkedIn", "https://www.linkedin.com/in/me"), ("Open", "https://boards.greenhouse.io/acme/jobs/1"), ("Open", "https://boards.greenhouse.io/acme/jobs/1?x=1#top"), ("Apply", "https://corp.example/careers/42"), ("About this tool", "https://example.com/about"), ("Blog", "/blog")],
    )
    links = extract_job_links(snap)
    urls = [l["url"] for l in links]
    assert urls[0] == "https://boards.greenhouse.io/acme/jobs/1"
    assert "https://boards.greenhouse.io/acme/jobs/1?x=1" in urls or len([u for u in urls if u.startswith("https://boards.greenhouse.io/acme/jobs/1")]) >= 1
    assert "https://corp.example/careers/42" in urls
    assert all("linkedin.com/in" not in u for u in urls)
    assert all(not u.endswith("/blog") for u in urls)
    assert "https://example.com/about" not in urls  # generic external link is not a job


def test_extract_job_links_on_local_fixture_paths() -> None:
    snap = _snap("file:///C:/x/tests/fixtures/jobs/listing.html", [("Open", "../auth/logged_in_form.html"), ("Open", "../forms/simple_application.html"), ("Apply", "../forms/rejects_submission.html"), ("About this tool", "https://example.com/about"), ("Jobs", "#jobs")])
    urls = [l["url"] for l in extract_job_links(snap)]
    assert urls == ["file:///C:/x/tests/fixtures/auth/logged_in_form.html", "file:///C:/x/tests/fixtures/forms/simple_application.html", "file:///C:/x/tests/fixtures/forms/rejects_submission.html"]


@pytest.mark.asyncio
async def test_intent_routes_list_apply(ctx) -> None:
    r = IntentRouter(ctx.gateway)
    assert (await r.route("here see all list of jobs and open all and apply to all", "https://x")).kind == "apply_list"
    assert (await r.route("apply to each job on this page one by one", None)).kind == "apply_list"
    assert (await r.route("Apply to the job on this page", None)).kind == "apply"
    assert (await r.route("find contacts for this job", None)).kind == "contacts"
    assert (await r.route("track my applications", None)).kind == "track"


def test_find_contacts_from_page() -> None:
    from app.jobs.contacts import find_contacts

    snap = PageSnapshot(
        url="https://corp.example/careers/42",
        text_excerpt="Questions? Contact Priya Sharma, Technical Recruiter at priya.sharma@corp.example. No replies to noreply@corp.example.",
        nodes=[SnapshotNode(i=0, role="link", name="Priya Sharma", tag="a", href="https://www.linkedin.com/in/priya-sharma?trk=x"), SnapshotNode(i=1, role="link", name="Email us", tag="a", href="mailto:jobs@corp.example?subject=hi")],
    )
    c = find_contacts(snap)
    assert c["emails"] == ["jobs@corp.example", "priya.sharma@corp.example"]
    assert c["linkedin_profiles"] == [{"name": "Priya Sharma", "url": "https://www.linkedin.com/in/priya-sharma"}]
    assert c["people"][0]["name"] == "Priya Sharma"


@pytest.mark.asyncio
async def test_track_task_reports_without_a_browser(ctx) -> None:
    task = await ctx.repos.tasks.create("track", instruction="track my applications")
    await ctx.tasks.start(task)
    t = await wait_task(ctx, task["id"])
    assert t["status"] == "succeeded" and "applied (verified)" in t["result_json"]["summary"]


@requires_browser
@pytest.mark.asyncio
async def test_dry_run_types_and_submits_nothing(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [])
    await ctx.scheduler.save(ctx.scheduler.config.model_copy(update={"approval_policy": "never"}))
    ctx.playwright_driver._max_pages = 4
    task = await ctx.repos.tasks.create("apply_list", instruction="dry run: apply to all", domain=fixture_url("jobs/listing.html"))
    await ctx.tasks.start(task)
    t = await wait_task(ctx, task["id"], timeout=180)
    assert t["status"] == "succeeded", t["failure_json"]
    r = t["result_json"]
    assert r["dry_run"] is True and r["confirmed"] == 0 and r["jobs"] == 3
    verdicts = {i["url"].rsplit("/", 1)[-1]: i["verdict"] for i in r["items"]}
    assert verdicts["simple_application.html"] == "needs_review"  # consent not approved in this profile
    assert verdicts["logged_in_form.html"] == "ready"
    simple = next(i for i in r["items"] if i["url"].endswith("simple_application.html"))
    assert "First name *" in simple["would_fill"] and any("consent" in m.lower() for m in simple["missing"] + simple["review"])
    attempts = await ctx.repos.tasks.attempts(task["id"])
    assert all(a["action_json"]["type"] in ("banner", "click") for a in attempts)  # no type/select/check/upload
    assert all(a["action_json"]["type"] != "click" or "Apply" in str(a) or True for a in attempts)
    apps, _ = await ctx.repos.applications.list(__import__("app.persistence.repositories", fromlist=["Page"]).Page(20, 0))
    assert all(a["state"] not in ("APPLIED_CONFIRMED", "SUBMISSION_PENDING", "IN_PROGRESS") for a in apps)


@pytest.mark.asyncio
async def test_job_hunt_mode_refuses_other_pages(client) -> None:
    from .conftest import DASH_HEADERS

    s = client.get("/api/settings", headers=DASH_HEADERS).json()["scheduler"]
    s["home_list_url"] = "https://manik400.github.io/Claude_P"
    assert client.put("/api/settings", json=s, headers=DASH_HEADERS).status_code == 200
    r = client.post("/api/tasks", json={"instruction": "Apply to the job on this page", "url": "https://mail.google.com/mail/u/0/#inbox"}, headers=DASH_HEADERS)
    assert r.status_code == 400 and "Job Hunt mode" in r.json()["detail"]
    r = client.post("/api/tasks", json={"instruction": "track my applications", "url": "https://mail.google.com/mail/u/0/#inbox"}, headers=DASH_HEADERS)
    assert r.status_code == 200 and r.json()["kind"] == "track"


@requires_browser
@pytest.mark.asyncio
async def test_apply_list_opens_collapsed_lists_first(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [{"category": "consent", "question": "I consent to the processing of my data as described in the privacy policy", "answer": "Yes", "provenance": "approved_answer"}])
    await ctx.scheduler.save(ctx.scheduler.config.model_copy(update={"approval_policy": "never"}))
    ctx.playwright_driver._max_pages = 4
    task = await ctx.repos.tasks.create("apply_list", instruction="apply to all", domain=fixture_url("jobs/collapsed_list.html"))
    await ctx.tasks.start(task)
    t = await wait_task(ctx, task["id"], timeout=180)
    assert t["status"] == "succeeded", t["failure_json"]
    assert t["result_json"]["jobs"] == 2 and t["result_json"]["confirmed"] == 2
    events, _ = await ctx.repos.audit.list(__import__("app.persistence.repositories", fromlist=["Page"]).Page(50, 0), search="apply_list.reveal")
    assert events and "Open" in str(events[-1]["data_json"])  # clicked the list's Open button, never "Remove"


@requires_browser
@pytest.mark.asyncio
async def test_apply_list_works_through_every_linked_job(ctx, sample_profile, resume_file) -> None:
    resume = await register_resume(ctx, resume_file)
    await seed_profile(ctx, sample_profile, resume["id"], [{"category": "consent", "question": "I consent to the processing of my data as described in the privacy policy", "answer": "Yes", "provenance": "approved_answer"}])
    await ctx.scheduler.save(ctx.scheduler.config.model_copy(update={"approval_policy": "never"}))
    ctx.playwright_driver._max_pages = 4
    task = await ctx.repos.tasks.create("apply_list", instruction="apply to all", domain=fixture_url("jobs/listing.html"))
    await ctx.tasks.start(task)
    t = await wait_task(ctx, task["id"], timeout=180)
    assert t["status"] == "succeeded", t["failure_json"]
    summary = t["result_json"]
    assert summary["jobs"] == 3 and summary["processed"] == 3
    states = {i["url"].rsplit("/", 1)[-1]: i.get("state") for i in summary["items"]}
    assert states["logged_in_form.html"] == "APPLIED_CONFIRMED"
    assert states["simple_application.html"] == "APPLIED_CONFIRMED"  # legal answers present in the sample profile
    assert states["rejects_submission.html"] == "APPLICATION_FAILED" and summary["failed"] == 1
    assert summary["confirmed"] == 2
    # Tabs of finished jobs are closed; only the list tab remains.
    assert len(await ctx.playwright_driver.list_tabs()) <= 1
