"""Typed browser action schema.

The model proposes instances of :class:`BrowserAction`; nothing is executed unless it validates
here and the policy engine allows it. The same schema is serialized to the extension and to the
Playwright driver, both of which execute it with the shared ``dom-agent`` bundle.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import RiskLevel

ActionType = Literal[
    "navigate",
    "click",
    "type",
    "select",
    "check",
    "upload",
    "press_key",
    "scroll",
    "wait",
    "extract",
    "snapshot",
    "done",
    "ask_user",
    "banner",
    "read_options",  # open a dropdown, list its options, close it (changes nothing)
]

LocatorStrategy = Literal["role", "label", "testid", "id", "name", "text", "css", "node"]

VerificationStrategy = Literal[
    "none",
    "url_change",
    "element_visible",
    "element_hidden",
    "text_present",
    "value_set",
    "navigation_complete",
]

# Actions that never change page state and are always low risk.
READ_ONLY_ACTIONS: frozenset[str] = frozenset({"wait", "extract", "snapshot", "done", "ask_user", "scroll", "banner", "read_options"})


class Target(BaseModel):
    """How to find the element. ``value`` is interpreted according to ``strategy``."""

    model_config = ConfigDict(extra="forbid")

    strategy: LocatorStrategy
    value: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=300, description="Accessible name when strategy=role")
    section: str | None = Field(default=None, max_length=300, description="Heading/fieldset text that scopes the search")
    nth: int = Field(default=0, ge=0, le=50, description="Index among equally-ranked matches")
    exact: bool = False


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    target: Target | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=15_000, ge=100, le=120_000)
    expected_outcome: str | None = Field(default=None, max_length=500)
    risk: RiskLevel = RiskLevel.LOW
    verification: VerificationStrategy = "none"
    verification_arg: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=500, description="Short model rationale, for the audit log only")

    @model_validator(mode="after")
    def _check_shape(self) -> "BrowserAction":
        needs_target = {"click", "type", "select", "check", "upload", "read_options"}
        if self.type in needs_target and self.target is None:
            raise ValueError(f"action '{self.type}' requires a target")
        if self.type == "navigate":
            url = self.args.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://", "file://")):
                raise ValueError("navigate requires args.url with http(s) or file scheme")
        if self.type == "type" and not isinstance(self.args.get("text"), str):
            raise ValueError("type requires args.text")
        if self.type == "select" and not isinstance(self.args.get("value"), str):
            raise ValueError("select requires args.value")
        if self.type == "check" and not isinstance(self.args.get("checked", True), bool):
            raise ValueError("check requires boolean args.checked")
        if self.type == "upload" and not isinstance(self.args.get("resume_id"), (str, int)):
            raise ValueError("upload requires args.resume_id (files are resolved by the agent, never by the model)")
        if self.type == "press_key" and not isinstance(self.args.get("key"), str):
            raise ValueError("press_key requires args.key")
        if self.type == "ask_user" and not isinstance(self.args.get("question"), str):
            raise ValueError("ask_user requires args.question")
        if self.type == "wait":
            ms = self.args.get("ms", 500)
            if not isinstance(ms, int) or ms < 0 or ms > 30_000:
                raise ValueError("wait.ms must be an int in [0, 30000]")
        # Disallow any attempt at code execution via args.
        for key in ("script", "javascript", "eval", "code", "sql", "shell"):
            if key in self.args:
                raise ValueError(f"forbidden argument '{key}'")
        return self

    def fingerprint(self) -> str:
        """Stable identity of an action used for duplicate/loop detection."""
        import hashlib
        import json

        payload = self.model_dump(exclude={"reason", "expected_outcome", "timeout_ms"}, mode="json")
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class ActionResult(BaseModel):
    """What the driver reports back after executing one action."""

    model_config = ConfigDict(extra="ignore")

    ok: bool
    action_type: str
    error_category: str | None = None
    message: str = ""
    url: str | None = None
    title: str | None = None
    matched_count: int | None = None
    value: Any = None
    duration_ms: int = 0
    evidence: dict[str, Any] = Field(default_factory=dict)


class PlannerOutput(BaseModel):
    """Structured output the planner model must return."""

    model_config = ConfigDict(extra="forbid")

    page_state: Literal["form", "login", "challenge", "confirmation", "error", "listing", "job_description", "other"]
    actions: list[BrowserAction] = Field(min_length=1, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str | None = Field(default=None, max_length=500)
