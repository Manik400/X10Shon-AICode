"""Small read-only helpers describing what a snapshot shows (for audit notes)."""

from __future__ import annotations

from ..browser.snapshot import PageSnapshot


def seen_summary(snap: PageSnapshot, *, max_items: int = 6) -> str:
    """" (saw: <title> | fields: a, b | buttons: x, y)" for a result message."""
    fields = [(n.name or n.placeholder or n.name_attr or n.role)[:24] for n in snap.form_fields() if n.visible][:max_items]
    buttons = [n.name[:20] for n in snap.nodes if n.role == "button" and n.visible and n.name][:max_items]
    title = (snap.title or "")[:40]
    parts = [f"title: {title}" if title else "no title", f"fields: {', '.join(fields) if fields else 'none'}", f"buttons: {', '.join(buttons) if buttons else 'none'}"]
    return " (saw " + " | ".join(parts) + ")"
