"""Point every model role at a local Ollama model and persist it, then run the eval harness.

Usage: python -m benchmarks.configure_model [--model qwen2.5:3b-instruct] [--planner-model NAME]
Writes the roles into the scheduler config (what the dashboard edits) so the running service and
future starts pick them up, and prints the evaluation report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings, set_settings  # noqa: E402
from app.models.evaluation import run_evaluation  # noqa: E402
from app.state import build_context  # noqa: E402

ROLES = ["intent", "planner", "extractor", "matcher", "field_mapper", "classifier"]


async def main(model: str, planner_model: str | None, provider: str, evaluate: bool, only_roles: list[str] | None = None, save: bool = True) -> None:
    settings = Settings(log_level="WARNING")
    set_settings(settings)
    ctx = await build_context(settings)
    chosen = set(only_roles or ROLES)
    roles = {
        r: (
            {"provider": provider, "model": planner_model if (r == "planner" and planner_model) else model, "temperature": 0.0, "max_output_tokens": 768 if r == "planner" else 512}
            if r in chosen
            else {"provider": "heuristic", "model": "", "temperature": 0.0, "max_output_tokens": 512}
        )
        for r in ROLES
    }
    if save:
        cfg = ctx.scheduler.config.model_copy(update={"model_roles": roles})
        await ctx.scheduler.save(cfg)
    ctx.gateway.update_roles(roles)
    health = await ctx.gateway.health()
    print(json.dumps({"roles": roles, "providers": health["providers"]}, indent=1))
    if evaluate:
        report = await run_evaluation(ctx.gateway)
        print(json.dumps(report, indent=1))
        out = ROOT / "benchmarks" / "results" / f"eval_{model.replace(':', '_').replace('/', '_')}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"written to {out}")
    await ctx.tasks.shutdown()
    await ctx.playwright_driver.shutdown()
    await ctx.gateway.close()
    await ctx.db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:3b-instruct")
    ap.add_argument("--planner-model", default=None)
    ap.add_argument("--provider", default="ollama", choices=["ollama", "openai_compatible", "heuristic"])
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--roles", default=None, help="comma-separated roles to put on the model; the rest stay on deterministic rules")
    ap.add_argument("--no-save", action="store_true", help="evaluate only; do not persist the role configuration")
    a = ap.parse_args()
    asyncio.run(main(a.model, a.planner_model, a.provider, not a.no_eval, a.roles.split(",") if a.roles else None, save=not a.no_save))
