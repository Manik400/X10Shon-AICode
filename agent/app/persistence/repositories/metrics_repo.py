from __future__ import annotations

from typing import Any

from ...core.util import now_iso
from .base import Repository


class MetricsRepository(Repository):
    async def record_model_call(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        latency_ms: int,
        prompt_chars: int,
        output_chars: int,
        prompt_tokens: int | None,
        output_tokens: int | None,
        cached: bool,
        ok: bool,
        error: str | None,
        task_id: str | None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO model_metrics(ts, role, provider, model, latency_ms, prompt_chars, output_chars, prompt_tokens, output_tokens, cached, ok, error, task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), role, provider, model, latency_ms, prompt_chars, output_chars, prompt_tokens, output_tokens, int(cached), int(ok), error, task_id),
        )

    async def summary(self, since: str | None = None) -> dict[str, Any]:
        clause = " WHERE ts >= ?" if since else ""
        params = [since] if since else []
        rows = await self.db.fetch_all(
            f"SELECT role, provider, model, latency_ms, cached, ok FROM model_metrics{clause} ORDER BY id DESC LIMIT 5000", params
        )
        by_role: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = r["role"]
            b = by_role.setdefault(key, {"role": key, "provider": r["provider"], "model": r["model"], "calls": 0, "cached": 0, "errors": 0, "latencies": []})
            b["calls"] += 1
            b["cached"] += int(r["cached"])
            b["errors"] += 0 if r["ok"] else 1
            if not r["cached"]:
                b["latencies"].append(int(r["latency_ms"]))
        out = []
        for b in by_role.values():
            lat = sorted(b.pop("latencies"))
            b["p50_ms"] = lat[len(lat) // 2] if lat else None
            b["p95_ms"] = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None
            out.append(b)
        return {"roles": out, "total_calls": len(rows)}

    async def prune(self, keep: int = 50_000) -> int:
        async with self.db.transaction():
            return await self.db.execute(
                "DELETE FROM model_metrics WHERE id < (SELECT COALESCE(MAX(id), 0) - ? FROM model_metrics)", (keep,)
            )
