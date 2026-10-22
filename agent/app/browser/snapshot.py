"""Compact page snapshot produced by the shared ``dom-agent`` bundle.

The snapshot is the only page representation the agent reasons about. It is bounded in size,
contains no raw HTML, and never includes values of password fields.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SnapshotNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    i: int
    role: str = ""
    name: str = ""
    tag: str = ""
    type: str | None = None
    value: str | None = None
    placeholder: str | None = None
    label: str | None = None
    checked: bool | None = None
    disabled: bool = False
    required: bool = False
    options: list[str] | None = None
    section: str | None = None
    text: str | None = None
    href: str | None = None
    testid: str | None = None
    id: str | None = None
    name_attr: str | None = None
    visible: bool = True
    form: int | None = None
    frame: int = 0
    in_dialog: bool = False
    autocomplete: str | None = None
    accept: str | None = None
    maxlength: int | None = None
    pattern: str | None = None


class FormInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    fields: list[int] = Field(default_factory=list)
    submit_nodes: list[int] = Field(default_factory=list)
    name: str | None = None
    action: str | None = None

    @field_validator("name", "action", mode="before")
    @classmethod
    def _stringish(cls, v: Any) -> str | None:
        # Older content scripts could send an element (serialised as {}) when a form field is
        # named "id"/"action"; never let page markup crash the snapshot.
        return v if isinstance(v, str) else None


class PageSignals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    has_password_field: bool = False
    has_email_or_username_field: bool = False
    has_captcha: bool = False
    captcha_solved: bool = False  # the user completed the on-page check (its response field is filled)
    has_otp_field: bool = False
    login_cta_present: bool = False
    logout_cta_present: bool = False
    signup_cta_present: bool = False
    password_recovery: bool = False
    account_locked: bool = False
    error_messages: list[str] = Field(default_factory=list)
    success_messages: list[str] = Field(default_factory=list)
    reference_numbers: list[str] = Field(default_factory=list)
    unsupported_reasons: list[str] = Field(default_factory=list)
    iframe_srcs: list[str] = Field(default_factory=list)
    has_jsonld_jobposting: bool = False


class PageSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    title: str = ""
    nodes: list[SnapshotNode] = Field(default_factory=list)
    forms: list[FormInfo] = Field(default_factory=list)
    text_excerpt: str = ""
    signals: PageSignals = Field(default_factory=PageSignals)
    truncated: bool = False
    generation: int = 0
    jsonld: list[dict[str, Any]] = Field(default_factory=list)
    frame_count: int = 1
    content_hash: str = ""

    # ------------------------------------------------------------------ helpers
    def node(self, i: int) -> SnapshotNode | None:
        for n in self.nodes:
            if n.i == i:
                return n
        return None

    def interactive(self) -> list[SnapshotNode]:
        return [n for n in self.nodes if n.role in INTERACTIVE_ROLES and not n.disabled]

    def form_fields(self) -> list[SnapshotNode]:
        return [n for n in self.nodes if n.role in FIELD_ROLES and not n.disabled]

    def to_compact_text(self, max_chars: int = 8000) -> str:
        """Render for a model prompt: one line per interactive/semantic node, bounded."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.signals.error_messages:
            lines.append("ERRORS: " + " | ".join(self.signals.error_messages[:5]))
        if self.signals.success_messages:
            lines.append("SUCCESS: " + " | ".join(self.signals.success_messages[:5]))
        for n in self.nodes:
            if n.role not in INTERACTIVE_ROLES and n.role not in {"heading", "alert", "status"}:
                continue
            parts = [f"[{n.i}] {n.role}"]
            if n.name:
                parts.append(f'"{n.name[:80]}"')
            if n.type and n.type not in ("text",):
                parts.append(f"type={n.type}")
            if n.required:
                parts.append("required")
            if n.disabled:
                parts.append("disabled")
            if n.checked is not None:
                parts.append(f"checked={str(n.checked).lower()}")
            if n.value:
                parts.append(f'value="{n.value[:40]}"')
            if n.options:
                parts.append("options=" + "/".join(o[:30] for o in n.options[:12]) + ("…" if len(n.options) > 12 else ""))
            if n.section:
                parts.append(f"section={n.section[:40]}")
            lines.append(" ".join(parts))
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 12] + "\n…(truncated)"
        return text


INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "spinbutton",
        "menuitem",
        "tab",
        "option",
        "file",
        "date",
    }
)

FIELD_ROLES = frozenset({"textbox", "searchbox", "combobox", "listbox", "checkbox", "radio", "switch", "spinbutton", "file", "date", "slider"})
