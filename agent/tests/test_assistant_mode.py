from __future__ import annotations

import json

import pytest

from app.agent.intent import IntentRouter
from app.agent.page_reader import page_text
from app.browser.snapshot import PageSnapshot, SnapshotNode
from app.models.base import ModelRequest
from app.models.heuristic import HeuristicProvider


@pytest.mark.asyncio
async def test_job_commands_still_route_and_everything_else_is_ask(ctx) -> None:
    r = IntentRouter(ctx.gateway)
    jobs = {
        "Apply to this job": "apply",
        "Can you apply to this job for me?": "apply",
        "Fill this application form with my profile": "fill_page",
        "Fill this page": "fill_page",
        "fill everything it asks": "fill_page",
        "Fill and submit this application": "apply",
        "Apply to each job on this list": "apply_list",
        "Dry run: apply to each job on this list": "apply_list",
        "Track my applications": "track",
        "Find contacts for these jobs": "contacts",
        "Extract this job and score it": "extract_job",
    }
    for text, kind in jobs.items():
        assert (await r.route(text, None)).kind == kind, text
    for text in [
        "Summarise this page",
        "What is the application deadline?",
        "How many years of experience does this job need?",
        "Draft an email reply to this message",
        "Explain this PDF in simple words",
        "Compare the two plans on this page",
        "translate this to Hindi",
    ]:
        assert (await r.route(text, None)).kind == "ask", text


def test_this_job_wording_keeps_a_single_application() -> None:
    from app.api.tasks import _THIS_JOB_RE

    for text in ["Apply to this job", "apply to the job on this page", "Apply here", "apply for this role", "Apply to this position please"]:
        assert _THIS_JOB_RE.search(text), text
    for text in ["Apply to each job on this list", "apply to all jobs", "Apply to the first 5 jobs"]:
        assert not _THIS_JOB_RE.search(text), text


def test_list_order_redirects_and_non_posting_links() -> None:
    from app.jobs.listing import account_needed_rank, extract_job_links, unwrap_redirect

    urls = ["https://www.linkedin.com/jobs/view/1", "https://acme.com/careers/backend-engineer-123", "https://jobs.lever.co/acme/abc", "https://acme.wd1.myworkdayjobs.com/x/job/1"]
    assert sorted(urls, key=account_needed_rank) == [urls[2], urls[1], urls[0], urls[3]]
    assert unwrap_redirect("https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fglobal-sas.icims.com%2Fjobs%2F4&urlhash=x") == "https://global-sas.icims.com/jobs/4"
    assert unwrap_redirect("https://www.linkedin.com/jobs/view/1") is None
    snap = PageSnapshot(url="https://jobs.lever.co/mendix/abc/apply", title="Apply", nodes=[
        SnapshotNode(i=0, role="link", name="Job seeker support", tag="a", href="https://www.lever.co/job-seeker-support/", visible=True),
        SnapshotNode(i=1, role="link", name="Mendix jobs", tag="a", href="https://www.mendix.com/job-openings/", visible=True),
        SnapshotNode(i=2, role="link", name="Backend Engineer", tag="a", href="https://jobs.lever.co/mendix/123-backend", visible=True),
    ])
    assert [l["url"] for l in extract_job_links(snap)] == ["https://jobs.lever.co/mendix/123-backend"]


def test_board_search_box_is_not_an_application_form() -> None:
    from app.applications.workflow import _form_is_application

    board = PageSnapshot(url="https://www.arbeitnow.com/jobs/x", title="Job", nodes=[
        SnapshotNode(i=0, role="searchbox", name="Search jobs", tag="input", type="search", visible=True),
        SnapshotNode(i=1, role="textbox", name="Email", tag="input", type="email", placeholder="Get job alerts", visible=True),
    ])
    form = PageSnapshot(url="https://jobs.lever.co/a/b/apply", title="Apply", nodes=[
        SnapshotNode(i=0, role="textbox", name="Full name", tag="input", type="text", visible=True),
        SnapshotNode(i=1, role="textbox", name="Email", tag="input", type="email", visible=True),
        SnapshotNode(i=2, role="file", name="Resume/CV", tag="input", type="file", visible=True),
    ])
    assert not _form_is_application(board)
    assert _form_is_application(form)


def test_workday_popup_choice_beats_the_apply_button_behind_it() -> None:
    from app.applications.workflow import find_apply_button

    snap = PageSnapshot(url="https://acme.wd1.myworkdayjobs.com/x/job/1", title="Job", nodes=[
        SnapshotNode(i=0, role="button", name="Apply", tag="a", visible=True),
        SnapshotNode(i=1, role="button", name="Autofill with Resume", tag="a", visible=True, in_dialog=True),
        SnapshotNode(i=2, role="button", name="Apply Manually", tag="a", visible=True, in_dialog=True),
    ])
    assert find_apply_button(snap).i == 2
    no_popup = PageSnapshot(url="https://x", title="Job", nodes=[SnapshotNode(i=0, role="button", name="Apply", tag="a", visible=True)])
    assert find_apply_button(no_popup).i == 0


def test_resume_keeps_what_the_user_typed() -> None:
    from app.applications.field_mapping import FieldPlan, FillPlan
    from app.applications.workflow import keep_page_answers

    plan = FillPlan(fields=[
        FieldPlan(node=0, label="Current CTC", field_type="text", required=True, category="legal", status="needs_input", current_value="12 LPA"),
        FieldPlan(node=1, label="Notice period", field_type="text", required=True, category="preferences", status="auto", value="Immediately", current_value="15 days"),
        FieldPlan(node=2, label="Email", field_type="email", required=True, category="contact", status="auto", value="a@b.c", current_value=""),
        FieldPlan(node=3, label="Country", field_type="select", required=True, category="contact", status="needs_input", current_value="Select…"),
    ])
    assert keep_page_answers(plan) == 2
    assert [f.status for f in plan.fields] == ["skip", "skip", "auto", "needs_input"]
    assert not [f for f in plan.blocking if f.node == 0]


def test_linkedin_long_apply_button_footer_links_and_single_posting() -> None:
    from app.api.tasks import _SINGLE_POSTING_URL
    from app.applications.workflow import find_apply_button
    from app.jobs.listing import extract_job_links

    snap = PageSnapshot(url="https://www.linkedin.com/jobs/view/4470355543/", title="Job", nodes=[
        SnapshotNode(i=0, role="searchbox", name="Search jobs", tag="input", type="search", visible=True),
        SnapshotNode(i=1, role="button", name="Apply to Software Engineer on company website", tag="button", visible=True),
        SnapshotNode(i=2, role="link", name="Apply resources", tag="a", href="https://www.linkedin.com/job-apply-resources/?jobPostingId=447035554", visible=True),
        SnapshotNode(i=3, role="link", name="Job tracker", tag="a", href="https://www.linkedin.com/jobs-tracker/?stage=clicked_apply", visible=True),
        SnapshotNode(i=4, role="link", name="Backend Engineer", tag="a", href="https://www.linkedin.com/jobs/view/4471428550/", visible=True),
    ])
    assert find_apply_button(snap).i == 1
    assert [l["url"] for l in extract_job_links(snap)] == ["https://www.linkedin.com/jobs/view/4471428550/"]
    assert _SINGLE_POSTING_URL.search("https://www.linkedin.com/jobs/view/4470355543/?trackingId=x")
    assert not _SINGLE_POSTING_URL.search("https://www.linkedin.com/jobs/search/?keywords=dev")


@pytest.mark.asyncio
async def test_navigator_picks_apply_and_job_links(ctx) -> None:
    from app.agent.navigator import choose_apply_click, choose_job_links

    page = PageSnapshot(url="https://careers.fisglobal.com/us/en/job/1", title="Junior Developer", nodes=[
        SnapshotNode(i=0, role="link", name="Sign in", tag="a", href="https://careers.fisglobal.com/login", visible=True),
        SnapshotNode(i=1, role="button", name="Share", tag="button", visible=True),
        SnapshotNode(i=2, role="button", name="Apply Now - Junior Developer, Pune", tag="button", visible=True),
    ])
    pick = await choose_apply_click(ctx.gateway, page)
    assert pick is not None and pick.i == 2
    board = PageSnapshot(url="https://board.example.test/", title="Jobs", nodes=[
        SnapshotNode(i=0, role="link", name="About", tag="a", href="https://board.example.test/about", visible=True),
        SnapshotNode(i=1, role="link", name="↗", tag="a", href="https://acme.wd1.myworkdayjobs.com/x/job/123", visible=True),
        SnapshotNode(i=2, role="link", name="↗", tag="a", href="https://jobs.lever.co/acme/abc-123", visible=True),
    ])
    links = await choose_job_links(ctx.gateway, board)
    assert [l["url"] for l in links] == ["https://acme.wd1.myworkdayjobs.com/x/job/123", "https://jobs.lever.co/acme/abc-123"]


def test_bad_model_picks_are_rejected_and_login_popups_detected() -> None:
    from app.agent.navigator import _leads_to_applying
    from app.applications.workflow import _account_gate

    page = PageSnapshot(url="https://in.linkedin.com/jobs/view/x", title="Job", nodes=[])
    assert not _leads_to_applying(SnapshotNode(i=0, role="link", name="Back to Jobs", tag="a", href="https://in.linkedin.com/jobs", visible=True), page)
    assert not _leads_to_applying(SnapshotNode(i=1, role="link", name="How do employees rate Cognyte?", tag="a", href="https://www.glassdoor.com/Reviews/x", visible=True), page)
    assert _leads_to_applying(SnapshotNode(i=2, role="button", name="I'm interested", tag="button", visible=True), page)
    assert _leads_to_applying(SnapshotNode(i=3, role="link", name="Cognyte careers", tag="a", href="https://careers.cognyte.com/job/123", visible=True), page)
    gate = PageSnapshot(url="https://wellfound.com/jobs/1", title="Job", nodes=[
        SnapshotNode(i=0, role="button", name="Log In", tag="button", visible=True, in_dialog=True),
        SnapshotNode(i=1, role="button", name="Sign up", tag="button", visible=True, in_dialog=True),
        SnapshotNode(i=2, role="button", name="Continue with Google", tag="button", visible=True, in_dialog=True),
    ])
    assert _account_gate(gate)
    assert not _account_gate(PageSnapshot(url="https://x", title="Job", nodes=[SnapshotNode(i=0, role="link", name="Sign in", tag="a", visible=True)]))


def test_embedded_comeet_form_and_account_boards() -> None:
    from app.applications.workflow import _ACCOUNT_BOARD_RE
    from app.browser.snapshot import PageSignals
    from app.jobs.listing import embedded_application_url

    snap = PageSnapshot(url="https://www.cognyte.com/careers/job/1", title="Job", nodes=[], signals=PageSignals(iframe_srcs=["https://consentcdn.cookiebot.com/sdk/bc-v4.min.html", "https://www.comeet.co/jobs/F2.009/61.473/apply?token=X"]))
    assert embedded_application_url(snap) == "https://www.comeet.co/jobs/F2.009/61.473/apply?token=X"
    for url in ["https://wellfound.com/jobs/4377418-software-engineer", "https://smartapply.indeed.com/beta/indeedapply/form/x", "https://www.foundit.in/rio/login/seeker"]:
        assert _ACCOUNT_BOARD_RE.search(url), url
    assert not _ACCOUNT_BOARD_RE.search("https://jobs.lever.co/acme/123")


def test_page_text_collects_headings_text_and_filled_values() -> None:
    snap = PageSnapshot(
        url="https://example.test/job",
        title="DevOps Engineer",
        nodes=[
            SnapshotNode(i=0, role="heading", name="DevOps Engineer", tag="h1", visible=True, text="DevOps Engineer"),
            SnapshotNode(i=1, role="textbox", name="Name", tag="input", value="Manik", visible=True),
        ],
        text_excerpt="Requires 3+ years with Kubernetes. Location: State College, PA.",
    )
    t = page_text(snap)
    assert "DevOps Engineer" in t and "Kubernetes" in t and "Name: Manik" in t


@pytest.mark.asyncio
async def test_offline_fallback_answers_from_the_page() -> None:
    text = "Granola builds AI notes. The salary range is £70k to £90k. Office is in Old Street, London."
    resp = await HeuristicProvider().generate(ModelRequest(task="ask", system="", prompt="", json_schema={}, inputs={"question": "What is the salary range?", "text": text}), "")
    assert "£70k" in json.loads(resp.text)["answer"]
