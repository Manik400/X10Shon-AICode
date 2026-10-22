"""Scheduler: picks up QUEUED/RETRY_SCHEDULED applications under configured policies.

Restart-safe: nothing is held in memory that is not also in the database; the loop wakes on a
bounded interval (no tight polling) and on explicit kicks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from ..core.enums import ApplicationState
from ..core.util import now_dt, now_iso
from ..observability.audit import AuditLogger
from ..persistence.unit import Repositories

log = logging.getLogger("lfa.scheduler")


class SchedulerConfig(BaseModel):
    enabled: bool = False
    daily_application_limit: int = 20
    max_concurrent_tasks: int = Field(default=1, ge=1, le=5, description="Browser tasks the scheduler may run at once (tasks the user starts on their own tab never wait behind them)")
    per_domain_concurrency: int = 1
    per_domain_delay_seconds: float = 5.0
    allowed_domains: list[str] = Field(default_factory=lambda: ["*"], description="'*' allows every http(s) site; list specific domains to restrict")
    home_list_url: str = Field(default="", description="When set, apply tasks may only be started from a page whose URL begins with this (e.g. the Job Hunt list)")
    active_hours_start: int = Field(default=8, ge=0, le=23)
    active_hours_end: int = Field(default=22, ge=1, le=24)
    active_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], description="0=Monday")
    max_retries: int = Field(default=1, ge=0, le=3, description="Automatic re-attempts after a transient failure (browser hiccup, rate limit, site down); 0 = a failure is final. A job that fails for its own reasons is never retried automatically.")
    retry_base_seconds: float = 60.0
    approval_policy: str = "always"  # always | never | per_site
    auto_pause_on_resource_pressure: bool = True
    wake_interval_seconds: float = 30.0
    max_ram_mb: int = 8192
    cpu_threads: int = 0
    gpu_enabled: bool = False
    model_roles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("max_concurrent_tasks", "max_retries", mode="before")
    @classmethod
    def _clamp_limits(cls, v: Any, info: ValidationInfo) -> Any:
        """Settings saved before these bounds existed (e.g. 100 concurrent tasks, 10 retries, which
        made every failing job come back again and again) load clamped instead of failing."""
        try:
            n = int(v)
        except (TypeError, ValueError):
            return v
        lo, hi = (1, 5) if info.field_name == "max_concurrent_tasks" else (0, 3)
        return min(hi, max(lo, n))


class Scheduler:
    def __init__(self, repos: Repositories, audit: AuditLogger, task_manager, policy, resources=None):  # type: ignore[no-untyped-def]
        self._repos = repos
        self._audit = audit
        self._tm = task_manager
        self._policy = policy
        self._resources = resources
        self._config = SchedulerConfig()
        self._config_listeners: list[Any] = []
        self._warm: Any = None  # async callable that loads the configured models
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_tick: str | None = None
        self.last_reason: str = "not started"
        # Answers "could a task open a page right now?" (extension connected / Playwright capacity).
        self.browser_ready: Callable[[], tuple[bool, str]] | None = None
        self._hold_until = 0.0
        self._hold_reason = ""
        if hasattr(task_manager, "browser_unavailable_cb"):
            task_manager.browser_unavailable_cb = self.hold

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

    def hold(self, reason: str, seconds: float = 300.0) -> None:
        """Stop starting tasks for a while: no browser could take the last one (extension offline,
        page limit). Without this every tick re-created the same tasks, each dying at once and each
        counted against its job as a failed attempt."""
        self._hold_until = max(self._hold_until, time.monotonic() + seconds)
        self._hold_reason = reason[:160]
        log.info("scheduler on hold for %ds: %s", int(seconds), self._hold_reason)

    def within_active_hours(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if now.weekday() not in self._config.active_days:
            return False
        return self._config.active_hours_start <= now.hour < self._config.active_hours_end

    async def _loop(self) -> None:
        import time as _time

        last_warm = 0.0
        while not self._stop.is_set():
            # Keep the local model loaded: right after start-up, then every 10 minutes, which also
            # reloads it after a reboot or sleep if the runtime dropped it.
            if self._warm is not None and _time.monotonic() - last_warm > 600:
                last_warm = _time.monotonic()
                try:
                    loaded = await self._warm()
                    if loaded:
                        log.info("model warm-up: %s", loaded)
                except Exception:
                    log.debug("model warm-up failed", exc_info=True)
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
        if time.monotonic() < self._hold_until:
            self.last_reason = f"waiting for a browser ({self._hold_reason}); next try in {int(self._hold_until - time.monotonic())}s"
            return {"started": 0, "reason": self.last_reason}
        if self.browser_ready is not None:
            ready, why = self.browser_ready()
            if not ready:
                self.last_reason = f"waiting for a browser: {why}"
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
