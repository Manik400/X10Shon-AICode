"""Find job links on a listing page (deterministic)."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from ..browser.snapshot import PageSnapshot

_JOB_URL_HINT = re.compile(r"(job|career|position|opening|posting|vacanc|apply|requisition|/jd/|greenhouse|lever\.co|ashbyhq|workday|myworkdayjobs|smartrecruiters|bamboohr|icims|taleo|successfactors|jobvite|recruitee|workable|breezy)", re.I)
_JOB_TEXT_HINT = re.compile(r"^(open|apply|apply now|view job|view|details|see job|job details|easy apply)$", re.I)
_SKIP_HOSTS = re.compile(r"(linkedin\.com/(company|in)/|facebook\.com|twitter\.com|x\.com|instagram\.com|youtube\.com|glassdoor\.com/Overview|google\.com/maps)", re.I)


_EMBEDDED_ATS = re.compile(r"(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|smartrecruiters\.com|icims\.com|workable\.com|recruitee\.com|bamboohr\.com|jobvite\.com|breezy\.hr|teamtailor\.com|personio\.)", re.I)


def embedded_application_url(snap: PageSnapshot) -> str | None:
    """Address of an applicant-tracking form embedded in a cross-origin frame (e.g. a company
    careers page hosting a Greenhouse form). Opening it directly makes the form reachable."""
    for src in snap.signals.iframe_srcs:
        if src.startswith(("http://", "https://")) and _EMBEDDED_ATS.search(src):
            return src
    # Older content scripts only report a truncated src in unsupported_reasons.
    for reason in snap.signals.unsupported_reasons:
        m = re.search(r"cross-origin iframe: (https?://\S+)", reason)
        if m and _EMBEDDED_ATS.search(m.group(1)) and len(m.group(1)) < 80:
            return m.group(1)
    return None


def extract_job_links(snap: PageSnapshot, *, max_links: int = 50) -> list[dict[str, str]]:
    """Return [{url, text}] for links that look like individual job pages, in page order, deduped."""
    base = snap.url
    base_host = urlsplit(base).netloc.lower()  # used to skip in-page anchors on the list itself
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for n in snap.nodes:
        if n.role != "link" or not n.href:
            continue
        href = n.href.strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base, href)
        parts = urlsplit(url)
        allowed_schemes = ("http", "https", "file") if base.startswith("file:") else ("http", "https")
        if parts.scheme not in allowed_schemes:
            continue
        if _SKIP_HOSTS.search(url):
            continue
        text = (n.name or "").strip()
        # A link counts as a job when its URL looks like a job/ATS page or its text is an
        # open/apply call to action; generic external links ("About", "Blog") are ignored.
        if not (_JOB_URL_HINT.search(url) or _JOB_TEXT_HINT.match(text)):
            continue
        if parts.scheme in ("http", "https") and parts.netloc.lower() == base_host and parts.path == urlsplit(base).path:
            continue  # in-page anchors / filters on the list itself
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": key, "text": text[:120]})
        if len(out) >= max_links:
            break
    return out
