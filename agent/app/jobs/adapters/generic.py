"""Generic adapter: any URL. Extraction via JSON-LD/heuristics/model; application via the
generic form filler. Submission always requires confirmation."""

from __future__ import annotations

from typing import Any

from ...browser.snapshot import PageSnapshot
from ...models.schemas import JobExtraction
from .base import AdapterCapabilities


class GenericAdapter:
    capabilities = AdapterCapabilities(
        name="generic",
        supported_operations=["extract", "apply"],
        auth_required_for_listing=False,
        auth_required_for_apply=False,
        access_policy="User-provided URLs only. No crawling. Respect robots/ToS of the site; the agent only visits pages the user asked for.",
        known_page_patterns=["any page with a JobPosting JSON-LD block or a recognisable job description", "any single-page application form with labelled fields"],
        form_capabilities=["text", "email", "phone", "textarea", "select", "radio", "checkbox", "file", "date"],
        verification_method="confirmation text / reference number / URL change heuristics (conservative: uncertain when unsure)",
        rate_limit_per_minute=10,
        test_fixtures=["tests/fixtures/forms/simple_application.html", "tests/fixtures/forms/multi_step.html"],
        failure_behavior="unknown structures → SITE_UNSUPPORTED; ambiguous outcomes → SUBMISSION_UNCERTAIN",
        requires_submit_confirmation=True,
        notes="Best effort; see SUPPORT_MATRIX.md",
    )

    def matches(self, url: str) -> bool:
        return True

    async def discover(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    async def extract(self, url: str, snapshot: PageSnapshot | None) -> JobExtraction | None:
        return None

    def submit_hints(self) -> dict[str, Any]:
        c = self.capabilities
        return {"submit_button_names": c.submit_button_names, "confirmation_text": None, "confirmation_url_fragment": None, "requires_submit_confirmation": True}
