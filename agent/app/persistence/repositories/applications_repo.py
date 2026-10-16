from __future__ import annotations

from typing import Any

from ...core.enums import ApplicationState
from ...core.state_machine import SCHEDULABLE, assert_transition
from ...core.util import dumps, new_id, now_iso
from .base import Page, Repository, row_to_dict

APP_JSON = ("evidence_json", "failure_json", "fill_plan_json", "pending_questions_json")


class ApplicationsRepository(Repository):
    async def get(self, app_id: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM applications WHERE id = ?", (app_id,)), APP_JSON)

    async def get_by_job(self, job_id: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM applications WHERE job_id = ?", (job_id,)), APP_JSON)

    async def find_by_identity(self, identity_key: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM applications WHERE identity_key = ?", (identity_key,)), APP_JSON)

    async def create(self, job_id: str, identity_key: str, state: ApplicationState, *, application_url: str | None, actor: str = "agent", reason: str | None = None) -> dict[str, Any]:
        aid = new_id()
        ts = now_iso()
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO applications(id, job_id, identity_key, state, application_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (aid, job_id, identity_key, state.value, application_url, ts, ts),
            )
            await self.db.execute(
                "INSERT INTO application_transitions(application_id, from_state, to_state, reason, actor, created_at) VALUES (?, NULL, ?, ?, ?, ?)",
                (aid, state.value, reason, actor, ts),
            )
        return await self.get(aid) or {"id": aid}

    async def transition(self, app_id: str, to_state: ApplicationState, *, reason: str | None = None, actor: str = "agent", fields: dict[str, Any] | None = None) -> dict[str, Any]:
        """Atomically validate and persist a state transition plus optional field updates."""
        async with self.db.transaction():
            row = await self.db.fetch_one("SELECT state FROM applications WHERE id = ?", (app_id,))
            if row is None:
                raise KeyError(app_id)
            current = ApplicationState(row["state"])
            if current != to_state:
                assert_transition(current, to_state)
            ts = now_iso()
            updates: dict[str, Any] = {"state": to_state.value, "updated_at": ts}
            for k, v in (fields or {}).items():
                if k in ("evidence", "failure", "fill_plan", "pending_questions"):
                    updates[f"{k}_json"] = dumps(v) if v is not None else None
                else:
                    updates[k] = v
            sets = ", ".join(f"{k} = ?" for k in updates)
            await self.db.execute(f"UPDATE applications SET {sets} WHERE id = ?", [*updates.values(), app_id])
            if current != to_state or reason:
                await self.db.execute(
                    "INSERT INTO application_transitions(application_id, from_state, to_state, reason, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (app_id, current.value, to_state.value, reason, actor, ts),
                )
        return await self.get(app_id) or {"id": app_id}

    async def update_fields(self, app_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        updates: dict[str, Any] = {"updated_at": now_iso()}
        for k, v in fields.items():
            if k in ("evidence", "failure", "fill_plan", "pending_questions"):
                updates[f"{k}_json"] = dumps(v) if v is not None else None
            else:
                updates[k] = v
        sets = ", ".join(f"{k} = ?" for k in updates)
        async with self.db.transaction():
            await self.db.execute(f"UPDATE applications SET {sets} WHERE id = ?", [*updates.values(), app_id])
        return await self.get(app_id)

    async def history(self, app_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM application_transitions WHERE application_id = ? ORDER BY id ASC", (app_id,)
        )
        return [dict(r) for r in rows]

    async def list(
        self,
        page: Page,
        *,
        state: str | None = None,
        search: str | None = None,
        since: str | None = None,
        verification: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if state:
            where.append("a.state = ?")
            params.append(state)
        if verification:
            where.append("a.verification_status = ?")
            params.append(verification)
        if search:
            where.append("(j.company LIKE ? OR j.title LIKE ? OR a.reference_number LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        if since:
            where.append("a.updated_at >= ?")
            params.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sort_col = sort if sort in {"updated_at", "created_at", "applied_at", "state", "company"} else "updated_at"
        sort_expr = f"j.{sort_col}" if sort_col == "company" else f"a.{sort_col}"
        direction = "ASC" if order.lower() == "asc" else "DESC"
        base = "FROM applications a JOIN jobs j ON j.id = a.job_id LEFT JOIN resumes r ON r.id = a.resume_id"
        total = int(await self.db.fetch_value(f"SELECT COUNT(*) {base}{clause}", params) or 0)
        rows = await self.db.fetch_all(
            f"SELECT a.*, j.company, j.title, j.location, j.canonical_url, j.source_kind, r.label AS resume_label "
            f"{base}{clause} ORDER BY {sort_expr} {direction} NULLS LAST LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        )
        return [row_to_dict(r, APP_JSON) or {} for r in rows], total

    async def counts_by_state(self) -> dict[str, int]:
        rows = await self.db.fetch_all("SELECT state, COUNT(*) AS n FROM applications GROUP BY state")
        return {r["state"]: int(r["n"]) for r in rows}

    async def schedulable(self, now: str, limit: int = 10) -> list[dict[str, Any]]:
        states = ", ".join("?" for _ in SCHEDULABLE)
        rows = await self.db.fetch_all(
            f"SELECT * FROM applications WHERE state IN ({states}) AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY COALESCE(next_attempt_at, created_at) ASC LIMIT ?",
            [*[s.value for s in SCHEDULABLE], now, limit],
        )
        return [row_to_dict(r, APP_JSON) or {} for r in rows]

    async def in_state(self, state: ApplicationState) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM applications WHERE state = ?", (state.value,))
        return [row_to_dict(r, APP_JSON) or {} for r in rows]

    async def applied_count_for_day(self, day_prefix: str) -> int:
        return int(await self.db.fetch_value("SELECT COUNT(*) FROM applications WHERE applied_at LIKE ?", (f"{day_prefix}%",)) or 0)
