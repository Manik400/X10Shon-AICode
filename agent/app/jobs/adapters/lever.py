"""Lever adapter using the official public Postings API.

Docs: https://github.com/lever/postings-api — ``GET https://api.lever.co/v0/postings/{site}``
and ``GET https://api.lever.co/v0/postings/{site}/{postingId}`` (unauthenticated, public).
"""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

from ...browser.snapshot import PageSnapshot
from ...core.errors import RateLimitedError, SiteUnsupportedError
from ...models.schemas import JobExtraction
from ..extraction import _experience_from_text, _remote_type, _requirements_from_text
from .base import AdapterCapabilities

_URL_RE = re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([^/?#]+)(?:/([0-9a-f-]{36}))?", re.I)


class LeverAdapter:
    capabilities = AdapterCapabilities(
        name="lever",
        supported_operations=["discover", "extract", "apply"],
        auth_required_for_listing=False,
        auth_required_for_apply=False,
        access_policy="Official public Postings API for listing/extraction; hosted apply form at jobs.lever.co/<site>/<id>/apply.",
        known_page_patterns=["jobs.lever.co/<site>", "jobs.lever.co/<site>/<uuid>", "jobs.lever.co/<site>/<uuid>/apply"],
        form_capabilities=["text", "email", "phone", "file:resume", "select", "custom cards", "EEO optional block", "links"],
        verification_method="confirmation page ('/thanks') or 'Application submitted' text",
        rate_limit_per_minute=30,
        test_fixtures=["tests/fixtures/forms/lever_like.html", "tests/fixtures/api/lever_posting.json"],
        failure_behavior="API 404 → SITE_UNSUPPORTED; 429 → RATE_LIMITED; form errors → SUBMISSION_REJECTED",
        requires_submit_confirmation=True,
        confirmation_text="Application submitted",
        confirmation_url_fragment="/thanks",
        submit_button_names=["Submit application", "Submit"],
    )

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "LocalFirstAgent/0.1 (+local job applicant tool)"})

    def matches(self, url: str) -> bool:
        return bool(_URL_RE.search(url))

    async def discover(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        site = config.get("site")
        if not site:
            raise SiteUnsupportedError("lever source requires 'site'")
        r = await self._client.get(f"https://api.lever.co/v0/postings/{site}", params={"mode": "json"})
        if r.status_code == 429:
            raise RateLimitedError("lever rate limited", retry_after_seconds=float(r.headers.get("retry-after", 30)))
        if r.status_code == 404:
            raise SiteUnsupportedError(f"lever site '{site}' not found")
        r.raise_for_status()
        out = []
        for p in r.json():
            cats = p.get("categories") or {}
            out.append({"url": p.get("hostedUrl"), "title": p.get("text"), "company": config.get("company") or site, "location": cats.get("location"), "requisition_id": p.get("id"), "posted_at": _ms_to_iso(p.get("createdAt")), "raw": p})
        return out

    async def extract(self, url: str, snapshot: PageSnapshot | None) -> JobExtraction | None:
        m = _URL_RE.search(url)
        if not m or not m.group(2):
            return None
        site, pid = m.group(1), m.group(2)
        r = await self._client.get(f"https://api.lever.co/v0/postings/{site}/{pid}")
        if r.status_code == 429:
            raise RateLimitedError("lever rate limited", retry_after_seconds=float(r.headers.get("retry-after", 30)))
        if r.status_code != 200:
            return None
        return self.from_api_json(r.json(), url, company_hint=site)

    @staticmethod
    def from_api_json(p: dict[str, Any], url: str, company_hint: str | None = None) -> JobExtraction:
        desc_parts = [p.get("descriptionPlain") or re.sub(r"<[^>]+>", " ", html.unescape(p.get("description") or ""))]
        for lst in p.get("lists") or []:
            desc_parts.append(str(lst.get("text", "")))
            desc_parts.append(re.sub(r"<[^>]+>", " • ", html.unescape(lst.get("content") or "")))
        desc = re.sub(r"\s+", " ", " ".join(desc_parts)).strip()
        cats = p.get("categories") or {}
        lo, hi = _experience_from_text(desc)
        wt = str(p.get("workplaceType") or "").lower()
        remote = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}.get(wt) or _remote_type(desc + " " + str(cats.get("location", "")))
        return JobExtraction(
            title=p.get("text"),
            company=company_hint,
            location=cats.get("location"),
            remote_type=remote,
            employment_type=str(cats.get("commitment") or "").lower() or None,
            experience_min_years=lo,
            experience_max_years=hi,
            requirements=_requirements_from_text(desc),
            description=desc[:8000] or None,
            apply_url=p.get("applyUrl") or (p.get("hostedUrl") + "/apply" if p.get("hostedUrl") else url),
            requisition_id=p.get("id"),
            posted_at=_ms_to_iso(p.get("createdAt")),
            confidence=0.95,
        )

    def submit_hints(self) -> dict[str, Any]:
        c = self.capabilities
        return {"submit_button_names": c.submit_button_names, "confirmation_text": c.confirmation_text, "confirmation_url_fragment": c.confirmation_url_fragment, "requires_submit_confirmation": c.requires_submit_confirmation}


def _ms_to_iso(ms: Any) -> str | None:
    try:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat() if ms else None
    except (TypeError, ValueError):
        return None
