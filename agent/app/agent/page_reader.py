"""Text of the current page (or PDF) for assistant-mode answers."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from ..browser.snapshot import PageSnapshot

log = logging.getLogger("lfa.page_reader")

_MAX_PDF_BYTES = 25 * 1024 * 1024
_MAX_PDF_PAGES = 60


def page_text(snap: PageSnapshot) -> str:
    """Headings, the main text excerpt and labelled controls, de-duplicated, in reading order."""
    parts: list[str] = []
    seen: set[str] = set()

    def add(s: str | None) -> None:
        s = " ".join((s or "").split())
        if s and s not in seen:
            seen.add(s)
            parts.append(s)

    for n in snap.nodes:
        if n.role in ("heading", "status", "alert"):
            add(n.text or n.name)
    add(snap.text_excerpt)
    for n in snap.nodes:
        if n.role in ("textbox", "combobox") and n.value:
            add(f"{n.name}: {n.value}")
    return "\n".join(parts)


def _pdf_bytes_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out = []
    for page in reader.pages[:_MAX_PDF_PAGES]:
        try:
            out.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page must not lose the rest
            continue
    return "\n".join(t for t in out if t.strip())


async def pdf_text_from_url(url: str) -> str:
    """Text of a PDF shown in the tab: local file:// paths are read directly, http(s) fetched."""
    try:
        parts = urlsplit(url)
        if parts.scheme == "file":
            path = Path(unquote(parts.path.lstrip("/")) if ":" in parts.path else unquote(parts.path))
            if not path.exists() or path.stat().st_size > _MAX_PDF_BYTES:
                return ""
            return _pdf_bytes_to_text(path.read_bytes())
        if parts.scheme in ("http", "https"):
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                r = await c.get(url)
                if r.status_code != 200 or len(r.content) > _MAX_PDF_BYTES or not r.content.startswith(b"%PDF"):
                    return ""
                return _pdf_bytes_to_text(r.content)
    except Exception as exc:  # noqa: BLE001 - fall back to the page text
        log.info("could not read PDF %s: %s", url[:120], exc)
    return ""
