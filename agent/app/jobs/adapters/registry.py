from __future__ import annotations

from typing import Any

from .base import SiteAdapter
from .generic import GenericAdapter
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter


class AdapterRegistry:
    def __init__(self, adapters: list[SiteAdapter] | None = None):
        self._adapters: list[SiteAdapter] = adapters or [GreenhouseAdapter(), LeverAdapter()]
        self._generic = GenericAdapter()

    def for_url(self, url: str) -> SiteAdapter:
        for a in self._adapters:
            if a.matches(url):
                return a
        return self._generic

    def by_name(self, name: str) -> SiteAdapter | None:
        for a in [*self._adapters, self._generic]:
            if a.capabilities.name == name:
                return a
        return None

    def describe(self) -> list[dict[str, Any]]:
        return [a.capabilities.to_dict() for a in [*self._adapters, self._generic]]
