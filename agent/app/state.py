"""Composition root: builds every service once and exposes them on ``app.state.ctx``."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent.approvals import ApprovalManager
from .agent.context import ContextBuilder
from .agent.intent import IntentRouter
from .agent.planner import TaskPlanner
from .agent.policy import PolicyEngine
from .agent.recovery import RecoveryManager
from .agent.runner import TaskManager, TaskRunner
from .applications.form_filler import FormFiller
from .applications.workflow import ApplicationWorkflow
from .browser.driver import BrowserDriver
from .browser.extension_driver import ExtensionDriver
from .browser.extension_hub import ExtensionHub
from .browser.playwright_driver import PlaywrightDriver
from .config import Settings
from .jobs.adapters import AdapterRegistry
from .jobs.extraction import JobExtractor
from .jobs.matching import JobMatcher
from .jobs.service import JobService
from .models.gateway import ModelGateway
from .observability.audit import AuditLogger
from .observability.resources import ResourceMonitor
from .persistence.db import Database
from .persistence.unit import Repositories
from .scheduler import Scheduler, SchedulerConfig
from .security.pairing import PairingService

log = logging.getLogger("lfa.state")


@dataclass
class AppContext:
    settings: Settings
    db: Database
    repos: Repositories
    audit: AuditLogger
    gateway: ModelGateway
    policy: PolicyEngine
    approvals: ApprovalManager
    hub: ExtensionHub
    extension_driver: ExtensionDriver
    playwright_driver: PlaywrightDriver
    adapters: AdapterRegistry
    jobs: JobService
    workflow: ApplicationWorkflow
    tasks: TaskManager
    scheduler: Scheduler
    pairing: PairingService
    resources: ResourceMonitor
    started_at: str
    # An extension has been paired: scheduled applications belong in the user's Chrome, so the
    # scheduler waits for it to (re)connect instead of running them in the headless browser.
    extension_paired: bool = False

    async def fetch_public_snapshot(self, url: str):  # type: ignore[no-untyped-def]
        """Snapshot of a public page through headless Playwright (no user session). Returns
        None when the browser is unavailable; the tab is always closed."""
        d = self.playwright_driver
        try:
            await d.start()
            tab = await d.open_tab(url)
        except Exception as exc:
            log.warning("headless fetch unavailable for %s: %s", url, exc)
            return None
        try:
            return await d.snapshot(tab, max_nodes=self.settings.snapshot_max_nodes)
        finally:
            try:
                await d.close_tab(tab)
            except Exception:
                pass

    def driver_for(self, task: dict[str, Any]) -> BrowserDriver:
        """Extension when the task names a Chrome tab or the extension is connected; otherwise
        Playwright (used for unauthenticated extraction and tests)."""
        tab = task.get("tab_id")
        if tab is not None and str(tab).isdigit():
            return self.extension_driver
        if self.hub.connected:
            return self.extension_driver
        return self.playwright_driver

    def browser_ready(self) -> tuple[bool, str]:
        """Whether a scheduled (tab-less) task could open a page right now. Checked before the
        scheduler creates work, so tasks are not started only to die on a missing browser."""
        if self.hub.connected:
            return True, ""
        if self.extension_paired:
            return False, "the Chrome extension is not connected (it reconnects within a minute; applications run in your Chrome, not headless)"
        free = self.playwright_driver.free_pages()
        if free <= 0:
            return False, f"extension not connected and all {self.playwright_driver._max_pages} Playwright pages are in use"
        return True, ""

    def browser_capacity(self) -> int:
        """Pages scheduled tasks may open right now: unbounded through the extension, otherwise
        the headless browser's free pages (without this cap a tick started five tasks on a
        two-page browser and three of them died at once with "max concurrent pages reached")."""
        if self.hub.connected:
            return 10_000
        return self.playwright_driver.free_pages()


async def build_context(settings: Settings, *, db_path: Path | str | None = None) -> AppContext:
    from .core.util import now_iso

    db = Database(db_path or settings.db_path)
    await db.connect()
    await db.migrate()
    repos = Repositories.build(db)
    audit = AuditLogger(repos.audit)
    await db.checkpoint()
    log.info(
        "database %s: %s jobs, %s applications, %s pairings, %s audit events",
        db.path,
        await db.fetch_value("SELECT COUNT(*) FROM jobs"),
        await db.fetch_value("SELECT COUNT(*) FROM applications"),
        await db.fetch_value("SELECT COUNT(*) FROM pairings WHERE revoked_at IS NULL"),
        await db.fetch_value("SELECT COUNT(*) FROM audit_events"),
    )
    gateway = ModelGateway(settings, repos.metrics)
    policy = PolicyEngine(allowed_domains=settings.allowed_domains, submit_policy=settings.require_submit_approval, per_domain_delay=settings.per_domain_delay_seconds, per_domain_concurrency=settings.per_domain_concurrency)
    approvals = ApprovalManager(repos.approvals, audit)
    hub = ExtensionHub()
    ext_driver = ExtensionDriver(hub, request_timeout=settings.action_timeout_seconds + 10)
    pw_driver = PlaywrightDriver(headless=settings.playwright_headless, max_pages=settings.max_concurrent_pages, navigation_timeout_ms=settings.navigation_timeout_ms)
    adapters = AdapterRegistry()
    extractor = JobExtractor(gateway)
    matcher = JobMatcher(gateway)
    jobs = JobService(repos, extractor, matcher, adapters, audit)
    filler = FormFiller(gateway)
    workflow = ApplicationWorkflow(repos, filler, adapters, audit, submit_policy=settings.require_submit_approval)
    planner = TaskPlanner(gateway, ContextBuilder(settings.model_context_chars // 2))
    intents = IntentRouter(gateway)
    recovery = RecoveryManager(settings.max_recovery_attempts)
    resources = ResourceMonitor(settings.max_ram_mb, memory_pressure_percent=settings.memory_pressure_percent, min_available_mb=settings.memory_min_available_mb)
    pairing = PairingService(repos.pairings, settings.pairing_code_ttl_seconds)

    ctx_holder: dict[str, AppContext] = {}

    def runner_factory() -> TaskRunner:
        ctx = ctx_holder["ctx"]
        # The driver is chosen per task when the runner starts; default to extension/playwright by availability.
        return _LazyRunner(ctx, planner=planner, approvals=approvals, recovery=recovery, intents=intents, resources=resources)

    tasks = TaskManager(settings, repos, audit, policy, runner_factory)
    scheduler = Scheduler(repos, audit, tasks, policy, resources)
    ctx = AppContext(
        settings=settings,
        db=db,
        repos=repos,
        audit=audit,
        gateway=gateway,
        policy=policy,
        approvals=approvals,
        hub=hub,
        extension_driver=ext_driver,
        playwright_driver=pw_driver,
        adapters=adapters,
        jobs=jobs,
        workflow=workflow,
        tasks=tasks,
        scheduler=scheduler,
        pairing=pairing,
        resources=resources,
        started_at=now_iso(),
    )
    ctx_holder["ctx"] = ctx
    try:
        ctx.extension_paired = bool(await repos.pairings.list())
    except Exception:  # noqa: BLE001 - a fresh database has no pairings table content yet
        ctx.extension_paired = False

    def _apply_limits(cfg: SchedulerConfig) -> None:
        # One source of truth for "how many at once" and "how often to retry": the saved scheduler
        # settings, applied at start-up and whenever they change (not only until the next restart).
        tasks.set_concurrency(cfg.max_concurrent_tasks)
        recovery.set_max_retries(cfg.max_retries)
        settings.max_concurrent_tasks = cfg.max_concurrent_tasks

    scheduler.on_config(_apply_limits)
    scheduler.browser_ready = ctx.browser_ready
    scheduler.browser_capacity = ctx.browser_capacity
    scheduler.inspect_tab = lambda tab_id: ext_driver.snapshot(tab_id, max_nodes=settings.snapshot_max_nodes)
    scheduler.close_tab = ext_driver.close_tab
    await scheduler.load()
    # Apply persisted model roles and settings to the gateway/policy.
    if scheduler.config.model_roles:
        gateway.update_roles(scheduler.config.model_roles)
    interrupted = await repos.tasks.mark_interrupted()
    if interrupted:
        await audit.log("service.recovered_interrupted_tasks", level="warning", count=interrupted)
        # Applications left IN_PROGRESS by a crash go back to retry so they are reconciled, never silently resumed.
        from .core.enums import ApplicationState

        for app in await repos.applications.in_state(ApplicationState.IN_PROGRESS):
            await repos.applications.transition(app["id"], ApplicationState.RETRY_SCHEDULED, reason="service restarted during execution; reconcile before retry", actor="system", fields={"next_attempt_at": None})

    async def push_status() -> None:
        pending = await repos.approvals.pending()
        waiting = None
        for t in await repos.tasks.running():
            if t["status"] == "waiting_user":
                waiting = {"task_id": t["id"], "message": ((t.get("progress_json") or {}).get("message")) or "The agent is waiting for you."}
                break
        await hub.push("status_update", {"status": {"paused_all": policy.paused, "current_task": tasks.current(), "pending_approvals": len(pending), "waiting_user": waiting}})

    tasks.on_change(push_status)
    scheduler._warm = gateway.warm_up
    scheduler.on_config(lambda cfg: setattr(workflow, "_submit_policy", cfg.approval_policy))
    workflow._submit_policy = scheduler.config.approval_policy

    def _apply_unattended(cfg: SchedulerConfig) -> None:
        workflow.auto_sign_in = cfg.auto_sign_in
        workflow.auto_create_accounts = cfg.auto_create_accounts
        policy.allow_stored_credentials = cfg.auto_sign_in

    scheduler.on_config(_apply_unattended)
    _apply_unattended(scheduler.config)

    # When the user finishes a login / challenge in a tab the agent handed over, the next page
    # load is inspected; if the page no longer needs a human, the waiting task resumes itself.
    in_flight: set[str] = set()

    async def on_extension_event(msg: dict[str, Any]) -> None:
        if msg.get("type") != "tab_event" or msg.get("event") not in ("updated", "taken_over"):
            return
        tab_id = str(msg.get("tab_id"))
        if tab_id in in_flight:
            return
        waiting = [
            t
            for t in await repos.tasks.running()
            if t["status"] == "waiting_user" and str(t.get("tab_id")) == tab_id and (t.get("failure_json") or {}).get("category") in ("AUTH_REQUIRED", "HUMAN_VERIFICATION_REQUIRED")
        ]
        if not waiting:
            return
        in_flight.add(tab_id)
        try:
            from .browser.page_state import classify_page

            snap = await ext_driver.snapshot(tab_id, max_nodes=settings.snapshot_max_nodes)
            cls = classify_page(snap)
            if cls.needs_human:
                return
            for t in waiting:
                new = await tasks.resume_task(t["id"])
                await audit.log("task.auto_resumed", task_id=t["id"], resumed_as=new["id"], url=snap.url, page_kind=cls.kind)
        except Exception as exc:
            log.debug("auto-resume check failed for tab %s: %s", tab_id, exc)
        finally:
            in_flight.discard(tab_id)

    hub.on_event(on_extension_event)
    return ctx


class _LazyRunner(TaskRunner):
    """TaskRunner whose driver is resolved from the task at run time."""

    def __init__(self, ctx: AppContext, *, planner, approvals, recovery, intents, resources):  # type: ignore[no-untyped-def]
        self._ctx = ctx
        super().__init__(
            settings=ctx.settings,
            repos=ctx.repos,
            driver=ctx.extension_driver,
            policy=ctx.policy,
            planner=planner,
            approvals=approvals,
            recovery=recovery,
            audit=ctx.audit,
            jobs=ctx.jobs,
            workflow=ctx.workflow,
            intents=intents,
            resources=resources,
        )

    async def run(self, task: dict[str, Any]) -> dict[str, Any]:
        self.driver = self._ctx.driver_for(task)
        from .agent.executor import BrowserExecutor

        self.executor = BrowserExecutor(self.driver)
        if self.driver is self._ctx.playwright_driver:
            await self._ctx.playwright_driver.start()
        return await super().run(task)
