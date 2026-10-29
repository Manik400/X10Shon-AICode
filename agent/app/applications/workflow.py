"""ApplicationWorkflow: the job-application state machine driver.

Runs inside a TaskRunner (which supplies the policy-checked executor, snapshots and audit).
Each step persists its transition before continuing so a crash never loses state.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin, urlsplit
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
from ..jobs.matching import US_ONLY_RE, location_blocker
from .field_mapping import find_submit_and_next
from .signin import SignInHelper, host_of

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
    has_password = snap.signals.has_password_field
    dialog = [n for n in snap.nodes if n.in_dialog and n.visible]
    if dialog:
        hits = sum(1 for n in dialog if n.role in ("button", "link") and _GATE_RE.match((n.name or "").strip()))
        return hits >= 2 or (hits >= 1 and has_password)
    # No popup: the page itself is the gate only when it says so (Workday's "Sign In" page with
    # "Sign in with Google" / "Sign in with email" and no password box yet; a board's "Log in to
    # apply"), never because a header carries a "Sign in" link beside a job description.
    hits = sum(1 for n in snap.nodes if n.visible and n.role in ("button", "link") and _GATE_RE.match((n.name or "").strip()))
    if hits == 0 or (snap.form_fields() and not has_password):
        return False  # no sign-in control, or some other form (search box, newsletter) is the page
    if hits >= 1 and has_password:
        return True
    from ..browser.page_state import signin_label

    return signin_label(snap.title) or any(n.role == "heading" and n.visible and signin_label(n.text or n.name) for n in snap.nodes)


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


def linkedin_easy_apply_only(snap: PageSnapshot) -> bool:
    """A LinkedIn posting whose only way to apply is LinkedIn's own Easy Apply."""
    return "linkedin.com/jobs" in snap.url and not _linkedin_external_apply(snap) and any(n.role in ("button", "link") and re.match(r"^\s*easy apply\b", n.name or "", re.I) for n in snap.nodes)


def location_from_page(snap: PageSnapshot) -> str | None:
    """Country named near the top of a job page (title, headings, first lines), or None."""
    from .profile import countries_in

    head = " ".join([snap.title or "", *(n.name or "" for n in snap.nodes[:25] if n.role in ("heading", "text", "status")), (snap.text_excerpt or "")[:600]])
    found = countries_in(head) - {"European Union"}
    return sorted(found)[0] if len(found) == 1 else None


# Career sites that send a removed posting's link to their search page say so in the address
# (iCIMS "notFound=1", PageUp "jobnotfound=true").
_CLOSED_URL_RE = re.compile(r"[?&](notFound=1|jobnotfound=true|job_not_found|expired=(1|true))\b|/(job-expired|job-closed|expired-job)\b", re.I)
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
    if plan.next_node is not None and (re.search(r"/apply", urlsplit(snap.url).path, re.I) or any(n.in_dialog for n in snap.form_fields())):
        return None  # a later step of an application wizard (questions, disclosures), or a step of a popup wizard (LinkedIn Easy Apply): no name here
    if not identity:
        return "no name or email field on the form"
    if len(identity) + len(applicant) < 2 and plan.next_node is None:
        return "the form asks for too little to be an application"
    return None


_SIGNIN_LOCKS: dict[str, asyncio.Lock] = {}


def _unattended_review(fields: list[Any]) -> None:
    """Unattended mode (submission policy "never"): nobody reviews, so a review-flagged field is
    filled with the value it has, verified or inferred, and an optional one without a value is
    left empty. Only a required field with no value, or a legally significant one with only a
    guessed value, still pauses the run."""
    for f in fields:
        if f.status != "needs_review":
            continue
        trusted = f.provenance in (Provenance.VERIFIED_FACT, Provenance.APPROVED_ANSWER)
        if f.value and (trusted or not f.legally_significant):
            f.status = "auto"
            f.reason = (f.reason + "; " if f.reason else "") + "filled unattended"
        elif not f.required and not f.value:
            f.status, f.reason = "skip", "optional; left empty (unattended)"


def _button_key(n: Any) -> tuple[Any, ...]:
    """What makes a button "the same one" across two snapshots (indices are renumbered)."""
    if n is None:
        return ("", None, "", "")
    return ((n.name or "").strip().lower(), n.form, (n.type or "").lower(), n.testid or n.id or "")


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
        # Unattended mode (scheduler settings): sign in with stored site accounts, create accounts
        # where a site offers it.
        self.auto_sign_in = True
        self.auto_create_accounts = True

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

    async def _needs_board_account(self, app: dict[str, Any], snap: PageSnapshot, *, note: str = "") -> None:
        """Boards that only take applications from their own logged-in users (Wellfound, Naukri,
        Indeed, foundit, Instahyre, Himalayas…): hand over to the user instead of failing."""
        host = urlsplit(snap.url).netloc.lower().removeprefix("www.")
        await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, f"{host} only accepts applications from signed-in users{note}")
        raise AuthRequiredError(f"{host} needs you signed in to apply{note}. Sign in there in this tab, then press Resume.", details={"url": snap.url})

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

        # Sign-in walls are passed with the profile's stored site accounts (or a new account
        # where the site offers one): once per host per run, then the run hands over as before.
        signin = SignInHelper(self._repos, self._audit, create_accounts=self.auto_create_accounts)
        tried_hosts: set[str] = set()
        wall_notes: list[str] = []

        async def try_wall(page: PageSnapshot) -> PageSnapshot | None:
            nonlocal profile
            host = host_of(page.url)
            if not self.auto_sign_in or not host or host in tried_hosts:
                return None
            tried_hosts.add(host)
            failures = await self._recent_signin_failures(host)
            if failures >= 3:
                # A site that refused the agent three times is not tried a fourth time by itself:
                # its jobs wait until the account under Profile → Site accounts is fixed.
                wall_notes.append(f"sign-in on {host} has failed {failures} times; not trying again until its account under Profile → Site accounts is fixed or removed")
                await self._audit.log("signin.skipped", application_id=app["id"], task_id=ctx.task_id, host=host, failures=failures)
                return None
            # One sign-in / sign-up per site at a time: two jobs on the same Workday tenant must
            # not both create "the" account. The second waits, re-reads the profile and signs in.
            lock = _SIGNIN_LOCKS.setdefault(host, asyncio.Lock())
            async with lock:
                profile = CandidateProfile.model_validate(await self._repos.profile.get() or {})
                res = await signin.attempt(ctx, page, profile, task_id=ctx.task_id)
            await self._audit.log("signin.attempt", application_id=app["id"], task_id=ctx.task_id, host=host, status=res.status, note=res.message[:160])
            if res.status not in ("signed_in", "created"):
                wall_notes.append(res.message)
                return None
            profile = CandidateProfile.model_validate(await self._repos.profile.get() or {})
            page = res.snap
            if target_url and not (page.form_fields() or find_apply_button(page)) and not page.url.startswith(target_url.split("?")[0][:60]):
                # Signed in, but the site went to its home page: back to the job.
                await ctx.execute(BrowserAction(type="navigate", args={"url": target_url}, risk=RiskLevel.LOW, verification="navigation_complete", timeout_ms=30_000), None)
                page = await ctx.snapshot()
            return page

        def wall_note() -> str:
            return f"; {wall_notes[-1][:140]}" if wall_notes else ""

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
                if linkedin_easy_apply_only(snap):
                    # LinkedIn Easy Apply jobs are left alone: nothing is clicked or filled, the job
                    # is closed out (never retried) and the tab is closed.
                    await self._transition(app["id"], ApplicationState.CANCELLED, "LinkedIn Easy Apply job: left alone", evidence={"url": snap.url})
                    await self._audit.log("application.easy_apply_skipped", application_id=app["id"], task_id=ctx.task_id, url=snap.url)
                    return {"state": "SKIPPED", "note": "LinkedIn Easy Apply job; left alone"}
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
                fixed = await try_wall(snap)
                if fixed is not None:
                    snap = fixed
                    continue
                await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, f"authentication required ({cls.auth_state.value}){wall_note()}")
                raise AuthRequiredError(f"Log in on {snap.url} in the browser, then resume{wall_note()}.", details={"auth_state": cls.auth_state.value})
            notice = ineligible_notice(snap)
            if notice:
                # The site itself says this candidate cannot apply here ("You are not eligible for
                # this job"): nothing is filled, the job is closed out and never retried.
                await self._transition(app["id"], ApplicationState.CANCELLED, f"ineligible: the site says \"{notice[:160]}\"", evidence={"url": snap.url, "notice": notice[:300]})
                await self._audit.log("application.ineligible", application_id=app["id"], task_id=ctx.task_id, url=snap.url, notice=notice[:200])
                return {"state": "SKIPPED", "note": f"not eligible — the site says: {notice[:140]}"}
            blocker = location_blocker(" ".join([snap.title or "", snap.text_excerpt or ""]), profile.model_dump())
            if blocker:
                # The posting's own facts rule the candidate out by location (Wellfound: on-site
                # elsewhere with no visa sponsorship / relocation, or "hires remotely in" countries
                # that exclude theirs): closed out without touching anything; the tab is closed.
                await self._transition(app["id"], ApplicationState.CANCELLED, f"ineligible: {blocker[:180]}", evidence={"url": snap.url, "notice": blocker[:300]})
                await self._audit.log("application.ineligible", application_id=app["id"], task_id=ctx.task_id, url=snap.url, notice=blocker[:200])
                return {"state": "SKIPPED", "note": f"not eligible — {blocker[:140]}"}
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
                if len(snap.nodes) < 12 and not snap.form_fields() and find_apply_button(snap) is None and not any(n.role == "heading" and n.visible for n in snap.nodes):
                    # Still a header-only shell after waiting (Workday under load draws nothing
                    # else for a long time): tried again later rather than written off.
                    raise NavigationError("the page did not finish loading (header only after waiting): " + snap.url[:100], details={"url": snap.url})
                if _account_gate(snap):
                    fixed = await try_wall(snap)
                    if fixed is not None:
                        snap = fixed
                        continue
                    await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "the site asks you to sign in or create an account to apply" + wall_note())
                    raise AuthRequiredError(f"{urlsplit(snap.url).netloc} needs you to sign in (or sign up) before applying{wall_note()}. Do that in this tab, then press Resume.", details={"url": snap.url})
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
            if apply_node is None and (_POSTING_CLOSED_RE.search(" ".join([snap.title, snap.text_excerpt[:1500], *(n.name for n in snap.nodes if n.role == "heading")])) or _CLOSED_URL_RE.search(snap.url)):
                # Removed/filled posting: nothing to apply to; never retried.
                await self._transition(app["id"], ApplicationState.CANCELLED, "posting closed or removed by the employer", evidence={"url": snap.url})
                return {"state": ApplicationState.CANCELLED.value, "note": "posting closed or removed"}
            if apply_node is None and _ACCOUNT_BOARD_RE.search(snap.url):
                fixed = await try_wall(snap)
                if fixed is not None:
                    snap = fixed
                    continue
                await self._needs_board_account(app, snap, note=wall_note())
            if apply_node is None:
                await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "no application form or apply button found", failure={"category": "SITE_UNSUPPORTED"})
                raise SiteUnsupportedError("no application form or apply button found on " + snap.url)
            # A real address on the control is followed as a navigation, whether the control is a
            # link or an anchor styled as a button (Workday's "Apply" / "Apply Manually"): a click
            # that unloads the page mid-action, or opens a pop-up Chrome blocks, is avoided that way.
            href = apply_node.href if apply_node.href and (apply_node.role == "link" or apply_node.tag == "a") else None
            if href and not href.startswith(("http://", "https://", "javascript:", "#", "mailto:", "tel:")):
                href = urljoin(snap.url, href)
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
                fixed = await try_wall(snap)
                if fixed is not None:
                    snap = fixed
                    continue
                await self._transition(app["id"], ApplicationState.AUTH_REQUIRED, "the site asks you to sign in or create an account to apply" + wall_note())
                raise AuthRequiredError(f"{urlsplit(snap.url).netloc} needs you to sign in (or sign up) before applying{wall_note()}. Do that in this tab, then press Resume.", details={"url": snap.url})
            if unchanged:
                clicks_without_effect += 1
                if clicks_without_effect >= 2 and _ACCOUNT_BOARD_RE.search(snap.url):
                    fixed = await try_wall(snap)
                    if fixed is not None:
                        snap = fixed
                        clicks_without_effect = 0
                        continue
                    await self._needs_board_account(app, snap, note=wall_note())
                if clicks_without_effect >= 2:
                    await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, f"clicking '{apply_node.name[:60]}' does nothing on this page", failure={"category": "SITE_UNSUPPORTED"})
                    raise SiteUnsupportedError(f"Clicking '{apply_node.name[:60]}' does nothing here (the site may need you signed in, or blocks automated clicks).")
        else:
            await self._transition(app["id"], ApplicationState.APPLICATION_FAILED, "could not reach an application form", failure={"category": "SITE_UNSUPPORTED"})
            raise SiteUnsupportedError("could not reach an application form")

        # 3. Fill (possibly multi-step), pausing on blocking questions.
        all_reports: list[dict[str, Any]] = []
        plan = None
        attached: set[str] = set()  # upload boxes already given a file (a redraw must not repeat them)
        names = hints["submit_button_names"]
        continued = False  # on a later page reached through Next after a Submit click that went nowhere
        submitting = False  # SUBMISSION_PENDING recorded (approval given, or not required)
        verdict = None
        after = snap
        for _round in range(3):
            # One round fills the page in front of it (and the pages behind its Next buttons) up to
            # a Submit, then submits. When Submit leaves the form exactly as it was and a Next is
            # there instead, the next round carries on with the following page, as a person would.
            refilled = False
            for step in range(self.MAX_STEPS):
                await self._probe_dropdowns(ctx, snap, fact_keys=list(profile.facts().keys()), answers=answers)
                if not job.get("location"):
                    # The job record has no location (some boards hide it from structured data): use
                    # the place named at the top of the page, e.g. "State College, Pennsylvania, United States".
                    job = {**job, "location": location_from_page(snap)}
                plan = await self._filler.plan(snap, profile, answers, resumes, submit_names=names, task_id=ctx.task_id, job_location=job.get("location"), job=job)
                if resumed:
                    keep_page_answers(plan)
                for f in plan.fields:
                    if f.field_type == "file" and f.status == "auto" and f.label in attached:
                        f.status, f.reason = "skip", "attached in an earlier pass"
                us_only = next((n.section or n.name for n in snap.nodes if US_ONLY_RE.search(f"{n.name or ''} {n.section or ''}")), None)
                authorized = [c.strip().lower() for c in profile.legal.work_authorized_countries if c.strip()]
                if us_only and authorized and not any(c in ("united states", "usa", "us") for c in authorized):
                    # The form itself says only US-based people can be hired: applying cannot succeed.
                    self._filler.discard_pending(plan)
                    await self._transition_via(app["id"], ApplicationState.CANCELLED, f"ineligible: hires only in the US (\"{us_only[:120]}\")")
                    return {"state": "SKIPPED", "note": f"hires only in the US: {us_only[:120]}"}
                if step == 0 and not resumed and not continued:
                    # (Not on a resume: a later wizard step — "My Experience", questions — has no name
                    # or email field, and the run already proved the page is an application.)
                    verdict_text = looks_like_application(snap, plan)
                    if verdict_text:
                        # Not a job application (mail, chat, dashboards, search filters…): touch nothing.
                        self._filler.discard_pending(plan)
                        await self._transition_via(app["id"], ApplicationState.APPLICATION_FAILED, f"not an application form: {verdict_text}", failure={"category": "SITE_UNSUPPORTED", "message": verdict_text})
                        raise SiteUnsupportedError(f"This page is not a job application form ({verdict_text}). Nothing was filled.")
                if self._submit_policy == "never":
                    _unattended_review(plan.fields)
                report = await self._filler.fill(None, ctx.tab_id, plan, resolve_payload=ctx.resolve_payload, execute=ctx.execute)  # type: ignore[arg-type]
                # The rules filled what they could straight away; the local model's picks for the
                # rest (it is slow on this machine) are applied now that they have had time to arrive.
                extra = await self._filler.finish_plan(plan, snap, profile, answers, resumes, submit_names=names, job_location=job.get("location"))
                if extra is not None and extra.auto:
                    if self._submit_policy == "never":
                        _unattended_review(extra.fields)
                    more = await self._filler.fill(None, ctx.tab_id, extra, resolve_payload=ctx.resolve_payload, execute=ctx.execute)  # type: ignore[arg-type]
                    report.filled.extend(more.filled)
                    report.failed.extend(more.failed)
                    report.skipped.extend(more.skipped)
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
                    await self._transition_via(app["id"], ApplicationState.NEEDS_USER_INPUT, f"{len(questions)} field(s) need your input", pending_questions=questions)
                    raise NeedsUserInputError("Some required fields need your input; see the application in the dashboard.", details={"count": len(questions)})
                if plan.review:
                    await self._transition_via(app["id"], ApplicationState.READY_FOR_REVIEW, f"{len(plan.review)} field(s) need review", pending_questions=[f.to_dict() for f in plan.review])
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
                plan.submit_node, plan.next_node = find_submit_and_next(snap.nodes, names)
                if after_fields - before_fields and step < self.MAX_STEPS - 1:
                    await self._audit.log("form.dynamic_fields", application_id=app["id"], new_fields=len(after_fields - before_fields))
                    continue  # dynamic form: new fields appeared after filling; plan them too
                if plan.submit_node is None and plan.next_node is None:
                    # Workday redraws the step after the last field is filled and its "Save and
                    # Continue" is briefly absent: look again once the page has settled.
                    snap = await self._settled_snapshot(ctx, snap, max_wait=8.0)
                    plan.submit_node, plan.next_node = find_submit_and_next(snap.nodes, names)
                if plan.submit_node is not None:
                    break
                if plan.next_node is not None:
                    await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(plan.next_node)), args={}, risk=RiskLevel.MEDIUM, verification="none", timeout_ms=15_000, expected_outcome="next step"), None)
                    snap = await self._next_step_snapshot(ctx, before_fields, snap.url)
                    cls = classify_page(snap)
                    if cls.kind == "login":
                        fixed = await try_wall(snap)
                        if fixed is not None:
                            snap = fixed
                            continue
                    if cls.kind in ("login", "challenge"):
                        await self._transition_via(app["id"], ApplicationState.AUTH_REQUIRED, "authentication/challenge during form steps" + wall_note())
                        raise AuthRequiredError("Authentication or a challenge appeared mid-form; handle it in the browser and resume.")
                    problems = _real_errors(snap.signals.error_messages)
                    if problems:
                        await self._transition_via(app["id"], ApplicationState.NEEDS_USER_INPUT, "site reported validation errors", pending_questions=[{"label": "validation", "reason": m} for m in problems[:10]])
                        raise NeedsUserInputError("The site reported validation errors: " + "; ".join(problems[:3]))
                    continue
                await self._transition_via(app["id"], ApplicationState.APPLICATION_FAILED, "no submit or next button found", failure={"category": "SITE_UNSUPPORTED"})
                raise SiteUnsupportedError("no submit or next button found")
            assert plan is not None
            if snap.signals.has_captcha and not captcha_done:
                # The form is filled; the CAPTCHA on it is the user's to complete. Resume submits.
                await self._repos.applications.update_fields(app["id"], {"fill_plan": {"steps": all_reports, "last_plan": plan.to_dict(), "captcha_handoff": True}})
                await self._transition_via(app["id"], ApplicationState.AUTH_REQUIRED, "form filled; complete the verification on the page, then resume to submit")
                raise HumanVerificationError("The form is filled. Tick the verification (CAPTCHA) box on the page yourself; the agent submits as soon as it is done.", details={"url": snap.url, "captcha_on_form": True})

            # 4. Review gate and submission approval.
            if plan.submit_node is None:
                await self._transition_via(app["id"], ApplicationState.APPLICATION_FAILED, "submit button not found", failure={"category": "SITE_UNSUPPORTED"})
                raise SiteUnsupportedError("submit button not found")
            if not submitting:
                requires_approval = self._submit_policy == "always" or (self._submit_policy == "per_site" and hints.get("requires_submit_confirmation", True))
                app = await self._transition(app["id"], ApplicationState.READY_FOR_REVIEW, "form filled; awaiting submission approval" if requires_approval else "form filled")
                if requires_approval:
                    summary = {"summary": f"Submit application for {job.get('title')} at {job.get('company')}?", "url": snap.url, "filled": sum(len(r["filled"]) for r in all_reports), "application_id": app["id"]}
                    approved = await ctx.request_approval("submit", summary)
                    if not approved:
                        await self._transition(app["id"], ApplicationState.READY_FOR_REVIEW, "submission not approved")
                        raise NeedsUserInputError("Submission was not approved.")
                app = await self._transition(app["id"], ApplicationState.SUBMISSION_PENDING, "submitting")
                submitting = True

            # 5. Submit and verify with authoritative evidence. Fresh snapshot first so the submit index
            # belongs to the live page (uploads and re-renders shift node numbers).
            snap = await ctx.snapshot()
            submit_node, _ = find_submit_and_next(snap.nodes, names)
            if submit_node is None:
                await self._transition_via(app["id"], ApplicationState.APPLICATION_FAILED, "submit button disappeared before submission", failure={"category": "SITE_UNSUPPORTED"})
                raise SiteUnsupportedError("submit button not found on the live page")
            tried: list[tuple[Any, ...]] = []
            go_on = False
            for _attempt in range(3):
                before = snap
                clicked = snap.node(submit_node)
                tried.append(_button_key(clicked))
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
                if verdict.status in (VerificationStatus.CONFIRMED, VerificationStatus.REJECTED):
                    break
                if not (after.form_fields() and after.url == before.url):
                    break
                # The form is still there and nothing was said: the click went nowhere. A person
                # would look for why: a required box still empty, another button that is the real
                # Submit (a page's top "Apply now" often only opens the form), or a Next to go on with.
                missing = [m for m in after.signals.missing_required if m][:15]
                if missing:
                    questions = [{"label": m, "reason": "the site requires this field and it is still empty"} for m in missing]
                    await self._audit.log("submit.blocked_by_required", application_id=app["id"], fields=missing[:10])
                    await self._transition_via(app["id"], ApplicationState.NEEDS_USER_INPUT, f"{len(missing)} required field(s) are still empty", pending_questions=questions)
                    raise NeedsUserInputError("The site will not take the form until these required fields are filled: " + ", ".join(missing[:5]), details={"count": len(missing)})
                rest = [n for n in after.nodes if n.role != "button" or _button_key(n) not in tried]
                alt_submit, alt_next = find_submit_and_next(rest, names)
                if alt_submit is not None:
                    log.info("submit click on %r changed nothing; trying %r instead", (clicked.name if clicked else "")[:40], (after.node(alt_submit).name if after.node(alt_submit) else "")[:40])
                    await self._audit.log("submit.retry_other_button", application_id=app["id"], clicked=(clicked.name if clicked else "")[:60])
                    snap, submit_node = after, alt_submit
                    continue
                if alt_next is not None:
                    before_fields = {(n.name_attr, n.id, n.name) for n in after.form_fields()}
                    await self._audit.log("submit.continue_next_page", application_id=app["id"], clicked=(clicked.name if clicked else "")[:60])
                    await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(alt_next)), args={}, risk=RiskLevel.MEDIUM, verification="none", timeout_ms=15_000, expected_outcome="next step"), None)
                    snap = await self._next_step_snapshot(ctx, before_fields, after.url)
                    continued = True
                    go_on = True
                break
            if go_on and verdict.status not in (VerificationStatus.CONFIRMED, VerificationStatus.REJECTED):
                continue  # next round: fill the following page, then submit from there
            break
        assert verdict is not None
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

    async def _recent_signin_failures(self, host: str) -> int:
        """Failed automatic sign-in / sign-up attempts on this host in the last two days."""
        try:
            row = await self._repos.db.fetch_one(
                "SELECT COUNT(*) AS n FROM audit_events WHERE event = 'signin.attempt' AND ts >= datetime('now', '-2 days') AND data_json LIKE ? AND data_json LIKE '%\"status\":\"failed\"%'",
                (f'%"host":"{host}"%',),
            )
            return int(row["n"]) if row else 0
        except Exception:  # noqa: BLE001 - a missing table (tests) means no history
            return 0

    async def _transition_via(self, app_id: str, state: ApplicationState, reason: str, **fields: Any) -> dict[str, Any]:
        """``_transition`` that also works after a submit click went nowhere: from SUBMISSION_PENDING
        the state machine only allows a step back through READY_FOR_REVIEW (and IN_PROGRESS), so
        those are taken first when the target needs them."""
        try:
            return await self._transition(app_id, state, reason, **fields)
        except ValueError:
            await self._transition(app_id, ApplicationState.READY_FOR_REVIEW, "the submit click did not go through")
            try:
                return await self._transition(app_id, state, reason, **fields)
            except ValueError:
                await self._transition(app_id, ApplicationState.IN_PROGRESS, "continuing on the next page of the form")
                return await self._transition(app_id, state, reason, **fields)
