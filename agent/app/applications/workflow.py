"""ApplicationWorkflow: the job-application state machine driver.

Runs inside a TaskRunner (which supplies the policy-checked executor, snapshots and audit).
Each step persists its transition before continuing so a crash never loses state.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..browser.page_state import classify_page
from ..browser.snapshot import PageSnapshot
from ..browser.verifier import verify_submission
from ..core.actions import ActionResult, BrowserAction, Target
from ..core.enums import ApplicationState, Provenance, RiskLevel, VerificationStatus
from ..core.errors import (
    AuthRequiredError,
    HumanVerificationError,
    NeedsUserInputError,
    SiteUnsupportedError,
    SubmissionRejectedError,
    SubmissionUncertainError,
)
from ..core.util import now_iso
from ..jobs.adapters import AdapterRegistry
from ..jobs.listing import embedded_application_url
from ..observability.audit import AuditLogger
from ..persistence.unit import Repositories
from .form_filler import FormFiller
from .profile import CandidateProfile

log = logging.getLogger("lfa.workflow")

Execute = Callable[[BrowserAction, dict[str, Any] | None], Awaitable[ActionResult]]
Snapshot = Callable[[], Awaitable[PageSnapshot]]
Approve = Callable[[str, dict[str, Any]], Awaitable[bool]]


_NOT_JOB_HOSTS = re.compile(
    r"(^|\.)(mail\.google\.com|outlook\.(live|office)\.com|chatgpt\.com|chat\.openai\.com|claude\.ai|gemini\.google\.com|web\.whatsapp\.com|"
    r"youtube\.com|udemy\.com|facebook\.com|instagram\.com|x\.com|twitter\.com|reddit\.com|docs\.google\.com|drive\.google\.com|github\.com)$",
    re.I,
)
_IDENTITY_KEYS = {"contact.first_name", "contact.last_name", "contact.full_name", "contact.email"}
_APPLY_EXACT = re.compile(r"^\s*(apply|apply now|apply for this (job|position|role)|apply manually|apply to this job|start (your )?application|i'?m interested|easy apply)\s*[›>→]?\s*$", re.I)
_APPLY_LOOSE = re.compile(r"\bapply\b", re.I)
_NOT_APPLY = re.compile(r"(applied|application status|how to apply|apply filter|applying tips|apply with linkedin)", re.I)


def find_apply_button(snap):  # type: ignore[no-untyped-def]
    """The control that opens the application: exact 'Apply'-style labels first, then any
    button/link mentioning apply that is not a filter, status or third-party sign-in."""
    controls = [n for n in snap.nodes if n.role in ("button", "link") and n.name and not n.disabled and n.visible]
    exact = [n for n in controls if _APPLY_EXACT.match(n.name)]
    if exact:
        return exact[0]
    loose = [n for n in controls if _APPLY_LOOSE.search(n.name) and not _NOT_APPLY.search(n.name) and len(n.name) <= 40]
    return loose[0] if loose else None


def looks_like_application(snap, plan) -> str | None:  # type: ignore[no-untyped-def]
    """Return a reason string when the page is clearly NOT a job application, else None.

    A real application form asks who you are: at least one identity field (name or email) plus a
    second applicant field (another identity field, phone, resume upload, or LinkedIn). Pages on
    mail/chat/social/learning sites are refused outright, as is the agent's own dashboard.
    """
    from urllib.parse import urlsplit

    host = urlsplit(snap.url).netloc.lower().split(":")[0]
    if _NOT_JOB_HOSTS.search(host):
        return f"{host} is not a job site"
    if urlsplit(snap.url).port == 8765 and "/fixtures/" not in snap.url:
        return "this is the agent's own dashboard"
    keys = {f.profile_key for f in plan.fields if f.profile_key}
    identity = keys & _IDENTITY_KEYS
    applicant = (keys & {"contact.phone", "links.linkedin"}) | ({"resume"} if any(f.field_type == "file" and f.resume_id for f in plan.fields) else set())
    if not identity:
        return "no name or email field on the form"
    if len(identity) + len(applicant) < 2:
        return "the form asks for too little to be an application"
    return None


@dataclass(slots=True)
class WorkflowContext:
    application_id: str
    task_id: str
    tab_id: str
    execute: Execute
    snapshot: Snapshot
    request_approval: Approve
    resolve_payload: Callable[[BrowserAction], Awaitable[dict[str, Any] | None]]


class ApplicationWorkflow:
    MAX_STEPS = 6  # multi-step wizard bound

    def __init__(self, repos: Repositories, filler: FormFiller, adapters: AdapterRegistry, audit: AuditLogger, *, submit_policy: str = "always"):
        self._repos = repos
        self._filler = filler
        self._adapters = adapters
        self._audit = audit
        self._submit_policy = submit_policy

    async def _transition(self, app_id: str, state: ApplicationState, reason: str, **fields: Any) -> dict[str, Any]:
        app = await self._repos.applications.transition(app_id, state, reason=reason, fields=fields or None)
        await self._audit.log("application.transition", application_id=app_id, to=state.value, reason=reason)
        return app

    # ------------------------------------------------------------------ entry
    async def run(self, ctx: WorkflowContext) -> dict[str, Any]:
        app = await self._repos.applications.get(ctx.application_id)
        if app is None:
            raise KeyError(ctx.application_id)
        job = await self._repos.jobs.get(app["job_id"]) or {}
        profile = CandidateProfile.model_validate(await self._repos.profile.get() or {})
        answers = await self._repos.profile.list_answers()
        resumes = await self._repos.profile.list_resumes()
        adapter = self._adapters.for_url(app.get("application_url") or job.get("apply_url") or "")
        hints = adapter.submit_hints()

        if app["state"] in (ApplicationState.APPLICATION_FAILED, ApplicationState.ELIGIBLE, ApplicationState.SKIPPED):
            # Explicit re-run of a failed or not-yet-queued application (e.g. "Apply on this page").
            app = await self._transition(app["id"], ApplicationState.QUEUED, "re-queued by an explicit apply request", actor="user")
        if app["state"] in (ApplicationState.QUEUED, ApplicationState.RETRY_SCHEDULED, ApplicationState.AUTH_REQUIRED, ApplicationState.NEEDS_USER_INPUT, ApplicationState.READY_FOR_REVIEW):
            app = await self._transition(app["id"], ApplicationState.IN_PROGRESS, "task started", last_attempt_at=now_iso(), attempt_count=int(app.get("attempt_count") or 0) + 1)
        if app["state"] != ApplicationState.IN_PROGRESS:
            raise SiteUnsupportedError(f"application is {app['state']}; nothing to do")

        # 1. Navigate to the application URL if not already there.
        snap = await ctx.snapshot()
        target_url = app.get("application_url") or job.get("apply_url") or job.get("canonical_url")
        if target_url and not snap.url.startswith(target_url.split("?")[0][:60]):
            await ctx.execute(BrowserAction(type="navigate", args={"url": target_url}, risk=RiskLevel.LOW, verification="navigation_complete", timeout_ms=30_000), None)
            snap = await ctx.snapshot()

        # 2. Session / challenge detection, then form location (bounded steps).
        for step in range(self.MAX_STEPS):
            cls = classify_page(snap)
            if cls.kind == "challenge":
                await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "human verification required: " + "; ".join(cls.reasons))
                raise HumanVerificationError("Complete the verification challenge in the browser, then resume.", details={"url": snap.url})
            if cls.kind == "login":  # a login/recovery/lock page; a header "Sign in" link is not a wall
                await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, f"authentication required ({cls.auth_state.value})")
                raise AuthRequiredError(f"Log in on {snap.url} in the browser, then resume.", details={"auth_state": cls.auth_state.value})
            if not snap.form_fields():
                embedded = embedded_application_url(snap)
                if embedded and embedded.split("#")[0] != snap.url.split("#")[0]:
                    # The form lives in a frame from the applicant-tracking site: open it directly.
                    await ctx.execute(BrowserAction(type="navigate", args={"url": embedded}, risk=RiskLevel.LOW, verification="navigation_complete", timeout_ms=30_000), None)
                    snap = await ctx.snapshot()
                    continue
            if cls.unsupported_reasons and not snap.form_fields() and not find_apply_button(snap):
                await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "unsupported page structure", failure={"category": "SITE_UNSUPPORTED", "reasons": cls.unsupported_reasons})
                raise SiteUnsupportedError("; ".join(cls.unsupported_reasons))
            if cls.kind == "confirmation" and step == 0:
                # Already applied (site shows confirmation before any action): treat as uncertain duplicate.
                await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "page already shows a confirmation; reconcile manually", verification_status=VerificationStatus.UNCERTAIN.value, evidence={"url": snap.url, "reasons": cls.reasons})
                raise SubmissionUncertainError("The page already shows a confirmation; verify whether you applied before.")
            if snap.form_fields():
                break
            # Job description page: click Apply.
            apply_node = find_apply_button(snap)
            if apply_node is None:
                await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "no application form or apply button found", failure={"category": "SITE_UNSUPPORTED"})
                raise SiteUnsupportedError("no application form or apply button found on " + snap.url)
            await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(apply_node.i)), args={}, risk=RiskLevel.LOW, verification="none", timeout_ms=15_000, expected_outcome="application form opens"), None)
            snap = await ctx.snapshot()
        else:
            await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "could not reach an application form", failure={"category": "SITE_UNSUPPORTED"})
            raise SiteUnsupportedError("could not reach an application form")

        # 3. Fill (possibly multi-step), pausing on blocking questions.
        all_reports: list[dict[str, Any]] = []
        plan = None
        for step in range(self.MAX_STEPS):
            plan = await self._filler.plan(snap, profile, answers, resumes, submit_names=hints["submit_button_names"], task_id=ctx.task_id)
            if step == 0:
                verdict = looks_like_application(snap, plan)
                if verdict:
                    # Not a job application (mail, chat, dashboards, search filters…): touch nothing.
                    await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, f"not an application form: {verdict}", failure={"category": "SITE_UNSUPPORTED", "message": verdict})
                    raise SiteUnsupportedError(f"This page is not a job application form ({verdict}). Nothing was filled.")
            if self._submit_policy == "never":
                # Unattended mode: review-flagged fields that already hold a verified/approved
                # value are filled; fields without a value still pause.
                for f in plan.fields:
                    if f.status == "needs_review" and f.value and f.provenance in (Provenance.VERIFIED_FACT, Provenance.APPROVED_ANSWER):
                        f.status = "auto"
            report = await self._filler.fill(None, ctx.tab_id, plan, resolve_payload=ctx.resolve_payload, execute=ctx.execute)  # type: ignore[arg-type]
            all_reports.append({"step": step, "url": snap.url, **report.to_dict()})
            await self._repos.applications.update_fields(app["id"], {"fill_plan": {"steps": all_reports, "last_plan": plan.to_dict()}})
            if plan.blocking or any(f.status == "contradiction" for f in plan.fields) or report.failed:
                questions = [f.to_dict() for f in plan.fields if f.status in ("needs_input", "contradiction") and (f.required or f.status == "contradiction")]
                questions += [{"node": e["node"], "label": e["label"], "status": "fill_failed", "reason": e["message"]} for e in report.failed]
                await self._transition(app["id"], ApplicationState.NEEDS_USER_INPUT, f"{len(questions)} field(s) need your input", pending_questions=questions)
                raise NeedsUserInputError("Some required fields need your input; see the application in the dashboard.", details={"count": len(questions)})
            if plan.review:
                await self._transition(app["id"], ApplicationState.READY_FOR_REVIEW, f"{len(plan.review)} field(s) need review", pending_questions=[f.to_dict() for f in plan.review])
                raise NeedsUserInputError("Fields filled; some answers need your review before submission.", details={"count": len(plan.review)})
            before_fields = {(n.name_attr, n.id, n.name) for n in snap.form_fields()}
            snap = await ctx.snapshot()
            after_fields = {(n.name_attr, n.id, n.name) for n in snap.form_fields()}
            if after_fields - before_fields and step < self.MAX_STEPS - 1:
                await self._audit.log("form.dynamic_fields", application_id=app["id"], new_fields=len(after_fields - before_fields))
                continue  # dynamic form: new fields appeared after filling; plan them too
            if plan.submit_node is not None:
                break
            if plan.next_node is not None:
                await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(plan.next_node)), args={}, risk=RiskLevel.MEDIUM, verification="none", timeout_ms=15_000, expected_outcome="next step"), None)
                snap = await ctx.snapshot()
                cls = classify_page(snap)
                if cls.kind in ("login", "challenge"):
                    await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "authentication/challenge during form steps")
                    raise AuthRequiredError("Authentication or a challenge appeared mid-form; handle it in the browser and resume.")
                if snap.signals.error_messages:
                    await self._transition(app["id"], ApplicationState.NEEDS_USER_INPUT, "site reported validation errors", pending_questions=[{"label": "validation", "reason": m} for m in snap.signals.error_messages[:10]])
                    raise NeedsUserInputError("The site reported validation errors: " + "; ".join(snap.signals.error_messages[:3]))
                continue
            await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "no submit or next button found", failure={"category": "SITE_UNSUPPORTED"})
            raise SiteUnsupportedError("no submit or next button found")
        assert plan is not None

        # 4. Review gate and submission approval.
        if plan.submit_node is None:
            await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "submit button not found", failure={"category": "SITE_UNSUPPORTED"})
            raise SiteUnsupportedError("submit button not found")
        requires_approval = self._submit_policy == "always" or (self._submit_policy == "per_site" and hints.get("requires_submit_confirmation", True))
        app = await self._transition(app["id"], ApplicationState.READY_FOR_REVIEW, "form filled; awaiting submission approval" if requires_approval else "form filled")
        if requires_approval:
            summary = {"summary": f"Submit application for {job.get('title')} at {job.get('company')}?", "url": snap.url, "filled": sum(len(r["filled"]) for r in all_reports), "application_id": app["id"]}
            approved = await ctx.request_approval("submit", summary)
            if not approved:
                await self._transition(app["id"], ApplicationState.READY_FOR_REVIEW, "submission not approved")
                raise NeedsUserInputError("Submission was not approved.")
        app = await self._transition(app["id"], ApplicationState.SUBMISSION_PENDING, "submitting")

        # 5. Submit and verify with authoritative evidence.
        before = snap
        await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(plan.submit_node)), args={}, risk=RiskLevel.HIGH, verification="none", timeout_ms=30_000, expected_outcome="confirmation page"), None)
        after = await ctx.snapshot()
        if after.url == before.url and after.content_hash == before.content_hash:
            # Give slow sites one more chance to render the confirmation.
            await ctx.execute(BrowserAction(type="wait", args={"ms": 1500}, risk=RiskLevel.LOW, verification="none"), None)
            after = await ctx.snapshot()
        verdict = verify_submission(before, after, expected_confirmation_text=hints.get("confirmation_text"), expected_url_fragment=hints.get("confirmation_url_fragment"))
        evidence = {**verdict.evidence, "checked_at": now_iso(), "source": "post_submit_snapshot"}
        if verdict.status == VerificationStatus.CONFIRMED:
            await self._transition(app["id"], ApplicationState.APPLIED_CONFIRMED, verdict.message, verification_status=verdict.status.value, reference_number=verdict.reference_number, evidence=evidence, applied_at=now_iso())
            await self._audit.log("application.confirmed", application_id=app["id"], reference=verdict.reference_number, url=after.url)
            return {"state": ApplicationState.APPLIED_CONFIRMED.value, "reference_number": verdict.reference_number, "evidence": evidence}
        if verdict.status == VerificationStatus.REJECTED:
            await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, verdict.message, verification_status=verdict.status.value, evidence=evidence, failure={"category": "SUBMISSION_REJECTED", "message": verdict.message})
            raise SubmissionRejectedError(verdict.message, details={"errors": after.signals.error_messages[:5]})
        await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, verdict.message, verification_status=verdict.status.value, evidence=evidence, failure={"category": "SUBMISSION_UNCERTAIN", "message": verdict.message})
        raise SubmissionUncertainError(verdict.message, details={"url": after.url})
