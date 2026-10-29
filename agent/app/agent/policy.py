"""PolicyEngine: decides whether a proposed action may run, and under what conditions.

Checks, in order: cancellation/pause → action schema sanity → domain allow-list → risk
classification → approval requirement → per-domain rate limits. Page content never influences
policy (instructions on a page cannot grant permissions).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from ..core.actions import READ_ONLY_ACTIONS, BrowserAction
from ..core.enums import RiskLevel

Decision = Literal["allow", "deny", "needs_approval"]

# "sign" means e-signing here; "Sign in" / "Sign up" / "Sign in with email" are ordinary navigation.
_HIGH_RISK_BUTTON = re.compile(r"\b(submit|confirm|pay|purchase|buy|delete|remove|send|withdraw|cancel application|unsubscribe|accept offer|sign(?![ -]?(in|up|on)\b)|agree)\b", re.I)
_PASSWORD_BOX = re.compile(r"password|passcode", re.I)
_NEVER_TYPED = re.compile(r"otp|one.?time|verification code|cvv|card number|ssn|social security|routing|account number", re.I)
_SENSITIVE_FIELD = re.compile(r"password|passcode|otp|one.?time|verification code|cvv|card number|ssn|social security|routing|account number", re.I)


@dataclass(slots=True)
class PolicyVerdict:
    decision: Decision
    risk: RiskLevel
    reason: str
    approval_kind: str | None = None
    wait_seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DomainBudget:
    last_action_at: float = 0.0
    active: int = 0


class PolicyEngine:
    @staticmethod
    def _norm(domain: str) -> str:
        d = domain.strip().lower()
        if d == "*":
            return "*"  # wildcard: every http(s) site
        return d.removeprefix("*.").removeprefix(".")

    def __init__(self, *, allowed_domains: list[str], submit_policy: str = "always", per_domain_delay: float = 5.0, per_domain_concurrency: int = 1):
        self._allowed = {self._norm(d) for d in allowed_domains if d.strip()}
        self._submit_policy = submit_policy
        self._delay = per_domain_delay
        self._concurrency = per_domain_concurrency
        self._budgets: dict[str, DomainBudget] = {}
        self.paused = False
        # A stored site password (profile → site accounts) may be typed into a password box
        # during automatic sign-in; one-time codes, card numbers and the like still never are.
        self.allow_stored_credentials = True

    # ------------------------------------------------------------------ config
    def set_allowed_domains(self, domains: list[str]) -> None:
        self._allowed = {self._norm(d) for d in domains if d.strip()}

    @property
    def allowed_domains(self) -> list[str]:
        return sorted(self._allowed)

    def allow_domain(self, domain: str) -> None:
        if domain.strip():
            self._allowed.add(self._norm(domain))

    def domain_allowed(self, url: str) -> bool:
        host = urlsplit(url).netloc.lower().split(":")[0]
        if host in ("127.0.0.1", "localhost") or url.startswith("file://"):
            return True
        if not host:
            return False
        if "*" in self._allowed:  # wildcard: every http(s) site is allowed
            return True
        return any(host == d or host.endswith("." + d) for d in self._allowed)

    # ------------------------------------------------------------------ risk
    @staticmethod
    def classify_risk(action: BrowserAction, *, target_name: str | None = None, field_label: str | None = None) -> RiskLevel:
        if action.type in READ_ONLY_ACTIONS:
            return RiskLevel.LOW
        if action.type == "navigate":
            return RiskLevel.LOW
        if action.type == "type":
            label = field_label or (action.target.name if action.target else "") or (action.target.value if action.target else "")
            if label and _SENSITIVE_FIELD.search(label):
                return RiskLevel.HIGH
            return RiskLevel.LOW
        if action.type in ("select", "check", "upload"):
            return RiskLevel.MEDIUM
        if action.type in ("click", "press_key"):
            name = target_name or (action.target.name if action.target else "") or (action.target.value if action.target and action.target.strategy in ("text", "role", "label") else "")
            if name and _HIGH_RISK_BUTTON.search(name):
                return RiskLevel.HIGH
            if action.type == "press_key" and str(action.args.get("key", "")).lower() == "enter":
                return RiskLevel.MEDIUM
            return RiskLevel.LOW if action.risk == RiskLevel.LOW else action.risk
        return action.risk

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, action: BrowserAction, *, current_url: str, target_name: str | None = None, field_label: str | None = None, explicit_submit: bool = False) -> PolicyVerdict:
        if self.paused:
            return PolicyVerdict("deny", RiskLevel.LOW, "all automation is paused")
        risk = self.classify_risk(action, target_name=target_name, field_label=field_label)
        # Model-declared risk can only raise, never lower, the computed level.
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH]
        if order.index(action.risk) > order.index(risk):
            risk = action.risk
        if action.type == "navigate":
            url = str(action.args.get("url", ""))
            if not self.domain_allowed(url):
                return PolicyVerdict("deny", risk, f"domain not in allow-list: {urlsplit(url).netloc}", details={"url": url})
        elif action.type not in READ_ONLY_ACTIONS and not self.domain_allowed(current_url):
            return PolicyVerdict("deny", risk, f"current page domain not in allow-list: {urlsplit(current_url).netloc}")
        if action.type == "type" and risk == RiskLevel.HIGH:
            label = field_label or (action.target.name if action.target else "") or ""
            if action.args.get("credential") is True and self.allow_stored_credentials and _PASSWORD_BOX.search(label) and not _NEVER_TYPED.search(label):
                return PolicyVerdict("allow", RiskLevel.MEDIUM, "stored site password for automatic sign-in")
            return PolicyVerdict("deny", risk, "typing into credential/secret fields is never automated")
        if risk == RiskLevel.HIGH:
            if explicit_submit and self._submit_policy == "never":
                return PolicyVerdict("allow", risk, "submission approved by policy configuration")
            return PolicyVerdict("needs_approval", risk, "high-risk action requires approval", approval_kind="submit" if explicit_submit else "high_risk_action")
        # The per-site delay paces page loads and clicks; filling fields on an already-loaded form
        # is one human-like burst (8 s between fields made a 20-field form take minutes).
        wait = self._rate_limit_wait(current_url) if action.type in ("navigate", "click", "press_key") else 0.0
        if wait > 0:
            return PolicyVerdict("allow", risk, f"rate limited: wait {wait:.1f}s", wait_seconds=wait)
        return PolicyVerdict("allow", risk, "ok")

    # ------------------------------------------------------------------ rate limits
    def _rate_limit_wait(self, url: str) -> float:
        host = urlsplit(url).netloc.lower()
        if not host or host.startswith(("127.0.0.1", "localhost")):
            return 0.0
        b = self._budgets.setdefault(host, DomainBudget())
        elapsed = time.monotonic() - b.last_action_at
        return max(0.0, self._delay - elapsed) if b.last_action_at else 0.0

    def record_action(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        if host:
            self._budgets.setdefault(host, DomainBudget()).last_action_at = time.monotonic()

    def acquire_domain(self, url: str) -> bool:
        host = urlsplit(url).netloc.lower()
        b = self._budgets.setdefault(host, DomainBudget())
        if b.active >= self._concurrency:
            return False
        b.active += 1
        return True

    def release_domain(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        b = self._budgets.get(host)
        if b and b.active > 0:
            b.active -= 1
