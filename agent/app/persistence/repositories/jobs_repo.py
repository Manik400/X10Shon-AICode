from __future__ import annotations

from typing import Any

from ...core.util import dumps, new_id, now_iso
from .base import Page, Repository, row_to_dict

JOB_JSON = ("requirements_json", "match_explanation_json", "filter_result_json")

_SORTABLE = {"created_at", "match_score", "company", "title", "posted_at", "updated_at"}


class JobsRepository(Repository):
    async def get(self, job_id: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,)), JOB_JSON)

    async def find_by_dedup_key(self, key: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM jobs WHERE dedup_key = ?", (key,)), JOB_JSON)

    async def find_by_fuzzy_key(self, key: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM jobs WHERE fuzzy_key = ? AND duplicate_of IS NULL ORDER BY created_at ASC LIMIT 20", (key,))
        return [row_to_dict(r, JOB_JSON) or {} for r in rows]

    async def insert(self, job: dict[str, Any]) -> dict[str, Any]:
        jid = job.get("id") or new_id()
        ts = now_iso()
        cols = {
            "id": jid,
            "source_id": job.get("source_id"),
            "source_kind": job.get("source_kind", "url"),
            "original_url": job["original_url"],
            "canonical_url": job["canonical_url"],
            "dedup_key": job["dedup_key"],
            "company": job.get("company"),
            "title": job.get("title"),
            "location": job.get("location"),
            "remote_type": job.get("remote_type"),
            "employment_type": job.get("employment_type"),
            "experience_min_years": job.get("experience_min_years"),
            "experience_max_years": job.get("experience_max_years"),
            "salary_min": job.get("salary_min"),
            "salary_max": job.get("salary_max"),
            "salary_currency": job.get("salary_currency"),
            "description": job.get("description"),
            "requirements_json": dumps(job.get("requirements", [])),
            "apply_url": job.get("apply_url"),
            "requisition_id": job.get("requisition_id"),
            "posted_at": job.get("posted_at"),
            "extraction_method": job.get("extraction_method"),
            "match_score": job.get("match_score"),
            "match_explanation_json": dumps(job["match_explanation"]) if job.get("match_explanation") is not None else None,
            "filter_result_json": dumps(job["filter_result"]) if job.get("filter_result") is not None else None,
            "duplicate_of": job.get("duplicate_of"),
            "fuzzy_key": job.get("fuzzy_key"),
            "created_at": ts,
            "updated_at": ts,
        }
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        async with self.db.transaction():
            await self.db.execute(f"INSERT INTO jobs({keys}) VALUES ({marks})", list(cols.values()))
        return await self.get(jid) or {"id": jid}

    async def update(self, job_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        if not fields:
            return await self.get(job_id)
        mapped: dict[str, Any] = {}
        for k, v in fields.items():
            if k == "requirements":
                mapped["requirements_json"] = dumps(v)
            elif k == "match_explanation":
                mapped["match_explanation_json"] = dumps(v) if v is not None else None
            elif k == "filter_result":
                mapped["filter_result_json"] = dumps(v) if v is not None else None
            else:
                mapped[k] = v
        mapped["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in mapped)
        async with self.db.transaction():
            await self.db.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*mapped.values(), job_id])
        return await self.get(job_id)

    async def delete(self, job_id: str) -> bool:
        async with self.db.transaction():
            return (await self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))) > 0

    async def list(
        self,
        page: Page,
        *,
        search: str | None = None,
        state: str | None = None,
        source_kind: str | None = None,
        min_score: float | None = None,
        since: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
        include_duplicates: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if search:
            where.append("(j.company LIKE ? OR j.title LIKE ? OR j.location LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        if source_kind:
            where.append("j.source_kind = ?")
            params.append(source_kind)
        if min_score is not None:
            where.append("j.match_score >= ?")
            params.append(min_score)
        if since:
            where.append("j.created_at >= ?")
            params.append(since)
        if not include_duplicates:
            where.append("j.duplicate_of IS NULL")
        if state:
            where.append("COALESCE(a.state, 'DISCOVERED') = ?")
            params.append(state)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sort_col = sort if sort in _SORTABLE else "created_at"
        direction = "ASC" if order.lower() == "asc" else "DESC"
        base = (
            "FROM jobs j LEFT JOIN applications a ON a.job_id = j.id"
        )
        total = int(await self.db.fetch_value(f"SELECT COUNT(*) {base}{clause}", params) or 0)
        rows = await self.db.fetch_all(
            f"SELECT j.*, a.id AS application_id, a.state AS application_state, a.last_attempt_at, a.next_attempt_at, a.failure_json "
            f"{base}{clause} ORDER BY j.{sort_col} {direction} NULLS LAST LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        )
        return [row_to_dict(r, (*JOB_JSON, "failure_json")) or {} for r in rows], total

    async def counts(self) -> dict[str, int]:
        total = int(await self.db.fetch_value("SELECT COUNT(*) FROM jobs") or 0)
        dups = int(await self.db.fetch_value("SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL") or 0)
        return {"total": total, "duplicates": dups}

    # ------------------------------------------------------------------ sources
    async def list_sources(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM job_sources ORDER BY created_at DESC")
        return [row_to_dict(r, ("config_json",)) or {} for r in rows]

    async def get_source(self, source_id: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM job_sources WHERE id = ?", (source_id,)), ("config_json",))

    async def add_source(self, kind: str, name: str, config: dict[str, Any]) -> dict[str, Any]:
        sid = new_id()
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO job_sources(id, kind, name, config_json, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (sid, kind, name, dumps(config), now_iso()),
            )
        return await self.get_source(sid) or {"id": sid}

    async def update_source(self, source_id: str, *, enabled: bool | None = None, last_run_at: str | None = None, last_error: str | None = "__keep__") -> None:
        async with self.db.transaction():
            if enabled is not None:
                await self.db.execute("UPDATE job_sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))
            if last_run_at is not None:
                await self.db.execute("UPDATE job_sources SET last_run_at = ? WHERE id = ?", (last_run_at, source_id))
            if last_error != "__keep__":
                await self.db.execute("UPDATE job_sources SET last_error = ? WHERE id = ?", (last_error, source_id))

    async def delete_source(self, source_id: str) -> bool:
        async with self.db.transaction():
            return (await self.db.execute("DELETE FROM job_sources WHERE id = ?", (source_id,))) > 0
