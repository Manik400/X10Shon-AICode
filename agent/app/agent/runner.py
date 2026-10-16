"""TaskRunner and TaskManager.

TaskRunner runs one task through the bounded loop
``Observe → Normalize → Classify → Plan → Validate Policy → Execute → Verify → Persist``.
TaskManager owns running tasks, cancellation, pause-all and the resume protocol.
"""

from __future__ import annotations

import asyncio
import base64
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
from ..core.actions import ActionResult, BrowserAction
from ..core.enums import ApplicationState, ErrorCategory, TaskStatus
from ..core.errors import (
    AgentError,
    AuthRequiredError,
    HumanVerificationError,
    NeedsUserInputError,
    PermissionDeniedError,
    UserCancelledError,
    classify_exception,
)
from ..core.util import now_iso
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

    async def snapshot(self) -> PageSnapshot:
        """Current page. After a navigation, tab open or click, waits (up to ~8 s) until the page
        stops changing, so app-rendered sites (Workday, Ashby…) are read once they have drawn."""
        self._check_bounds()
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
        await self.repos.tasks.add_attempt(self.task["id"], seq, action.model_dump(mode="json"), {**result.model_dump(mode="json"), "verification": {"ok": ver.ok, "strategy": ver.strategy, "message": ver.message}}, result.ok and ver.ok, result.duration_ms)
        await self.repos.tasks.update_progress(self.task["id"], {"last_action": entry, "message": self.message}, action_count=self.action_count)
        if not result.ok and result.error_category == "PERMISSION_DENIED":
            raise PermissionDeniedError(result.message)
        return result

    async def request_approval(self, kind: str, payload: dict[str, Any]) -> bool:
        approval = await self.approvals.request(kind, payload, task_id=self.task["id"], application_id=self.task.get("application_id"))
        await self.repos.tasks.set_status(self.task["id"], TaskStatus.WAITING_APPROVAL, progress={"message": payload.get("summary", "awaiting approval"), "approval_id": approval["id"]})
        self.message = "awaiting approval"
        ok = await self.approvals.wait(approval["id"], timeout=self.s.task_deadline_seconds)
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
                self.tab_id = await self.driver.open_tab(url if url.startswith(("http://", "https://", "file://")) else "about:blank")
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
            else:
                result = await self._run_general(task)
            await self.repos.tasks.set_status(task["id"], TaskStatus.SUCCEEDED, result=result, action_count=self.action_count)
            await self.audit.log("task.succeeded", task_id=task["id"], actions=self.action_count)
            return {"status": "succeeded", "result": result}
        except (AuthRequiredError, HumanVerificationError, NeedsUserInputError) as exc:
            failure = exc.to_failure()
            await self.repos.tasks.set_status(task["id"], TaskStatus.WAITING_USER, failure=failure.to_dict(), progress={"message": str(exc)}, action_count=self.action_count)
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
        if app["state"] != ApplicationState.IN_PROGRESS:
            # Failed before the workflow started (e.g. browser not reachable): count the attempt so
            # the scheduler backs off instead of retrying every tick.
            app = await self.repos.applications.transition(app_id, ApplicationState.IN_PROGRESS, reason="attempt started", fields={"attempt_count": int(app.get("attempt_count") or 0) + 1, "last_attempt_at": now_iso()})
        decision = self.recovery.decide(failure, int(app.get("attempt_count") or 1))
        if decision.retry:
            from datetime import timedelta

            from ..core.util import now_dt

            next_at = (now_dt() + timedelta(seconds=decision.wait_seconds)).isoformat(timespec="seconds")
            await self.repos.applications.transition(app_id, ApplicationState.RETRY_SCHEDULED, reason=f"{failure.category.value}: {failure.message[:200]}", fields={"failure": failure.to_dict(), "next_attempt_at": next_at})
        else:
            await self.repos.applications.transition(app_id, ApplicationState.APPLICATION_FAILED, reason=f"{failure.category.value}: {failure.message[:200]}", fields={"failure": failure.to_dict()})

    # ------------------------------------------------------------------ task kinds
    async def _run_apply(self, task: dict[str, Any]) -> dict[str, Any]:
        app_id = task.get("application_id")
        if not app_id:
            raise AgentError("apply task without application", category=ErrorCategory.UNKNOWN)
        ctx = WorkflowContext(application_id=app_id, task_id=task["id"], tab_id=self.tab_id, execute=self.execute, snapshot=self.snapshot, request_approval=self.request_approval, resolve_payload=self.resolve_payload)
        self.message = "running application workflow"
        return await self.workflow.run(ctx)

    async def _run_apply_list(self, task: dict[str, Any]) -> dict[str, Any]:
        """Work through every job linked from the current page: open it in its own tab, import it,
        run the application workflow, record the outcome, close the tab, return to the list.
        One job's failure or pause never stops the others."""
        from ..jobs.listing import extract_job_links

        list_tab = self.tab_id
        snap = await self.snapshot()
        dry_run = bool(re.search(r"\b(dry[- ]?run|preview|simulate|do not submit|don'?t submit)\b", task.get("instruction") or "", re.I))
        limit = int(self.s.daily_application_limit)
        m = re.search(r"\b(?:first|top|only)\s+(\d{1,3})\b", task.get("instruction") or "", re.I)
        if m:
            limit = max(1, min(limit, int(m.group(1))))
        links = extract_job_links(snap, max_links=limit)
        if not links:
            links = await self._reveal_job_list(snap, limit)
        await self.audit.log("apply_list.started", task_id=task["id"], links=len(links), url=self.last_snapshot.url if self.last_snapshot else snap.url)
        if not links:
            raise AgentError("no job links found on this page, even after opening its lists", category=ErrorCategory.SITE_UNSUPPORTED)
        await self.execute(BrowserAction(type="banner", args={"text": f"Found {len(links)} jobs; applying one by one"}))
        outcomes: list[dict[str, Any]] = []
        total_actions = 0
        kept_open = 0  # tabs left open for the user (sign-in, missing answers); capped
        max_kept_open = 5
        for index, link in enumerate(links, start=1):
            if self.cancel_event.is_set() or self.policy.paused:
                break
            item: dict[str, Any] = {"url": link["url"], "text": link["text"]}
            tab_id = ""
            try:
                # Bounds are per job: a long list must not exhaust the task deadline.
                self.deadline = time.monotonic() + self.s.task_deadline_seconds
                total_actions += self.action_count
                self.action_count = 0
                self.loops.reset()
                self.history = []
                tab_id = await self.driver.open_tab(link["url"], active=True)
                self.tab_id = tab_id
                self._settle_next = True
                self.message = f"job {index}/{len(links)}"
                await self.repos.tasks.update_progress(task["id"], {"message": self.message, "current_url": link["url"], "done": outcomes})
                page = await self.snapshot()
                cls = classify_page(page)
                if cls.needs_human and dry_run:
                    item.update({"verdict": "sign_in_needed", "state": "AUTH_REQUIRED", "note": f"{cls.kind} page at {page.url[:120]}"})
                    outcomes.append(item)
                    await self._close_item_tab(tab_id)
                    continue
                if cls.needs_human:
                    if kept_open < max_kept_open:
                        kept_open += 1
                        item.update({"state": "AUTH_REQUIRED", "note": f"sign-in needed on {page.url}; tab left open"})
                    else:
                        item.update({"state": "AUTH_REQUIRED", "note": f"sign-in needed on {page.url}; tab closed (open-tab limit)"})
                        await self._close_item_tab(tab_id)
                    outcomes.append(item)
                    continue
                job, dup = await self.jobs.import_from_snapshot(page, source_kind="manual", task_id=task["id"])
                app = await self.repos.applications.get_by_job(job["id"])
                item["job_id"] = job["id"]
                if app is None or app["state"] in ("APPLIED_CONFIRMED", "DUPLICATE", "CANCELLED", "IN_PROGRESS", "SUBMISSION_PENDING"):
                    item.update({"state": (app or {}).get("state", "DUPLICATE"), "note": "already handled"})
                    outcomes.append(item)
                    await self._close_item_tab(tab_id)
                    continue
                item["application_id"] = app["id"]
                if dry_run:
                    item.update(await self._preview_application(page))
                    outcomes.append(item)
                    await self.audit.log("apply_list.preview", task_id=task["id"], index=index, url=link["url"], verdict=item.get("verdict"))
                    await self._close_item_tab(tab_id)
                    continue
                if app["state"] in ("ELIGIBLE", "SKIPPED", "IMPORTED", "SCREENING", "APPLICATION_FAILED", "RETRY_SCHEDULED", "AUTH_REQUIRED", "NEEDS_USER_INPUT", "READY_FOR_REVIEW"):
                    app = await self.repos.applications.transition(app["id"], ApplicationState.QUEUED, reason="queued by list apply", fields={"next_attempt_at": None})
                ctx = WorkflowContext(application_id=app["id"], task_id=task["id"], tab_id=tab_id, execute=self.execute, snapshot=self.snapshot, request_approval=self.request_approval, resolve_payload=self.resolve_payload)
                try:
                    result = await self.workflow.run(ctx)
                    item.update({"state": result.get("state"), "reference": result.get("reference_number")})
                    await self._close_item_tab(tab_id)
                except (AuthRequiredError, HumanVerificationError, NeedsUserInputError) as exc:
                    item.update({"state": (await self.repos.applications.get(app["id"]) or {}).get("state"), "note": str(exc)[:160]})
                    if kept_open < max_kept_open:
                        # Left open with a waiting task: the auto-resume hook continues it after sign-in.
                        kept_open += 1
                        waiting = await self.repos.tasks.create("apply", application_id=app["id"], job_id=job["id"], tab_id=int(tab_id) if tab_id.isdigit() else None, domain=link["url"])
                        await self.repos.tasks.set_status(waiting["id"], TaskStatus.WAITING_USER, failure=exc.to_failure().to_dict(), progress={"message": str(exc)[:200]})
                    else:
                        await self._close_item_tab(tab_id)
                except TaskCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - recorded per job
                    failure = classify_exception(exc)
                    item.update({"state": (await self.repos.applications.get(app["id"]) or {}).get("state"), "failure": failure.category.value, "note": failure.message[:160]})
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
        self.action_count += total_actions
        summary = {"dry_run": dry_run, "jobs": len(links), "processed": len(outcomes), "confirmed": sum(1 for o in outcomes if o.get("state") == "APPLIED_CONFIRMED"), "waiting_user": sum(1 for o in outcomes if o.get("state") in ("AUTH_REQUIRED", "NEEDS_USER_INPUT", "READY_FOR_REVIEW")), "failed": sum(1 for o in outcomes if o.get("failure")), "items": outcomes}
        if dry_run:
            summary["ready"] = sum(1 for o in outcomes if o.get("verdict") == "ready")
            summary["summary"] = f"Dry run: {summary['ready']} of {len(outcomes)} forms could be filled and submitted now; nothing was typed or submitted"
            await self.execute(BrowserAction(type="banner", args={"text": summary["summary"]}))
        else:
            await self.execute(BrowserAction(type="banner", args={"text": f"Done: {summary['confirmed']} applied, {summary['waiting_user']} need you, {summary['failed']} failed"}))
        return summary

    async def _preview_application(self, page: PageSnapshot) -> dict[str, Any]:
        """Dry run for one job page: reach the form (only 'Apply' clicks), plan the fill, report.
        Types nothing, uploads nothing, submits nothing."""
        from ..applications.profile import CandidateProfile
        from ..applications.workflow import looks_like_application
        from ..core.actions import Target

        from ..applications.workflow import find_apply_button
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
        plan = await self.workflow._filler.plan(snap, profile, answers, resumes, submit_names=["Submit application", "Submit Application", "Submit", "Apply", "Send application"])
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
        try:
            await self.driver.close_tab(tab_id)
        except Exception:
            log.debug("could not close tab %s", tab_id, exc_info=True)

    async def _run_extract(self, task: dict[str, Any]) -> dict[str, Any]:
        snap = await self.snapshot()
        if task.get("instruction") and task["instruction"].startswith("http"):
            await self.execute(BrowserAction(type="navigate", args={"url": task["instruction"]}, verification="navigation_complete", timeout_ms=self.s.navigation_timeout_ms))
            snap = await self.snapshot()
        cls = classify_page(snap)
        if cls.needs_human:
            raise AuthRequiredError(f"page requires authentication ({cls.auth_state.value}); log in and resume")
        job, dup = await self.jobs.import_from_snapshot(snap, source_kind="manual", task_id=task["id"])
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
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrent_tasks))
        self._listeners: list[Any] = []

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

        async def _go() -> None:
            async with self._sem:
                try:
                    await runner.run(task)
                finally:
                    self._running.pop(task["id"], None)
                    await self._notify()

        self._running[task["id"]] = (runner, asyncio.create_task(_go(), name=f"task-{task['id'][:8]}"))
        await self._notify()

    async def cancel(self, task_id: str) -> bool:
        entry = self._running.get(task_id)
        if entry is None:
            t = await self.repos.tasks.get(task_id)
            if t and t["status"] in ("pending", "waiting_user", "waiting_approval", "paused"):
                await self.repos.tasks.set_status(task_id, TaskStatus.CANCELLED, failure={"category": "USER_CANCELLED", "message": "cancelled by user", "retryable": False, "recommended_action": "none"})
                app_id = t.get("application_id")
                if app_id:
                    app = await self.repos.applications.get(app_id)
                    if app and app["state"] not in ("APPLIED_CONFIRMED", "DUPLICATE", "CANCELLED"):
                        await self.repos.applications.transition(app_id, ApplicationState.CANCELLED, reason="task cancelled by user", actor="user")
                return True
            return False
        runner, _ = entry
        runner.cancel_event.set()
        await self.audit.log("task.cancel_requested", task_id=task_id)
        return True

    async def pause_all(self) -> None:
        self.policy.paused = True
        await self.audit.log("control.pause_all")
        await self._notify()

    async def resume_all(self) -> None:
        self.policy.paused = False
        await self.audit.log("control.resume_all")
        await self._notify()

    async def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume a task waiting on the user: create a fresh task continuing the same application
        in the same tab (the previous runner has finished and persisted everything)."""
        t = await self.repos.tasks.get(task_id)
        if t is None:
            raise KeyError(task_id)
        if t["status"] != "waiting_user":
            raise ValueError(f"task is {t['status']}, not waiting_user")
        app_id = t.get("application_id")
        if app_id:
            app = await self.repos.applications.get(app_id)
            if app and app["state"] in ("AUTH_REQUIRED", "NEEDS_USER_INPUT", "READY_FOR_REVIEW"):
                await self.repos.applications.transition(app_id, ApplicationState.QUEUED, reason="resumed by user", actor="user")
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
