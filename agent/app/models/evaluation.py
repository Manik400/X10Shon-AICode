"""Model evaluation harness: run real workflow tasks against candidate models and score them.

Cases live in ``agent/benchmarks/eval_cases.json``. Each case names a task, structured inputs,
and expectations; the harness renders the same prompts the workflow uses, so results reflect
actual behaviour rather than synthetic benchmarks.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..core.actions import PlannerOutput
from .base import ModelRequest
from .gateway import ModelGateway
from .schemas import FieldMappingOutput, IntentOutput, JobExtraction, MatchOutput

TASK_MODELS: dict[str, type[BaseModel]] = {
    "intent": IntentOutput,
    "plan": PlannerOutput,
    "extract_job": JobExtraction,
    "map_fields": FieldMappingOutput,
    "match_job": MatchOutput,
}
TASK_ROLE = {"intent": "intent", "plan": "planner", "extract_job": "extractor", "map_fields": "field_mapper", "match_job": "matcher"}

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "eval_cases.json"


def _score(task: str, output: dict[str, Any], expect: dict[str, Any]) -> tuple[float, list[str]]:
    notes: list[str] = []
    hits = 0
    total = 0
    for key, want in expect.items():
        total += 1
        got = output.get(key)
        if isinstance(want, dict) and "contains" in want:
            ok = want["contains"].lower() in str(got or "").lower()
        elif isinstance(want, dict) and "min" in want:
            ok = isinstance(got, (int, float)) and got >= want["min"]
        elif isinstance(want, dict) and "any_of" in want:
            ok = got in want["any_of"]
        elif isinstance(want, list):
            ok = all(str(w).lower() in [str(g).lower() for g in (got or [])] for w in want)
        else:
            ok = got == want
        hits += int(ok)
        if not ok:
            notes.append(f"{key}: expected {want!r}, got {got!r}")
    if task == "plan":
        total += 1
        first = (output.get("actions") or [{}])[0]
        ok = first.get("type") == expect.get("first_action_type", first.get("type"))
        hits += int(ok)
    return (hits / total if total else 0.0), notes


async def run_evaluation(gateway: ModelGateway, cases_path: Path = DEFAULT_CASES, roles_override: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    from ..agent.prompts import render_prompt

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    if roles_override:
        gateway.update_roles(roles_override)
    results = []
    t_all = time.perf_counter()
    for case in cases:
        task = case["task"]
        model_cls = TASK_MODELS[task]
        system, prompt = render_prompt(task, case["inputs"])
        req = ModelRequest(task=task, system=system, prompt=prompt, json_schema={}, inputs=case["inputs"])
        t0 = time.perf_counter()
        try:
            out = await gateway.complete(TASK_ROLE[task], req, model_cls, use_cache=False)
            latency = int((time.perf_counter() - t0) * 1000)
            score, notes = _score(task, out.model_dump(), case.get("expect", {}))
            results.append({"case": case["name"], "task": task, "ok": True, "score": round(score, 3), "latency_ms": latency, "notes": notes})
        except Exception as exc:
            results.append({"case": case["name"], "task": task, "ok": False, "score": 0.0, "latency_ms": int((time.perf_counter() - t0) * 1000), "notes": [str(exc)[:200]]})
    avg = sum(r["score"] for r in results) / len(results) if results else 0.0
    lat = sorted(r["latency_ms"] for r in results)
    return {
        "cases": len(results),
        "average_score": round(avg, 3),
        "p50_latency_ms": lat[len(lat) // 2] if lat else None,
        "p95_latency_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
        "total_ms": int((time.perf_counter() - t_all) * 1000),
        "roles": {r: c.model_dump() for r, c in gateway._settings.model_roles.items()},
        "results": results,
    }
