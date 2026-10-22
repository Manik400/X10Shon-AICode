"""Deterministic page-state classification from snapshot signals.

This runs before any model call. It is the authority for authentication and challenge detection:
a model may refine *what to do*, never *whether a login or CAPTCHA is present*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from ..core.enums import AuthState
from .snapshot import PageSnapshot

PageKind = Literal["form", "login", "challenge", "confirmation", "error", "listing", "job_description", "other"]

_LOGIN_URL = re.compile(r"/(login|signin|sign-in|log-in|sso|session/new|users/sign_in|account/login)/?($|\?)", re.I)
# Account-creation pages a job board sends applicants to (WeWorkRemotely, Himalayas, foundit…).
_SIGNUP_URL = re.compile(r"/(signup|sign-up|sign_up|register|registration|join|create-account|account/(new|register)|onboarding)(/|\?|$)|/signup/", re.I)
_RECOVERY_RE = re.compile(r"(forgot (your )?password|reset (your )?password|password reset|recover (your )?account)", re.I)
_LOCKED_RE = re.compile(r"(account (is |has been )?(locked|suspended|disabled)|too many (failed )?attempts|unusual activity)", re.I)
_SIGNUP_RE = re.compile(r"(create (an |your )?account|sign up|register|new user)", re.I)
_CONFIRM_RE = re.compile(
    r"(thank(s| you) for (applying|your application|submitting)|application (has been )?(received|submitted|sent|complete)|we('ve| have) (received|got) your application|successfully (submitted|applied)|your application (was|has been) (submitted|sent|received)|application submitted)",
    re.I,
)
_BOT_CHECK_RE = re.compile(
    r"(just a moment\.\.\.|confirm you are (a )?human|verify(ing)? you are (a )?human|are you a (human|robot)|performing security verification|checking your browser|attention required|security check (before|to) continu|press (and|&) hold)",
    re.I,
)
_REFERENCE_RE = re.compile(r"(?:reference|confirmation|application)\s*(?:number|id|#|no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})", re.I)
_JOB_DESC_HINT = re.compile(r"(responsibilities|qualifications|requirements|what you('|’)ll do|about the role|job description)", re.I)


@dataclass(slots=True)
class PageClassification:
    kind: PageKind
    auth_state: AuthState
    reasons: list[str] = field(default_factory=list)
    reference_numbers: list[str] = field(default_factory=list)
    unsupported_reasons: list[str] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        # Only an actual login/recovery/lock page or a challenge stops the agent. A "Sign in" link
        # in a site header (logged-out browsing) is not a wall: job pages are public.
        return self.kind in ("login", "challenge")


_APP_FIELD_KINDS = (re.compile(r"\b(first|last|full)\s*name\b|^name\b", re.I), re.compile(r"e-?mail", re.I), re.compile(r"phone|mobile", re.I), re.compile(r"resume|\bcv\b|curriculum", re.I))


def _application_form_present(snap: PageSnapshot) -> bool:
    """At least two distinct application fields (name, email, phone, resume) are on the page."""
    kinds = 0
    for rx in _APP_FIELD_KINDS:
        if any(n.role in ("textbox", "file") and rx.search(n.name or "") for n in snap.nodes):
            kinds += 1
    return kinds >= 2


# A verdict the site shows about THIS candidate before they apply ("You are not eligible for this
# job", "Your profile does not match the requirements", "not accepting applications from your
# location"). Applying cannot succeed, so the job is closed out instead of filled or retried.
_INELIGIBLE_RE = re.compile(
    r"you(?:'re| are) not eligible(?: for this (?:job|role|position|opening|vacancy)| to apply)?(?![^.!?]{0,24}\b(?:visa|sponsorship|relocation|benefits?|bonus|reimbursement))"
    r"|your (?:profile|experience|qualifications?|background|skills) (?:is not eligible|(?:does ?n[o']t|do ?n[o']t|did not|doesn't) (?:match|meet|qualify|fit)|is not a (?:match|fit)|isn'?t a (?:match|fit))"
    r"|you (?:do not|don'?t) (?:meet|match|fulfil|fulfill|satisfy) the (?:minimum |eligibility |basic |mandatory |required |job )?(?:requirements|criteria|qualifications|eligibility)"
    r"|not (?:a )?(?:match|fit) for your profile"
    r"|you (?:cannot|can'?t|are not able to|are unable to|are not allowed to|may not) apply (?:for|to) this (?:job|role|position|opening)"
    r"|this (?:job|role|position|opening) is not (?:available|open) (?:in|to|for) (?:your|candidates (?:in|from) your) (?:country|region|location)"
    r"|(?:we(?:'re| are) (?:not|unable to) accept(?:ing)?|not accepting) applications? from your (?:country|region|location)"
    r"|applications? (?:is|are) not (?:open|accepted) (?:in|from) your (?:country|region|location)"
    r"|your (?:profile|application) (?:has been|was|is) (?:marked|flagged|rejected) as (?:not eligible|ineligible)"
    r"|(?:profile|you) (?:does not|doesn'?t|do not|don'?t) (?:match|meet) (?:the )?(?:job|role) (?:criteria|requirements)",
    re.I,
)
# "If you do not meet the requirements, please do not apply": a hypothetical in the description, not a verdict.
_HYPOTHETICAL_RE = re.compile(r"\b(?:if|unless|should|when|whether|in case|candidates who|those who|applicants who|anyone who)\b[^.?!]{0,60}$", re.I)


def ineligible_notice(snap: PageSnapshot) -> str | None:
    """The site's own message that this candidate cannot apply for this job, if the page shows one
    (visible status/alert/heading text and the page excerpt). Hypotheticals in a job description
    ("if you do not meet the requirements…") do not count."""
    notices = " ".join(n.text or n.name or "" for n in snap.nodes if n.role in ("status", "alert", "heading") and n.visible)
    text = f"{snap.text_excerpt or ''} {notices}"
    for m in _INELIGIBLE_RE.finditer(text):
        if _HYPOTHETICAL_RE.search(text[max(0, m.start() - 80) : m.start()]):
            continue
        return " ".join(m.group(0).split())
    return None


def classify_page(snap: PageSnapshot) -> PageClassification:
    s = snap.signals
    # Status / alert messages ("Success — Thanks for your application") can sit outside the main
    # text excerpt, so they are read too.
    # Headings too: a "Thank you for your application!" heading inside a shadow root (Manatal
    # career pages) never reaches the text excerpt.
    # Only visible ones: forms often hold a hidden "Thank you" block that appears after submitting.
    notices = " ".join(n.text or n.name or "" for n in snap.nodes if n.role in ("status", "alert", "heading") and n.visible)
    text = f"{snap.text_excerpt or ''} {notices}".strip()
    reasons: list[str] = []
    refs = list(s.reference_numbers)
    for m in _REFERENCE_RE.finditer(text):
        if m.group(1) not in refs:
            refs.append(m.group(1))

    # 1. Challenges beat everything: never automate through them.
    if s.has_captcha and not _application_form_present(snap):
        reasons.append("captcha widget detected")
        return PageClassification("challenge", AuthState.CHALLENGE, reasons, refs, list(s.unsupported_reasons))
    if s.has_captcha:
        # A CAPTCHA sitting on an application form (BambooHR): the form is filled as usual and the
        # verification itself is left to the user right before submitting.
        reasons.append("captcha on the application form (user completes it before submit)")
    if s.has_otp_field:
        reasons.append("one-time code field detected")
        return PageClassification("challenge", AuthState.CHALLENGE, reasons, refs, list(s.unsupported_reasons))
    if _BOT_CHECK_RE.search(f"{snap.title or ''} {text[:600]}") and len(snap.nodes) < 40:
        # Cloudflare / "confirm you are human" interstitials in front of the real page.
        reasons.append("bot-protection check page")
        return PageClassification("challenge", AuthState.CHALLENGE, reasons, refs, list(s.unsupported_reasons))
    if s.account_locked or _LOCKED_RE.search(text):
        reasons.append("account lock / security warning text")
        return PageClassification("login", AuthState.LOCKED, reasons, refs, list(s.unsupported_reasons))
    if s.password_recovery or _RECOVERY_RE.search(snap.title or "") or (_RECOVERY_RE.search(text) and s.has_email_or_username_field and not s.has_password_field):
        reasons.append("password recovery flow")
        return PageClassification("login", AuthState.PASSWORD_RECOVERY, reasons, refs, list(s.unsupported_reasons))

    # 2. Login forms.
    if s.has_password_field:
        if s.signup_cta_present and not s.login_cta_present and _SIGNUP_RE.search(snap.title or ""):
            reasons.append("password field on a sign-up page")
            return PageClassification("login", AuthState.NEW_ACCOUNT_FLOW, reasons, refs, list(s.unsupported_reasons))
        reasons.append("password field present")
        return PageClassification("login", AuthState.LOGGED_OUT, reasons, refs, list(s.unsupported_reasons))
    if _SIGNUP_URL.search(snap.url) and not s.logout_cta_present:
        # A job board's own account sign-up, not the employer's application form.
        reasons.append("account sign-up page")
        return PageClassification("login", AuthState.NEW_ACCOUNT_FLOW, reasons, refs, list(s.unsupported_reasons))
    if _LOGIN_URL.search(snap.url) and s.has_email_or_username_field and not s.logout_cta_present:
        reasons.append("login URL with credential field")
        return PageClassification("login", AuthState.LOGGED_OUT, reasons, refs, list(s.unsupported_reasons))

    # 3. Outcome pages.
    if s.success_messages or _CONFIRM_RE.search(text) or _CONFIRM_RE.search(snap.title or ""):
        reasons.append("confirmation text present")
        return PageClassification("confirmation", _auth_from_ctas(s), reasons, refs, list(s.unsupported_reasons))
    if s.error_messages and not snap.form_fields():
        reasons.append("error text without a form")
        return PageClassification("error", _auth_from_ctas(s), reasons, refs, list(s.unsupported_reasons))

    # 4. Forms and content.
    if snap.form_fields():
        reasons.append(f"{len(snap.form_fields())} form fields")
        return PageClassification("form", _auth_from_ctas(s), reasons, refs, list(s.unsupported_reasons))
    if s.has_jsonld_jobposting or _JOB_DESC_HINT.search(text):
        reasons.append("job description content")
        return PageClassification("job_description", _auth_from_ctas(s), reasons, refs, list(s.unsupported_reasons))
    links = [n for n in snap.nodes if n.role == "link"]
    if len(links) >= 15:
        reasons.append("many links; listing page")
        return PageClassification("listing", _auth_from_ctas(s), reasons, refs, list(s.unsupported_reasons))
    return PageClassification("other", _auth_from_ctas(s), reasons, refs, list(s.unsupported_reasons))


def _auth_from_ctas(s) -> AuthState:  # type: ignore[no-untyped-def]
    if s.logout_cta_present:
        return AuthState.AUTHENTICATED
    if s.login_cta_present:
        return AuthState.LOGGED_OUT
    return AuthState.UNKNOWN
