"""ApprovalManager: persists approval requests and lets tasks await decisions."""

from __future__ import annotations

import asyncio
from typing import Any

from ..observability.audit import AuditLogger
from ..persistence.repositories import ApprovalsRepository


class ApprovalManager:
    def __init__(self, repo: ApprovalsRepository, audit: AuditLogger):
        self._repo = repo
        self._audit = audit
        self._waiters: dict[str, asyncio.Event] = {}

    async def request(self, kind: str, payload: dict[str, Any], *, task_id: str | None, application_id: str | None) -> dict[str, Any]:
        approval = await self._repo.create(kind, payload, task_id=task_id, application_id=application_id)
        self._waiters[approval["id"]] = asyncio.Event()
        await self._audit.log("approval.requested", task_id=task_id, application_id=application_id, kind=kind, approval_id=approval["id"])
        return approval

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        """Block until decided. Returns True if approved. Times out → False (not approved)."""
        ev = self._waiters.get(approval_id)
        if ev is None:
            current = await self._repo.get(approval_id)
            return bool(current and current["status"] == "approved")
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._repo.decide(approval_id, "expired")
            self._waiters.pop(approval_id, None)
            return False
        self._waiters.pop(approval_id, None)
        current = await self._repo.get(approval_id)
        return bool(current and current["status"] == "approved")

    async def decide(self, approval_id: str, decision: str) -> dict[str, Any] | None:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        updated = await self._repo.decide(approval_id, decision)
        await self._audit.log("approval.decided", approval_id=approval_id, decision=decision, task_id=(updated or {}).get("task_id"), application_id=(updated or {}).get("application_id"))
        ev = self._waiters.get(approval_id)
        if ev is not None:
            ev.set()
        return updated

    async def pending(self) -> list[dict[str, Any]]:
        return await self._repo.pending()

    async def cancel_for_task(self, task_id: str) -> None:
        for a in await self._repo.pending_for_task(task_id):
            await self._repo.decide(a["id"], "expired")
            ev = self._waiters.pop(a["id"], None)
            if ev is not None:
                ev.set()
