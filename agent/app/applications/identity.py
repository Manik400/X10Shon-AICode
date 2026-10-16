"""Composite application identity used to prevent duplicate applications.

identity = candidate (single-user: constant) | normalized company | requisition id or canonical
apply URL | destination host. Configurable pieces live in ``settings.application_identity``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..core.util import sha256_hex
from ..jobs.canonical import canonicalize_url, normalize_company, normalize_title

DEFAULT_IDENTITY_FIELDS = ["company", "requisition_or_url", "host"]


def identity_key_for(job: dict[str, Any], *, fields: list[str] | None = None, suffix: str | None = None) -> str:
    fields = fields or DEFAULT_IDENTITY_FIELDS
    apply_url = canonicalize_url(job.get("apply_url") or job.get("canonical_url") or job.get("original_url") or "")
    parts = ["candidate:self"]
    for f in fields:
        if f == "company":
            parts.append("company:" + normalize_company(job.get("company")))
        elif f == "requisition_or_url":
            parts.append("req:" + (str(job.get("requisition_id")).lower() if job.get("requisition_id") else apply_url))
        elif f == "title":
            parts.append("title:" + normalize_title(job.get("title")))
        elif f == "host":
            parts.append("host:" + urlsplit(apply_url).netloc.lower())
    if suffix:
        parts.append(suffix)
    return sha256_hex("|".join(parts))[:40]
