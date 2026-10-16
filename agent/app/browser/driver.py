"""BrowserDriver: the single interface both transports implement.

A *tab handle* is an opaque string. For the extension it is the Chrome tab id; for Playwright it
is an internal page key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..core.actions import ActionResult, BrowserAction
from .snapshot import PageSnapshot


@dataclass(slots=True)
class TabInfo:
    tab_id: str
    url: str
    title: str
    active: bool = False


class BrowserDriver(ABC):
    name: str = "abstract"

    @abstractmethod
    async def is_available(self) -> bool: ...

    @abstractmethod
    async def list_tabs(self) -> list[TabInfo]: ...

    @abstractmethod
    async def open_tab(self, url: str, *, active: bool = True) -> str: ...

    async def activate_tab(self, tab_id: str) -> None:  # pragma: no cover - default no-op
        return None

    @abstractmethod
    async def close_tab(self, tab_id: str) -> None: ...

    @abstractmethod
    async def navigate(self, tab_id: str, url: str, timeout_ms: int) -> ActionResult: ...

    @abstractmethod
    async def snapshot(self, tab_id: str, *, max_nodes: int) -> PageSnapshot: ...

    @abstractmethod
    async def execute(self, tab_id: str, action: BrowserAction, *, payload: dict[str, Any] | None = None) -> ActionResult:
        """Execute a validated action. ``payload`` carries agent-resolved data the model must
        never see or choose (e.g. resume bytes for ``upload``)."""

    async def tab_url(self, tab_id: str) -> str:
        for t in await self.list_tabs():
            if t.tab_id == tab_id:
                return t.url
        return ""

    async def shutdown(self) -> None:  # pragma: no cover - trivial default
        return None
