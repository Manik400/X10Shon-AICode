"""AuditLogger: persists redacted, structured events and fans them out to live subscribers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from ..persistence.repositories import AuditRepository
from ..security.redaction import redact

log = logging.getLogger("lfa.audit")

Subscriber = Callable[[dict[str, Any]], None]


class AuditLogger:
    def __init__(self, repo: AuditRepository):
        self._repo = repo
        self._subscribers: set[Subscriber] = set()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        self._subscribers.add(fn)
        return lambda: self._subscribers.discard(fn)

    async def log(
        self,
        event: str,
        *,
        level: str = "info",
        task_id: str | None = None,
        application_id: str | None = None,
        job_id: str | None = None,
        **data: Any,
    ) -> None:
        safe = redact(data, pii=False)
        try:
            await self._repo.add(event, level=level, task_id=task_id, application_id=application_id, job_id=job_id, data=safe)
        except Exception:  # audit must never break a workflow
            log.exception("failed to persist audit event %s", event)
        record = {"event": event, "level": level, "task_id": task_id, "application_id": application_id, "job_id": job_id, "data": safe}
        log.log(logging.getLevelName(level.upper()) if isinstance(level, str) else logging.INFO, event, extra={"task_id": task_id, "application_id": application_id, "event": event})
        for fn in list(self._subscribers):
            try:
                fn(record)
            except Exception:
                log.exception("audit subscriber failed")

    def log_nowait(self, event: str, **kw: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.log(event, **kw))
