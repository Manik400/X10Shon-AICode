from __future__ import annotations

import pytest

from app.jobs.matching import JobFilters, apply_filters, deterministic_score

JOB = {
    "title": "Senior Backend Engineer",
    "company": "Nimbus Cloud",
    "location": "Berlin, Germany",
    "remote_type": "hybrid",
    "employment_type": "full-time",
    "experience_min_years": 4,
    "salary_min": 80000,
    "salary_max": 100000,
    "description": "We need Python, FastAPI and PostgreSQL. We are unable to sponsor visas at this time.",
    "requirements": ["4+ years of experience with Go or Python", "Kubernetes"],
}


def test_filters_pass_when_empty() -> None:
    assert apply_filters(JOB, JobFilters()).passed


def test_mandatory_filters_reject_with_reasons() -> None:
    f = JobFilters(titles=["Frontend"], excluded_keywords=["kubernetes"], max_experience_years=3, remote_types=["remote"], salary_min=120000, requires_sponsorship=True, required_skills=["Rust"], excluded_companies=["nimbus"])
    res = apply_filters(JOB, f)
    assert not res.passed
    joined = " ".join(res.reasons)
    for frag in ("title", "excluded keywords", "requires 4", "work arrangement", "salary", "sponsorship", "Rust", "excluded"):
        assert frag in joined, frag


def test_location_filter_skipped_for_remote_jobs() -> None:
    f = JobFilters(locations=["Austin"])
    assert apply_filters({**JOB, "remote_type": "remote"}, f).passed
    assert not apply_filters(JOB, f).passed


def test_score_has_evidence_and_bounds(sample_profile) -> None:
    score, ev = deterministic_score(JOB, sample_profile, JobFilters())
    assert 0 <= score <= 1
    assert set(ev["skills_matched"]) == {"Python", "FastAPI", "PostgreSQL"}
    assert ev["experience_score"] == 1.0
    assert ev["title_score"] == 1.0  # preferred title "Backend Engineer" is a substring


def test_score_penalizes_missing_experience(sample_profile) -> None:
    low, _ = deterministic_score({**JOB, "experience_min_years": 10}, sample_profile, JobFilters())
    high, _ = deterministic_score(JOB, sample_profile, JobFilters())
    assert low < high


@pytest.mark.asyncio
async def test_screen_marks_eligible_or_skipped_and_explains(ctx, sample_profile) -> None:
    await ctx.repos.profile.save(sample_profile)
    job, _ = await ctx.jobs.import_url("https://x.example/j/1", prefill={"title": "Backend Engineer", "company": "X", "description": "Python FastAPI PostgreSQL"})
    app = await ctx.repos.applications.get_by_job(job["id"])
    assert app["state"] == "ELIGIBLE"
    assert job["match_explanation_json"]["evidence"]["skills_matched"]
    await ctx.jobs.save_filters(JobFilters(min_match_score=0.99))
    res = await ctx.jobs.screen(job["id"])
    assert res["passed"] is False
    assert any("below minimum" in r for r in res["reasons"])
    assert res["application"]["state"] == "SKIPPED"
