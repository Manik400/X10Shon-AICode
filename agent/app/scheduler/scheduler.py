"""Scheduler: picks up QUEUED/RETRY_SCHEDULED applications under configured policies.

Restart-safe: nothing is held in memory that is not also in the database; the loop wakes on a
bounded interval (no tight polling) and on explicit kicks.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..core.enums import ApplicationState
from ..core.util import now_dt, now_iso
from ..observability.audit import AuditLogger
from ..persistence.unit import Repositories

log = logging.getLogger("lfa.scheduler")


class SchedulerConfig(BaseModel):
    enabled: bool = False
    daily_application_limit: int = 20
    max_concurrent_tasks: int = 1
    per_domain_concurrency: int = 1
    per_domain_delay_seconds: float = 5.0
    allowed_domains: list[str] = Field(default_factory=lambda: ["*"], description="'*' allows every http(s) site; list specific domains to restrict")
    home_list_url: str = Field(default="", description="When set, apply tasks may only be started from a page whose URL begins with this (e.g. the Job Hunt list)")
    active_hours_start: int = Field(default=8, ge=0, le=23)
    active_hours_end: int = Field(default=22, ge=1, le=24)
    active_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], description="0=Monday")
    max_retries: int = 3
    retry_base_seconds: float = 60.0
    approval_policy: str = "always"  # always | never | per_site
    auto_pause_on_resource_pressure: bool = True
    wake_interval_seconds: float = 30.0
    max_ram_mb: int = 8192
    cpu_threads: int = 0
    gpu_enabled: bool = False
    model_roles: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Scheduler:
    def __init__(self, repos: Repositories, audit: AuditLogger, task_manager, policy, resources=None):  # type: ignore[no-untyped-def]
        self._repos = repos
        self._audit = audit
        self._tm = task_manager
        self._policy = policy
        self._resources = resources
        self._config = SchedulerConfig()
        self._config_listeners: list[Any] = []
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_tick: str | None = None
        self.last_reason: str = "not started"

    @property
    def config(self) -> SchedulerConfig:
        return self._config

    def on_config(self, fn) -> None:  # type: ignore[no-untyped-def]
        self._config_listeners.append(fn)

    async def load(self) -> SchedulerConfig:
        row = await self._repos.db.fetch_one("SELECT data_json FROM scheduler_config WHERE id = 1")
        if row:
            from ..core.util import loads

            self._config = SchedulerConfig.model_validate(loads(row["data_json"], {}))
        self._apply_to_policy()
        return self._config

    async def save(self, cfg: SchedulerConfig) -> SchedulerConfig:
        from ..core.util import dumps

        self._config = cfg
        async with self._repos.db.transaction():
            await self._repos.db.execute(
                "INSERT INTO scheduler_config(id, data_json, updated_at) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET data_json = excluded.data_json, updated_at = excluded.updated_at",
                (dumps(cfg.model_dump()), now_iso()),
            )
        self._apply_to_policy()
        self.kick()
        return cfg

    def _apply_to_policy(self) -> None:
        self._policy.set_allowed_domains(self._config.allowed_domains)
        self._policy._submit_policy = self._config.approval_policy
        for fn in self._config_listeners:
            fn(self._config)
        self._policy._delay = self._config.per_domain_delay_seconds
        self._policy._concurrency = self._config.per_domain_concurrency
        if self._resources is not None:
            self._resources.max_ram_mb = self._config.max_ram_mb

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                self._task.cancel()
            self._task = None

    def kick(self) -> None:
        self._wake.set()

    def within_active_hours(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if now.weekday() not in self._config.active_days:
            return False
        return self._config.active_hours_start <= now.hour < self._config.active_hours_end

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
                await self._repos.db.checkpoint()  # keep the main db file current (see Database.checkpoint)
            except Exception:
                log.exception("scheduler tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._config.wake_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> dict[str, Any]:
        self.last_tick = now_iso()
        cfg = self._config
        if not cfg.enabled:
            self.last_reason = "scheduler disabled"
            return {"started": 0, "reason": self.last_reason}
        if self._policy.paused:
            self.last_reason = "paused"
            return {"started": 0, "reason": self.last_reason}
        if not self.within_active_hours():
            self.last_reason = "outside active hours"
            return {"started": 0, "reason": self.last_reason}
        if self._resources is not None and cfg.auto_pause_on_resource_pressure:
            pressured, why = self._resources.under_pressure()
            if pressured:
                self.last_reason = f"resource pressure: {why}"
                return {"started": 0, "reason": self.last_reason}
        day = now_dt().strftime("%Y-%m-%d")
        used = await self._repos.settings.get_counter(day, "applications_prepared")
        if used >= cfg.daily_application_limit:
            self.last_reason = f"daily limit {cfg.daily_application_limit} reached"
            return {"started": 0, "reason": self.last_reason}
        free = max(0, cfg.max_concurrent_tasks - len(self._tm.running_ids))
        if free == 0:
            self.last_reason = "all workers busy"
            return {"started": 0, "reason": self.last_reason}
        candidates = await self._repos.applications.schedulable(now_iso(), limit=free * 3)
        started = 0
        for app in candidates:
            if started >= free or used + started >= cfg.daily_application_limit:
                break
            url = app.get("application_url") or ""
            if not self._policy.domain_allowed(url):
                continue  # user must allow the domain first; stays queued
            if int(app.get("attempt_count") or 0) > cfg.max_retries and app["state"] == ApplicationState.RETRY_SCHEDULED:
                await self._repos.applications.transition(app["id"], ApplicationState.APPLICATION_FAILED, reason=f"retry limit {cfg.max_retries} exceeded", actor="scheduler")
                continue
            if not self._policy.acquire_domain(url):
                continue
            try:
                task = await self._repos.tasks.create("apply", application_id=app["id"], job_id=app["job_id"], domain=url)
                await self._tm.start(task)
                await self._repos.settings.increment_counter(day, "applications_prepared")
                started += 1
                await self._audit.log("scheduler.started", application_id=app["id"], task_id=task["id"])
            finally:
                self._policy.release_domain(url)
        self.last_reason = f"started {started}" if started else "nothing schedulable"
        return {"started": started, "reason": self.last_reason}

    def status(self) -> dict[str, Any]:
        return {"enabled": self._config.enabled, "running": self._task is not None and not self._task.done(), "last_tick": self.last_tick, "last_reason": self.last_reason, "within_active_hours": self.within_active_hours(), "paused": self._policy.paused}
