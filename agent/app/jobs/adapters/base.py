"""Adapter contract for job sources and application sites."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ...browser.snapshot import PageSnapshot
from ...models.schemas import JobExtraction


@dataclass(slots=True)
class AdapterCapabilities:
    name: str
    supported_operations: list[str]  # discover | extract | apply
    auth_required_for_listing: bool
    auth_required_for_apply: bool
    access_policy: str  # free-text description of ToS/robots posture
    known_page_patterns: list[str]
    form_capabilities: list[str]
    verification_method: str
    rate_limit_per_minute: int
    test_fixtures: list[str]
    failure_behavior: str
    supported: bool = True
    requires_submit_confirmation: bool = True
    confirmation_text: str | None = None
    confirmation_url_fragment: str | None = None
    submit_button_names: list[str] = field(default_factory=lambda: ["Submit application", "Submit", "Apply"])
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


class SiteAdapter(Protocol):
    capabilities: AdapterCapabilities

    def matches(self, url: str) -> bool: ...

    async def discover(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """List jobs from a configured board. Returns dicts with at least ``url``."""

    async def extract(self, url: str, snapshot: PageSnapshot | None) -> JobExtraction | None:
        """Adapter-specific extraction (e.g. via official API); None to fall back to generic."""

    def submit_hints(self) -> dict[str, Any]:
        """Submission button names and confirmation expectations."""
