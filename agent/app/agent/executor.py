"""BrowserExecutor: executes one validated, policy-approved action with timeout and bookkeeping."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

from ..browser.driver import BrowserDriver
from ..core.actions import ActionResult, BrowserAction
from ..core.errors import AgentError, BrowserDisconnectedError


class LoopDetector:
    """Flags repeated identical actions on an unchanged page."""

    def __init__(self, max_repeats: int = 3):
        self._max = max_repeats
        self._counts: Counter[str] = Counter()

    def check(self, action: BrowserAction, page_hash: str) -> bool:
        key = f"{action.fingerprint()}|{page_hash}"
        self._counts[key] += 1
        return self._counts[key] > self._max

    def reset(self) -> None:
        self._counts.clear()


class BrowserExecutor:
    def __init__(self, driver: BrowserDriver):
        self._driver = driver

    async def execute(self, tab_id: str, action: BrowserAction, payload: dict[str, Any] | None = None) -> ActionResult:
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(self._driver.execute(tab_id, action, payload=payload), timeout=action.timeout_ms / 1000 + 10)
        except asyncio.TimeoutError:
            return ActionResult(ok=False, action_type=action.type, error_category="NAVIGATION_FAILED", message=f"action timed out after {action.timeout_ms} ms", duration_ms=int((time.perf_counter() - t0) * 1000))
        except BrowserDisconnectedError as exc:
            return ActionResult(ok=False, action_type=action.type, error_category="BROWSER_DISCONNECTED", message=str(exc), duration_ms=int((time.perf_counter() - t0) * 1000))
        except AgentError as exc:
            return ActionResult(ok=False, action_type=action.type, error_category=exc.category.value, message=str(exc), duration_ms=int((time.perf_counter() - t0) * 1000))
        if not result.duration_ms:
            result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result
