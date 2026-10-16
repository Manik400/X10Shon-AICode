"""Repeatable benchmark suite. Writes benchmarks/results/<timestamp>.json.

Measures (on this machine, with whatever model runtime is configured):
  * service cold start (build_context incl. migrations)
  * database query latency (job list, application list, overview counts)
  * snapshot + action latency through Playwright on local fixtures
  * full application workflow duration on the simple fixture
  * model gateway latency per role (heuristic provider unless a runtime is configured) + eval harness
  * process RSS / CPU at idle and during work
Run:  python -m benchmarks.run_benchmarks [--with-model]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "agent" / "tests"))

from app.config import Settings, set_settings  # noqa: E402
from app.core.actions import BrowserAction, Target  # noqa: E402
from app.core.enums import ApplicationState  # noqa: E402
from app.models.evaluation import run_evaluation  # noqa: E402
from app.persistence.repositories import Page  # noqa: E402
from app.state import build_context  # noqa: E402
from conftest import approve_all, fixture_url, register_resume, seed_profile, wait_task  # noqa: E402

SAMPLE_PROFILE = {
    "contact": {"first_name": "Ada", "last_name": "Testperson", "email": "ada.testperson@example.test", "phone": "+1 555 0100", "city": "Austin"},
    "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
    "experience_years": 6,
    "education": [{"school": "State University", "degree": "BSc"}],
    "employment": [{"company": "Current Co", "title": "Senior Engineer", "current": True}],
    "links": {"linkedin": "https://linkedin.com/in/ada-test"},
    "legal": {"work_authorization": "Yes", "requires_sponsorship": "No"},
    "preferences": {"titles": ["Software Engineer"]},
}


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


async def main(with_model: bool, model: str | None = None, roles: list[str] | None = None) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="lfa-bench-"))
    settings = Settings(data_dir=tmp / "data", per_domain_delay_seconds=0.0, log_level="ERROR")
    if model:
        from app.config import ModelRoleConfig

        for role in roles or list(settings.model_roles):
            settings.model_roles[role] = ModelRoleConfig(provider="ollama", model=model, max_output_tokens=768 if role == "planner" else 512)
    set_settings(settings)
    results: dict = {"machine": {"platform": platform.platform(), "python": platform.python_version(), "cpu_count": os.cpu_count()}, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    t0 = time.perf_counter()
    ctx = await build_context(settings)
    results["service_cold_start_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    idle = ctx.resources.snapshot().to_dict()
    results["resources_idle"] = idle

    # ---- database -----------------------------------------------------------------------
    await ctx.repos.profile.save(SAMPLE_PROFILE)
    t0 = time.perf_counter()
    for i in range(200):
        await ctx.jobs.import_url(f"https://jobs.example.com/j/{i}", prefill={"title": f"Software Engineer {i}", "company": f"Co {i % 20}", "description": "Python FastAPI PostgreSQL Docker"})
    results["import_200_jobs_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    lat = []
    for _ in range(50):
        t0 = time.perf_counter()
        await ctx.repos.jobs.list(Page(50, 0), search="Engineer", sort="match_score")
        lat.append((time.perf_counter() - t0) * 1000)
    results["db_job_list_ms"] = {"p50": round(pct(lat, 0.5), 2), "p95": round(pct(lat, 0.95), 2)}
    lat = []
    for _ in range(50):
        t0 = time.perf_counter()
        await ctx.repos.applications.counts_by_state()
        await ctx.repos.jobs.counts()
        lat.append((time.perf_counter() - t0) * 1000)
    results["db_overview_counts_ms"] = {"p50": round(pct(lat, 0.5), 2), "p95": round(pct(lat, 0.95), 2)}

    # ---- browser ------------------------------------------------------------------------
    d = ctx.playwright_driver
    t0 = time.perf_counter()
    await d.start()
    results["browser_launch_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    tab = await d.open_tab(fixture_url("forms/greenhouse_like.html"))
    snaps, acts = [], []
    for _ in range(20):
        t0 = time.perf_counter()
        snap = await d.snapshot(tab, max_nodes=600)
        snaps.append((time.perf_counter() - t0) * 1000)
    results["snapshot_nodes"] = len(snap.nodes)
    first = next(n for n in snap.nodes if n.name.startswith("First Name"))
    for i in range(20):
        t0 = time.perf_counter()
        await d.execute(tab, BrowserAction(type="type", target=Target(strategy="node", value=str(first.i)), args={"text": f"Ada{i}"}))
        acts.append((time.perf_counter() - t0) * 1000)
    results["snapshot_ms"] = {"p50": round(pct(snaps, 0.5), 1), "p95": round(pct(snaps, 0.95), 1)}
    results["type_action_ms"] = {"p50": round(pct(acts, 0.5), 1), "p95": round(pct(acts, 0.95), 1)}
    results["resources_browser_open"] = ctx.resources.snapshot().to_dict()
    await d.close_tab(tab)

    # ---- full workflow ---------------------------------------------------------------------
    resume_path = tmp / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4 bench\n")
    resume = await register_resume(ctx, resume_path)
    await seed_profile(ctx, SAMPLE_PROFILE, resume["id"], [{"category": "consent", "question": "I consent to the processing of my data as described in the privacy policy", "answer": "Yes", "provenance": "approved_answer"}])
    durations = []
    model_calls_before = ctx.gateway.calls
    for i in range(3):
        job, _ = await ctx.jobs.import_url(fixture_url("forms/simple_application.html") + f"?run={i}", prefill={"title": f"Bench {i}", "company": f"Bench Co {i}"})
        app = await ctx.repos.applications.get_by_job(job["id"])
        await ctx.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="bench")
        task = await ctx.repos.tasks.create("apply", application_id=app["id"], job_id=job["id"], domain=app["application_url"])
        t0 = time.perf_counter()
        await ctx.tasks.start(task)
        approver = asyncio.ensure_future(approve_all(ctx, timeout=60))
        t = await wait_task(ctx, task["id"], timeout=90)
        await approver
        durations.append((time.perf_counter() - t0) * 1000)
        final = await ctx.repos.applications.get(app["id"])
        results.setdefault("workflow_runs", []).append({"status": t["status"], "state": final["state"], "actions": t["action_count"], "ms": round(durations[-1], 1)})
    results["workflow_simple_form_ms"] = {"mean": round(statistics.mean(durations), 1), "max": round(max(durations), 1)}
    results["model_calls_per_workflow"] = round((ctx.gateway.calls - model_calls_before) / 3, 2)
    results["resources_during_work"] = ctx.resources.snapshot().to_dict()

    # ---- model ------------------------------------------------------------------------------
    if with_model:
        results["model_health"] = await ctx.gateway.health()
    results["model_eval"] = await run_evaluation(ctx.gateway)
    results["model_eval"].pop("results", None)
    results["model_gateway"] = {"calls": ctx.gateway.calls, "cache_hits": ctx.gateway.cache_hits, "failures": ctx.gateway.failures}

    await ctx.tasks.shutdown()
    await d.shutdown()
    await ctx.gateway.close()
    await ctx.db.close()
    results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-model", action="store_true", help="include configured model runtime health/eval")
    ap.add_argument("--model", default=None, help="run roles on this Ollama model (e.g. qwen2.5:3b-instruct)")
    ap.add_argument("--roles", default=None, help="comma-separated roles to put on the model (default: all)")
    args = ap.parse_args()
    out = asyncio.run(main(args.with_model or bool(args.model), args.model, args.roles.split(",") if args.roles else None))
    dest = Path(__file__).resolve().parent / "results"
    dest.mkdir(exist_ok=True)
    path = dest / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nwritten to {path}")
