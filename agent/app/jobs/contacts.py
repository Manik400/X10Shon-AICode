"""Find people to contact on a page: emails, LinkedIn profiles and named recruiters (deterministic)."""

from __future__ import annotations

import re
from typing import Any

from ..browser.snapshot import PageSnapshot

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ROLE = re.compile(r"(recruit\w*|talent (acquisition|partner)|hiring manager|hr (manager|business partner)|people (team|partner)|sourcer)", re.I)
_NAME_NEAR_ROLE = re.compile(r"([A-Z][a-z]+(?: [A-Z][a-z'-]+){1,2})\s*[,\-–|·(]\s*(?:[A-Za-z ]{0,30})?(recruit\w*|talent (?:acquisition|partner)|hiring manager|sourcer)", re.I)
_LEAD_WORDS = {"contact", "email", "ask", "reach", "message", "call", "meet", "questions", "write", "ping", "your", "our", "the", "by", "with"}
_NOREPLY = re.compile(r"(no-?reply|donotreply|privacy|support@|help@|info@|example\.com)", re.I)


def find_contacts(snap: PageSnapshot) -> dict[str, Any]:
    emails: list[str] = []
    profiles: list[dict[str, str]] = []
    people: list[dict[str, str]] = []
    for n in snap.nodes:
        if n.role != "link" or not n.href:
            continue
        href = n.href
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0]
            if addr and addr not in emails and not _NOREPLY.search(addr):
                emails.append(addr)
        elif re.search(r"linkedin\.com/in/", href, re.I):
            entry = {"name": (n.name or "").strip()[:80], "url": href.split("?")[0]}
            if entry["url"] not in {p["url"] for p in profiles}:
                profiles.append(entry)
    text = snap.text_excerpt or ""
    for m in _EMAIL.finditer(text):
        addr = m.group(0)
        if addr not in emails and not _NOREPLY.search(addr):
            emails.append(addr)
    for m in _NAME_NEAR_ROLE.finditer(text):
        words = m.group(1).split()
        while words and words[0].lower() in _LEAD_WORDS:
            words.pop(0)
        if len(words) < 2:
            continue
        entry = {"name": " ".join(words), "role": m.group(2)}
        if entry["name"] not in {p["name"] for p in people}:
            people.append(entry)
    return {"url": snap.url, "emails": emails[:20], "linkedin_profiles": profiles[:20], "people": people[:20], "mentions_recruiting": bool(_ROLE.search(text))}
