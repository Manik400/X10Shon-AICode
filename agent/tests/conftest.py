"""Shared fixtures: isolated settings/data dir, app context, API client, fixture URLs, Playwright."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
FIXTURES = ROOT / "tests" / "fixtures"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from app.config import Settings, set_settings  # noqa: E402
from app.state import AppContext, build_context  # noqa: E402

DASH_HEADERS = {"X-LFA-Client": "dashboard"}
EXT_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def fixture_url(rel: str) -> str:
    return (FIXTURES / rel).resolve().as_uri()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", trusted_hosts=["testserver"], per_domain_delay_seconds=0.0, task_deadline_seconds=120, action_timeout_seconds=10, log_level="WARNING")
    set_settings(s)
    return s


@pytest_asyncio.fixture
async def ctx(settings: Settings) -> Any:
    c = await build_context(settings)
    try:
        yield c
    finally:
        await c.tasks.shutdown()
        await c.playwright_driver.shutdown()
        await c.gateway.close()
        await c.db.close()


@pytest.fixture
def client(settings: Settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sample_profile() -> dict[str, Any]:
    return {
        "contact": {"first_name": "Ada", "last_name": "Testperson", "email": "ada.testperson@example.test", "phone": "+1 555 0100", "city": "Austin", "state": "TX", "country": "United States"},
        "summary": "Backend engineer with Python and FastAPI experience.",
        "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "React"],
        "experience_years": 6,
        "education": [{"school": "State University", "degree": "BSc", "field_of_study": "Computer Science", "start_date": "2014", "end_date": "2018"}],
        "employment": [{"company": "Current Co", "title": "Senior Engineer", "start_date": "2021", "current": True}],
        "links": {"linkedin": "https://linkedin.com/in/ada-test", "github": "https://github.com/ada-test"},
        "preferences": {"titles": ["Software Engineer", "Backend Engineer"], "remote_types": ["remote", "hybrid"], "notice_period": "4 weeks", "salary_min": 90000, "salary_currency": "EUR"},
        "legal": {"work_authorization": "Yes", "requires_sponsorship": "No"},
        "cover_letter_template": "I am excited about this role.",
    }


@pytest.fixture
def resume_file(tmp_path: Path) -> Path:
    p = tmp_path / "ada_resume.pdf"
    p.write_bytes(b"%PDF-1.4\n% synthetic test resume\n%%EOF\n")
    return p


async def register_resume(ctx: AppContext, path: Path) -> dict[str, Any]:
    from app.core.util import sha256_hex

    data = path.read_bytes()
    r = await ctx.repos.profile.add_resume({"label": "Default", "file_path": str(path), "file_name": path.name, "size_bytes": len(data), "sha256": sha256_hex(data), "mime_type": "application/pdf", "targeting": {"default": True}})
    return r


async def seed_profile(ctx: AppContext, profile: dict[str, Any], resume_id: str | None = None, answers: list[dict[str, Any]] | None = None) -> None:
    p = dict(profile)
    if resume_id:
        p["default_resume_id"] = resume_id
    await ctx.repos.profile.save(p)
    for a in answers or []:
        await ctx.repos.profile.upsert_answer(a)


def greenhouse_lever_transport() -> httpx.MockTransport:
    """Offline transport serving the recorded API fixtures."""
    import json

    gh = json.loads((FIXTURES / "api" / "greenhouse_job.json").read_text())
    lv = json.loads((FIXTURES / "api" / "lever_posting.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "boards-api.greenhouse.io" and path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [gh]})
        if host == "boards-api.greenhouse.io" and "/jobs/" in path:
            return httpx.Response(200, json=gh)
        if host == "api.lever.co" and path.rstrip("/").endswith("/postings/orbitlabs"):
            return httpx.Response(200, json=[lv])
        if host == "api.lever.co":
            return httpx.Response(200, json=lv)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def offline_adapters(ctx: AppContext) -> None:
    from app.jobs.adapters.greenhouse import GreenhouseAdapter
    from app.jobs.adapters.lever import LeverAdapter
    from app.jobs.adapters.registry import AdapterRegistry

    client = httpx.AsyncClient(transport=greenhouse_lever_transport())
    reg = AdapterRegistry([GreenhouseAdapter(client), LeverAdapter(client)])
    ctx.adapters = reg
    ctx.jobs._adapters = reg
    ctx.workflow._adapters = reg


def playwright_available() -> bool:
    bundle = ROOT / "extension" / "dist" / "dom-agent.js"
    return bundle.exists()


requires_browser = pytest.mark.skipif(not playwright_available(), reason="extension bundle not built (run npm run build in extension/)")


@pytest_asyncio.fixture
async def driver(ctx: AppContext):
    """Started Playwright driver (headless)."""
    d = ctx.playwright_driver
    await d.start()
    yield d


async def approve_all(ctx: AppContext, *, timeout: float = 20.0) -> list[str]:
    """Background helper: approve every pending approval as it appears, until the task finishes."""
    approved: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for a in await ctx.repos.approvals.pending():
            await ctx.approvals.decide(a["id"], "approved")
            approved.append(a["id"])
        if not ctx.tasks.running_ids:
            break
        await asyncio.sleep(0.1)
    return approved


async def wait_task(ctx: AppContext, task_id: str, timeout: float = 60.0) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if task_id not in ctx.tasks.running_ids:
            t = await ctx.repos.tasks.get(task_id)
            if t and t["status"] not in ("pending", "running"):
                return t
        await asyncio.sleep(0.1)
    raise TimeoutError(f"task {task_id} did not finish")
