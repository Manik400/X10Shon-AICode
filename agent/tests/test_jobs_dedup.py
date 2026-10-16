from __future__ import annotations

import pytest

from app.applications.identity import identity_key_for
from app.jobs.canonical import canonicalize_url, dedup_key, detect_requisition, fuzzy_key, normalize_title

from .conftest import FIXTURES


def test_canonicalize_strips_tracking_and_normalizes() -> None:
    a = canonicalize_url("https://www.Example.com/jobs/123/?utm_source=x&gh_src=y&ref=z")
    b = canonicalize_url("http://example.com:80/jobs//123")
    assert a == "https://example.com/jobs/123"
    assert b == "http://example.com/jobs/123"  # scheme preserved so http-only sites stay reachable
    assert dedup_key(a) == dedup_key(b)  # but http/https variants are the same job
    assert canonicalize_url("http://127.0.0.1:8765/fixtures/x.html") == "http://127.0.0.1:8765/fixtures/x.html"


def test_canonicalize_keeps_significant_query_sorted() -> None:
    assert canonicalize_url("https://x.com/apply?b=2&a=1&utm_medium=m") == "https://x.com/apply?a=1&b=2"


def test_requisition_detection() -> None:
    assert detect_requisition("https://boards.greenhouse.io/acme/jobs/4011223344") == ("greenhouse", "4011223344")
    assert detect_requisition("https://jobs.lever.co/acme/1b2c3d4e-5f60-4718-9abc-def012345678/apply") == ("lever", "1b2c3d4e-5f60-4718-9abc-def012345678")
    assert detect_requisition("https://x.com/careers?jobid=77") == (None, "77")
    assert detect_requisition("https://x.com/careers/abc") == (None, None)


def test_dedup_key_uses_requisition_over_url_variants() -> None:
    k1 = dedup_key(canonicalize_url("https://boards.greenhouse.io/acme/jobs/1?gh_src=a"))
    k2 = dedup_key(canonicalize_url("https://job-boards.greenhouse.io/acme/jobs/1"))
    assert k1 == k2
    assert dedup_key("https://a.com/x") != dedup_key("https://a.com/y")


def test_fuzzy_key_ignores_seniority_noise_and_suffixes() -> None:
    assert normalize_title("Senior Software Engineer (Remote)") == "software engineer"
    assert fuzzy_key("Acme Inc.", "Sr. Software Engineer", "Austin") == fuzzy_key("ACME", "Software Engineer", "austin")


def test_identity_key_stable_and_configurable() -> None:
    job = {"company": "Acme Inc", "requisition_id": "123", "apply_url": "https://boards.greenhouse.io/acme/jobs/123"}
    assert identity_key_for(job) == identity_key_for({**job, "company": "ACME"})
    assert identity_key_for(job, fields=["company", "title"]) != identity_key_for(job)


@pytest.mark.asyncio
async def test_import_same_job_twice_is_duplicate(ctx, offline_adapters) -> None:
    j1, d1 = await ctx.jobs.import_url("https://boards.greenhouse.io/nimbuscloud/jobs/4011223344?gh_src=x")
    j2, d2 = await ctx.jobs.import_url("https://www.boards.greenhouse.io/nimbuscloud/jobs/4011223344/?utm_source=li")
    assert d1 is False and d2 is True
    assert j1["id"] == j2["id"]
    assert j1["title"] == "Backend Engineer" and j1["extraction_method"] == "api"
    assert (await ctx.repos.jobs.counts())["total"] == 1


@pytest.mark.asyncio
async def test_fuzzy_duplicate_marks_record_duplicate(ctx) -> None:
    j1, _ = await ctx.jobs.import_url("https://a.example/careers/1", prefill={"company": "Acme Inc", "title": "Software Engineer", "location": "Austin"})
    j2, dup = await ctx.jobs.import_url("https://b.example/jobs/9", prefill={"company": "ACME", "title": "Senior Software Engineer", "location": "Austin"})
    assert dup is True
    assert j2["duplicate_of"] == j1["id"]
    app = await ctx.repos.applications.get_by_job(j2["id"])
    assert app["state"] == "DUPLICATE"


@pytest.mark.asyncio
async def test_csv_import_reports_duplicates_and_errors(ctx, offline_adapters) -> None:
    res = await ctx.jobs.import_csv((FIXTURES / "jobs" / "sample_jobs.csv").read_text())
    assert res["imported"] == 3
    assert res["duplicates"] == 1
    assert res["errors"] == []


@pytest.mark.asyncio
async def test_lever_source_discovery_offline(ctx, offline_adapters) -> None:
    src = await ctx.repos.jobs.add_source("lever", "Orbit", {"site": "orbitlabs", "company": "Orbit Labs"})
    res = await ctx.jobs.run_source(src["id"])
    assert res["listed"] == 1 and res["imported"] == 1
    again = await ctx.jobs.run_source(src["id"])
    assert again["duplicates"] == 1 and again["imported"] == 0
