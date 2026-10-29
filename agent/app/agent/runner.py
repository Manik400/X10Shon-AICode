"""TaskRunner and TaskManager.

TaskRunner runs one task through the bounded loop
``Observe → Normalize → Classify → Plan → Validate Policy → Execute → Verify → Persist``.
TaskManager owns running tasks, cancellation, pause-all and the resume protocol.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from ..applications.profile import CandidateProfile
from ..applications.workflow import ApplicationWorkflow, WorkflowContext
from ..browser.driver import BrowserDriver
from ..browser.page_state import classify_page
from ..browser.snapshot import PageSnapshot
from ..browser.verifier import verify_action
from ..config import Settings
from ..core.actions import ActionResult, BrowserAction, Target
from ..core.enums import ApplicationState, ErrorCategory, TaskStatus
from ..core.errors import (
    AgentError,
    AuthRequiredError,
    BrowserDisconnectedError,
    HumanVerificationError,
    NeedsUserInputError,
    PermissionDeniedError,
    SubmissionUncertainError,
    UserCancelledError,
    classify_exception,
)
from ..core.util import now_iso
from ..jobs.matching import eligibility_blockers
from ..jobs.service import JobService
from ..observability.audit import AuditLogger
from ..observability.resources import ResourceMonitor
from ..persistence.unit import Repositories
from .approvals import ApprovalManager
from .executor import BrowserExecutor, LoopDetector
from .intent import IntentRouter
from .planner import TaskPlanner
from .policy import PolicyEngine
from .recovery import RecoveryManager

log = logging.getLogger("lfa.runner")

# A list filter that shows everything: "All boards · 110", "All jobs (52)", "All platforms".
_ALL_FILTER_RE = re.compile(r"^all (boards|jobs|platforms|sources|sites|postings|portals)\b", re.I)


# Tabs waiting on the user (tab id -> ("check" | "signin", host)): one desktop notification covers them all.
_WAITING: dict[str, tuple[str, str]] = {}


class TaskCancelled(UserCancelledError):
    pass


class TaskRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        repos: Repositories,
        driver: BrowserDriver,
        policy: PolicyEngine,
        planner: TaskPlanner,
        approvals: ApprovalManager,
        recovery: RecoveryManager,
        audit: AuditLogger,
        jobs: JobService,
        workflow: ApplicationWorkflow,
        intents: IntentRouter,
        resources: ResourceMonitor | None = None,
    ):
        self.s = settings
        self.repos = repos
        self.driver = driver
        self.policy = policy
        self.planner = planner
        self.approvals = approvals
        self.recovery = recovery
        self.audit = audit
        self.jobs = jobs
        self.workflow = workflow
        self.intents = intents
        self.resources = resources
        self.executor = BrowserExecutor(driver)
        self.cancel_event = asyncio.Event()
        self.resume_event = asyncio.Event()
        self.resume_cb: Any = None  # set by the TaskManager: resumes a waiting task by id
        self.browser_unavailable_cb: Any = None  # set by the TaskManager: no browser could take a task
        self.action_count = 0
        self.model_calls_at_start = 0
        self.history: list[dict[str, Any]] = []
        self.loops = LoopDetector()
        self.deadline = 0.0
        self.task: dict[str, Any] = {}
        self.tab_id: str = ""
        self.last_snapshot: PageSnapshot | None = None
        self.message = ""
        self._owns_tab = False
        self._settle_next = True  # first snapshot of a task also waits for the page to settle
        self._adopted_tabs: list[str] = []  # job tabs left behind when an Apply opened a new tab

    # ------------------------------------------------------------------ primitives available to workflows
    def _check_bounds(self) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled("task cancelled by user")
        if self.policy.paused:
            raise TaskCancelled("automation paused")
        if time.monotonic() > self.deadline:
            raise AgentError("task deadline exceeded", category=ErrorCategory.RESOURCE_LIMIT_REACHED)
        if self.action_count >= self.s.max_actions_per_task:
            raise AgentError(f"max actions per task ({self.s.max_actions_per_task}) reached", category=ErrorCategory.RESOURCE_LIMIT_REACHED)
        if self.resources and self.s.pause_on_resource_pressure:
            pressured, why = self.resources.under_pressure()
            if pressured:
                raise AgentError(f"resource pressure: {why}", category=ErrorCategory.RESOURCE_LIMIT_REACHED)

    async def _home_tab(self) -> tuple[str | None, str | None]:
        """(home list URL, id of an open tab showing it) from the scheduler config, if any."""
        try:
            raw = await self.repos.db.fetch_value("SELECT data_json FROM scheduler_config WHERE id = 1")
            home = str(json.loads(raw).get("home_list_url") or "") if raw else ""
        except Exception:  # noqa: BLE001 - no config yet
            home = ""
        if not home:
            return None, None
        try:
            for t in await self.driver.list_tabs():
                if str(t.url or "").split("#")[0].startswith(home.split("#")[0]):
                    return home, str(t.tab_id)
        except Exception:  # noqa: BLE001 - listing tabs is best effort
            pass
        return home, None

    async def _wait_for_resources(self, max_wait_s: float = 120.0) -> None:
        """Memory pressure from other programs is usually brief: wait for it to pass (checking every
        5 s, up to 2 minutes) before _check_bounds gives up on the job."""
        if not (self.resources and self.s.pause_on_resource_pressure):
            return
        waited = 0.0
        while waited < max_wait_s and not self.cancel_event.is_set():
            pressured, why = self.resources.under_pressure()
            if not pressured:
                return
            if waited == 0:
                log.info("waiting for resources: %s", why)
            self.message = f"waiting for free memory ({why})"
            await asyncio.sleep(5)
            waited += 5

    async def snapshot(self) -> PageSnapshot:
        """Current page. After a navigation, tab open or click, waits (up to ~8 s) until the page
        stops changing, so app-rendered sites (Workday, Ashby…) are read once they have drawn."""
        await self._wait_for_resources()
        self._check_bounds()
        try:
            snap = await self.driver.snapshot(self.tab_id, max_nodes=self.s.snapshot_max_nodes)
        except BrowserDisconnectedError as exc:
            # A frozen background tab cannot run the content script: bring it forward, retry once.
            log.info("snapshot of tab %s failed (%s); activating and retrying", self.tab_id, exc)
            try:
                await self.driver.activate_tab(self.tab_id)
            except Exception:  # noqa: BLE001
                log.debug("could not activate tab %s", self.tab_id, exc_info=True)
            await asyncio.sleep(1.5)
            snap = await self.driver.snapshot(self.tab_id, max_nodes=self.s.snapshot_max_nodes)
        if self._settle_next:
            self._settle_next = False
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.8)
                nxt = await self.driver.snapshot(self.tab_id, max_nodes=self.s.snapshot_max_nodes)
                stable = nxt.content_hash == snap.content_hash and nxt.url == snap.url
                snap = nxt
                if stable and len(snap.nodes) >= 5:
                    break
        self.last_snapshot = snap
        return snap

    async def resolve_payload(self, action: BrowserAction) -> dict[str, Any] | None:
        if action.type != "upload":
            return None
        resume = await self.repos.profile.get_resume(str(action.args.get("resume_id", "")))
        if resume is None:
            raise PermissionDeniedError("upload refers to an unknown resume")
        path = Path(resume["file_path"])
        if not path.exists():
            raise PermissionDeniedError(f"resume file missing: {path.name}")
        data = path.read_bytes()
        from ..core.util import sha256_hex

        if sha256_hex(data) != resume["sha256"]:
            raise PermissionDeniedError("resume file changed on disk since it was registered; re-upload it")
        return {"file_name": resume["file_name"], "mime_type": resume["mime_type"], "bytes_b64": base64.b64encode(data).decode()}

    async def execute(self, action: BrowserAction, payload: dict[str, Any] | None = None, *, explicit_submit: bool = False) -> ActionResult:
        """Policy-checked execution with verification, loop detection and persistence."""
        await self._wait_for_resources()
        self._check_bounds()
        snap = self.last_snapshot
        current_url = snap.url if snap else ""
        target_name = None
        field_label = None
        if action.target and action.target.strategy == "node" and snap:
            node = snap.node(int(action.target.value))
            if node:
                target_name = node.name
                field_label = node.name or node.label or node.placeholder
        is_submit = explicit_submit or (action.type == "click" and bool(action.expected_outcome) and "confirmation" in action.expected_outcome.lower())
        verdict = self.policy.evaluate(action, current_url=current_url, target_name=target_name, field_label=field_label, explicit_submit=is_submit)
        await self.audit.log("policy.evaluated", task_id=self.task["id"], action=action.type, decision=verdict.decision, risk=verdict.risk.value, reason=verdict.reason)
        if verdict.decision == "deny":
            raise PermissionDeniedError(verdict.reason, details=verdict.details)
        if verdict.decision == "needs_approval":
            approved = await self.request_approval(verdict.approval_kind or "high_risk_action", {"summary": f"{action.type} '{target_name or (action.target.value if action.target else '')}' on {current_url}", "action": action.model_dump(mode='json')})
            if not approved:
                raise NeedsUserInputError("action was not approved")
        if verdict.wait_seconds > 0:
            await asyncio.sleep(verdict.wait_seconds)
        page_hash = snap.content_hash if snap else ""
        if self.loops.check(action, page_hash):
            raise AgentError("loop detected: identical action repeated on an unchanged page", category=ErrorCategory.ELEMENT_NOT_FOUND if action.type != "click" else ErrorCategory.SITE_UNSUPPORTED)

        self.action_count += 1
        seq = self.action_count
        result = await self.executor.execute(self.tab_id, action, payload)
        if self._page_unresponsive(action, result, current_url):
            # Chrome freezes/throttles background tabs: bring this one to the front and retry once.
            log.info("tab %s unresponsive during %s (%s); activating and retrying", self.tab_id, action.type, result.message)
            try:
                await self.driver.activate_tab(self.tab_id)
                await asyncio.sleep(1.5)
            except Exception:  # noqa: BLE001
                log.debug("could not activate tab %s", self.tab_id, exc_info=True)
            result = await self.executor.execute(self.tab_id, action, payload)
        self.policy.record_action(current_url)
        if action.type in ("navigate", "click", "press_key"):
            self._settle_next = True
        after: PageSnapshot | None = None
        if action.verification not in ("none", "value_set"):
            try:
                after = await self.snapshot()
            except AgentError:
                after = None
        ver = verify_action(action, result, snap, after)
        entry = {"seq": seq, "type": action.type, "target": action.target.model_dump() if action.target else None, "ok": result.ok, "verified": ver.ok, "message": result.message or ver.message, "url": result.url}
        self.history.append(entry)
        recorded_action = action.model_dump(mode="json")
        recorded_result = {**result.model_dump(mode="json"), "verification": {"ok": ver.ok, "strategy": ver.strategy, "message": ver.message}}
        if recorded_action.get("args", {}).get("credential"):
            # A stored site password is typed, never written to the task log.
            recorded_action["args"]["text"] = "***"
            recorded_result["value"] = "***"
        await self.repos.tasks.add_attempt(self.task["id"], seq, recorded_action, recorded_result, result.ok and ver.ok, result.duration_ms)
        await self.repos.tasks.update_progress(self.task["id"], {"last_action": entry, "message": self.message}, action_count=self.action_count)
        if not result.ok and result.error_category == "PERMISSION_DENIED":
            raise PermissionDeniedError(result.message)
        return result

    def _created_tab_count(self) -> int:
        hub = getattr(self.driver, "_hub", None)
        return len(getattr(hub, "created_tabs", []) or [])

    async def _adopt_new_tab(self, since: int, wait_s: float = 4.0) -> bool:
        """Switch to a tab the current tab opened after ``since`` (an "Apply" that opens the company
        site in a new tab). The previous tab is remembered and closed with the job."""
        hub = getattr(self.driver, "_hub", None)
        if hub is None:
            return False
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            for t in list(getattr(hub, "created_tabs", []) or [])[since:]:
                # Sites that open the employer's page with "noopener" leave no opener: a tab created
                # right after the click is still the one the click opened.
                opener = t.get("opener_tab_id")
                if (str(opener) == str(self.tab_id) or opener in (None, "", 0)) and t.get("tab_id") and str(t.get("tab_id")) != str(self.tab_id):
                    self._adopted_tabs.append(self.tab_id)
                    self.tab_id = str(t["tab_id"])
                    self._settle_next = True
                    log.info("following the application into new tab %s", self.tab_id)
                    return True
            await asyncio.sleep(0.4)
        return False

    async def _watch_captcha(self, task_id: str, tab_id: str, *, max_minutes: float = 30.0) -> None:
        """Poll the paused tab (read-only) until the user has completed its verification box, then
        resume the task so it submits. Never touches the check itself."""
        deadline = time.monotonic() + max_minutes * 60
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            t = await self.repos.tasks.get(task_id)
            if not t or t["status"] != TaskStatus.WAITING_USER:
                return  # resumed or cancelled by the user meanwhile
            try:
                snap = await self.driver.snapshot(tab_id, max_nodes=50)
            except Exception:  # noqa: BLE001 - a background tab can be briefly unresponsive
                try:
                    if not any(str(t.tab_id) == str(tab_id) for t in await self.driver.list_tabs()):
                        return  # the tab is gone: nothing to watch
                except Exception:  # noqa: BLE001
                    pass
                continue
            if snap.signals.captcha_solved:
                log.info("verification completed by the user on tab %s; resuming task %s", tab_id, task_id[:8])
                _WAITING.pop(str(tab_id), None)
                try:
                    await self.resume_cb(task_id)
                except Exception:  # noqa: BLE001
                    log.exception("could not resume task %s after the verification", task_id[:8])
                return

    async def _alert_user(self, tab_id: str | None, url: str, *, challenge: bool = False) -> None:
        """Desktop alert: a job needs the user to sign in / pass a check; the agent resumes after.
        One notification for all waiting jobs (replaced in place), not one per job."""
        from urllib.parse import urlsplit

        host = urlsplit(url or "").netloc or "a job site"
        _WAITING[str(tab_id)] = ("check", host) if challenge else ("signin", host)
        n = len(_WAITING)
        if n == 1:
            title = f"Verification needed on {host}" if challenge else f"Sign in to continue: {host}"
            message = (
                "The form is filled. Click here, tick the verification box in that tab, and the agent submits by itself."
                if challenge
                else "A job application needs you to sign in. Click to open the tab, sign in, and the agent continues by itself."
            )
        else:
            checks = sum(1 for k, _ in _WAITING.values() if k == "check")
            signins = n - checks
            parts = [f"{checks} filled form(s) wait for your verification tick" if checks else "", f"{signins} need you to sign in" if signins else ""]
            title = f"{n} jobs need you"
            message = "; ".join(p for p in parts if p) + f". Latest: {host}. Click to open it; each continues by itself afterwards."
        try:
            await self.driver.notify(title, message, tab_id=tab_id, key="lfa-waiting")
        except Exception:  # noqa: BLE001 - alerts are best effort
            log.debug("could not send the sign-in alert", exc_info=True)

    async def _app_has_live_task(self, application_id: str, own_task_id: str) -> bool:
        """Whether another task is running right now for this application."""
        try:
            rows = await self.repos.tasks.db.fetch_all(
                "SELECT id FROM tasks WHERE application_id = ? AND id != ? AND status IN ('running', 'queued', 'waiting_approval')",
                (application_id, own_task_id),
            )
        except Exception:  # noqa: BLE001 - when unsure, assume it is live and leave it alone
            return True
        return bool(rows)

    @staticmethod
    def _page_unresponsive(action: BrowserAction, result: ActionResult, url_before: str) -> bool:
        msg = (result.message or "").lower()
        if not result.ok and re.search(r"did not answer|injection timed out|not respond|timed out", msg):
            return True
        # A field action reported as "navigation" while the page stayed put means the page never answered.
        same_page = (result.url or "").split("#")[0] == (url_before or "").split("#")[0]
        return action.type in ("type", "select", "check", "upload", "read_options") and "navigation occurred" in msg and same_page

    async def request_approval(self, kind: str, payload: dict[str, Any]) -> bool:
        approval = await self.approvals.request(kind, payload, task_id=self.task["id"], application_id=self.task.get("application_id"))
        await self.repos.tasks.set_status(self.task["id"], TaskStatus.WAITING_APPROVAL, progress={"message": payload.get("summary", "awaiting approval"), "approval_id": approval["id"]})
        self.message = "awaiting approval"
        waited_from = time.monotonic()
        ok = await self.approvals.wait(approval["id"], timeout=self.s.task_deadline_seconds)
        self.deadline += time.monotonic() - waited_from  # time spent waiting for a person is not the job's time
        self._check_bounds()
        await self.repos.tasks.set_status(self.task["id"], TaskStatus.RUNNING)
        return ok

    # ------------------------------------------------------------------ main entry
    async def run(self, task: dict[str, Any]) -> dict[str, Any]:
        self.task = task
        self.deadline = time.monotonic() + self.s.task_deadline_seconds
        self.tab_id = str(task.get("tab_id") or "")
        await self.repos.tasks.set_status(task["id"], TaskStatus.RUNNING)
        await self.audit.log("task.started", task_id=task["id"], kind=task["kind"])
        try:
            if task["kind"] == "track":
                result = await self._run_track(task)
                await self.repos.tasks.set_status(task["id"], TaskStatus.SUCCEEDED, result=result, action_count=0)
                await self.audit.log("task.succeeded", task_id=task["id"], actions=0)
                return {"status": "succeeded", "result": result}
            if not self.tab_id:
                url = task.get("domain") or ""
                # A task with no tab of its own (dashboard / API) opens next to the job list's tab
                # when one is open, so it lands in the agent's window, never the user's current one.
                home_url, opener = await self._home_tab()
                self.tab_id = await self.driver.open_tab(url if url.startswith(("http://", "https://", "file://")) else "about:blank", opener_tab_id=opener, home_url=home_url)
                self._owns_tab = True
                await self.repos.tasks.set_status(task["id"], TaskStatus.RUNNING, tab_id=int(self.tab_id) if self.tab_id.isdigit() else None)
            if task["kind"] == "apply":
                result = await self._run_apply(task)
            elif task["kind"] == "apply_list":
                result = await self._run_apply_list(task)
            elif task["kind"] == "contacts":
                from ..jobs.contacts import find_contacts

                result = find_contacts(await self.snapshot())
                result["summary"] = f"{len(result['emails'])} emails, {len(result['linkedin_profiles'])} LinkedIn profiles, {len(result['people'])} named recruiters on this page"
            elif task["kind"] == "extract_job":
                result = await self._run_extract(task)
            elif task["kind"] == "navigate":
                result = await self._run_navigate(task)
            elif task["kind"] in ("ask", "read"):
                result = await self._run_ask(task)
            elif task["kind"] == "fill_page":
                result = await self._run_fill_page(task)
            elif task["kind"] == "edit_page":
                result = await self._run_edit_page(task)
            else:
                result = await self._run_general(task)
            await self.repos.tasks.set_status(task["id"], TaskStatus.SUCCEEDED, result=result, action_count=self.action_count)
            await self.audit.log("task.succeeded", task_id=task["id"], actions=self.action_count)
            return {"status": "succeeded", "result": result}
        except (AuthRequiredError, HumanVerificationError, NeedsUserInputError) as exc:
            failure = exc.to_failure()
            await self.repos.tasks.set_status(task["id"], TaskStatus.WAITING_USER, failure=failure.to_dict(), progress={"message": str(exc)}, action_count=self.action_count)
            if isinstance(exc, (AuthRequiredError, HumanVerificationError)) and self.tab_id:
                await self._alert_user(self.tab_id, (self.last_snapshot.url if self.last_snapshot else ""), challenge=isinstance(exc, HumanVerificationError))
            if self.tab_id and hasattr(self.driver, "protect_tab"):
                await self.driver.protect_tab(self.tab_id)  # the filled page must not be discarded while it waits
            if isinstance(exc, HumanVerificationError) and exc.details.get("captcha_on_form") and self.tab_id and self.resume_cb:
                # The form is filled; only the on-page check is left to the user. Watch for it being
                # completed and continue (submit) by itself, so one tick is all the user does.
                asyncio.create_task(self._watch_captcha(task["id"], self.tab_id), name="captcha-watch-" + task["id"][:8])
            await self.audit.log("task.waiting_user", level="warning", task_id=task["id"], category=failure.category.value, message=str(exc))
            return {"status": "waiting_user", "failure": failure.to_dict()}
        except TaskCancelled as exc:
            failure = exc.to_failure()
            await self.repos.tasks.set_status(task["id"], TaskStatus.CANCELLED, failure=failure.to_dict(), action_count=self.action_count)
            await self._mark_application_after_failure(task, failure)
            await self.audit.log("task.cancelled", task_id=task["id"])
            return {"status": "cancelled", "failure": failure.to_dict()}
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded
            failure = classify_exception(exc)
            log.warning("task %s failed: %s", task["id"], failure.message, exc_info=not isinstance(exc, AgentError))
            await self.repos.tasks.set_status(task["id"], TaskStatus.FAILED, failure=failure.to_dict(), action_count=self.action_count)
            await self._mark_application_after_failure(task, failure)
            await self.audit.log("task.failed", level="error", task_id=task["id"], category=failure.category.value, message=failure.message)
            return {"status": "failed", "failure": failure.to_dict()}
        finally:
            await self.approvals.cancel_for_task(task["id"])
            # Tabs the runner opened are closed unless the task is handing off to the user, who
            # needs the page (login, challenge, review) to stay open.
            status = (await self.repos.tasks.get(task["id"]) or {}).get("status")
            if self.tab_id and (not self._owns_tab or status == TaskStatus.WAITING_USER.value):
                # The page stays with the user: take the progress banner down so it does not
                # show a stale "Clicking Save and Continue" over their work.
                try:
                    await self.driver.execute(self.tab_id, BrowserAction(type="banner", args={"text": ""}))
                except Exception:  # noqa: BLE001 - best effort
                    pass
            if self._owns_tab and status != TaskStatus.WAITING_USER.value:
                try:
                    await self.driver.close_tab(self.tab_id)
                except Exception:
                    log.debug("could not close tab %s", self.tab_id, exc_info=True)

    async def _mark_application_after_failure(self, task: dict[str, Any], failure) -> None:  # type: ignore[no-untyped-def]
        app_id = task.get("application_id")
        if not app_id:
            return
        app = await self.repos.applications.get(app_id)
        if not app or app["state"] not in (ApplicationState.IN_PROGRESS, ApplicationState.QUEUED, ApplicationState.RETRY_SCHEDULED):
            return
        if app["state"] != ApplicationState.IN_PROGRESS and failure.category == ErrorCategory.BROWSER_DISCONNECTED:
            # No browser could take the task (extension offline, page limit reached): the job itself
            # was never touched, so this is not one of its attempts. It stays queued for a later
            # look, and the scheduler holds off instead of re-creating the task every tick, which
            # used to burn through a job's retries in minutes without ever reaching its page.
            from datetime import timedelta

            from ..core.util import now_dt

            next_at = (now_dt() + timedelta(seconds=self.BROWSER_RETRY_SECONDS)).isoformat(timespec="seconds")
            await self.repos.applications.update_fields(app_id, {"next_attempt_at": next_at, "failure": failure.to_dict()})
            if self.browser_unavailable_cb is not None:
                try:
                    self.browser_unavailable_cb(failure.message[:160])
                except Exception:  # noqa: BLE001 - best effort
                    log.debug("browser-unavailable hook failed", exc_info=True)
            await self.audit.log("application.browser_unavailable", level="warning", application_id=app_id, task_id=task["id"], message=failure.message[:200])
            return
        if app["state"] != ApplicationState.IN_PROGRESS:
            # Failed before the workflow started: still one attempt of this job.
            app = await self.repos.applications.transition(app_id, ApplicationState.IN_PROGRESS, reason="attempt started", fields={"attempt_count": int(app.get("attempt_count") or 0) + 1, "last_attempt_at": now_iso()})
        await self._settle_failed_application(app, failure)

    BROWSER_RETRY_SECONDS = 300

    async def _settle_failed_application(self, app: dict[str, Any], failure) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        """An IN_PROGRESS application whose attempt just failed. One more automatic attempt only for
        a transient failure (browser hiccup, rate limit, site down) within the retry limit; every
        other failure is final: the job is left alone until the user retries it from the dashboard."""
        decision = self.recovery.decide(failure, int(app.get("attempt_count") or 1))
        reason = f"{failure.category.value}: {failure.message[:200]}"
        if decision.retry:
            from datetime import timedelta

            from ..core.util import now_dt

            next_at = (now_dt() + timedelta(seconds=decision.wait_seconds)).isoformat(timespec="seconds")
            return await self.repos.applications.transition(app["id"], ApplicationState.RETRY_SCHEDULED, reason=reason, fields={"failure": failure.to_dict(), "next_attempt_at": next_at})
        return await self.repos.applications.transition(app["id"], ApplicationState.APPLICATION_FAILED, reason=reason, fields={"failure": failure.to_dict()})

    # ------------------------------------------------------------------ task kinds
    async def _run_apply(self, task: dict[str, Any]) -> dict[str, Any]:
        app_id = task.get("application_id")
        if not app_id:
            raise AgentError("apply task without application", category=ErrorCategory.UNKNOWN)
        ctx = WorkflowContext(application_id=app_id, task_id=task["id"], tab_id=self.tab_id, execute=self.execute, snapshot=self.snapshot, request_approval=self.request_approval, resolve_payload=self.resolve_payload, created_tabs=self._created_tab_count, adopt_new_tab=self._adopt_new_tab, user_tab=not self._owns_tab)
        self.message = "running application workflow"
        return await self.workflow.run(ctx)

    async def _run_apply_list(self, task: dict[str, Any]) -> dict[str, Any]:
        """Work through every job linked from the current page: open it in its own tab, import it,
        run the application workflow, record the outcome, close the tab, return to the list.
        One job's failure or pause never stops the others."""
        from ..jobs.listing import extract_job_links

        list_tab = self.tab_id
        snap = await self.snapshot()
        home = ""
        try:
            home = str((await self.repos.db.fetch_value("SELECT data_json FROM scheduler_config WHERE id = 1")) or "")
            home = str(__import__("json").loads(home).get("home_list_url") or "") if home else ""
        except Exception:
            home = ""
        # The list is the page the user is on — a saved report, a filtered view, any site. The
        # app's default job list is only a fallback when this page turns out to hold no jobs.
        dry_run = bool(re.search(r"\b(dry[- ]?run|preview|simulate|do not submit|don'?t submit)\b", task.get("instruction") or "", re.I))
        limit = int(self.s.daily_application_limit)
        instruction = task.get("instruction") or ""
        m = re.search(r"\b(?:first|top|only)\s+(\d{1,3})\b", instruction, re.I)
        if m:
            limit = max(1, int(m.group(1)))
        elif re.search(r"\b(each|every|all)\b", instruction, re.I):
            limit = 300  # "apply to each job": the whole list, not a daily slice of it
        scan = max(limit, 300)  # rank the whole list (lists of 100+ postings), then take `limit`
        if re.search(r"\b(each|every|all)\b", instruction, re.I) and not m:
            # "Apply to each job" while a source filter shows only part of the list ("LinkedIn · 20"
            # of "All boards · 110"): widen it first so every posting is included.
            widen = next((n for n in snap.nodes if n.role in ("button", "tab", "link") and _ALL_FILTER_RE.match((n.name or "").strip())), None)
            if widen is not None:
                await self.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(widen.i)), args={}, verification="none", timeout_ms=10_000, expected_outcome="show every posting"))
                await self.audit.log("apply_list.widen_filter", task_id=task["id"], button=widen.name)
                snap = await self.snapshot()
        links = extract_job_links(snap, max_links=scan)
        if not links:
            links = await self._reveal_job_list(snap, scan)
        if len(links) < 2:
            # Unfamiliar layout: let the local model read the page and pick the job links.
            from .navigator import choose_job_links

            self.message = "reading the page for jobs"
            picked = await choose_job_links(self.intents.gateway, self.last_snapshot or snap, task_id=task["id"])
            known = {l["url"] for l in links}
            links += [l for l in picked if l["url"] not in known]
            if picked:
                await self.audit.log("apply_list.model_links", task_id=task["id"], count=len(picked))
        from ..jobs.listing import account_needed_rank

        # Jobs that can be completed without an account first; account-wall sites last.
        if not links and home and snap.url.startswith(home.split("#")[0]) and not snap.url.startswith(home):
            # Nothing on this view of the Job Hunt app: fall back to its job list once.
            await self.audit.log("apply_list.fallback_home", task_id=task["id"], from_url=snap.url)
            await self.execute(BrowserAction(type="navigate", args={"url": home}, verification="navigation_complete", timeout_ms=30_000))
            snap = await self.snapshot()
            links = extract_job_links(snap, max_links=scan) or await self._reveal_job_list(snap, scan)
        links = sorted(links, key=lambda l: account_needed_rank(l["url"]))[:limit]
        await self.audit.log("apply_list.started", task_id=task["id"], links=len(links), url=self.last_snapshot.url if self.last_snapshot else snap.url)
        if not links:
            raise AgentError("no job links found on this page, even after opening its lists", category=ErrorCategory.SITE_UNSUPPORTED)
        await self.execute(BrowserAction(type="banner", args={"text": f"Found {len(links)} jobs; applying one by one"}))
        from ..core.errors import BrowserDisconnectedError
        from ..jobs.canonical import canonicalize_url, dedup_key

        # New tabs open in the list's own window when the extension supports it; otherwise each
        # job is opened inside the list tab itself, so nothing lands in another window.
        same_window = "open_in_window" in await self.driver.capabilities()
        list_url = snap.url
        self._list_tab = list_tab
        outcomes: list[dict[str, Any]] = []
        total_actions = 0
        kept_open = 0  # tabs left open for the user (sign-in, missing answers); capped
        max_kept_open = 5 if same_window else 0
        for index, link in enumerate(links, start=1):
            if self.cancel_event.is_set() or self.policy.paused:
                break
            item: dict[str, Any] = {"url": link["url"], "text": link["text"]}
            tab_id = ""
            # Never repeat a job that was already applied to (checked before opening anything).
            known = await self.repos.jobs.find_by_dedup_key(dedup_key(canonicalize_url(link["url"])))
            if known:
                prior = await self.repos.applications.get_by_job(known["id"])
                if prior and prior["state"] in ("APPLIED_CONFIRMED", "DUPLICATE"):
                    item.update({"state": prior["state"], "note": "already applied; skipped"})
                    outcomes.append(item)
                    continue
            try:
                # Bounds are per job: a long list must not exhaust the task deadline.
                self.deadline = time.monotonic() + self.s.task_deadline_seconds
                total_actions += self.action_count
                self.action_count = 0
                self.loops.reset()
                self._adopted_tabs = []
                self.history = []
                if same_window:
                    tab_id = await self.driver.open_tab(link["url"], active=True, opener_tab_id=list_tab)
                else:
                    tab_id = list_tab
                    await self.driver.navigate(list_tab, link["url"], timeout_ms=30_000)
                self.tab_id = tab_id
                self._settle_next = True
                self.message = f"job {index}/{len(links)}"
                await self.repos.tasks.update_progress(task["id"], {"message": self.message, "current_url": link["url"], "done": outcomes})
                try:
                    page = await self.snapshot()
                except BrowserDisconnectedError:
                    await asyncio.sleep(3)  # slow page or content script start-up: one retry
                    self._settle_next = True
                    page = await self.snapshot()
                cls = classify_page(page)
                if cls.kind == "challenge" and "bot-protection check page" in cls.reasons:
                    # "Just a moment…" checks usually clear by themselves: wait and look again
                    # (never click or solve them) before asking the user.
                    for _ in range(10):
                        await asyncio.sleep(2)
                        page = await self.snapshot()
                        cls = classify_page(page)
                        if cls.kind != "challenge":
                            break
                if cls.needs_human and dry_run:
                    item.update({"verdict": "sign_in_needed", "state": "AUTH_REQUIRED", "note": f"{cls.kind} page at {page.url[:120]}"})
                    outcomes.append(item)
                    await self._close_item_tab(tab_id)
                    continue
                if cls.needs_human:
                    if kept_open < max_kept_open:
                        kept_open += 1
                        item.update({"state": "AUTH_REQUIRED", "note": f"sign-in needed on {page.url}; tab left open"})
                        await self._alert_user(tab_id, page.url, challenge=cls.kind == "challenge")
                    else:
                        item.update({"state": "AUTH_REQUIRED", "note": f"sign-in needed on {page.url}; tab closed (open-tab limit)"})
                        await self._close_item_tab(tab_id)
                    outcomes.append(item)
                    continue
                job, dup = await self.jobs.import_from_snapshot(page, source_kind="manual", task_id=task["id"], screen_with_model=False)
                app = await self.repos.applications.get_by_job(job["id"])
                item["job_id"] = job["id"]
                if app is not None and app["state"] == "IN_PROGRESS" and not await self._app_has_live_task(app["id"], task["id"]):
                    # Left "in progress" by a cancelled or crashed run: nothing is working on it.
                    app = await self.repos.applications.transition(app["id"], ApplicationState.RETRY_SCHEDULED, reason="stale in-progress state from an interrupted run")
                if app is not None and app["state"] == "APPLICATION_FAILED" and ((app.get("failure_json") or {}).get("category") == "SUBMISSION_UNCERTAIN" or app.get("verification_status") == "uncertain"):
                    # It may already have been sent: never submit twice. The user checks it.
                    item.update({"state": app["state"], "note": "submitted earlier without a visible confirmation; check your email before re-applying"})
                    outcomes.append(item)
                    await self._close_item_tab(tab_id)
                    continue
                if app is None or app["state"] in ("APPLIED_CONFIRMED", "DUPLICATE", "CANCELLED", "IN_PROGRESS", "SUBMISSION_PENDING"):
                    item.update({"state": (app or {}).get("state", "DUPLICATE"), "note": "already handled"})
                    outcomes.append(item)
                    await self._close_item_tab(tab_id)
                    continue
                if app["state"] == "RETRY_SCHEDULED" and int(app.get("attempt_count") or 0) > self.recovery.max_retries:
                    app = await self.repos.applications.transition(app["id"], ApplicationState.APPLICATION_FAILED, reason=f"retry limit {self.recovery.max_retries} exceeded")
                if app["state"] == "APPLICATION_FAILED" and ((app.get("failure_json") or {}).get("category") or "") != "USER_CANCELLED":
                    # Failed in an earlier run: a job that failed for its own reasons is not tried
                    # again and again on every pass over the list. "Retry now" in the dashboard
                    # queues it once more (a run the user paused or cancelled is picked up again).
                    why = (app.get("failure_json") or {}).get("category") or "failed earlier"
                    item.update({"state": app["state"], "note": f"failed earlier ({why}); not retried. Use Retry now in the dashboard to try it again"})
                    outcomes.append(item)
                    await self.audit.log("apply_list.failed_earlier", task_id=task["id"], url=link["url"], category=why)
                    await self._close_item_tab(tab_id)
                    continue
                item["application_id"] = app["id"]
                blockers = eligibility_blockers(job, await self.repos.profile.get() or {})
                if blockers:
                    # Applying cannot succeed (e.g. clearance-only role): skip without touching the form.
                    if app["state"] != "SKIPPED":
                        via = {"IMPORTED": ApplicationState.SCREENING, "SCREENING": None, "ELIGIBLE": None, "QUEUED": None}.get(app["state"], ApplicationState.QUEUED)
                        try:
                            if via is not None:
                                app = await self.repos.applications.transition(app["id"], via, reason="eligibility check")
                            app = await self.repos.applications.transition(app["id"], ApplicationState.SKIPPED, reason="; ".join(blockers)[:400])
                        except Exception as exc:  # unusual state: leave it, still do not apply
                            log.warning("could not mark %s skipped: %s", app["id"], exc)
                    item.update({"state": "SKIPPED", "note": blockers[0][:200]})
                    outcomes.append(item)
                    await self.audit.log("apply_list.ineligible", task_id=task["id"], url=link["url"], reasons=blockers)
                    await self._close_item_tab(tab_id)
                    continue
                if dry_run:
                    item.update(await self._preview_application(page, job_location=job.get("location")))
                    outcomes.append(item)
                    await self.audit.log("apply_list.preview", task_id=task["id"], index=index, url=link["url"], verdict=item.get("verdict"))
                    await self._close_item_tab(tab_id)
                    continue
                if app["state"] in ("ELIGIBLE", "SKIPPED", "IMPORTED", "SCREENING", "APPLICATION_FAILED", "RETRY_SCHEDULED", "AUTH_REQUIRED", "NEEDS_USER_INPUT", "READY_FOR_REVIEW"):
                    app = await self.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="queued by list apply", fields={"next_attempt_at": None})
                ctx = WorkflowContext(application_id=app["id"], task_id=task["id"], tab_id=tab_id, execute=self.execute, snapshot=self.snapshot, request_approval=self.request_approval, resolve_payload=self.resolve_payload, created_tabs=self._created_tab_count, adopt_new_tab=self._adopt_new_tab)
                try:
                    result = await self.workflow.run(ctx)
                    item.update({"state": result.get("state"), "reference": result.get("reference_number"), **({"note": result["note"]} if result.get("note") else {})})
                    await self._close_item_tab(tab_id)
                except (AuthRequiredError, HumanVerificationError, NeedsUserInputError) as exc:
                    item.update({"state": (await self.repos.applications.get(app["id"]) or {}).get("state"), "note": str(exc)[:160]})
                    if kept_open < max_kept_open:
                        # Left open with a waiting task: the auto-resume hook continues it after sign-in.
                        kept_open += 1
                        if self.tab_id != tab_id:
                            # The application continued in a tab its Apply opened: keep that one.
                            for old in dict.fromkeys([tab_id, *self._adopted_tabs]):
                                if old != self.tab_id and old != list_tab:
                                    try:
                                        await self.driver.close_tab(old)
                                    except Exception:  # noqa: BLE001
                                        pass
                            self._adopted_tabs = []
                            tab_id = self.tab_id
                        if isinstance(exc, (AuthRequiredError, HumanVerificationError)):
                            await self._alert_user(tab_id, link["url"], challenge=isinstance(exc, HumanVerificationError))
                        waiting = await self.repos.tasks.create("apply", application_id=app["id"], job_id=job["id"], tab_id=int(tab_id) if tab_id.isdigit() else None, domain=link["url"])
                        await self.repos.tasks.set_status(waiting["id"], TaskStatus.WAITING_USER, failure=exc.to_failure().to_dict(), progress={"message": str(exc)[:200]})
                    else:
                        await self._close_item_tab(tab_id)
                except SubmissionUncertainError as exc:
                    # Submitted but no confirmation and no readable error: leave the page open so the
                    # outcome can be checked by eye instead of losing it.
                    item.update({"state": (await self.repos.applications.get(app["id"]) or {}).get("state"), "failure": "SUBMISSION_UNCERTAIN", "note": f"{str(exc)[:120]}; tab left open to check"})
                    if kept_open < max_kept_open:
                        kept_open += 1
                    else:
                        await self._close_item_tab(tab_id)
                except TaskCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - recorded per job
                    failure = classify_exception(exc)
                    current = await self.repos.applications.get(app["id"]) or {}
                    if current.get("state") in ("IN_PROGRESS", "QUEUED"):
                        # The workflow recorded no outcome: settle it here so it is not left "in
                        # progress" (which list-apply treats as handled). Only a transient failure
                        # within the retry limit is tried again later; a job that fails for its own
                        # reasons is marked failed and left alone.
                        try:
                            if current["state"] == "QUEUED":
                                current = await self.repos.applications.transition(app["id"], ApplicationState.IN_PROGRESS, reason="task started", fields={"attempt_count": int(current.get("attempt_count") or 0) + 1, "last_attempt_at": now_iso()})
                            current = await self._settle_failed_application(current, failure)
                        except Exception as texc:  # noqa: BLE001
                            log.warning("could not settle %s after a failure: %s", app["id"], texc)
                    item.update({"state": current.get("state"), "failure": failure.category.value, "note": failure.message[:160]})
                    await self._close_item_tab(tab_id)
            except TaskCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                failure = classify_exception(exc)
                item.update({"state": "FAILED", "failure": failure.category.value, "note": failure.message[:160]})
                if tab_id:
                    await self._close_item_tab(tab_id)
            outcomes.append(item)
            await self.audit.log("apply_list.item", task_id=task["id"], index=index, **{k: v for k, v in item.items() if k in ("state", "failure", "url")})
            try:
                await self.driver.activate_tab(list_tab)
            except Exception:
                pass
        self.tab_id = list_tab
        if not same_window:
            try:  # bring the list tab back to the job list it started on
                await self.driver.navigate(list_tab, home or list_url, timeout_ms=30_000)
                self._settle_next = True
                await self.snapshot()
            except Exception:
                log.debug("could not return the list tab to %s", home or list_url, exc_info=True)
        summary = {"same_window": same_window, "dry_run": dry_run, "jobs": len(links), "processed": len(outcomes), "confirmed": sum(1 for o in outcomes if o.get("state") == "APPLIED_CONFIRMED"), "waiting_user": sum(1 for o in outcomes if o.get("state") in ("AUTH_REQUIRED", "NEEDS_USER_INPUT", "READY_FOR_REVIEW")), "failed": sum(1 for o in outcomes if o.get("failure")), "items": outcomes}
        if dry_run:
            summary["ready"] = sum(1 for o in outcomes if o.get("verdict") == "ready")
            summary["summary"] = f"Dry run: {summary['ready']} of {len(outcomes)} forms could be filled and submitted now; nothing was typed or submitted"
            await self.execute(BrowserAction(type="banner", args={"text": summary["summary"]}))
        else:
            await self.execute(BrowserAction(type="banner", args={"text": f"Done: {summary['confirmed']} applied, {summary['waiting_user']} need you, {summary['failed']} failed"}))
        # The run's total is recorded only now: added earlier, it tripped the per-job action budget
        # on the final banner and threw the whole summary away.
        self.action_count += total_actions
        return summary

    async def _preview_application(self, page: PageSnapshot, job_location: str | None = None) -> dict[str, Any]:
        """Dry run for one job page: reach the form (only 'Apply' clicks), plan the fill, report.
        Types nothing, uploads nothing, submits nothing."""
        from ..applications.profile import CandidateProfile
        from ..applications.workflow import looks_like_application
        from ..core.actions import Target

        from ..applications.workflow import find_apply_button, linkedin_easy_apply_only
        from ..jobs.listing import embedded_application_url

        profile = CandidateProfile.model_validate(await self.repos.profile.get() or {})
        answers = await self.repos.profile.list_answers()
        resumes = await self.repos.profile.list_resumes()
        snap = page
        for _ in range(4):
            cls = classify_page(snap)
            if cls.needs_human:
                return {"verdict": "sign_in_needed", "state": "AUTH_REQUIRED", "note": f"{cls.kind} page ({cls.auth_state.value}) at {snap.url[:90]}"}
            if snap.form_fields():
                break
            if linkedin_easy_apply_only(snap):
                return {"verdict": "easy_apply", "state": "SKIPPED", "note": "LinkedIn Easy Apply job; left alone"}
            embedded = embedded_application_url(snap)
            if embedded and embedded.split("#")[0] != snap.url.split("#")[0]:
                await self.execute(BrowserAction(type="navigate", args={"url": embedded}, verification="navigation_complete", timeout_ms=30_000))
                snap = await self.snapshot()
                continue
            apply_node = find_apply_button(snap)
            if apply_node is None:
                return {"verdict": "no_form", "note": f"no application form or Apply button on {snap.url[:90]}"}
            await self.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(apply_node.i)), args={}, timeout_ms=15_000, expected_outcome="application form opens"))
            snap = await self.snapshot()
        else:
            return {"verdict": "no_form", "note": "could not reach a form"}
        plan = await self.workflow._filler.plan(snap, profile, answers, resumes, submit_names=["Submit application", "Submit Application", "Submit", "Apply", "Send application"], job_location=job_location, model_budget_seconds=self.s.model_request_timeout_seconds)
        self.workflow._filler.discard_pending(plan)
        not_application = looks_like_application(snap, plan)
        if not_application:
            return {"verdict": "not_application", "note": not_application}
        if self.workflow._submit_policy == "never":
            # Same promotion the real run applies in unattended mode.
            from ..core.enums import Provenance

            for f in plan.fields:
                if f.status == "needs_review" and f.value and f.provenance in (Provenance.VERIFIED_FACT, Provenance.APPROVED_ANSWER):
                    f.status = "auto"
        auto = [f.label for f in plan.fields if f.status == "auto"]
        missing = [f.label for f in plan.fields if f.status in ("needs_input", "contradiction") and (f.required or f.legally_significant)]
        review = [f.label for f in plan.fields if f.status == "needs_review"]
        if missing:
            verdict = "needs_answers"
        elif review:
            verdict = "needs_review"  # the real run would stop at READY_FOR_REVIEW
        elif plan.submit_node is None and plan.next_node is None:
            verdict = "no_submit_button"
        else:
            verdict = "ready"
        return {"verdict": verdict, "form_url": snap.url[:200], "would_fill": auto[:40], "missing": missing[:20], "review": review[:20], "has_submit": plan.submit_node is not None, "multi_step": plan.next_node is not None}

    _OPENER_RE = re.compile(r"^(open|view|show|see|expand|browse|load more|show more|view (all|jobs|results)|see (all|jobs|results)|show (jobs|results))\b", re.I)
    _LIST_SECTION_RE = re.compile(r"(job|search|result|listing|opening|position|career|role)s?\b", re.I)
    _NEVER_CLICK_RE = re.compile(r"(remove|delete|clear|reset|sign ?out|log ?out|unsubscribe|hide|dismiss|close|cancel)", re.I)

    async def _reveal_job_list(self, snap: PageSnapshot, limit: int) -> list[dict[str, str]]:
        """The page shows no job links yet (collapsed sections, saved searches, 'Open' cards):
        click list-opening controls one at a time, the way a person would, until links appear."""
        from ..core.actions import Target
        from ..jobs.listing import extract_job_links

        tried: set[str] = set()
        for _ in range(6):
            candidates = [n for n in snap.nodes if n.role in ("button", "link", "tab") and not n.disabled and n.name and not self._NEVER_CLICK_RE.search(n.name)]
            openers = [n for n in candidates if self._OPENER_RE.match(n.name.strip())]
            sections = [n for n in candidates if n.tag in ("h1", "h2", "h3", "summary") or (n.role == "tab")]
            sections = [n for n in sections if self._LIST_SECTION_RE.search(n.name)]
            pick = next((n for n in openers + sections if f"{n.name}|{n.i}" not in tried), None)
            if pick is None:
                return []
            tried.add(f"{pick.name}|{pick.i}")
            await self.audit.log("apply_list.reveal", task_id=self.task["id"], clicked=pick.name[:60])
            await self.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(pick.i)), args={}, timeout_ms=15_000, expected_outcome="job list opens"))
            await asyncio.sleep(1.0)
            snap = await self.snapshot()
            links = extract_job_links(snap, max_links=limit)
            if links:
                return links
        return []

    async def _run_track(self, task: dict[str, Any]) -> dict[str, Any]:
        """Application status report; no browser needed."""
        from ..persistence.repositories import Page

        by_state = await self.repos.applications.counts_by_state()
        recent, total = await self.repos.applications.list(Page(15, 0), sort="updated_at")
        items = [{"company": a.get("company"), "title": a.get("title"), "state": a["state"], "verification": a.get("verification_status"), "reference": a.get("reference_number"), "updated_at": a.get("updated_at")} for a in recent]
        confirmed = by_state.get("APPLIED_CONFIRMED", 0)
        waiting = sum(by_state.get(s, 0) for s in ("AUTH_REQUIRED", "NEEDS_USER_INPUT", "READY_FOR_REVIEW"))
        failed = by_state.get("APPLICATION_FAILED", 0)
        return {"summary": f"{confirmed} applied (verified), {waiting} need you, {failed} failed, {total} tracked in total", "by_state": by_state, "recent": items}

    async def _close_item_tab(self, tab_id: str) -> None:
        # The job's tab plus any tab its Apply opened (the task followed the application there).
        for t in dict.fromkeys([tab_id, *self._adopted_tabs, self.tab_id]):
            if not t or t == getattr(self, "_list_tab", None):
                continue  # working inside the list tab: never close it (it is navigated back instead)
            try:
                await self.driver.close_tab(t)
            except Exception:
                log.debug("could not close tab %s", t, exc_info=True)
        self._adopted_tabs = []

    async def _run_extract(self, task: dict[str, Any]) -> dict[str, Any]:
        snap = await self.snapshot()
        if task.get("instruction") and task["instruction"].startswith("http"):
            await self.execute(BrowserAction(type="navigate", args={"url": task["instruction"]}, verification="navigation_complete", timeout_ms=self.s.navigation_timeout_ms))
            snap = await self.snapshot()
        cls = classify_page(snap)
        if cls.needs_human:
            raise AuthRequiredError(f"page requires authentication ({cls.auth_state.value}); log in and resume")
        job, dup = await self.jobs.import_from_snapshot(snap, source_kind="manual", task_id=task["id"], screen_with_model=False)
        await self.repos.tasks.set_status(task["id"], TaskStatus.RUNNING, job_id=job["id"])
        return {"job_id": job["id"], "duplicate": dup, "title": job.get("title"), "company": job.get("company"), "match_score": job.get("match_score")}

    async def _run_navigate(self, task: dict[str, Any]) -> dict[str, Any]:
        url = task.get("domain") or ""
        if not url.startswith("http"):
            raise AgentError("navigate task requires a URL", category=ErrorCategory.NAVIGATION_FAILED)
        res = await self.execute(BrowserAction(type="navigate", args={"url": url}, verification="navigation_complete", timeout_ms=self.s.navigation_timeout_ms))
        if not res.ok:
            raise AgentError(res.message, category=ErrorCategory.NAVIGATION_FAILED)
        snap = await self.snapshot()
        return {"url": snap.url, "title": snap.title, "page_kind": classify_page(snap).kind}

    async def _run_fill_page(self, task: dict[str, Any]) -> dict[str, Any]:
        """"Fill this page": fill every field of the form in front of the user from their profile
        and saved answers. Never submits, and never overwrites what is already typed."""
        from ..applications.workflow import WorkflowContext, keep_page_answers, location_from_page

        snap = await self.snapshot()
        if not snap.form_fields():
            return {"summary": "There is no form on this page to fill.", "filled": 0, "left_for_you": []}
        profile = CandidateProfile.model_validate(await self.repos.profile.get() or {})
        answers = await self.repos.profile.list_answers()
        resumes = await self.repos.profile.list_resumes()
        ctx = WorkflowContext(application_id="", task_id=task["id"], tab_id=self.tab_id, execute=self.execute, snapshot=self.snapshot, request_approval=self.request_approval, resolve_payload=self.resolve_payload)
        self.message = "reading the form"
        await self.workflow._probe_dropdowns(ctx, snap)
        plan = await self.workflow._filler.plan(snap, profile, answers, resumes, submit_names=["Submit", "Apply"], task_id=task["id"], job_location=location_from_page(snap))
        keep_page_answers(plan)
        for f in plan.fields:
            # Review-flagged fields that hold a verified/approved value are filled too; the user
            # reviews the whole page before submitting anyway.
            if f.status == "needs_review" and f.value and f.provenance.value in ("verified_fact", "approved_answer"):
                f.status = "auto"
        self.message = "filling the form"
        report = await self.workflow._filler.fill(None, self.tab_id, plan, resolve_payload=self.resolve_payload, execute=self.execute)  # type: ignore[arg-type]
        # The rules' fields are on the page already; give the (slow) model a short while longer for
        # the rest, then fill those too. The user is watching, so this does not wait the full grace.
        self.message = "checking the remaining fields"
        extra = await self.workflow._filler.finish_plan(plan, snap, profile, answers, resumes, submit_names=["Submit", "Apply"], job_location=location_from_page(snap), extra_wait=min(20.0, self.workflow._filler.grace_seconds()))
        if extra is not None:
            for f in extra.fields:
                if f.status == "needs_review" and f.value and f.provenance.value in ("verified_fact", "approved_answer"):
                    f.status = "auto"
        if extra is not None and extra.auto:
            more = await self.workflow._filler.fill(None, self.tab_id, extra, resolve_payload=self.resolve_payload, execute=self.execute)  # type: ignore[arg-type]
            report.filled.extend(more.filled)
            report.failed.extend(more.failed)
            report.skipped.extend(more.skipped)
        left = [f.label for f in plan.fields if f.status in ("needs_input", "needs_review", "contradiction") and f.label] + [e["label"] for e in report.failed if e.get("label")]
        summary = f"Filled {len(report.filled)} field(s). " + (f"Left for you: {', '.join(left[:12])}." if left else "Nothing left to fill.") + " Nothing was submitted: check the page and submit when ready."
        return {"summary": summary, "filled": len(report.filled), "left_for_you": left, "kept_your_answers": sum(1 for f in plan.fields if f.reason == "answered on the page by you")}

    async def _run_edit_page(self, task: dict[str, Any]) -> dict[str, Any]:
        """Change fields of the form in front of the user as instructed ("put my GitHub in link
        2"). Rules first, then one bounded model call; the edits are typed through the normal
        filler. Never submits."""
        from ..applications.field_mapping import FieldPlan, FillPlan
        from ..applications.page_edits import build_edit_prompt, edits_from_model, rule_edits
        from ..core.enums import Provenance
        from ..models.base import ModelRequest
        from ..models.schemas import EditsOutput

        instruction = (task.get("instruction") or "").strip()
        snap = await self.snapshot()
        fields = [n for n in snap.form_fields() if n.visible and not n.disabled]
        if not fields:
            return {"summary": "There is no form on this page to change.", "edits": []}
        profile = CandidateProfile.model_validate(await self.repos.profile.get() or {})
        facts = profile.facts()
        self.message = "working out what to change"
        edits = rule_edits(instruction, fields, facts)
        note = ""
        gateway = self.intents.gateway
        if not edits and gateway.role_config("field_mapper").provider != "heuristic":
            system, prompt = build_edit_prompt(instruction, fields, facts)
            self.message = "asking the local model which fields to change"
            try:
                out = await asyncio.wait_for(gateway.complete("field_mapper", ModelRequest(task="edit_page", system=system, prompt=prompt, json_schema={}, inputs={"instruction": instruction}), EditsOutput, task_id=task["id"], use_cache=False, max_output_tokens=600), timeout=self.s.model_request_timeout_seconds)
                edits = edits_from_model(out, fields)
                note = out.note
            except Exception as exc:  # noqa: BLE001 - say so instead of guessing
                log.info("edit_page: model unavailable: %s", exc)
                note = "the local model did not answer in time"
        if not edits:
            return {"summary": "I could not tell which fields to change" + (f" ({note})" if note else "") + '. Say the field and the value, e.g. "set Link 2 to my GitHub".', "edits": []}
        by_node = {n.i: n for n in fields}
        plan = FillPlan(fields=[FieldPlan(node=e.node, label=e.label, field_type=e.field_type, required=False, category="edit", value=e.value, provenance=Provenance.VERIFIED_FACT, confidence=1.0, status="auto", reason=e.reason, options=by_node[e.node].options) for e in edits])
        self.message = "changing the fields"
        report = await self.workflow._filler.fill(None, self.tab_id, plan, resolve_payload=self.resolve_payload, execute=self.execute)  # type: ignore[arg-type]
        done = [f"{e.label or 'field ' + str(e.node)} → {e.value[:50]}" for e in edits if any(str(x.get("node")) == str(e.node) for x in report.filled)]
        failed = [f"{x.get('label') or x.get('node')}: {x.get('message', '')[:60]}" for x in report.failed]
        summary = (f"Changed {len(done)} field(s): " + "; ".join(done) if done else "Nothing changed") + (f". Could not change: {'; '.join(failed)}" if failed else "") + (f". {note}" if note else "") + ". Nothing was submitted."
        return {"summary": summary, "edits": [{"node": e.node, "label": e.label, "value": e.value, "reason": e.reason} for e in edits], "failed": report.failed}

    async def _run_ask(self, task: dict[str, Any]) -> dict[str, Any]:
        """Assistant mode: answer a free-form request about the current page or PDF with the local
        model. Read-only: nothing on the page is clicked, typed or submitted."""
        from ..models.base import ModelRequest
        from ..models.schemas import AnswerOutput
        from .page_reader import page_text, pdf_text_from_url

        question = (task.get("instruction") or "").strip()
        snap = await self.snapshot()
        self.message = "reading the page"
        source = "page"
        text = page_text(snap)
        if re.search(r"\.pdf($|[?#])", snap.url, re.I) or "pdf" in (snap.title or "").lower()[-8:]:
            pdf = await pdf_text_from_url(snap.url)
            if pdf:
                source, text = "pdf", pdf
        budget = max(2000, self.s.model_context_chars - len(question) - 600)
        excerpt = text[:budget]
        gateway = self.intents.gateway
        role = "assistant" if gateway.role_config("assistant").provider != "heuristic" else "extractor"
        system = (
            "You are a helpful assistant inside the user's browser. Answer the user's request using the "
            "page content given. Be accurate and concise; if the page does not contain the answer, say so "
            "plainly. When asked to write or draft something, write it in full. Put your WHOLE reply — "
            "including any lists, bullet points or drafted text — inside the single \"answer\" string "
            "(use '\\n' and '- ' for lines and bullets). Reply only as JSON: "
            '{"answer": "<your complete answer>"}.'
        )
        about_me = ""
        if re.search(r"\b(me|my|mine|i|cover letter|match|fit|profile|resume|cv|qualif|eligible)\b", question, re.I):
            # The user's own background for "does this match me" / "write my cover letter".
            # Contact details (email, phone, addresses) are never included.
            p = CandidateProfile.model_validate(await self.repos.profile.get() or {})
            lines = [
                f"Name: {p.contact.full_name}",
                f"Current role: {p.employment[0].title} at {p.employment[0].company}" if p.employment else "",
                f"Experience: {p.experience_years:g} years" if p.experience_years else "",
                f"Location: {p.contact.city}, {p.contact.country}".strip(", "),
                f"Work rights: {', '.join(p.legal.work_authorized_countries)}" if p.legal.work_authorized_countries else "",
                f"Education: {p.education[0].degree}, {p.education[0].school}" if p.education else "",
                f"Skills: {', '.join(p.skills[:60])}" if p.skills else "",
                f"Summary: {p.summary[:800]}" if p.summary else "",
            ]
            about_me = "\n\n--- ABOUT THE USER ---\n" + "\n".join(x for x in lines if x) + "\n--- END ---"
            excerpt = excerpt[: max(1500, budget - len(about_me))]
        prompt = f"Page title: {snap.title}\nPage URL: {snap.url}\nSource: {source}\n\n--- PAGE CONTENT ---\n{excerpt}\n--- END ---{about_me}\n\nUser request: {question}"
        self.message = "thinking…"
        try:
            out = await gateway.complete(role, ModelRequest(task="ask", system=system, prompt=prompt, json_schema={}, inputs={"question": question, "text": excerpt}), AnswerOutput, task_id=task["id"], use_cache=False, max_output_tokens=1200)
            answer = out.full_text()
        except AgentError as exc:
            log.warning("assistant model failed: %s", exc)
            answer = ""
        if not answer:
            answer = "I could not get an answer from the local model right now. Is Ollama running?"
        return {"summary": answer[:300], "answer": answer, "source": source, "url": snap.url, "title": snap.title, "chars_read": len(excerpt), "truncated": len(text) > len(excerpt)}

    async def _run_general(self, task: dict[str, Any]) -> dict[str, Any]:
        """Bounded observe/plan/act loop for free-form instructions."""
        goal = task.get("kind", "general")
        instruction = task.get("instruction") or ""
        profile = CandidateProfile.model_validate(await self.repos.profile.get() or {})
        # Only non-sensitive facts are ever exposed to the planner.
        facts = {k: v for k, (v, _) in profile.facts().items() if k.startswith(("contact.", "links.")) and "address" not in k}
        attempt = 0
        extracted: dict[str, Any] | None = None
        while True:
            snap = await self.snapshot()
            cls = classify_page(snap)
            self.message = f"page: {cls.kind}"
            plan = await self.planner.next_actions(snap, cls, goal=goal, instruction=instruction, facts=facts, history=self.history, task_id=task["id"])
            for action in plan.actions:
                if action.type == "ask_user":
                    raise NeedsUserInputError(str(action.args.get("question")))
                if action.type == "done":
                    return {"summary": action.args.get("summary"), "extracted": extracted, "url": snap.url, "actions": self.action_count}
                res = await self.execute(action)
                if action.type == "extract" and res.ok:
                    extracted = res.value if isinstance(res.value, dict) else {"value": res.value}
                if not res.ok:
                    failure = self.recovery.classify_result(action, res)
                    attempt += 1
                    decision = self.recovery.decide(failure, attempt)
                    await self.audit.log("recovery.decision", task_id=task["id"], category=failure.category.value, strategy=decision.strategy, retry=decision.retry)
                    if decision.strategy == "handoff":
                        raise AgentError(failure.message, category=failure.category)
                    if not decision.retry:
                        raise AgentError(failure.message, category=failure.category)
                    await asyncio.sleep(decision.wait_seconds)
                    break  # re-snapshot and re-plan
            else:
                attempt = 0


class TaskManager:
    """Owns live tasks. One runner per task; bounded by ``max_concurrent_tasks``."""

    def __init__(self, settings: Settings, repos: Repositories, audit: AuditLogger, policy: PolicyEngine, runner_factory):  # type: ignore[no-untyped-def]
        self.s = settings
        self.repos = repos
        self.audit = audit
        self.policy = policy
        self._factory = runner_factory
        self._running: dict[str, tuple[TaskRunner, asyncio.Task[Any]]] = {}
        self._concurrency = max(1, settings.max_concurrent_tasks)
        self._sem = asyncio.Semaphore(self._concurrency)
        self._listeners: list[Any] = []
        self.browser_unavailable_cb: Any = None  # set by the Scheduler: hold off when no browser can take a task

    def set_concurrency(self, n: int) -> None:
        """Follow the scheduler's ``max_concurrent_tasks`` (background work only; see ``start``)."""
        n = max(1, int(n))
        if n != self._concurrency:
            self._concurrency = n
            self._sem = asyncio.Semaphore(n)

    def on_change(self, fn) -> None:  # type: ignore[no-untyped-def]
        self._listeners.append(fn)

    async def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                await fn()
            except Exception:
                log.exception("task listener failed")

    @property
    def running_ids(self) -> list[str]:
        return list(self._running)

    def current(self) -> dict[str, Any] | None:
        for tid, (runner, _) in self._running.items():
            return {"id": tid, "kind": runner.task.get("kind"), "status": "running", "action_count": runner.action_count, "message": runner.message}
        return None

    async def start(self, task: dict[str, Any]) -> None:
        if task["id"] in self._running:
            return
        runner: TaskRunner = self._factory()
        # A task the user starts on a tab of theirs runs at once: it never waits behind the
        # scheduler's queue of background applications (which is what the concurrency limit bounds).
        foreground = task.get("tab_id") is not None

        async def _run() -> None:
            if runner.cancel_event.is_set():
                # Cancelled while it waited its turn: never opens a page.
                await self.repos.tasks.set_status(task["id"], TaskStatus.CANCELLED, failure={"category": "USER_CANCELLED", "message": "cancelled before it started", "retryable": False, "recommended_action": "none"})
                await self._cancel_application_of(task)
                return
            await runner.run(task)

        async def _go() -> None:
            try:
                if foreground:
                    await _run()
                else:
                    async with self._sem:
                        await _run()
            finally:
                self._running.pop(task["id"], None)
                await self._notify()

        runner.resume_cb = self.resume_task
        runner.browser_unavailable_cb = self.browser_unavailable_cb
        self._running[task["id"]] = (runner, asyncio.create_task(_go(), name=f"task-{task['id'][:8]}"))
        await self._notify()

    async def cancel(self, task_id: str) -> bool:
        entry = self._running.get(task_id)
        if entry is None:
            t = await self.repos.tasks.get(task_id)
            if t and t["status"] in ("pending", "waiting_user", "waiting_approval", "paused"):
                await self.repos.tasks.set_status(task_id, TaskStatus.CANCELLED, failure={"category": "USER_CANCELLED", "message": "cancelled by user", "retryable": False, "recommended_action": "none"})
                await self._cancel_application_of(t)
                return True
            return False
        runner, _ = entry
        runner.cancel_event.set()
        await self.audit.log("task.cancel_requested", task_id=task_id)
        return True

    async def _cancel_application_of(self, task: dict[str, Any]) -> None:
        app_id = task.get("application_id")
        if not app_id:
            return
        app = await self.repos.applications.get(app_id)
        if app and app["state"] not in ("APPLIED_CONFIRMED", "DUPLICATE", "CANCELLED"):
            try:
                await self.repos.applications.transition(app_id, ApplicationState.CANCELLED, reason="task cancelled by user", actor="user")
            except ValueError:
                log.debug("application %s could not be cancelled from %s", app_id, app["state"])

    async def pause_all(self) -> None:
        self.policy.paused = True
        await self.audit.log("control.pause_all")
        await self._notify()

    async def resume_all(self) -> None:
        self.policy.paused = False
        await self.audit.log("control.resume_all")
        await self._notify()

    async def rearm_watchers(self) -> int:
        """After a restart: watch again every tab that is waiting for the user's verification tick,
        so the tick still leads to the submit (the watchers died with the old process)."""
        rows = await self.repos.db.fetch_all("SELECT id, tab_id, failure_json FROM tasks WHERE status = 'waiting_user' AND tab_id IS NOT NULL")
        n = 0
        for row in rows:
            try:
                failure = json.loads(row["failure_json"] or "{}") if isinstance(row["failure_json"], str) else (row["failure_json"] or {})
            except Exception:  # noqa: BLE001
                failure = {}
            if not (failure.get("details") or {}).get("captcha_on_form"):
                continue
            runner: TaskRunner = self._factory()
            runner.resume_cb = self.resume_task
            if hasattr(runner.driver, "protect_tab"):
                await runner.driver.protect_tab(str(row["tab_id"]))
            asyncio.create_task(runner._watch_captcha(row["id"], str(row["tab_id"])), name="captcha-watch-" + str(row["id"])[:8])
            n += 1
        if n:
            log.info("re-armed %d verification watcher(s) after restart", n)
        return n

    async def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume a task waiting on the user: create a fresh task continuing the same application
        in the same tab (the previous runner has finished and persisted everything)."""
        t = await self.repos.tasks.get(task_id)
        if t is None:
            raise KeyError(task_id)
        if t["status"] != "waiting_user":
            raise ValueError(f"task is {t['status']}, not waiting_user")
        app_id = t.get("application_id")
        if app_id and any((r.task or {}).get("application_id") == app_id for r, _ in self._running.values()):
            # Two runs on one application at once would fight over its state and its tab.
            raise ValueError("another task is already working on this application")
        # The application stays in its waiting state (AUTH_REQUIRED / NEEDS_USER_INPUT /
        # READY_FOR_REVIEW): the workflow sees it is a resume and continues on the open page,
        # keeping what the user typed, instead of re-queuing and reloading the job from scratch.
        await self.audit.log("task.resumed_by_user", task_id=task_id, application_id=app_id)
        _WAITING.pop(str(t.get("tab_id")), None)
        new = await self.repos.tasks.create(t["kind"], instruction=t.get("instruction"), application_id=app_id, job_id=t.get("job_id"), tab_id=t.get("tab_id"), domain=t.get("domain"))
        await self.repos.tasks.set_status(task_id, TaskStatus.SUCCEEDED, result={"resumed_as": new["id"]})
        await self.start(new)
        return new

    async def shutdown(self) -> None:
        for _, (runner, task) in list(self._running.items()):
            runner.cancel_event.set()
        for _, (_, task) in list(self._running.items()):
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                task.cancel()
