"""PlaywrightDriver: same action semantics as the extension, driven by Playwright.

The compiled ``dom-agent`` bundle (``extension/dist/dom-agent.js``) is injected into every page so
the exact same locator/action code runs in both transports. Used for tests and for
unauthenticated job-page extraction.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

from ..core.actions import ActionResult, BrowserAction
from ..core.errors import BrowserDisconnectedError, NavigationError
from .driver import BrowserDriver, TabInfo
from .snapshot import PageSnapshot

log = logging.getLogger("lfa.playwright")

DOM_AGENT_BUNDLE = Path(__file__).resolve().parents[3] / "extension" / "dist" / "dom-agent.js"


def load_dom_agent_source(path: Path = DOM_AGENT_BUNDLE) -> str:
    if not path.exists():
        raise BrowserDisconnectedError(f"dom-agent bundle not found at {path}; run `npm run build` in extension/")
    return path.read_text(encoding="utf-8")


class PlaywrightDriver(BrowserDriver):
    name = "playwright"

    def __init__(self, *, headless: bool = True, max_pages: int = 2, navigation_timeout_ms: int = 30_000, bundle_path: Path = DOM_AGENT_BUNDLE):
        self._headless = headless
        self._max_pages = max_pages
        self._nav_timeout = navigation_timeout_ms
        self._bundle_path = bundle_path
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}
        self._counter = 0
        self._lock = asyncio.Lock()
        self._source: str | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright

            self._source = load_dom_agent_source(self._bundle_path)
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            self._context = await self._browser.new_context()
            self._context.set_default_navigation_timeout(self._nav_timeout)
            self._context.set_default_timeout(self._nav_timeout)
            await self._context.add_init_script(self._source)

    async def shutdown(self) -> None:
        async with self._lock:
            try:
                if self._context is not None:
                    await self._context.close()
                if self._browser is not None:
                    await self._browser.close()
                if self._pw is not None:
                    await self._pw.stop()
            finally:
                self._context = self._browser = self._pw = None
                self._pages.clear()

    async def is_available(self) -> bool:
        try:
            await self.start()
            return True
        except Exception as exc:  # pragma: no cover - depends on local browser install
            log.warning("playwright unavailable: %s", exc)
            return False

    # ------------------------------------------------------------------ tabs
    def _page(self, tab_id: str) -> Any:
        page = self._pages.get(tab_id)
        if page is None or page.is_closed():
            raise BrowserDisconnectedError(f"unknown or closed tab {tab_id}")
        return page

    async def list_tabs(self) -> list[TabInfo]:
        out = []
        for tid, page in list(self._pages.items()):
            if page.is_closed():
                self._pages.pop(tid, None)
                continue
            out.append(TabInfo(tab_id=tid, url=page.url, title=await page.title()))
        return out

    async def open_tab(self, url: str, *, active: bool = True) -> str:
        await self.start()
        live = [t for t, p in self._pages.items() if not p.is_closed()]
        if len(live) >= self._max_pages:
            raise BrowserDisconnectedError(f"max concurrent pages ({self._max_pages}) reached")
        self._counter += 1
        tid = f"pw-{self._counter}"
        page = await self._context.new_page()
        self._pages[tid] = page
        if url:
            await self._goto(page, url, self._nav_timeout)
        return tid

    async def close_tab(self, tab_id: str) -> None:
        page = self._pages.pop(tab_id, None)
        if page is not None and not page.is_closed():
            await page.close()

    async def _goto(self, page: Any, url: str, timeout_ms: int) -> None:
        try:
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except Exception as exc:
            raise NavigationError(f"navigation to {url} failed: {type(exc).__name__}") from exc

    async def navigate(self, tab_id: str, url: str, timeout_ms: int) -> ActionResult:
        page = self._page(tab_id)
        import time

        t0 = time.perf_counter()
        try:
            await self._goto(page, url, timeout_ms)
        except NavigationError as exc:
            return ActionResult(ok=False, action_type="navigate", error_category="NAVIGATION_FAILED", message=str(exc), url=page.url)
        return ActionResult(ok=True, action_type="navigate", url=page.url, title=await page.title(), duration_ms=int((time.perf_counter() - t0) * 1000))

    # ------------------------------------------------------------------ dom-agent bridge
    async def _ensure_agent(self, page: Any) -> None:
        present = await page.evaluate("() => typeof window.__lfaDomAgent !== 'undefined'")
        if not present:
            await page.evaluate(self._source or load_dom_agent_source(self._bundle_path))

    async def snapshot(self, tab_id: str, *, max_nodes: int) -> PageSnapshot:
        page = self._page(tab_id)
        await self._ensure_agent(page)
        raw = await page.evaluate("(opts) => window.__lfaDomAgent.snapshot(opts)", {"maxNodes": max_nodes})
        return PageSnapshot.model_validate(raw)

    async def execute(self, tab_id: str, action: BrowserAction, *, payload: dict[str, Any] | None = None) -> ActionResult:
        if action.type == "navigate":
            return await self.navigate(tab_id, action.args["url"], action.timeout_ms)
        page = self._page(tab_id)
        await self._ensure_agent(page)
        data = action.model_dump(mode="json", exclude={"reason"})
        if action.type == "upload" and payload and "bytes" in payload:
            # Resolve the file into base64 so the same dom-agent code path handles it.
            payload = {**payload, "bytes_b64": base64.b64encode(payload.pop("bytes")).decode()}
        try:
            raw = await asyncio.wait_for(
                page.evaluate("async ({action, payload}) => await window.__lfaDomAgent.execute(action, payload || null)", {"action": data, "payload": payload}),
                timeout=action.timeout_ms / 1000 + 5,
            )
        except asyncio.TimeoutError:
            return ActionResult(ok=False, action_type=action.type, error_category="NAVIGATION_FAILED", message="action timed out", url=page.url)
        except Exception as exc:
            # A click that triggers navigation destroys the execution context; treat as success-with-navigation.
            if "Execution context was destroyed" in str(exc) or "navigation" in str(exc).lower():
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=action.timeout_ms)
                except Exception:
                    pass
                return ActionResult(ok=True, action_type=action.type, url=page.url, title=await page.title(), message="navigation occurred during action")
            return ActionResult(ok=False, action_type=action.type, error_category="UNKNOWN", message=str(exc)[:300], url=page.url)
        result = ActionResult.model_validate(raw)
        if result.ok and action.type in {"click", "press_key"}:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            result.url = page.url
        return result
