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
    async def open_tab(self, url: str, *, active: bool = True, opener_tab_id: str | None = None, home_url: str | None = None) -> str: ...

    async def capabilities(self) -> set[str]:
        """Optional transport features (e.g. 'open_in_window'). Default: all in-process."""
        return {"open_in_window", "activate_tab"}

    async def activate_tab(self, tab_id: str) -> None:  # pragma: no cover - default no-op
        return None

    async def notify(self, title: str, message: str, *, tab_id: str | None = None, key: str | None = None) -> None:  # pragma: no cover - default no-op
        """Desktop alert for the user (e.g. "sign in to continue"); drivers without one ignore it."""
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
