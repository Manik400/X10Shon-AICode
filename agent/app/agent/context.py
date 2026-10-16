"""ContextBuilder: bounded, redacted prompt context for the planner."""

from __future__ import annotations

from typing import Any

from ..browser.page_state import PageClassification
from ..browser.snapshot import PageSnapshot


class ContextBuilder:
    def __init__(self, max_chars: int = 8000):
        self._max = max_chars

    def build(self, snap: PageSnapshot, cls: PageClassification, *, goal: str, instruction: str = "", facts: dict[str, str], history: list[dict[str, Any]]) -> dict[str, Any]:
        """``facts`` must already be limited to what the task may type (never legal answers unless
        approved, never passwords)."""
        nodes = [
            {"i": n.i, "role": n.role, "name": n.name[:80], "type": n.type, "required": n.required, "value": (n.value or "")[:40], "options": (n.options or [])[:10], "section": n.section}
            for n in snap.nodes
            if n.role in {"button", "link", "textbox", "combobox", "listbox", "checkbox", "radio", "file", "date", "heading"}
        ][:150]
        return {
            "goal": goal,
            "instruction": instruction[:1000],
            "facts": facts,
            "page_kind": cls.kind,
            "auth_state": cls.auth_state.value,
            "page_text": snap.to_compact_text(self._max),
            "nodes": nodes,
            "history": [{"type": h.get("type"), "target": h.get("target"), "ok": h.get("ok"), "message": (h.get("message") or "")[:80]} for h in history[-8:]],
            "url": snap.url,
        }
