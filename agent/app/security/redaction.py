"""Redact secrets and personal data before anything reaches logs or diagnostic exports."""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|secret|token|cookie|session|otp|authorization|api[_-]?key|ssn|social.?security|credit.?card|cvv)",
    re.IGNORECASE,
)
_PII_KEYS = re.compile(r"(email|phone|address|date_of_birth|dob|first_name|last_name|full_name)", re.IGNORECASE)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\d.])(\+?\d[\d\s().-]{7,}\d)(?![\d.])")
_IPV4_RE = re.compile(r"(?<!\d)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d{1,5})?(?!\d)")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
_OTP_RE = re.compile(r"(?i)\b(otp|code)[:\s]+\d{4,8}\b")


def redact_text(text: str, *, pii: bool = True) -> str:
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _OTP_RE.sub(r"\1: [REDACTED]", text)
    if pii:
        text = _EMAIL_RE.sub("[EMAIL]", text)
        # Keep loopback/IP addresses readable in logs; they are not personal data.
        ips: list[str] = []

        def _keep(m: re.Match[str]) -> str:
            ips.append(m.group(0))
            return f"IP{len(ips) - 1}"

        text = _IPV4_RE.sub(_keep, text)
        text = _PHONE_RE.sub("[PHONE]", text)
        for i, ip in enumerate(ips):
            text = text.replace(f"IP{i}", ip)
    return text


def redact(obj: Any, *, pii: bool = True, _depth: int = 0) -> Any:
    """Deep-copy ``obj`` replacing sensitive values. Keys matching secret patterns are always
    removed; keys matching PII patterns are masked when ``pii`` is true."""
    if _depth > 12:
        return "[TRUNCATED]"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if _SENSITIVE_KEYS.search(ks):
                out[ks] = "[REDACTED]"
            elif pii and _PII_KEYS.search(ks):
                out[ks] = "[PII]" if v not in (None, "") else v
            else:
                out[ks] = redact(v, pii=pii, _depth=_depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(v, pii=pii, _depth=_depth + 1) for v in obj[:200]]
    if isinstance(obj, str):
        return redact_text(obj, pii=pii)
    return obj
