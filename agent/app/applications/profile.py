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
    middle_name: str = ""
    last_name: str = ""
    date_of_birth: str = ""  # YYYY-MM-DD; age is computed from it
    email: str = ""
    phone: str = ""
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""
    alternate_phone: str = ""
    permanent_address: str = ""  # one line, e.g. "Omaxe, Bahadurgarh, Haryana 124507, India"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Education(BaseModel):
    school: str = ""
    location: str = ""  # city/country of the school ("Delhi, India")
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
    # Expected annual salary per currency, e.g. {"INR": 1800000, "EUR": 40000, "USD": 40000}.
    # A salary question is answered in the currency it (or the job's country) names, else paused.
    salary_expectations: dict[str, float] = Field(default_factory=dict)
    # Current annual salary per currency ({"INR": 900000}): answers "current CTC" questions.
    current_salary: dict[str, float] = Field(default_factory=dict)
    # For a currency with no saved figure, convert the USD figure at an approximate rate.
    salary_convert_missing_from_usd: bool = False
    referral_source: str = ""  # answer to "How did you hear about us?"
    referral_fallback: str = ""  # used when the form has no option like referral_source
    # Topics the user confirmed experience with: "Do you have experience with Docker…?" → Yes.
    experienced_with: list[str] = Field(default_factory=list)
    agree_to_privacy_notices: bool = False  # required privacy / data-processing consent questions
    # Talent-community / newsletter / SMS-email updates / "consider me for other roles".
    marketing_opt_in: bool = False
    why_join: str = ""  # "Why do you want to join us?" / "Why <company>?"
    reason_for_leaving: str = ""


class LegalAnswers(BaseModel):
    """Legally significant declarations. Empty string means 'not provided' → always pause."""

    work_authorization: str = ""  # free text approved by the user, e.g. "Yes, authorized to work in the US"
    # Countries where the candidate already has the right to work. When set, work-authorization and
    # sponsorship questions are answered per country: Yes/No for the country named in the question
    # (or the job's country), never by reusing an answer given for another country.
    work_authorized_countries: list[str] = Field(default_factory=list)
    requires_sponsorship: str = ""  # "Yes" / "No"
    willing_to_relocate: str = ""
    disability_status: str = ""  # "Prefer not to answer" is a valid approved answer
    veteran_status: str = ""
    gender: str = ""
    ethnicity: str = ""
    criminal_history: str = ""
    over_18: str = ""
    citizenship: str = ""  # e.g. "Indian"
    relatives_at_company: str = ""  # "Do you have family/friends working here?"
    non_compete: str = ""  # "Are you subject to a non-compete / non-solicitation agreement?"
    previously_employed_here: str = ""  # "Have you ever worked for <company> before?"
    board_or_official: str = ""  # board member / director / government official / politically exposed


class CandidateProfile(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)  # e.g. ["English (fluent)", "German (basic)"]
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
        age = _age_from(self.contact.date_of_birth)
        if age is not None:
            put("contact.age", age)
        if self.employment:
            put("employment.currently_working", "Yes" if self.employment[0].current else "No")
        if self.experience_years is not None:
            put("employment.has_experience", "Yes" if self.experience_years > 0 else "No")
        c = self.contact
        put("contact.full_address", ", ".join(x for x in (c.address_line1, c.address_line2, c.city, f"{c.state} {c.postal_code}".strip(), c.country) if x and x.strip()))
        put("summary", self.summary)
        put("skills", ", ".join(self.skills))
        ey = self.experience_years
        put("experience_years", (f"{ey:g}" if isinstance(ey, float) else ey) if ey is not None else None)
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
        # "Personal website / Portfolio" with none saved: the closest real link the user has.
        best = self.links.website or self.links.portfolio or self.links.github or self.links.linkedin
        if not self.links.website and best:
            put("links.website", best)
        if not self.links.portfolio and (self.links.github or self.links.website):
            put("links.portfolio", self.links.github or self.links.website)
        put("languages", ", ".join(self.languages))
        for k, v in self.preferences.model_dump().items():
            if isinstance(v, dict):
                for cur, amount in v.items():
                    put(f"preferences.{k}.{str(cur).upper()}", f"{amount:.0f}" if isinstance(amount, (int, float)) else amount)
            elif isinstance(v, list):
                put(f"preferences.{k}", ", ".join(v))
            else:
                put(f"preferences.{k}", v)
        for k, v in self.legal.model_dump().items():
            put(f"legal.{k}", ", ".join(str(x).strip() for x in v if str(x).strip()) if isinstance(v, list) else v)
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


def _age_from(dob: str) -> int | None:
    from datetime import date

    m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$", dob or "")
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    today = date.today()
    return today.year - y - ((today.month, today.day) < (mo, d))


LEGAL_KEYS = {k for k in LegalAnswers.model_fields}

_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "United States": ("united states", "u.s.", "usa", "u.s.a", " us ", "america", "h-1b", "h1b", "green card", "us citizen", "san francisco", "new york", "seattle", "sunnyvale", "atlanta", "boston", "austin", "chicago", "los angeles", "indianapolis", "redlands"),
    "India": ("india", "bengaluru", "bangalore", "gurugram", "gurgaon", "mumbai", "pune", "hyderabad", "chennai", "noida", "new delhi", "delhi", "mohali", "chandigarh", "punjab", "haryana", "karnataka", "maharashtra", "telangana", "tamil nadu", "kerala", "kolkata", "ahmedabad", "jaipur", "indore", "kochi", "cochin", "trivandrum", "thiruvananthapuram", "coimbatore", "bhubaneswar", "lucknow", "nagpur", "vadodara", "surat", "visakhapatnam", "vizag", "mysuru", "mysore", "gandhinagar", "faridabad", "ghaziabad", "navi mumbai", "thane", "bhopal", "goa", "dehradun", "20b9", "inr", "lpa", "lakh"),
    "United Kingdom": ("united kingdom", " uk ", "u.k.", "britain", "england", "london", "manchester", "edinburgh", "cambridge, uk"),
    "Canada": ("canada", "toronto", "vancouver", "montreal"),
    "Germany": ("germany", "deutschland", "berlin", "munich", "münchen", "düsseldorf", "dusseldorf", "hamburg", "frankfurt", "cologne"),
    "Ireland": ("ireland", "dublin"),
    "Australia": ("australia", "sydney", "melbourne"),
    "Netherlands": ("netherlands", "holland", "amsterdam", "rotterdam"),
    "France": ("france", "paris"),
    "Spain": ("spain", "madrid", "barcelona"),
    "Switzerland": ("switzerland", "zurich", "zürich", "geneva"),
    "Sweden": ("sweden", "stockholm"),
    "Poland": ("poland", "warsaw", "krakow", "kraków"),
    "Japan": ("japan", "tokyo"),
    "Singapore": ("singapore",),
    "United Arab Emirates": ("united arab emirates", "uae", "dubai", "abu dhabi"),
    "European Union": ("european union", " eu ", "eea"),
}


def countries_in(text: str) -> set[str]:
    """Countries named in a question or a job location (aliases like 'H-1B' imply the US)."""
    t = f" {(text or '').lower()} "
    t = re.sub(r"[^a-z0-9.\s-]", " ", t)  # "the EU?*" → " the eu  " (dots kept for "u.s.")
    t = re.sub(r"\b(eu|uk|usa)\.", r"\1 ", t)  # sentence-final "the EU."
    return {country for country, aliases in _COUNTRY_ALIASES.items() if any(a in t for a in aliases)}


def country_answer(category: str, question: str, authorized: list[str], job_location: str | None, outside_answer: str) -> str | None:
    """Per-country answer for work-authorization / sponsorship questions, or None when unknown.

    * work_authorization: 'Yes' when the country (from the question, else the job location) is one
      the candidate is authorized in, 'No' otherwise.
    * requires_sponsorship: 'No' inside an authorized country, else the candidate's own answer for
      countries where they lack work rights (``outside_answer``).
    """
    if not authorized:
        return None
    named = countries_in(question) or countries_in(job_location or "")
    if not named:
        return None
    inside = bool(named & {c.strip() for c in authorized})
    if category == "work_authorization":
        return "Yes" if inside else "No"
    if category == "requires_sponsorship":
        return "No" if inside else (outside_answer or None)
    return None

_CURRENCY_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("INR", re.compile(r"₹|\binr\b|rupee|\blpa\b|lakh|\bctc\b", re.I)),
    ("EUR", re.compile(r"€|\beur\b|euro", re.I)),
    ("GBP", re.compile(r"£|\bgbp\b|pound", re.I)),
    ("CAD", re.compile(r"\bcad\b|c\$", re.I)),
    ("AUD", re.compile(r"\baud\b|a\$", re.I)),
    ("SGD", re.compile(r"\bsgd\b|s\$", re.I)),
    ("CHF", re.compile(r"\bchf\b", re.I)),
    ("USD", re.compile(r"\$|\busd\b|dollar", re.I)),
]
_COUNTRY_CURRENCY = {"India": "INR", "United States": "USD", "United Kingdom": "GBP", "Germany": "EUR", "Ireland": "EUR", "Netherlands": "EUR", "France": "EUR", "Spain": "EUR", "European Union": "EUR", "Canada": "CAD", "Australia": "AUD", "Singapore": "SGD", "United Arab Emirates": "AED", "Switzerland": "CHF", "Sweden": "SEK", "Poland": "PLN", "Japan": "JPY"}
# Approximate units per 1 USD, only for converting the user's USD figure when they asked for it.
USD_RATES = {"CAD": 1.37, "AUD": 1.52, "SGD": 1.34, "AED": 3.67, "CHF": 0.88, "SEK": 10.5, "PLN": 4.0, "JPY": 150.0, "GBP": 0.78, "EUR": 0.92, "INR": 84.0}


def salary_currency(question: str, job_location: str | None) -> str | None:
    """Currency a salary question expects: named in the question, else the job country's."""
    for cur, pat in _CURRENCY_HINTS:
        if pat.search(question or ""):
            return cur
    named = countries_in(job_location or "")
    curs = {_COUNTRY_CURRENCY[c] for c in named if c in _COUNTRY_CURRENCY}
    return curs.pop() if len(curs) == 1 else None


_LEGAL_PATTERNS: dict[str, re.Pattern[str]] = {
    # Checked first: "Do you require a visa / work permit…" asks about NEEDING sponsorship (Yes when
    # not authorized), the opposite polarity of "Are you authorized to work…".
    "requires_sponsorship": re.compile(r"sponsor(ship)?|\b(require|need)s?\b[^?]{0,40}\b(visa|work permit|permit)\b|\bvisa\b", re.I),
    "work_authorization": re.compile(r"(legally )?(authori[sz]ed|eligible|permitted|right) to work|work (authori[sz]ation|permit|eligibility)|employment eligib", re.I),
    "willing_to_relocate": re.compile(r"relocat", re.I),
    "disability_status": re.compile(r"disabilit", re.I),
    "veteran_status": re.compile(r"veteran|military", re.I),
    "gender": re.compile(r"\bgender\b|\bsex\b", re.I),
    "ethnicity": re.compile(r"ethnicit|race\b|racial|hispanic", re.I),
    "criminal_history": re.compile(r"convict|criminal|felony|background check", re.I),
    "non_compete": re.compile(r"non-?compet|non-?solicit|restrictive covenant", re.I),
    "previously_employed_here": re.compile(r"(previously|currently|ever),? (worked|been employed|work) (at|for|with|by)\b|former employee|currently (employed|working) with\b|(been |previously |ever )?associated with\b|ex-?employee|(have you|did you) (ever |previously )?(worked|been employed) (for|at|with)\b|(currently|previously|ever) (work(ed)?|employed) (for|at|by)\b|former employee|worked here before|rehire", re.I),
    "board_or_official": re.compile(r"board (member|of directors)|\bdirector of\b|officer of (a|any) company|government (official|employee)|public official|politically exposed|\bPEP\b", re.I),
    "relatives_at_company": re.compile(r"(personal|intimate|close) relationship|business ties|shared financial interests|(connected|acquainted|associated) with (any )?(associates?|employees?|staff|people|anyone|someone)|know (anyone|someone) (who )?(works|working|employed|at)|(relatives?|family( members?)?|friends?|related to)\b.{0,60}\b(work|employ|current)|(know|related to) (anyone|someone) (who )?(works|at)", re.I),
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
