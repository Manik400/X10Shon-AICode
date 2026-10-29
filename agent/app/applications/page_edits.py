"""Changing fields of the form in front of the user on their instruction ("put my GitHub in
link 2", "clear the salary box", "fill one portfolio, one GitHub and one LeetCode").

Rules handle the common corrections at once; anything else goes to the local model in one
bounded call that returns field edits, which the normal filler then types or selects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..browser.snapshot import SnapshotNode
from ..core.enums import Provenance

_EDIT_VERB_RE = re.compile(r"\b(fill?|fil in|put|set|change|enter|type|use|replace|correct|fix|clear|remove|delete|select|choose|pick|tick|untick|check|uncheck|upload|attach|add|update|write|make|swap|switch|paste|redo|do it again)\b", re.I)
_QUESTION_ONLY_RE = re.compile(r"^\s*(what|why|how|when|where|who|which|is|are|does|do|can|could|should|would)\b[^.!]*\?\s*$", re.I)

# Link kinds a person names, with the profile keys that hold them (in order of preference).
_LINK_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("portfolio", ("links.portfolio", "links.website")),
    ("github", ("links.github",)),
    ("linkedin", ("links.linkedin",)),
    ("leetcode", ("links.other",)),
    ("website", ("links.website", "links.portfolio")),
    ("personal site", ("links.website", "links.portfolio")),
]
_LINK_FIELD_RE = re.compile(r"\b(link|url|website|web ?site|site|profile)\b", re.I)
_SPECIFIC_LINK_RE = re.compile(r"linkedin|github|portfolio|leetcode", re.I)


def looks_like_edit(instruction: str) -> bool:
    """An instruction that asks for something on the page to be changed (not a question)."""
    text = (instruction or "").strip()
    if not text or _QUESTION_ONLY_RE.match(text):
        return False
    return bool(_EDIT_VERB_RE.search(text))


@dataclass(slots=True)
class Edit:
    node: int
    label: str
    field_type: str
    value: str
    reason: str = ""


def _mention_at(text: str, word: str) -> int | None:
    """Where ``word`` is mentioned in the instruction, typos allowed ("pottfolio", "linkdin")."""
    pos = text.find(word)
    if pos >= 0:
        return pos
    from difflib import SequenceMatcher

    for m in re.finditer(r"[a-z]{4,}", text):
        tok = m.group(0)
        if abs(len(tok) - len(word.replace(" ", ""))) <= 2 and SequenceMatcher(None, tok, word.replace(" ", "")).ratio() >= 0.8:
            return m.start()
    return None


def _field_type(n: SnapshotNode) -> str:
    if n.role in ("combobox", "listbox"):
        return n.type or "select"
    if n.role in ("checkbox", "radio", "file"):
        return n.role
    return n.type or "text"


def _link_value(word: str, keys: tuple[str, ...], facts: dict[str, tuple[str, Provenance]], used: set[str]) -> str | None:
    # A named site wins wherever it is stored ("leetcode" may sit under links.other or links.website).
    for k, (v, _) in facts.items():
        if k.startswith("links.") and word.replace(" ", "") in v.lower().replace(" ", "") and v not in used:
            return v
    for k in keys:
        v = facts.get(k, ("", None))[0]
        if v and v not in used:
            return v
    return None


def rule_edits(instruction: str, fields: list[SnapshotNode], facts: dict[str, tuple[str, Provenance]]) -> list[Edit]:
    """Edits the rules are sure about. Today: handing out named links ("one portfolio, one
    GitHub, one LeetCode") across the form's link boxes, in the order they are named."""
    text = (instruction or "").lower()
    wanted = sorted(((pos, w, keys) for w, keys in _LINK_WORDS for pos in [_mention_at(text, w)] if pos is not None), key=lambda x: x[0])
    if not wanted:
        return []
    edits: list[Edit] = []
    used: set[str] = set()
    taken: set[int] = set()
    # Boxes whose label names the site ("GitHub profile") take that link.
    for _, w, keys in wanted:
        for n in fields:
            if n.i in taken or n.role not in ("textbox",) or not re.search(re.escape(w), n.name or "", re.I):
                continue
            v = _link_value(w, keys, facts, used)
            if v:
                used.add(v)
                taken.add(n.i)
                edits.append(Edit(n.i, n.name or "", _field_type(n), v, f"{w} link, as asked"))
            break
    # Generic boxes ("Link 1", "URL", "Website") take the remaining named links in order.
    generic = [n for n in fields if n.i not in taken and n.role == "textbox" and _LINK_FIELD_RE.search(n.name or "") and not _SPECIFIC_LINK_RE.search(n.name or "")]
    remaining = [(w, keys) for _, w, keys in wanted if not any(e.reason.startswith(w) for e in edits)]
    for (w, keys), n in zip(remaining, generic):
        v = _link_value(w, keys, facts, used)
        if not v:
            continue
        used.add(v)
        edits.append(Edit(n.i, n.name or "", _field_type(n), v, f"{w} link, as asked"))
    return edits


def build_edit_prompt(instruction: str, fields: list[SnapshotNode], facts: dict[str, tuple[str, Provenance]]) -> tuple[str, str]:
    listing = "\n".join(
        f"- node {n.i}: \"{(n.name or n.placeholder or n.name_attr or '')[:80]}\" ({_field_type(n)}{', required' if n.required else ''})"
        + (f" current=\"{(n.value or '')[:60]}\"" if n.value else "")
        + (f" options={[o[:30] for o in (n.options or [])[:12]]}" if n.options else "")
        for n in fields[:80]
    )
    known = "\n".join(f"- {k}: {v[:100]}" for k, (v, _) in sorted(facts.items()) if k.startswith(("contact.", "links.", "preferences.", "employment.", "education[")) and "address" not in k)
    system = (
        "You change fields of a web form on the user's instruction. You are given the form's fields (node numbers, labels, "
        "current values, options) and the user's known details. Reply with the edits to make, only for fields the "
        "instruction is about: {\"edits\": [{\"node\": <number>, \"value\": \"<text, or the option to pick, or 'true'/'false' for a box>\", \"reason\": \"<short>\"}], \"note\": \"<short>\"}. "
        "Never invent personal details: use the known details, or leave a field out and say so in the note."
    )
    prompt = f"Instruction: {instruction.strip()[:800]}\n\nFields:\n{listing}\n\nKnown details:\n{known}"
    return system, prompt


def edits_from_model(out: Any, fields: list[SnapshotNode]) -> list[Edit]:
    by_node = {n.i: n for n in fields}
    edits: list[Edit] = []
    for e in getattr(out, "edits", []) or []:
        n = by_node.get(int(e.node))
        if n is None or not str(e.value).strip():
            continue
        edits.append(Edit(n.i, n.name or "", _field_type(n), str(e.value).strip(), (e.reason or "").strip()[:120]))
    return edits
