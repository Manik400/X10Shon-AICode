"""URL canonicalization and deduplication keys for jobs."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.util import sha256_hex

_TRACKING_PARAMS = re.compile(r"^(utm_|gh_src|lever-source|source|ref|referrer|src|trk|trackingid|fbclid|gclid|mc_|_hs|hs_|igshid|si|cid|campaign|medium)", re.I)
_KEEP_PARAMS = {"gh_jid", "jobid", "job_id", "jid", "id", "req", "reqid", "requisitionid", "posting", "job", "p", "jk"}

# Host-specific requisition id patterns (official job-board URL shapes).
_REQ_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", re.I), "greenhouse"),
    (re.compile(r"job-boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", re.I), "ashby"),
    (re.compile(r"myworkdayjobs\.com/.*?/job/[^/]+/[^/]*?_([A-Z0-9-]+)", re.I), "workday"),
    (re.compile(r"linkedin\.com/jobs/view/(?:[^/]*-)?(\d+)", re.I), "linkedin"),
    (re.compile(r"indeed\.com/.*?[?&]jk=([0-9a-f]+)", re.I), "indeed"),
]


def canonicalize_url(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("file://"):
        return url
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(":443") or host.endswith(":80"):
        host = host.rsplit(":", 1)[0]
    path = re.sub(r"/{2,}", "/", parts.path) or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not _TRACKING_PARAMS.match(k)]
    query.sort()
    # The scheme is preserved (an http-only site must stay reachable); dedup keys ignore it.
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def detect_requisition(url: str) -> tuple[str | None, str | None]:
    """Return (ats_name, requisition_id) when the URL follows a known ATS pattern."""
    for pattern, name in _REQ_PATTERNS:
        m = pattern.search(url)
        if m:
            return name, m.group(m.lastindex or 1)
    parts = urlsplit(url)
    for k, v in parse_qsl(parts.query):
        if k.lower() in _KEEP_PARAMS and v:
            return None, v
    return None, None


def normalize_title(title: str | None) -> str:
    t = (title or "").lower()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    t = re.sub(r"[^a-z0-9+# ]+", " ", t)
    t = re.sub(r"\b(sr|senior|jr|junior|lead|principal|staff|ii|iii|iv|remote|hybrid|onsite|us|usa|uk)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_company(company: str | None) -> str:
    c = (company or "").lower()
    c = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|pvt|private|technologies|technology|labs)\b\.?", " ", c)
    c = re.sub(r"[^a-z0-9 ]+", " ", c)
    return re.sub(r"\s+", " ", c).strip()


def dedup_key(canonical_url: str, *, company: str | None = None, title: str | None = None, requisition_id: str | None = None) -> str:
    """Primary identity of a job.

    Preference order: ATS requisition id (host + id) → canonical URL. Company/title are used by
    the service for a secondary fuzzy duplicate check, not in the primary key, so two distinct
    requisitions with the same title remain distinct.
    """
    parts = urlsplit(canonical_url)
    host = parts.netloc.lower()
    ats, req = detect_requisition(canonical_url)
    if requisition_id or req:
        return sha256_hex(f"{ats or host}|{(requisition_id or req or '').lower()}")[:32]
    scheme_less = canonical_url.split("://", 1)[1] if "://" in canonical_url else canonical_url
    return sha256_hex(scheme_less)[:32]


def fuzzy_key(company: str | None, title: str | None, location: str | None = None) -> str:
    return sha256_hex(f"{normalize_company(company)}|{normalize_title(title)}|{(location or '').lower().strip()[:40]}")[:32]
