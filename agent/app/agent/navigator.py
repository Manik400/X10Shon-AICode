"""Model-guided navigation: when the rules cannot tell what to click, the local model decides like a
person would — which control leads to the application form, or which links on a page are jobs.

The model only ever picks an element from a numbered list of what is on the page; the agent then
clicks it through the normal policy checks. It never types, signs in, or submits on its own.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ..browser.snapshot import PageSnapshot, SnapshotNode
from ..models.base import ModelRequest

log = logging.getLogger("lfa.navigator")

_NEVER = ("sign in", "log in", "login", "sign up", "signup", "register", "create account", "share", "save", "cookie", "accept all", "reject", "privacy", "terms", "logout", "sign out", "delete", "remove")


class ChoiceOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int | None = None
    reason: str = Field(default="", max_length=400)


class ItemsOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    indices: list[int] = Field(default_factory=list)


def _describe(n: SnapshotNode) -> str:
    where = ""
    if n.href:
        p = urlsplit(n.href)
        where = f" -> {p.netloc}{p.path[:60]}"
    sec = f" (in: {n.section[:40]})" if n.section else ""
    return f"[{n.role}] {(n.name or '').strip()[:90]}{where}{sec}"


def _candidates(snap: PageSnapshot, *, links_only: bool = False) -> list[SnapshotNode]:
    out = []
    for n in snap.nodes:
        if n.role not in (("link",) if links_only else ("button", "link", "tab", "menuitem")):
            continue
        if not n.visible or n.disabled:
            continue
        name = (n.name or "").strip().lower()
        if not name and not n.href:
            continue
        if any(w in name for w in _NEVER):
            continue
        out.append(n)
    return out[:150]


def _role(gateway: Any) -> str:
    return "assistant" if gateway.role_config("assistant").provider != "heuristic" else "extractor"


async def choose_apply_click(gateway: Any, snap: PageSnapshot, *, task_id: str | None = None) -> SnapshotNode | None:
    """The control a person would click next to reach this job's application form, or None."""
    cands = _candidates(snap)
    if not cands:
        return None
    listing = "\n".join(f"{i}. {_describe(n)}" for i, n in enumerate(cands))
    system = (
        "You operate a web browser for a job applicant. The page shows a job (or a job board entry). "
        "Choose the ONE element a person would click next to get to the application form for THIS job: "
        "an Apply / Apply now / Apply on company website / I'm interested / Continue to application control, "
        "or the link to the employer's own posting when the board only links out. Never choose sign-in, "
        "sign-up, share, save, filters, other or similar jobs, cookie, menu or footer links. "
        'Reply only JSON: {"index": <number from the list or null>, "reason": "<short>"}. Use null when nothing leads to applying.'
    )
    prompt = f"Page: {snap.title}\nURL: {snap.url}\n\nClickable elements:\n{listing}"
    try:
        out = await gateway.complete(_role(gateway), ModelRequest(task="choose_click", system=system, prompt=prompt, json_schema={}, inputs={"items": [(n.name or "") for n in cands]}), ChoiceOutput, task_id=task_id, use_cache=False, max_output_tokens=200)
    except Exception as exc:  # noqa: BLE001 - model unavailable: rules decide
        log.info("navigator model unavailable: %s", exc)
        return None
    if out.index is None or not (0 <= out.index < len(cands)):
        log.info("navigator found nothing to click on %s (%s)", snap.url[:100], out.reason[:120])
        return None
    pick = cands[out.index]
    if not _leads_to_applying(pick, snap):
        # A small local model sometimes picks "Back to jobs" or a reviews link: only accept a
        # choice that plausibly leads to the application.
        log.info("navigator pick rejected (does not lead to applying): %r", pick.name)
        return None
    log.info("navigator chose %r (%s)", pick.name, out.reason[:120])
    return pick


# "Open" / "View job" / "Details" on a board row or a post lead one step closer to the posting
# and are followed too (a person keeps clicking through until the real Apply shows up).
_APPLY_WORDS = re.compile(r"\b(apply|application|interested|continue|proceed|next|submit|start|candidate|bewerb|postul|solicit|hae\b|aanmeld|open|view (job|posting|position|details|more)|details|read more|learn more|see (job|posting|more))\b", re.I)
_OFF_TOPIC = re.compile(r"\b(back|jobs?|similar|more|other|review|rate|rating|salar(y|ies)|company|about|faq|glassdoor|benefits|interview|blog|news|filter|search|alert|follow)\b", re.I)


_SUBMIT_LIKE = re.compile(r"^\s*(submit|send|finish|complete)\b", re.I)


def _leads_to_applying(n: SnapshotNode, snap: PageSnapshot) -> bool:
    name = (n.name or "").strip()
    if _SUBMIT_LIKE.search(name):
        # "Submit Application" sends a form; it is never the way INTO one (the fill step
        # decides when to submit, under its own checks).
        return False
    if _APPLY_WORDS.search(name):
        return True
    if n.href:
        # An employer's own posting / ATS link from a board that only links out.
        here = urlsplit(snap.url).netloc.lower()
        there = urlsplit(n.href).netloc.lower()
        if there and there != here and re.search(r"(apply|/job/|/jobs/\w|careers?|greenhouse|lever\.co|ashbyhq|workday|smartrecruiters|icims|taleo|bamboohr)", n.href, re.I) and not _OFF_TOPIC.search(name):
            return True
    return False


async def choose_job_links(gateway: Any, snap: PageSnapshot, *, task_id: str | None = None) -> list[dict[str, str]]:
    """Links on a page that each open ONE job posting (the model reads the page like a person)."""
    cands = [n for n in _candidates(snap, links_only=True) if n.href and n.href.startswith(("http://", "https://"))]
    if not cands:
        return []
    listing = "\n".join(f"{i}. {_describe(n)}" for i, n in enumerate(cands))
    system = (
        "You operate a web browser for a job applicant. From the numbered links on this page, list every link "
        "that opens ONE specific job posting or its application (job titles, 'View job', 'Apply', open/external "
        "icons in job rows). Exclude navigation, filters, company pages, search, login, help and footer links. "
        'Reply only JSON: {"indices": [numbers]}.'
    )
    prompt = f"Page: {snap.title}\nURL: {snap.url}\n\nLinks:\n{listing}"
    try:
        out = await gateway.complete(_role(gateway), ModelRequest(task="choose_items", system=system, prompt=prompt, json_schema={}, inputs={"items": [(n.name or "") + " " + (n.href or "") for n in cands]}), ItemsOutput, task_id=task_id, use_cache=False, max_output_tokens=400)
    except Exception as exc:  # noqa: BLE001
        log.info("navigator model unavailable: %s", exc)
        return []
    seen: set[str] = set()
    links = []
    for i in out.indices:
        if 0 <= i < len(cands):
            url = (cands[i].href or "").split("#")[0]
            if url and url not in seen:
                seen.add(url)
                links.append({"url": url, "text": (cands[i].name or "")[:120]})
    return links
