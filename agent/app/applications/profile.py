"""Candidate profile model and flattening into (key → fact) with provenance.

Everything the user types in the profile is a ``verified_fact``. Facts are addressed by dot keys
(``contact.first_name``, ``education[0].school``) and mapped onto form fields.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..core.enums import Provenance


class Contact(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Education(BaseModel):
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    start_date: str = ""
    end_date: str = ""
    gpa: str = ""


class Employment(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    current: bool = False
    description: str = ""


class Project(BaseModel):
    name: str = ""
    description: str = ""
    url: str = ""


class Links(BaseModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    website: str = ""
    other: str = ""


class Preferences(BaseModel):
    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_types: list[str] = Field(default_factory=list)
    experience_level: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    notice_period: str = ""
    earliest_start_date: str = ""


class LegalAnswers(BaseModel):
    """Legally significant declarations. Empty string means 'not provided' → always pause."""

    work_authorization: str = ""  # free text approved by the user, e.g. "Yes, authorized to work in the US"
    requires_sponsorship: str = ""  # "Yes" / "No"
    willing_to_relocate: str = ""
    disability_status: str = ""  # "Prefer not to answer" is a valid approved answer
    veteran_status: str = ""
    gender: str = ""
    ethnicity: str = ""
    criminal_history: str = ""
    over_18: str = ""


class CandidateProfile(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience_years: float | None = None
    education: list[Education] = Field(default_factory=list)
    employment: list[Employment] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    links: Links = Field(default_factory=Links)
    preferences: Preferences = Field(default_factory=Preferences)
    legal: LegalAnswers = Field(default_factory=LegalAnswers)
    cover_letter_template: str = ""
    default_resume_id: str | None = None

    @field_validator("skills")
    @classmethod
    def _clean_skills(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out = []
        for s in v:
            s2 = s.strip()
            if s2 and s2.lower() not in seen:
                seen.add(s2.lower())
                out.append(s2)
        return out

    # ------------------------------------------------------------------ facts
    def facts(self) -> dict[str, tuple[str, Provenance]]:
        """Flatten to dot keys. Only non-empty values are facts."""
        out: dict[str, tuple[str, Provenance]] = {}

        def put(key: str, val: Any) -> None:
            if val is None:
                return
            s = str(val).strip() if not isinstance(val, bool) else ("Yes" if val else "No")
            if s:
                out[key] = (s, Provenance.VERIFIED_FACT)

        for k, v in self.contact.model_dump().items():
            put(f"contact.{k}", v)
        put("contact.full_name", self.contact.full_name)
        put("summary", self.summary)
        put("skills", ", ".join(self.skills))
        put("experience_years", self.experience_years)
        for i, e in enumerate(self.education):
            for k, v in e.model_dump().items():
                put(f"education[{i}].{k}", v)
        for i, e in enumerate(self.employment):
            for k, v in e.model_dump().items():
                put(f"employment[{i}].{k}", v)
        if self.employment:
            put("employment.current_company", self.employment[0].company)
            put("employment.current_title", self.employment[0].title)
        for i, p in enumerate(self.projects):
            for k, v in p.model_dump().items():
                put(f"projects[{i}].{k}", v)
        for k, v in self.links.model_dump().items():
            put(f"links.{k}", v)
        for k, v in self.preferences.model_dump().items():
            if isinstance(v, list):
                put(f"preferences.{k}", ", ".join(v))
            else:
                put(f"preferences.{k}", v)
        for k, v in self.legal.model_dump().items():
            put(f"legal.{k}", v)
        put("cover_letter", self.cover_letter_template)
        return out

    def completeness(self) -> dict[str, Any]:
        required = ["contact.first_name", "contact.last_name", "contact.email", "contact.phone", "skills", "experience_years", "education[0].school", "employment[0].company"]
        recommended = ["links.linkedin", "preferences.titles", "legal.work_authorization", "legal.requires_sponsorship", "contact.city", "contact.country", "summary"]
        facts = self.facts()
        missing_required = [k for k in required if k not in facts]
        missing_recommended = [k for k in recommended if k not in facts]
        total = len(required) + len(recommended)
        score = (total - len(missing_required) - len(missing_recommended)) / total if total else 0
        return {"score": round(score, 2), "missing_required": missing_required, "missing_recommended": missing_recommended, "has_default_resume": bool(self.default_resume_id)}


LEGAL_KEYS = {k for k in LegalAnswers.model_fields}

_LEGAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "work_authorization": re.compile(r"(legally )?(authori[sz]ed|eligible|permitted|right) to work|work (authori[sz]ation|permit|eligibility)|employment eligib", re.I),
    "requires_sponsorship": re.compile(r"sponsor(ship)?|visa", re.I),
    "willing_to_relocate": re.compile(r"relocat", re.I),
    "disability_status": re.compile(r"disabilit", re.I),
    "veteran_status": re.compile(r"veteran|military", re.I),
    "gender": re.compile(r"\bgender\b|\bsex\b", re.I),
    "ethnicity": re.compile(r"ethnicit|race\b|racial|hispanic", re.I),
    "criminal_history": re.compile(r"convict|criminal|felony|background check", re.I),
    "over_18": re.compile(r"(over|at least|above) (18|eighteen)|18 years", re.I),
}
_SALARY_PATTERN = re.compile(r"salary|compensation|pay (expectation|range)|expected (pay|salary|ctc)|\bctc\b", re.I)
_NOTICE_PATTERN = re.compile(r"notice period|earliest (start|join)|availability|available to start|start date", re.I)


def legal_category_for(label: str) -> str | None:
    for key, pat in _LEGAL_PATTERNS.items():
        if pat.search(label):
            return key
    return None


def is_sensitive_question(label: str) -> bool:
    return legal_category_for(label) is not None or bool(_SALARY_PATTERN.search(label)) or bool(_NOTICE_PATTERN.search(label))
