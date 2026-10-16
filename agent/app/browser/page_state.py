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
_RECOVERY_RE = re.compile(r"(forgot (your )?password|reset (your )?password|password reset|recover (your )?account)", re.I)
_LOCKED_RE = re.compile(r"(account (is |has been )?(locked|suspended|disabled)|too many (failed )?attempts|unusual activity)", re.I)
_SIGNUP_RE = re.compile(r"(create (an |your )?account|sign up|register|new user)", re.I)
_CONFIRM_RE = re.compile(
    r"(thank you for (applying|your application)|application (has been )?(received|submitted|sent)|we('ve| have) received your application|successfully (submitted|applied)|your application was submitted)",
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


def classify_page(snap: PageSnapshot) -> PageClassification:
    s = snap.signals
    text = snap.text_excerpt or ""
    reasons: list[str] = []
    refs = list(s.reference_numbers)
    for m in _REFERENCE_RE.finditer(text):
        if m.group(1) not in refs:
            refs.append(m.group(1))

    # 1. Challenges beat everything: never automate through them.
    if s.has_captcha:
        reasons.append("captcha widget detected")
        return PageClassification("challenge", AuthState.CHALLENGE, reasons, refs, list(s.unsupported_reasons))
    if s.has_otp_field:
        reasons.append("one-time code field detected")
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
