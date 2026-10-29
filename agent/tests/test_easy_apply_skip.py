"""LinkedIn Easy Apply jobs are left alone; LinkedIn jobs that apply on the company site are not."""

from __future__ import annotations

from app.applications.workflow import linkedin_easy_apply_only
from app.browser.snapshot import PageSnapshot, SnapshotNode


def test_easy_apply_button_on_linkedin_is_left_alone() -> None:
    snap = PageSnapshot(url="https://www.linkedin.com/jobs/view/123", title="Job", nodes=[
        SnapshotNode(i=0, role="button", name="Easy Apply to Backend Engineer at Acme", tag="button", visible=True),
    ])
    assert linkedin_easy_apply_only(snap)


def test_easy_apply_link_on_linkedin_search_page_is_left_alone() -> None:
    snap = PageSnapshot(url="https://www.linkedin.com/jobs/search/?currentJobId=123", title="Jobs", nodes=[
        SnapshotNode(i=0, role="link", name="Easy Apply", tag="a", href="https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true", visible=True),
    ])
    assert linkedin_easy_apply_only(snap)


def test_linkedin_job_applying_on_company_site_is_not_skipped() -> None:
    snap = PageSnapshot(url="https://www.linkedin.com/jobs/view/123", title="Job", nodes=[
        SnapshotNode(i=0, role="link", name="Apply on company website", tag="a", href="https://www.linkedin.com/safety/go?url=https%3A%2F%2Fjobs.lever.co%2Facme%2F1", visible=True),
    ])
    assert not linkedin_easy_apply_only(snap)


def test_easy_apply_off_linkedin_is_not_skipped() -> None:
    snap = PageSnapshot(url="https://www.dice.com/job-detail/1", title="Job", nodes=[
        SnapshotNode(i=0, role="button", name="Easy apply", tag="button", visible=True),
    ])
    assert not linkedin_easy_apply_only(snap)
