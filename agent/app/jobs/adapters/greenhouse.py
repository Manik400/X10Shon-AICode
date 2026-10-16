"""Greenhouse Job Board adapter using the official public Job Board API.

Docs: https://developers.greenhouse.io/job-board.html  (unauthenticated read access to public
boards). Listing: ``GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true``.
Job: ``GET .../jobs/{id}?questions=true``.
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

_URL_RE = re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)(?:/jobs/(\d+))?", re.I)
_EMBED_RE = re.compile(r"gh_jid=(\d+)")


class GreenhouseAdapter:
    capabilities = AdapterCapabilities(
        name="greenhouse",
        supported_operations=["discover", "extract", "apply"],
        auth_required_for_listing=False,
        auth_required_for_apply=False,
        access_policy="Official public Job Board API for listing/extraction; application forms are the public hosted forms. Respect per-board rate limits.",
        known_page_patterns=["boards.greenhouse.io/<board>", "boards.greenhouse.io/<board>/jobs/<id>", "job-boards.greenhouse.io/<board>/jobs/<id>", "<company site>?gh_jid=<id>"],
        form_capabilities=["text", "email", "phone", "file:resume", "file:cover_letter", "select", "custom questions", "EEOC optional block"],
        verification_method="'Thank you for applying' confirmation heading on the same page / confirmation URL",
        rate_limit_per_minute=30,
        test_fixtures=["tests/fixtures/forms/greenhouse_like.html", "tests/fixtures/api/greenhouse_job.json"],
        failure_behavior="API 404 → SITE_UNSUPPORTED; 429 → RATE_LIMITED with Retry-After; form errors → SUBMISSION_REJECTED",
        requires_submit_confirmation=True,
        confirmation_text="Thank you for applying",
        submit_button_names=["Submit application", "Submit Application", "Submit"],
    )

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "LocalFirstAgent/0.1 (+local job applicant tool)"})

    def matches(self, url: str) -> bool:
        return bool(_URL_RE.search(url)) or bool(_EMBED_RE.search(url))

    def _parse(self, url: str) -> tuple[str | None, str | None]:
        m = _URL_RE.search(url)
        if m:
            return m.group(1), m.group(2)
        m2 = _EMBED_RE.search(url)
        return (None, m2.group(1)) if m2 else (None, None)

    async def discover(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        board = config.get("board")
        if not board:
            raise SiteUnsupportedError("greenhouse source requires 'board'")
        r = await self._client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
        if r.status_code == 429:
            raise RateLimitedError("greenhouse rate limited", retry_after_seconds=float(r.headers.get("retry-after", 30)))
        if r.status_code == 404:
            raise SiteUnsupportedError(f"greenhouse board '{board}' not found")
        r.raise_for_status()
        out = []
        for j in r.json().get("jobs", []):
            out.append({"url": j.get("absolute_url"), "title": j.get("title"), "company": config.get("company") or board, "location": (j.get("location") or {}).get("name"), "requisition_id": str(j.get("id")), "posted_at": j.get("updated_at"), "raw": j})
        return out

    async def extract(self, url: str, snapshot: PageSnapshot | None) -> JobExtraction | None:
        board, job_id = self._parse(url)
        if not job_id:
            return None
        if not board:
            return None  # embedded boards need the board token; generic extraction handles the page
        r = await self._client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}", params={"questions": "true"})
        if r.status_code == 429:
            raise RateLimitedError("greenhouse rate limited", retry_after_seconds=float(r.headers.get("retry-after", 30)))
        if r.status_code != 200:
            return None
        return self.from_api_json(r.json(), url, company_hint=board)

    @staticmethod
    def from_api_json(j: dict[str, Any], url: str, company_hint: str | None = None) -> JobExtraction:
        desc = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(j.get("content") or "")))
        desc = re.sub(r"\s+", " ", desc).strip()
        lo, hi = _experience_from_text(desc)
        meta = {m.get("name"): m.get("value") for m in j.get("metadata") or [] if isinstance(m, dict)}
        return JobExtraction(
            title=j.get("title"),
            company=(j.get("company_name") or company_hint),
            location=(j.get("location") or {}).get("name"),
            remote_type=_remote_type(desc + " " + str((j.get("location") or {}).get("name", ""))),
            employment_type=str(meta.get("Employment Type") or "").lower() or None,
            experience_min_years=lo,
            experience_max_years=hi,
            requirements=_requirements_from_text(desc),
            description=desc[:8000] or None,
            apply_url=j.get("absolute_url") or url,
            requisition_id=str(j.get("id")) if j.get("id") else None,
            posted_at=j.get("updated_at"),
            confidence=0.95,
        )

    def submit_hints(self) -> dict[str, Any]:
        c = self.capabilities
        return {"submit_button_names": c.submit_button_names, "confirmation_text": c.confirmation_text, "confirmation_url_fragment": c.confirmation_url_fragment, "requires_submit_confirmation": c.requires_submit_confirmation}
