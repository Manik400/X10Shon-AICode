from __future__ import annotations

from typing import Any

from ...core.util import dumps, new_id, now_iso
from .base import Repository, row_to_dict


class ApprovalsRepository(Repository):
    async def create(self, kind: str, payload: dict[str, Any], *, task_id: str | None, application_id: str | None) -> dict[str, Any]:
        aid = new_id()
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO approvals(id, task_id, application_id, kind, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (aid, task_id, application_id, kind, dumps(payload), now_iso()),
            )
        return await self.get(aid) or {"id": aid}

    async def get(self, approval_id: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,)), ("payload_json",))

    async def decide(self, approval_id: str, status: str) -> dict[str, Any] | None:
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'", (status, now_iso(), approval_id)
            )
        return await self.get(approval_id)

    async def pending(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at ASC")
        return [row_to_dict(r, ("payload_json",)) or {} for r in rows]

    async def pending_for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM approvals WHERE status = 'pending' AND task_id = ? ORDER BY created_at ASC", (task_id,))
        return [row_to_dict(r, ("payload_json",)) or {} for r in rows]

    async def list(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))
        return [row_to_dict(r, ("payload_json",)) or {} for r in rows]
