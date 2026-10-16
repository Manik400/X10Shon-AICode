from __future__ import annotations

from typing import Any

from ...core.util import dumps, now_iso
from .base import Page, Repository, row_to_dict


class AuditRepository(Repository):
    async def add(
        self,
        event: str,
        *,
        level: str = "info",
        task_id: str | None = None,
        application_id: str | None = None,
        job_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        return await self.db.execute_returning_id(
            "INSERT INTO audit_events(ts, level, event, task_id, application_id, job_id, data_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), level, event, task_id, application_id, job_id, dumps(data or {})),
        )

    async def list(
        self,
        page: Page,
        *,
        task_id: str | None = None,
        application_id: str | None = None,
        level: str | None = None,
        since: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if task_id:
            where.append("task_id = ?")
            params.append(task_id)
        if application_id:
            where.append("application_id = ?")
            params.append(application_id)
        if level:
            where.append("level = ?")
            params.append(level)
        if since:
            where.append("ts >= ?")
            params.append(since)
        if search:
            where.append("(event LIKE ? OR data_json LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(await self.db.fetch_value(f"SELECT COUNT(*) FROM audit_events{clause}", params) or 0)
        rows = await self.db.fetch_all(
            f"SELECT * FROM audit_events{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, page.limit, page.offset],
        )
        return [row_to_dict(r, ("data_json",)) or {} for r in rows], total

    async def prune(self, keep: int = 20_000) -> int:
        async with self.db.transaction():
            return await self.db.execute(
                "DELETE FROM audit_events WHERE id < (SELECT COALESCE(MAX(id), 0) - ? FROM audit_events)", (keep,)
            )
