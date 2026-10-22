"""Find job links on a listing page (deterministic)."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from ..browser.snapshot import PageSnapshot

_JOB_URL_HINT = re.compile(r"(job|career|position|opening|posting|vacanc|apply|requisition|/jd/|greenhouse|lever\.co|ashbyhq|workday|myworkdayjobs|smartrecruiters|bamboohr|icims|taleo|successfactors|jobvite|recruitee|workable|breezy)", re.I)
_JOB_TEXT_HINT = re.compile(r"^(open|apply|apply now|view job|view|details|see job|job details|easy apply)$", re.I)
_SKIP_HOSTS = re.compile(
    r"(linkedin\.com/(company|in|feed|sharing|posts|pulse|shareArticle|messaging|job-apply-resources|jobs-tracker|my-items|help|legal|premium|learning|notifications|mynetwork|search)|linkedin\.com/jobs/(search|collections|home)?(\?|/?$)|facebook\.com|twitter\.com|//x\.com|instagram\.com|youtube\.com|whatsapp\.com|t\.me/|"
    r"reddit\.com|glassdoor\.com/Overview|google\.com/maps|/share\b|[?&]share|intent/tweet|mailto:)",
    re.I,
)
# Sites where an application can be completed without creating an account: tried first.
_NO_ACCOUNT_ATS = re.compile(r"(ashbyhq\.com|greenhouse\.io|gh_jid=|lever\.co|smartrecruiters\.com|workable\.com|recruitee\.com|breezy\.hr|teamtailor\.com|personio\.)", re.I)
_ACCOUNT_ATS = re.compile(r"(myworkdayjobs\.com|taleo\.net|successfactors|icims\.com|oraclecloud\.com)", re.I)
# Job boards whose own "Apply" is an in-site flow tied to the user's account (LinkedIn Easy Apply,
# Naukri apply): tried after company / ATS sites, which take applications directly.
_JOB_BOARDS = re.compile(r"(linkedin\.com/jobs|naukri\.com|indeed\.(com|co\.in)|foundit\.in|monster\.|glassdoor\.|instahyre\.com|wellfound\.com|cutshort\.io)", re.I)


def account_needed_rank(url: str) -> int:
    """0 = no account normally needed (ATS), 1 = company site / unknown, 2 = job board,
    3 = the site requires an account (Workday, Taleo…)."""
    if _NO_ACCOUNT_ATS.search(url):
        return 0
    if _ACCOUNT_ATS.search(url):
        return 3
    if _JOB_BOARDS.search(url):
        return 2
    return 1


_NOT_A_POSTING = re.compile(
    r"(^/?$|/(job-seeker-support|support|help|faq|privacy|terms|legal|cookies?|login|log-in|signin|sign-in|signup|sign-up|register|about|blog|contact)(/|$)"
    r"|/(job-openings|open-positions|openings|careers|jobs|vacancies)/?$)",
    re.I,
)

_LINKEDIN_REDIRECT = re.compile(r"linkedin\.com/(safety/go|redir/redirect)", re.I)


def unwrap_redirect(url: str) -> str | None:
    """Destination of a job board's "you are leaving" redirect page (LinkedIn safety/go?url=…)."""
    from urllib.parse import parse_qs, unquote

    if not _LINKEDIN_REDIRECT.search(url or ""):
        return None
    q = parse_qs(urlsplit(url).query)
    for key in ("url", "dest", "target"):
        if q.get(key):
            dest = unquote(q[key][0])
            if dest.startswith(("http://", "https://")):
                return dest
    return None


_EMBEDDED_ATS = re.compile(r"(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|smartrecruiters\.com|icims\.com|workable\.com|recruitee\.com|bamboohr\.com|jobvite\.com|breezy\.hr|teamtailor\.com|personio\.|comeet\.(co|com)|jazzhr\.com|applytojob\.com|zohorecruit|freshteam\.com|keka\.com|darwinbox|/apply\b)", re.I)


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
        # A link counts as a job when its address (host + path, never the query string, which
        # may carry arbitrary share text) looks like a job/ATS page, or its text is an open/apply
        # call to action; generic external links ("About", "Blog") are ignored.
        addr = f"{parts.netloc}{parts.path}" if parts.scheme in ("http", "https") else url
        gh = "gh_jid=" in (parts.query or "")
        if not (_JOB_URL_HINT.search(addr) or gh or _JOB_TEXT_HINT.match(text)):
            continue
        if parts.scheme in ("http", "https") and parts.netloc.lower() == base_host and parts.path == urlsplit(base).path:
            continue  # in-page anchors / filters on the list itself
        if parts.scheme in ("http", "https") and _NOT_A_POSTING.search(parts.path or "/"):
            continue  # help, privacy, sign-in or the careers home page: not one job posting
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": key, "text": text[:120]})
        if len(out) >= max_links:
            break
    return out
