"""ApplicationWorkflow: the job-application state machine driver.

Runs inside a TaskRunner (which supplies the policy-checked executor, snapshots and audit).
Each step persists its transition before continuing so a crash never loses state.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..browser.page_state import classify_page, ineligible_notice
from ..browser.snapshot import PageSnapshot
from ..browser.verifier import verify_submission
from ..core.actions import ActionResult, BrowserAction, Target
from ..core.enums import ApplicationState, Provenance, RiskLevel, VerificationStatus
from ..core.errors import (
    AuthRequiredError,
    HumanVerificationError,
    NavigationError,
    NeedsUserInputError,
    SiteUnsupportedError,
    SubmissionRejectedError,
    SubmissionUncertainError,
)
from ..core.util import now_iso
from ..jobs.adapters import AdapterRegistry
from ..jobs.listing import embedded_application_url, unwrap_redirect
from ..observability.audit import AuditLogger
from ..persistence.unit import Repositories
from ..jobs.matching import US_ONLY_RE
from .field_mapping import find_submit_and_next

_EMPTY_VALUE_RE = re.compile(r"^\s*$|^(select|choose|please select|--+|—|none selected)\b.*$", re.I)


def keep_page_answers(plan: Any) -> int:
    """After the user filled fields themselves and pressed Resume: whatever is already on the page
    counts as answered and is never overwritten. Returns how many fields were kept."""
    kept = 0
    for f in plan.fields:
        cur = (f.current_value or "").strip()
        has_value = bool(cur) and not _EMPTY_VALUE_RE.match(cur) and not (f.field_type in ("checkbox", "switch") and cur.lower() == "no")
        if not has_value or f.field_type == "file":
            continue
        if f.status in ("needs_input", "needs_review", "contradiction") or (f.status == "auto" and cur != (f.value or "").strip()):
            f.status = "skip"
            f.reason = "answered on the page by you"
            kept += 1
    return kept


_APPLICANT_FIELD_RE =re.compile(r"\b(first|last|full|your|given|family|sur)?\s*name\b|e-?mail|\bresume\b|\bcv\b|phone|mobile|linkedin|\baddress\b|\bstreet\b|postal|\bzip\b|\bcity\b", re.I)


# Job boards whose Apply only works for their own signed-in users.
_ACCOUNT_BOARD_RE = re.compile(r"(wellfound\.com|naukri\.com|indeed\.[a-z.]+|smartapply\.indeed|foundit\.in|instahyre\.com|himalayas\.app|cutshort\.io|weworkremotely\.com/job-seekers|glassdoor\.[a-z.]+/(job-listing|partner))", re.I)

_GATE_RE = re.compile(r"^(sign ?in|log ?in|sign ?up|create (an |your )?account|register|join now|continue with (google|linkedin|apple|email|github))\b", re.I)


def _account_gate(snap: PageSnapshot) -> bool:
    """A popup (or page) asking to log in / sign up before applying, with no application form."""
    if bool(snap.form_fields()) and _form_is_application(snap):
        return False
    dialog = [n for n in snap.nodes if n.in_dialog and n.visible]
    pool = dialog or ([] if snap.form_fields() else [])
    hits = sum(1 for n in pool if n.role in ("button", "link") and _GATE_RE.match((n.name or "").strip()))
    has_password = snap.signals.has_password_field
    return hits >= 2 or (hits >= 1 and has_password)


_SEARCH_FIELD_RE = re.compile(r"^(search|find|filter|keyword)s?\b|search (for )?(jobs?|roles?|positions?)|job search|type to search", re.I)


def _real_form_fields(snap: PageSnapshot) -> list:  # type: ignore[type-arg]
    """Form fields other than a page's own search/filter boxes (Workday's "Search for Jobs")."""
    return [n for n in snap.form_fields() if n.role != "searchbox" and (n.type or "") != "search" and not _SEARCH_FIELD_RE.search(f"{n.name or ''} {n.placeholder or ''}")]


def _form_is_application(snap: PageSnapshot) -> bool:
    """The page's form asks for applicant details (name/email + resume/phone…), unlike a job
    board's search box or newsletter sign-up."""
    kinds: set[str] = set()
    for n in snap.form_fields():
        label = " ".join(x for x in (n.name, n.placeholder, n.name_attr) if x)
        m = _APPLICANT_FIELD_RE.search(label)
        if m:
            word = m.group(0).lower()
            kinds.add("name" if "name" in word else "email" if "mail" in word else "resume" if word in ("resume", "cv") else word.strip())
    return len(kinds) >= 2


def _linkedin_external_apply(snap: PageSnapshot) -> str | None:
    """The company-site address behind a LinkedIn posting's "Apply" link, when it has one."""
    for n in snap.nodes:
        if n.role == "link" and n.href and re.search(r"\bapply\b", n.name or "", re.I):
            dest = unwrap_redirect(n.href) or (n.href if "linkedin.com" not in n.href and n.href.startswith("http") else None)
            if dest:
                return dest
    return None


def location_from_page(snap: PageSnapshot) -> str | None:
    """Country named near the top of a job page (title, headings, first lines), or None."""
    from .profile import countries_in

    head = " ".join([snap.title or "", *(n.name or "" for n in snap.nodes[:25] if n.role in ("heading", "text", "status")), (snap.text_excerpt or "")[:600]])
    found = countries_in(head) - {"European Union"}
    return sorted(found)[0] if len(found) == 1 else None


_POSTING_CLOSED_RE =re.compile(r"job (was )?not found|(position|job|role|vacancy|opening)[^.]{0,60}(has been|is|was) (filled|closed)|has been filled|no longer (accepting applications|accepting|available|open)|(job|position|vacancy) (has )?expired|no longer (accepting applications|available|open)|job (posting )?(has )?(expired|closed)|this (job|position|role) (is no longer|has been removed)|page (you requested )?(was )?not found", re.I)
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


_TYPED_PICK_KEYS = re.compile(r"^(contact\.(city|state|country|address|postal_code|zip|street)|education\[|education\.|employment\[|employment\.|links\.)")
_SITE_DOWN_RE = re.compile(r"temporarily unavailable|service unavailable|\b50[234]\b|bad gateway|gateway time-?out|under maintenance|scheduled maintenance|down for maintenance|try again (later|in a few)|we['’]?ll be (right )?back|site is down|route not found|error_msg", re.I)


_SUCCESS_NOTICE_RE = re.compile(r"success|uploaded|saved|added|attached|complete[d]?\b|thank you|received", re.I)
_PROBLEM_WORDS_RE = re.compile(r"\b(error|invalid|required|missing|failed|could not|couldn't|unable|must|please (enter|select|complete|provide|fix|correct|upload))\b", re.I)


def _real_errors(messages: list[str]) -> list[str]:
    """Error-styled notices that really report a problem. Workday shows "Resume.pdf successfully
    uploaded" in the same alert slot as its validation errors."""
    return [m for m in messages if _PROBLEM_WORDS_RE.search(m) or not _SUCCESS_NOTICE_RE.search(m)]


def _site_down(snap: PageSnapshot) -> bool:
    """An outage/maintenance page (BambooHR "temporarily unavailable"): nothing to apply to now,
    but the job is still there, so the attempt is retried later rather than failed."""
    if _SITE_DOWN_RE.search(" ".join([snap.title, *(n.name for n in snap.nodes if n.role == "heading")])):
        return True
    # Body text counts only on a near-empty page with nothing to apply through.
    if len(snap.nodes) <= 12 and not snap.form_fields() and find_apply_button(snap) is None:
        return bool(_SITE_DOWN_RE.search(snap.text_excerpt[:800]))
    return False


def find_apply_button(snap):  # type: ignore[no-untyped-def]
    """The control that opens the application: exact 'Apply'-style labels first, then any
    button/link mentioning apply that is not a filter, status or third-party sign-in."""
    controls = [n for n in snap.nodes if n.role in ("button", "link") and n.name and not n.disabled and n.visible]
    # An open "Start your application" popup (Workday: Autofill with Resume / Apply Manually) wins
    # over the page's own Apply button behind it, which would just reopen the same popup.
    in_dialog = [n for n in controls if n.in_dialog]
    if in_dialog:
        manual = [n for n in in_dialog if re.search(r"apply manually|apply now|start (your )?application|continue", n.name, re.I)]
        if manual:
            return manual[0]
        loose_dialog = [n for n in in_dialog if _APPLY_LOOSE.search(n.name) and not _NOT_APPLY.search(n.name)]
        if loose_dialog:
            return loose_dialog[0]
    exact = [n for n in controls if _APPLY_EXACT.match(n.name)]
    if exact:
        # "Apply Manually" / "Start application" only exist once Apply was clicked: take that step.
        exact.sort(key=lambda n: 0 if re.search(r"manually|start", n.name, re.I) else 1)
        return exact[0]
    # Long labels count when they START with apply: LinkedIn's "Apply to Software Engineer on
    # company website", "Apply now at Acme". Anywhere else a mention of "apply" must stay short.
    loose = [n for n in controls if not _NOT_APPLY.search(n.name) and ((_APPLY_LOOSE.search(n.name) and len(n.name) <= 40) or (re.match(r"^\s*(apply|easy apply)\b", n.name, re.I) and len(n.name) <= 120))]
    loose.sort(key=lambda n: 0 if re.search(r"company (website|site)|external|career", n.name, re.I) else 1)
    return loose[0] if loose else None


_ADDRESS_KEYS = {"contact.address_line1", "contact.address_line2", "contact.full_address", "contact.permanent_address", "contact.city", "contact.state", "contact.postal_code", "contact.country", "contact.middle_name"}

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
    # A name/email field answered from an approved answer (not a profile key) is identity too.
    identity |= {f"answer:{f.label}" for f in plan.fields if f.answer_id and re.search(r"\bname\b|e-?mail", f.label or "", re.I)}
    applicant = (keys & {"contact.phone", "links.linkedin"}) | ({"resume"} if any(f.field_type == "file" and f.resume_id for f in plan.fields) else set())
    # Workday-style wizards ask name + address on step 1 and phone/resume on later steps.
    applicant |= {"address"} if keys & _ADDRESS_KEYS else set()
    if plan.next_node is not None and re.search(r"/apply", urlsplit(snap.url).path, re.I):
        return None  # a later step of an application wizard (questions, disclosures): no name here
    if not identity:
        return "no name or email field on the form"
    if len(identity) + len(applicant) < 2 and plan.next_node is None:
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
    # Following an "Apply" that opens the company site in a NEW tab: how many tabs have been opened
    # so far, and a callback that switches the task to a tab this one opened after that point.
    created_tabs: Callable[[], int] | None = None
    adopt_new_tab: Callable[[int], Awaitable[bool]] | None = None
    # The tab was handed over by the user (side panel / dashboard), not opened by the runner: the
    # page is taken as it is — no reload, their answers kept, continue from the step shown.
    user_tab: bool = False


class ApplicationWorkflow:
    MAX_STEPS = 8  # multi-step wizard bound (and "Open" → post → job → Apply chains)

    def __init__(self, repos: Repositories, filler: FormFiller, adapters: AdapterRegistry, audit: AuditLogger, *, submit_policy: str = "always"):
        self._repos = repos
        self._filler = filler
        self._adapters = adapters
        self._audit = audit
        self._submit_policy = submit_policy

    async def _probe_dropdowns(self, ctx: WorkflowContext, snap: PageSnapshot, limit: int = 30, *, fact_keys: list[str] | None = None, answers: list[dict[str, Any]] | None = None) -> None:
        """Searchable dropdowns only render options when opened: open each (read-only) to learn its
        options, so answers are matched to real choices (ranges, multi-selects, unknown wording).
        Type-ahead pickers the rules already answer with a plain value (city, country, school,
        employer, country code) are not opened: typing the value into them is the selection, and
        reading a 250-entry list first cost 2-13 s per field."""
        from .field_mapping import heuristic_map_fields

        combos = [n for n in snap.nodes if n.role in ("combobox", "listbox") and n.tag != "select" and n.options is None and n.visible and not n.disabled]
        typed: set[int] = set()
        if fact_keys is not None:
            inputs = [{"node": n.i, "label": n.name or n.placeholder or n.name_attr or "", "type": n.type or n.role, "options": None, "required": n.required} for n in combos]
            for m in heuristic_map_fields(inputs, fact_keys, answers or []):
                if _TYPED_PICK_KEYS.match(str(m.get("profile_key") or "")):
                    typed.add(int(m["node"]))
        typed.update(n.i for n in combos if re.search(r"country code|dial code|phone code", n.name or "", re.I))
        probed = 0
        for n in combos:
            if probed >= limit:
                break
            if n.i in typed:
                continue
            probed += 1
            try:
                res = await ctx.execute(BrowserAction(type="read_options", target=Target(strategy="node", value=str(n.i)), args={}, risk=RiskLevel.LOW, verification="none", timeout_ms=8_000), None)
            except Exception as exc:  # noqa: BLE001 - a dropdown that will not open is planned without options
                log.info("could not read options of %s: %s", n.name, exc)
                continue
            opts = (res.evidence or {}).get("options") if res.ok else None
            if isinstance(opts, list) and opts:
                n.options = [str(o)[:200] for o in opts][:200]

    async def _settled_snapshot(self, ctx: WorkflowContext, snap: PageSnapshot, *, max_wait: float = 20.0) -> PageSnapshot:
        """Re-read a page that is still rendering until a form or an Apply control shows up, or its
        node count has stopped changing (four identical readings over 6 s). Workday's "Start Your
        Application" page draws its header first and its Apply links many seconds later."""
        waited = 0.0
        same = 0
        last = len(snap.nodes)
        while waited < max_wait:
            await asyncio.sleep(1.5)
            waited += 1.5
            snap = await ctx.snapshot()
            if snap.form_fields() or find_apply_button(snap) is not None:
                return snap
            same = same + 1 if len(snap.nodes) == last else 0
            last = len(snap.nodes)
            # A near-empty page is a loading shell (skeleton + "skip to content"), not a stable
            # page: keep waiting for it up to the limit.
            if same >= 3 and waited >= 6 and len(snap.nodes) >= 15:
                return snap
        return snap

    async def _next_step_snapshot(self, ctx: WorkflowContext, before_fields: set, url_before: str, *, max_wait: float = 15.0) -> PageSnapshot:  # type: ignore[type-arg]
        """After Next / Save and Continue: wait until the page really moved on (different fields,
        a new URL, or validation errors) before reading it — Workday redraws a step slowly, and a
        snapshot taken too early still shows the step just completed."""
        waited = 0.0
        snap = await ctx.snapshot()
        while waited < max_wait:
            fields = {(n.name_attr, n.id, n.name) for n in snap.form_fields()}
            if fields != before_fields or snap.url != url_before or snap.signals.error_messages or not snap.form_fields():
                return snap
            await asyncio.sleep(1.5)
            waited += 1.5
            snap = await ctx.snapshot()
        return snap

    async def _needs_board_account(self, app: dict[str, Any], snap: PageSnapshot) -> None:
        """Boards that only take applications from their own logged-in users (Wellfound, Naukri,
        Indeed, foundit, Instahyre, Himalayas…): hand over to the user instead of failing."""
        host = urlsplit(snap.url).netloc.lower().removeprefix("www.")
        await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, f"{host} only accepts applications from signed-in users")
        raise AuthRequiredError(f"{host} needs you signed in to apply. Sign in there in this tab, then press Resume.", details={"url": snap.url})

    async def _transition(self, app_id: str, state: ApplicationState, reason: str, *, actor: str = "agent", **fields: Any) -> dict[str, Any]:
        # ``actor`` is recorded on the transition, not stored as an application column.
        app = await self._repos.applications.transition(app_id, state, reason=reason, actor=actor, fields=fields or None)
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

        if app["state"] == ApplicationState.APPLICATION_FAILED and ((app.get("failure_json") or {}).get("category") == "SUBMISSION_UNCERTAIN" or app.get("verification_status") == VerificationStatus.UNCERTAIN.value):
            # It may already have been sent: never submit twice.
            raise SubmissionUncertainError("This application was submitted earlier without a visible confirmation. Check your email; mark it confirmed or failed in the dashboard before applying again.")
        # Resuming after the user answered / signed in on the page: continue on that page.
        resumed = app["state"] in (ApplicationState.NEEDS_USER_INPUT, ApplicationState.AUTH_REQUIRED, ApplicationState.READY_FOR_REVIEW) or ctx.user_tab
        # The user was already asked to complete the form's CAPTCHA (and pressed Resume): submit now.
        captcha_done = bool((app.get("fill_plan_json") or {}).get("captcha_handoff"))
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
        # On a resume, any form still open counts: later steps of multi-step forms ("Work and
        # Education") have no name/email fields, and leaving them would restart from step 1.
        on_form = bool(snap.form_fields()) and (resumed or _form_is_application(snap))
        if target_url and not snap.url.startswith(target_url.split("?")[0][:60]) and not (resumed and on_form):
            # (Never when resuming on the open form: reloading would wipe what the user typed.)
            await ctx.execute(BrowserAction(type="navigate", args={"url": target_url}, risk=RiskLevel.LOW, verification="navigation_complete", timeout_ms=30_000), None)
            snap = await ctx.snapshot()

        # 2. Session / challenge detection, then form location (bounded steps).
        clicks_without_effect = 0
        for step in range(self.MAX_STEPS):
            dest = unwrap_redirect(snap.url)
            if dest:
                # LinkedIn's "You're leaving LinkedIn" page: go straight to the company's site.
                await ctx.execute(BrowserAction(type="navigate", args={"url": dest}, risk=RiskLevel.LOW, verification="navigation_complete", timeout_ms=30_000), None)
                snap = await ctx.snapshot()
                continue
            if "linkedin.com/jobs" in snap.url and not _form_is_application(snap):
                external = _linkedin_external_apply(snap)
                if external:
                    await ctx.execute(BrowserAction(type="navigate", args={"url": external}, risk=RiskLevel.LOW, verification="navigation_complete", timeout_ms=30_000), None)
                    snap = await ctx.snapshot()
                    continue
                if any(n.role == "button" and re.search(r"easy apply", n.name or "", re.I) for n in snap.nodes):
                    await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "LinkedIn Easy Apply is not automated", failure={"category": "SITE_UNSUPPORTED", "message": "LinkedIn Easy Apply"})
                    raise SiteUnsupportedError("LinkedIn Easy Apply job: not automated (LinkedIn restricts accounts that use automation). Apply on LinkedIn yourself, or via the company's site.")
            cls = classify_page(snap)
            if cls.kind == "challenge" and "bot-protection check page" in cls.reasons:
                # Cloudflare-style "Just a moment…" pages usually clear themselves in a normal browser
                # within seconds. Only wait and look again; never click or solve anything on them.
                for _ in range(10):
                    await asyncio.sleep(2)
                    snap = await ctx.snapshot()
                    cls = classify_page(snap)
                    if cls.kind != "challenge":
                        break
            if cls.kind == "challenge":
                await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "human verification required: " + "; ".join(cls.reasons))
                raise HumanVerificationError("Complete the verification challenge in the browser, then resume.", details={"url": snap.url})
            if cls.kind == "login":  # a login/recovery/lock page; a header "Sign in" link is not a wall
                await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, f"authentication required ({cls.auth_state.value})")
                raise AuthRequiredError(f"Log in on {snap.url} in the browser, then resume.", details={"auth_state": cls.auth_state.value})
            notice = ineligible_notice(snap)
            if notice:
                # The site itself says this candidate cannot apply here ("You are not eligible for
                # this job"): nothing is filled, the job is closed out and never retried.
                await self._transition(app["id"], ApplicationState.CANCELLED, f"ineligible: the site says \"{notice[:160]}\"", evidence={"url": snap.url, "notice": notice[:300]})
                await self._audit.log("application.ineligible", application_id=app["id"], task_id=ctx.task_id, url=snap.url, notice=notice[:200])
                return {"state": "SKIPPED", "note": f"not eligible — the site says: {notice[:140]}"}
            if not (snap.form_fields() and _form_is_application(snap)):
                embedded = embedded_application_url(snap)
                if embedded and embedded.split("#")[0] != snap.url.split("#")[0]:
                    # The form lives in a frame from the applicant-tracking site: open it directly.
                    await ctx.execute(BrowserAction(type="navigate", args={"url": embedded}, risk=RiskLevel.LOW, verification="navigation_complete", timeout_ms=30_000), None)
                    snap = await ctx.snapshot()
                    continue
            if cls.kind == "confirmation" and step == 0:
                # Already applied (site shows confirmation before any action): treat as uncertain duplicate.
                await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "page already shows a confirmation; reconcile manually", verification_status=VerificationStatus.UNCERTAIN.value, evidence={"url": snap.url, "reasons": cls.reasons})
                raise SubmissionUncertainError("The page already shows a confirmation; verify whether you applied before.")
            if not _real_form_fields(snap) and find_apply_button(snap) is None:
                # Single-page sites (Workday) draw the job and its Apply button seconds after the
                # URL loads: look again while the page is still changing before deciding. (A
                # header's job-search box is not a form.)
                snap = await self._settled_snapshot(ctx, snap)
                cls = classify_page(snap)
                if cls.kind in ("login", "challenge"):
                    continue  # what finished loading is a sign-in wall or a check: handled at the top
                if _account_gate(snap):
                    await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "the site asks you to sign in or create an account to apply")
                    raise AuthRequiredError(f"{urlsplit(snap.url).netloc} needs you to sign in (or sign up) before applying. Do that in this tab, then press Resume.", details={"url": snap.url})
            on_application = bool(snap.form_fields()) and _form_is_application(snap)
            apply_node = None if on_application else find_apply_button(snap)
            if apply_node is None and not on_application and _site_down(snap):
                # The site itself is down right now: retried later by the scheduler.
                raise NavigationError("site temporarily unavailable: " + (snap.title or snap.url)[:100], details={"url": snap.url})
            if apply_node is None and not on_application:
                # The rules found no way forward: let the local model read the page like a person and
                # pick the control that leads to this job's application.
                from ..agent.navigator import choose_apply_click

                apply_node = await choose_apply_click(self._filler._gateway, snap, task_id=ctx.task_id)
            if cls.unsupported_reasons and not snap.form_fields() and apply_node is None:
                await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "unsupported page structure", failure={"category": "SITE_UNSUPPORTED", "reasons": cls.unsupported_reasons})
                raise SiteUnsupportedError("; ".join(cls.unsupported_reasons))
            if on_application or (_real_form_fields(snap) and apply_node is None):
                log.info("form reached on %s (application=%s): fields=%s", snap.url[:120], on_application, [(n.role, (n.name or n.placeholder or "")[:30]) for n in _real_form_fields(snap)][:10])
                break
            # Job description page (a board's search or newsletter box is not the application): click Apply.
            if apply_node is None and _POSTING_CLOSED_RE.search(" ".join([snap.title, snap.text_excerpt[:1500], *(n.name for n in snap.nodes if n.role == "heading")])):
                # Removed/filled posting: nothing to apply to; never retried.
                await self._transition(app["id"], ApplicationState.CANCELLED, "posting closed or removed by the employer", evidence={"url": snap.url})
                return {"state": ApplicationState.CANCELLED.value, "note": "posting closed or removed"}
            if apply_node is None and _ACCOUNT_BOARD_RE.search(snap.url):
                await self._needs_board_account(app, snap)
            if apply_node is None:
                await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "no application form or apply button found", failure={"category": "SITE_UNSUPPORTED"})
                raise SiteUnsupportedError("no application form or apply button found on " + snap.url)
            href = apply_node.href if apply_node.role == "link" else None
            same_page = bool(href) and href.split("#")[0] == snap.url.split("#")[0]
            if href and href.startswith(("http://", "https://")) and not same_page:
                # Follow the Apply link itself: a scripted click that opens a pop-up is blocked by
                # Chrome (Adzuna), and "Apply on company site" in a new tab would leave the task behind.
                await ctx.execute(BrowserAction(type="navigate", args={"url": unwrap_redirect(href) or href}, risk=RiskLevel.LOW, verification="navigation_complete", timeout_ms=30_000), None)
                snap = await ctx.snapshot()
                continue
            opened_before = ctx.created_tabs() if ctx.created_tabs else 0
            before = snap
            await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(apply_node.i)), args={}, risk=RiskLevel.LOW, verification="none", timeout_ms=15_000, expected_outcome="application form opens"), None)
            snap = await ctx.snapshot()
            unchanged = snap.url == before.url and snap.content_hash == before.content_hash
            if unchanged and ctx.adopt_new_tab and await ctx.adopt_new_tab(opened_before):
                # The click opened the application in a new tab: continue there.
                snap = await ctx.snapshot()
                unchanged = False
            if _account_gate(snap):
                # Apply opened the board's own "Log in / Sign up" popup (Wellfound, Himalayas…):
                # applying there needs the user's account on that site.
                await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "the site asks you to sign in or create an account to apply")
                raise AuthRequiredError(f"{urlsplit(snap.url).netloc} needs you to sign in (or sign up) before applying. Do that in this tab, then press Resume.", details={"url": snap.url})
            if unchanged:
                clicks_without_effect += 1
                if clicks_without_effect >= 2 and _ACCOUNT_BOARD_RE.search(snap.url):
                    await self._needs_board_account(app, snap)
                if clicks_without_effect >= 2:
                    await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, f"clicking '{apply_node.name[:60]}' does nothing on this page", failure={"category": "SITE_UNSUPPORTED"})
                    raise SiteUnsupportedError(f"Clicking '{apply_node.name[:60]}' does nothing here (the site may need you signed in, or blocks automated clicks).")
        else:
            await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "could not reach an application form", failure={"category": "SITE_UNSUPPORTED"})
            raise SiteUnsupportedError("could not reach an application form")

        # 3. Fill (possibly multi-step), pausing on blocking questions.
        all_reports: list[dict[str, Any]] = []
        plan = None
        refilled = False
        attached: set[str] = set()  # upload boxes already given a file (a redraw must not repeat them)
        for step in range(self.MAX_STEPS):
            await self._probe_dropdowns(ctx, snap, fact_keys=list(profile.facts().keys()), answers=answers)
            if not job.get("location"):
                # The job record has no location (some boards hide it from structured data): use
                # the place named at the top of the page, e.g. "State College, Pennsylvania, United States".
                job = {**job, "location": location_from_page(snap)}
            plan = await self._filler.plan(snap, profile, answers, resumes, submit_names=hints["submit_button_names"], task_id=ctx.task_id, job_location=job.get("location"), job=job)
            if resumed:
                keep_page_answers(plan)
            for f in plan.fields:
                if f.field_type == "file" and f.status == "auto" and f.label in attached:
                    f.status, f.reason = "skip", "attached in an earlier pass"
            us_only =next((n.section or n.name for n in snap.nodes if US_ONLY_RE.search(f"{n.name or ''} {n.section or ''}")), None)
            authorized = [c.strip().lower() for c in profile.legal.work_authorized_countries if c.strip()]
            if us_only and authorized and not any(c in ("united states", "usa", "us") for c in authorized):
                # The form itself says only US-based people can be hired: applying cannot succeed.
                await self._transition(app["id"], ApplicationState.CANCELLED, f"ineligible: hires only in the US (\"{us_only[:120]}\")")
                return {"state": "SKIPPED", "note": f"hires only in the US: {us_only[:120]}"}
            if step == 0 and not resumed:
                # (Not on a resume: a later wizard step — "My Experience", questions — has no name
                # or email field, and the run already proved the page is an application.)
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
            attached.update(e["label"] for e in report.filled if str(e.get("message") or "").startswith("file attached"))
            await self._repos.applications.update_fields(app["id"], {"fill_plan": {"steps": all_reports, "last_plan": plan.to_dict()}})
            if report.failed and not plan.blocking and step < self.MAX_STEPS - 1 and not refilled:
                # Filling can re-render the form (SmartRecruiters parses an uploaded resume and
                # redraws every field; React forms replace nodes): read the live page once more and
                # fill what is still empty before asking the user.
                refilled = True
                await self._audit.log("form.refill", application_id=app["id"], failed=[e["label"] for e in report.failed][:10])
                await asyncio.sleep(1.5)
                snap = await ctx.snapshot()
                continue
            if plan.blocking or any(f.status == "contradiction" for f in plan.fields) or report.failed:
                questions = [f.to_dict() for f in plan.fields if f.status in ("needs_input", "contradiction") and (f.required or f.status == "contradiction")]
                questions += [{"node": e["node"], "label": e["label"], "status": "fill_failed", "reason": e["message"]} for e in report.failed]
                await self._transition(app["id"], ApplicationState.NEEDS_USER_INPUT, f"{len(questions)} field(s) need your input", pending_questions=questions)
                raise NeedsUserInputError("Some required fields need your input; see the application in the dashboard.", details={"count": len(questions)})
            if plan.review:
                await self._transition(app["id"], ApplicationState.READY_FOR_REVIEW, f"{len(plan.review)} field(s) need review", pending_questions=[f.to_dict() for f in plan.review])
                raise NeedsUserInputError("Fields filled; some answers need your review before submission.", details={"count": len(plan.review)})
            before_fields = {(n.name_attr, n.id, n.name) for n in snap.form_fields()}
            # A type-ahead (Workday skills) can leave hundreds of suggestions open, pushing the
            # Save/Next button past the snapshot limit: close it first.
            try:
                await ctx.execute(BrowserAction(type="press_key", args={"key": "Escape"}, risk=RiskLevel.LOW, verification="none", timeout_ms=3_000), None)
            except Exception:  # noqa: BLE001 - best effort
                pass
            snap = await ctx.snapshot()
            after_fields = {(n.name_attr, n.id, n.name) for n in snap.form_fields()}
            # The new snapshot renumbered the page: re-resolve buttons against it.
            plan.submit_node, plan.next_node = find_submit_and_next(snap.nodes, hints["submit_button_names"])
            if after_fields - before_fields and step < self.MAX_STEPS - 1:
                await self._audit.log("form.dynamic_fields", application_id=app["id"], new_fields=len(after_fields - before_fields))
                continue  # dynamic form: new fields appeared after filling; plan them too
            if plan.submit_node is not None:
                break
            if plan.next_node is not None:
                await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(plan.next_node)), args={}, risk=RiskLevel.MEDIUM, verification="none", timeout_ms=15_000, expected_outcome="next step"), None)
                snap = await self._next_step_snapshot(ctx, before_fields, snap.url)
                cls = classify_page(snap)
                if cls.kind in ("login", "challenge"):
                    await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "authentication/challenge during form steps")
                    raise AuthRequiredError("Authentication or a challenge appeared mid-form; handle it in the browser and resume.")
                problems = _real_errors(snap.signals.error_messages)
                if problems:
                    await self._transition(app["id"], ApplicationState.NEEDS_USER_INPUT, "site reported validation errors", pending_questions=[{"label": "validation", "reason": m} for m in problems[:10]])
                    raise NeedsUserInputError("The site reported validation errors: " + "; ".join(problems[:3]))
                continue
            await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "no submit or next button found", failure={"category": "SITE_UNSUPPORTED"})
            raise SiteUnsupportedError("no submit or next button found")
        assert plan is not None
        if snap.signals.has_captcha and not captcha_done:
            # The form is filled; the CAPTCHA on it is the user's to complete. Resume submits.
            await self._repos.applications.update_fields(app["id"], {"fill_plan": {"steps": all_reports, "last_plan": plan.to_dict(), "captcha_handoff": True}})
            await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "form filled; complete the verification on the page, then resume to submit")
            raise HumanVerificationError("The form is filled. Tick the verification (CAPTCHA) box on the page yourself; the agent submits as soon as it is done.", details={"url": snap.url, "captcha_on_form": True})

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

        # 5. Submit and verify with authoritative evidence. Fresh snapshot first so the submit index
        # belongs to the live page (uploads and re-renders shift node numbers).
        snap = await ctx.snapshot()
        submit_node, _ = find_submit_and_next(snap.nodes, hints["submit_button_names"])
        if submit_node is None:
            await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "submit button disappeared before submission", failure={"category": "SITE_UNSUPPORTED"})
            raise SiteUnsupportedError("submit button not found on the live page")
        before = snap
        await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(submit_node)), args={}, risk=RiskLevel.HIGH, verification="none", timeout_ms=30_000, expected_outcome="confirmation page"), None)
        after = await ctx.snapshot()
        verdict = verify_submission(before, after, expected_confirmation_text=hints.get("confirmation_text"), expected_url_fragment=hints.get("confirmation_url_fragment"))
        for _ in range(8):
            # Sites take a few seconds to send the form and show "Thank you" (or an error):
            # keep looking for up to ~12 s before calling the outcome uncertain.
            if verdict.status in (VerificationStatus.CONFIRMED, VerificationStatus.REJECTED):
                break
            await asyncio.sleep(1.5)  # a plain pause: repeated "wait" actions would trip loop detection
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
