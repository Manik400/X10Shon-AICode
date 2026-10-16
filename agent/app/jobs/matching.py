"""Job filters (deterministic, mandatory) and scoring (deterministic + optional model ranking).

Every score comes with observable evidence. A high score is never treated as a guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

from ..core.util import now_dt, parse_iso
from ..models.base import ModelRequest
from ..models.gateway import ModelGateway
from ..models.schemas import MatchOutput


class JobFilters(BaseModel):
    """User-configured filters. All lists are case-insensitive substrings/aliases."""

    titles: list[str] = Field(default_factory=list, description="Accepted job titles or aliases; empty = any")
    keywords: list[str] = Field(default_factory=list, description="At least one must appear when non-empty")
    excluded_keywords: list[str] = Field(default_factory=list)
    min_experience_years: float | None = None
    max_experience_years: float | None = None
    locations: list[str] = Field(default_factory=list, description="Accepted locations; empty = any")
    remote_types: list[str] = Field(default_factory=list, description="Subset of remote|hybrid|onsite|unknown")
    salary_min: float | None = None
    salary_max: float | None = None
    employment_types: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    requires_sponsorship: bool | None = Field(default=None, description="True if the candidate needs sponsorship; jobs stating 'no sponsorship' are rejected")
    required_skills: list[str] = Field(default_factory=list, description="Hard requirements the description must mention")
    max_posting_age_days: int | None = None
    min_match_score: float = 0.0


@dataclass(slots=True)
class FilterResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)  # why it failed
    notes: list[str] = field(default_factory=list)  # informational

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": self.reasons, "notes": self.notes}


_NO_SPONSOR_RE = re.compile(r"(no (visa )?sponsorship|not (able to|offering) sponsor|unable to sponsor|without sponsorship|cannot sponsor|will not sponsor)", re.I)


def _has(text: str, term: str) -> bool:
    return bool(term) and re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text) is not None


def apply_filters(job: dict[str, Any], f: JobFilters) -> FilterResult:
    text = " ".join(str(job.get(k) or "") for k in ("title", "company", "location", "description")).lower() + " " + " ".join(job.get("requirements") or []).lower()
    title = (job.get("title") or "").lower()
    res = FilterResult(passed=True)

    if f.titles and not any(t.lower() in title for t in f.titles):
        res.reasons.append(f"title '{job.get('title')}' matches none of {f.titles}")
    if f.keywords and not any(_has(text, k) for k in f.keywords):
        res.reasons.append(f"none of the keywords {f.keywords} found")
    hit_ex = [k for k in f.excluded_keywords if _has(text, k)]
    if hit_ex:
        res.reasons.append(f"excluded keywords present: {hit_ex}")
    comp = (job.get("company") or "").lower()
    if comp and any(c.lower() in comp for c in f.excluded_companies):
        res.reasons.append(f"company '{job.get('company')}' is excluded")
    jmin = job.get("experience_min_years")
    if jmin is not None and f.max_experience_years is not None and jmin > f.max_experience_years:
        res.reasons.append(f"requires {jmin}+ years, above your maximum {f.max_experience_years}")
    jmax = job.get("experience_max_years")
    if jmax is not None and f.min_experience_years is not None and jmax < f.min_experience_years:
        res.reasons.append(f"targets up to {jmax} years, below your minimum {f.min_experience_years}")
    if f.remote_types and (job.get("remote_type") or "unknown") not in f.remote_types:
        res.reasons.append(f"work arrangement '{job.get('remote_type')}' not in {f.remote_types}")
    loc = (job.get("location") or "").lower()
    if f.locations and job.get("remote_type") != "remote" and not any(l.lower() in loc for l in f.locations):
        if loc:
            res.reasons.append(f"location '{job.get('location')}' not in {f.locations}")
        else:
            res.notes.append("location unknown; location filter not applied")
    smax = job.get("salary_max") or job.get("salary_min")
    if f.salary_min is not None and smax is not None and smax < f.salary_min:
        res.reasons.append(f"salary up to {smax} is below your minimum {f.salary_min}")
    if f.employment_types and job.get("employment_type") and not any(e.lower() in str(job["employment_type"]).lower() for e in f.employment_types):
        res.reasons.append(f"employment type '{job.get('employment_type')}' not in {f.employment_types}")
    if f.requires_sponsorship and _NO_SPONSOR_RE.search(text):
        res.reasons.append("posting states sponsorship is not available")
    missing = [s for s in f.required_skills if not _has(text, s)]
    if missing:
        res.reasons.append(f"required skills not mentioned: {missing}")
    if f.max_posting_age_days is not None and job.get("posted_at"):
        posted = parse_iso(str(job["posted_at"]))
        if posted and now_dt() - posted > timedelta(days=f.max_posting_age_days):
            res.reasons.append(f"posted more than {f.max_posting_age_days} days ago")
    res.passed = not res.reasons
    return res


def deterministic_score(job: dict[str, Any], profile: dict[str, Any], f: JobFilters) -> tuple[float, dict[str, Any]]:
    """Weighted keyword scoring with evidence. Range 0..1."""
    text = ((job.get("description") or "") + " " + " ".join(job.get("requirements") or [])).lower()
    title = (job.get("title") or "").lower()
    skills = [s for s in profile.get("skills", []) if s]
    matched = [s for s in skills if _has(text, s.lower()) or _has(title, s.lower())]
    skill_score = (len(matched) / len(skills)) if skills else 0.0

    pref_titles = [t.lower() for t in (profile.get("preferences", {}).get("titles") or f.titles)]
    title_score = 1.0 if any(t in title for t in pref_titles) else (0.5 if not pref_titles else 0.0)

    years = profile.get("experience_years")
    exp_score = 0.5
    jmin = job.get("experience_min_years")
    if years is not None and jmin is not None:
        exp_score = 1.0 if years >= jmin else max(0.0, 1.0 - (jmin - years) / 4.0)

    remote_pref = profile.get("preferences", {}).get("remote_types") or f.remote_types
    rt = job.get("remote_type") or "unknown"
    remote_score = 1.0 if (not remote_pref or rt in remote_pref) else (0.5 if rt == "unknown" else 0.0)

    weights = {"skills": 0.5, "title": 0.25, "experience": 0.15, "remote": 0.10}
    score = weights["skills"] * skill_score + weights["title"] * title_score + weights["experience"] * exp_score + weights["remote"] * remote_score
    evidence = {
        "skills_matched": matched,
        "skills_total": len(skills),
        "skill_score": round(skill_score, 3),
        "title_score": title_score,
        "experience_score": round(exp_score, 3),
        "remote_score": remote_score,
        "weights": weights,
    }
    return round(score, 3), evidence


class JobMatcher:
    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway

    async def score(self, job: dict[str, Any], profile: dict[str, Any], filters: JobFilters, *, use_model: bool = True, task_id: str | None = None) -> tuple[float, dict[str, Any]]:
        det, evidence = deterministic_score(job, profile, filters)
        explanation: dict[str, Any] = {"deterministic_score": det, "evidence": evidence}
        final = det
        if use_model and self._gateway.role_config("matcher").provider != "heuristic":
            from ..agent.prompts import render_prompt

            inputs = {
                "title": job.get("title"),
                "description": (job.get("description") or "")[:6000],
                "requirements": (job.get("requirements") or [])[:25],
                "skills": profile.get("skills", [])[:60],
                "experience_years": profile.get("experience_years"),
                "summary": (profile.get("summary") or "")[:1000],
            }
            system, prompt = render_prompt("match_job", inputs)
            try:
                out = await self._gateway.complete("matcher", ModelRequest(task="match_job", system=system, prompt=prompt, json_schema={}, inputs=inputs), MatchOutput, task_id=task_id)
                explanation["model"] = out.model_dump()
                final = round(0.6 * det + 0.4 * out.semantic_score, 3)
            except Exception as exc:
                explanation["model_error"] = str(exc)[:200]
        explanation["final_score"] = final
        explanation["disclaimer"] = "Score reflects keyword/semantic overlap only; it is not a qualification guarantee."
        return final, explanation
