"""Job description extraction: JSON-LD → deterministic text heuristics → model."""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from ..browser.snapshot import PageSnapshot
from ..models.base import ModelRequest
from ..models.gateway import ModelGateway
from ..models.schemas import JobExtraction

log = logging.getLogger("lfa.jobs.extract")

_EXP_RE = re.compile(r"(\d{1,2})\s*(?:\+|-|to|–)?\s*(\d{1,2})?\s*\+?\s*(?:years|yrs)(?:'| of)?\s*(?:experience|exp)?", re.I)
_SALARY_RE = re.compile(r"(?:(USD|EUR|GBP|INR|CAD|AUD|\$|€|£|₹)\s?)(\d{2,3}(?:,\d{3})+|\d{2,3}k|\d{4,7})(?:\s*(?:-|–|to)\s*(?:\$|€|£|₹)?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}k|\d{4,7}))?", re.I)
_REMOTE_RE = re.compile(r"\b(fully remote|remote[- ]first|work from home|remote)\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_ONSITE_RE = re.compile(r"\b(on[- ]?site|in[- ]office)\b", re.I)
_EMPLOYMENT_RE = re.compile(r"\b(full[- ]time|part[- ]time|contract(?:or)?|internship|intern|temporary|freelance)\b", re.I)
_REQ_HEADINGS = re.compile(r"(requirements|qualifications|what you('|’)ll need|what we('|’)re looking for|must have|you have|skills)", re.I)


def _money(s: str) -> float:
    s = s.lower().replace(",", "")
    if s.endswith("k"):
        return float(s[:-1]) * 1000
    return float(s)


def _currency(sym: str) -> str:
    return {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}.get(sym, sym.upper())


def extract_from_jsonld(items: list[dict[str, Any]], url: str) -> JobExtraction | None:
    for it in items:
        if "JobPosting" not in str(it.get("@type", "")):
            continue
        org = it.get("hiringOrganization") or {}
        company = org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else None)
        loc = it.get("jobLocation")
        location = None
        if isinstance(loc, list) and loc:
            loc = loc[0]
        if isinstance(loc, dict):
            addr = loc.get("address") or {}
            if isinstance(addr, dict):
                location = ", ".join(str(addr.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry") if addr.get(k)) or None
            elif isinstance(addr, str):
                location = addr
        remote = "remote" if str(it.get("jobLocationType", "")).upper() == "TELECOMMUTE" else "unknown"
        salary = it.get("baseSalary") or {}
        smin = smax = None
        cur = None
        if isinstance(salary, dict):
            cur = salary.get("currency")
            val = salary.get("value") or {}
            if isinstance(val, dict):
                smin = _to_float(val.get("minValue") or val.get("value"))
                smax = _to_float(val.get("maxValue"))
        desc_html = it.get("description") or ""
        desc = html.unescape(re.sub(r"<[^>]+>", " ", str(desc_html)))
        desc = re.sub(r"\s+", " ", desc).strip()
        et = it.get("employmentType")
        if isinstance(et, list):
            et = ", ".join(str(x) for x in et)
        exp_min, exp_max = _experience_from_text(desc)
        return JobExtraction(
            title=str(it.get("title") or "").strip() or None,
            company=str(company).strip() if company else None,
            location=location,
            remote_type=remote if remote != "unknown" else _remote_type(desc + " " + (location or "")),
            employment_type=str(et).lower().replace("_", "-") if et else None,
            experience_min_years=exp_min,
            experience_max_years=exp_max,
            salary_min=smin,
            salary_max=smax,
            salary_currency=str(cur) if cur else None,
            requirements=_requirements_from_text(desc),
            description=desc[:8000] or None,
            apply_url=str(it.get("url") or url),
            requisition_id=str(it.get("identifier", {}).get("value")) if isinstance(it.get("identifier"), dict) and it["identifier"].get("value") else None,
            posted_at=str(it.get("datePosted")) if it.get("datePosted") else None,
            confidence=0.95,
        )
    return None


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _experience_from_text(text: str) -> tuple[float | None, float | None]:
    best: tuple[float | None, float | None] = (None, None)
    for m in _EXP_RE.finditer(text):
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else None
        if lo > 30:
            continue
        if best[0] is None or lo < best[0]:
            best = (lo, hi)
    return best


def _remote_type(text: str) -> str:
    if _HYBRID_RE.search(text):
        return "hybrid"
    if _REMOTE_RE.search(text):
        return "remote"
    if _ONSITE_RE.search(text):
        return "onsite"
    return "unknown"


def _requirements_from_text(text: str) -> list[str]:
    """Pull bullet-like requirement sentences following a requirements heading."""
    out: list[str] = []
    m = _REQ_HEADINGS.search(text)
    if not m:
        return out
    tail = text[m.end() : m.end() + 3000]
    for sent in re.split(r"(?<=[.!?])\s+|\s*[•·▪●]\s*|\s{2,}", tail):
        s = sent.strip(" -•")
        if 12 <= len(s) <= 220 and not s.lower().startswith(("benefits", "about us", "we offer")):
            out.append(s)
        if len(out) >= 25:
            break
    return out


def heuristic_extract_from_text(title: str, text: str, url: str) -> dict[str, Any]:
    """Deterministic extraction used by the heuristic provider and as a model fallback."""
    t = text or ""
    job_title = None
    company = None
    # "<Title> at <Company>" / "<Title> - <Company>" / "<Company> - <Title>" in the document title.
    if title:
        m = re.match(r"^(.*?)\s+(?:at|@)\s+(.*?)(?:\s*[|•-].*)?$", title)
        if m:
            job_title, company = m.group(1).strip(), m.group(2).strip()
        else:
            parts = [p.strip() for p in re.split(r"\s[|•–-]\s", title) if p.strip()]
            if len(parts) >= 2:
                job_title, company = parts[0], parts[1]
            elif parts:
                job_title = parts[0]
    if not company:
        m = re.search(r"\b(?:at|join)\s+([A-Z][A-Za-z0-9&.]+(?:\s+[A-Z][A-Za-z0-9&.]+){0,2})", t)
        if m:
            company = m.group(1)
    location = None
    m = re.search(r"\b(?:location|based in|located in)\s*[:\-]?\s*([A-Z][A-Za-z .]+(?:,\s*[A-Z][A-Za-z .]+)*)", t)
    if m:
        location = m.group(1).strip()[:80]
    exp_min, exp_max = _experience_from_text(t)
    smin = smax = None
    cur = None
    m = _SALARY_RE.search(t)
    if m:
        cur = _currency(m.group(1))
        smin = _money(m.group(2))
        smax = _money(m.group(3)) if m.group(3) else None
    et = None
    m = _EMPLOYMENT_RE.search(t)
    if m:
        et = m.group(1).lower().replace(" ", "-")
    return {
        "title": job_title,
        "company": company,
        "location": location,
        "remote_type": _remote_type(t),
        "employment_type": et,
        "experience_min_years": exp_min,
        "experience_max_years": exp_max,
        "salary_min": smin,
        "salary_max": smax,
        "salary_currency": cur,
        "requirements": _requirements_from_text(t),
        "description": t[:8000] or None,
        "apply_url": url,
        "requisition_id": None,
        "posted_at": None,
        "confidence": 0.45 if job_title else 0.25,
    }


class JobExtractor:
    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway

    async def extract(self, snap: PageSnapshot, *, task_id: str | None = None) -> tuple[JobExtraction, str]:
        """Return (extraction, method)."""
        ld = extract_from_jsonld(snap.jsonld, snap.url)
        if ld and ld.title:
            return ld, "jsonld"
        # Rules first: they are instant and precise when the page has a conventional title.
        out = JobExtraction.model_validate(heuristic_extract_from_text(snap.title, snap.text_excerpt, snap.url))
        method = "dom"
        if (out.confidence < 0.45 or not out.title) and self._gateway.role_config("extractor").provider != "heuristic":
            from ..agent.prompts import render_prompt

            inputs = {"title": snap.title, "text": snap.text_excerpt[:6000], "url": snap.url}
            system, prompt = render_prompt("extract_job", inputs)
            req = ModelRequest(task="extract_job", system=system, prompt=prompt, json_schema={}, inputs=inputs)
            try:
                model_out = await self._gateway.complete("extractor", req, JobExtraction, task_id=task_id)
                # Merge: the model may fill what rules left empty, never overwrite rule-found facts.
                for k, v in model_out.model_dump().items():
                    if k == "confidence" or v in (None, [], "", "unknown"):
                        continue
                    if getattr(out, k) in (None, [], "", "unknown"):
                        setattr(out, k, v)
                method = "dom+model"
            except Exception as exc:
                log.warning("model extraction failed, keeping heuristics: %s", exc)
        # Never let the model override authoritative JSON-LD facts when present.
        if ld:
            for k, v in ld.model_dump().items():
                if v not in (None, [], "", "unknown") and k != "confidence":
                    setattr(out, k, v)
            method = "jsonld+" + method
        if not out.apply_url:
            out.apply_url = snap.url
        # Find an apply link on the page if the extraction did not provide one.
        if out.apply_url == snap.url:
            for n in snap.nodes:
                if n.role == "link" and n.href and re.search(r"\bapply\b", n.name or "", re.I):
                    out.apply_url = _absolutize(snap.url, n.href)
                    break
        return out, method


def _absolutize(base: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, href)
