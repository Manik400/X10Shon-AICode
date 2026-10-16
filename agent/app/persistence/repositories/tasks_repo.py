from __future__ import annotations

from typing import Any

from ...core.enums import TaskStatus
from ...core.util import dumps, new_id, now_iso
from .base import Page, Repository, row_to_dict

TASK_JSON = ("progress_json", "result_json", "failure_json")


class TasksRepository(Repository):
    async def create(self, kind: str, *, instruction: str | None = None, application_id: str | None = None, job_id: str | None = None, tab_id: int | None = None, domain: str | None = None) -> dict[str, Any]:
        tid = new_id()
        ts = now_iso()
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO tasks(id, kind, status, instruction, application_id, job_id, tab_id, domain, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, kind, TaskStatus.PENDING.value, instruction, application_id, job_id, tab_id, domain, ts, ts),
            )
        return await self.get(tid) or {"id": tid}

    async def get(self, task_id: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,)), TASK_JSON)

    async def set_status(self, task_id: str, status: TaskStatus, **fields: Any) -> dict[str, Any] | None:
        updates: dict[str, Any] = {"status": status.value, "updated_at": now_iso()}
        if status == TaskStatus.RUNNING:
            row = await self.get(task_id)
            if row and not row.get("started_at"):
                updates["started_at"] = updates["updated_at"]
        if status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            updates["finished_at"] = updates["updated_at"]
        for k, v in fields.items():
            if k in ("progress", "result", "failure"):
                updates[f"{k}_json"] = dumps(v) if v is not None else None
            else:
                updates[k] = v
        sets = ", ".join(f"{k} = ?" for k in updates)
        async with self.db.transaction():
            await self.db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*updates.values(), task_id])
        return await self.get(task_id)

    async def update_progress(self, task_id: str, progress: dict[str, Any], *, action_count: int | None = None, model_calls: int | None = None) -> None:
        updates: dict[str, Any] = {"progress_json": dumps(progress), "updated_at": now_iso()}
        if action_count is not None:
            updates["action_count"] = action_count
        if model_calls is not None:
            updates["model_calls"] = model_calls
        sets = ", ".join(f"{k} = ?" for k in updates)
        await self.db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*updates.values(), task_id])

    async def add_attempt(self, task_id: str, seq: int, action: dict[str, Any], result: dict[str, Any] | None, ok: bool | None, duration_ms: int) -> None:
        await self.db.execute(
            "INSERT INTO execution_attempts(task_id, seq, action_json, result_json, ok, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, seq, dumps(action), dumps(result) if result is not None else None, None if ok is None else int(ok), duration_ms, now_iso()),
        )

    async def attempts(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM execution_attempts WHERE task_id = ? ORDER BY seq ASC", (task_id,))
        return [row_to_dict(r, ("action_json", "result_json")) or {} for r in rows]

    async def list(self, page: Page, *, status: str | None = None, kind: str | None = None) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(await self.db.fetch_value(f"SELECT COUNT(*) FROM tasks{clause}", params) or 0)
        rows = await self.db.fetch_all(f"SELECT * FROM tasks{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?", [*params, page.limit, page.offset])
        return [row_to_dict(r, TASK_JSON) or {} for r in rows], total

    async def running(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM tasks WHERE status IN ('running', 'pending', 'paused', 'waiting_approval', 'waiting_user') ORDER BY created_at ASC")
        return [row_to_dict(r, TASK_JSON) or {} for r in rows]

    async def mark_interrupted(self) -> int:
        """On startup: anything that was running when the process died is marked failed with an
        explicit interruption reason, so it can be reconciled rather than silently resumed."""
        async with self.db.transaction():
            return await self.db.execute(
                "UPDATE tasks SET status = 'failed', failure_json = ?, finished_at = ?, updated_at = ? WHERE status = 'running'",
                (
                    dumps({"category": "BROWSER_DISCONNECTED", "message": "service restarted while the task was running", "retryable": True, "recommended_action": "Reconcile the application state and re-queue."}),
                    now_iso(),
                    now_iso(),
                ),
            )
