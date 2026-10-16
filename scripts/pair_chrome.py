"""Set up a dedicated Chrome profile with the LFA extension loaded and pair it with the agent.

Uses Playwright's persistent context on the installed Google Chrome so the profile (cookies,
extension storage with the pairing token) survives and can later be launched with
scripts/launch-chrome.ps1. Run from the repo root with the agent running:

    agent/.venv/Scripts/python.exe scripts/pair_chrome.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension" / "dist"
PROFILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / ".lfa" / "chrome-profile"
AGENT = os.environ.get("LFA_AGENT_URL", "http://127.0.0.1:8765")
H = {"X-LFA-Client": "dashboard", "Content-Type": "application/json"}


def api(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(AGENT + path, data=json.dumps(body).encode() if body is not None else None, headers=H, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


async def main() -> int:
    from playwright.async_api import async_playwright

    if not (EXT / "manifest.json").exists():
        print("extension not built: run npm run build in extension/")
        return 2
    PROFILE.mkdir(parents=True, exist_ok=True)
    code = api("/api/pairing/code", {})["code"]
    async with async_playwright() as pw:
        # Google Chrome 137+ ignores --load-extension, so the dedicated LFA browser is Playwright's
        # Chromium build (installed with `playwright install chromium`); the profile persists.
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            ignore_default_args=["--disable-extensions"],
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--no-first-run", "--no-default-browser-check"],
            viewport=None,
        )
        try:
            ext_id = None
            for _ in range(30):
                await asyncio.sleep(1)
                for name in ("Secure Preferences", "Preferences"):
                    prefs = PROFILE / "Default" / name
                    if prefs.exists():
                        try:
                            data = json.loads(prefs.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            continue
                        for eid, entry in (data.get("extensions", {}).get("settings", {}) or {}).items():
                            p = str(entry.get("path", ""))
                            if p and Path(p).resolve() == EXT.resolve():
                                ext_id = eid
                if ext_id:
                    break
            if not ext_id:
                workers = ctx.service_workers
                if workers:
                    ext_id = workers[0].url.split("/")[2]
            if not ext_id:
                print("extension did not load; check chrome://extensions in the LFA profile")
                return 3
            print("extension id:", ext_id)
            page = await ctx.new_page()
            await page.goto(f"chrome-extension://{ext_id}/options.html")
            await page.fill("#code", code)
            await page.click("#pair")
            await page.wait_for_function("() => /Paired|failed/.test(document.getElementById('msg').textContent)", timeout=20000)
            msg = await page.text_content("#msg")
            print("pairing:", msg)
            for _ in range(20):
                await asyncio.sleep(0.5)
                if api("/api/health")["extension"]["connected"]:
                    print("agent reports extension connected")
                    break
            else:
                print("warning: agent does not report the extension as connected yet")
            await page.goto(AGENT)
            await asyncio.sleep(1.0)
        finally:
            await ctx.close()
    (PROFILE / "extension_id.txt").write_text(ext_id, encoding="utf-8")
    print("profile:", PROFILE)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
