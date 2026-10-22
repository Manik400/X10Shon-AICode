"""Semantic mapping of form fields to profile facts and approved answers.

Deterministic synonym tables first; the model only proposes mappings for fields the rules could
not place, and even then it may only reference existing fact keys / answer ids. Values are
copied from facts — a model can never invent a value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..browser.snapshot import SnapshotNode
from ..core.enums import Provenance
from .profile import _COUNTRY_CURRENCY, USD_RATES, countries_in, country_answer, is_sensitive_question, legal_category_for, salary_currency

# label regex → (profile key, category)
_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"signature|e-?sign|sign (here|your name)|type your (full )?(legal )?name", re.I), "contact.full_name", "contact"),
    (re.compile(r"alternat(e|ive) (phone|mobile|number|contact)|secondary (phone|mobile|number)|other (phone|mobile) number", re.I), "contact.alternate_phone", "contact"),
    (re.compile(r"permanent address", re.I), "contact.permanent_address", "contact"),
    (re.compile(r"(current|residential|home|mailing|present|correspondence|physical|postal|street) address|^address$|^full address|where do you live", re.I), "contact.full_address", "contact"),
    (re.compile(r"reason for (leaving|change|switch|looking)|why (are you|do you want to) (leav|chang|switch|look)|why (do you want|are you looking) (to|for) (a )?(change|new)", re.I), "preferences.reason_for_leaving", "other"),
    (re.compile(r"^why\b|why (do you want|would you like|are you interested|join|this (role|company|position|team))|what (excites|attracts|interests|draws|motivates) you|tell us why|interest(ed)? in (this|the|our) (role|position|company|team)|why (us|our company)", re.I), "preferences.why_join", "other"),
    (re.compile(r"how many years|years? of (professional |relevant |work |industry |hands-on )?experience|total experience|experience \(years\)", re.I), "experience_years", "employment"),
    (re.compile(r"middle name", re.I), "contact.middle_name", "contact"),
    (re.compile(r"date of birth|\bdob\b|birth ?date|born on", re.I), "contact.date_of_birth", "contact"),
    (re.compile(r"^(your )?age\b|\bhow old\b|\bage \(years\)|\bcurrent age\b", re.I), "contact.age", "contact"),
    (re.compile(r"(are you|currently) (currently )?(working|employed)\b|current(ly)? employment status|employment status", re.I), "employment.currently_working", "employment"),
    (re.compile(r"(do you have|have you got) (any )?(prior |previous |professional )?(work|job|industry) experience\b|^work experience\??$", re.I), "employment.has_experience", "employment"),
    (re.compile(r"citizenship|nationality", re.I), "legal.citizenship", "contact"),
    (re.compile(r"how did you (hear|find|learn|come across)|where did you (hear|find|learn|see)|source of (your )?application", re.I), "preferences.referral_source", "other"),
    (re.compile(r"language", re.I), "languages", "other"),
    (re.compile(r"^(first|given)\s*name|^first$|fname|firstname", re.I), "contact.first_name", "contact"),
    (re.compile(r"^(last|family|sur)\s*name|^last$|lname|lastname|preferred (last|family)\s*name|preferred surname|^surname|family name", re.I), "contact.last_name", "contact"),
    (re.compile(r"^(full\s*)?name$|\b(your|full|legal|candidate)( full)? name\b", re.I), "contact.full_name", "contact"),
    (re.compile(r"e-?mail", re.I), "contact.email", "contact"),
    (re.compile(r"^(?!.*(extension|\bext\b|device type|phone type|country code|phone code|dial))(?=.*(phone|mobile|telephone|\bcell\b))|contact (number|no\.?)|whatsapp (number|no)|^number$", re.I), "contact.phone", "contact"),
    (re.compile(r"address line ?2|\bapt\b|\bapartment\b|\bsuite\b|\bunit (no|number|#)", re.I), "contact.address_line2", "contact"),
    (re.compile(r"street|address( line ?1)?$|^address$", re.I), "contact.address_line1", "contact"),
    (re.compile(r"\bcity\b|town", re.I), "contact.city", "contact"),
    # "Country/Region" is the country; only a label without "country" is the state/province.
    (re.compile(r"^(?!.*countr)(?=.*(\bstate\b|province|region))", re.I), "contact.state", "contact"),
    (re.compile(r"zip|postal|pin ?code", re.I), "contact.postal_code", "contact"),
    (re.compile(r"country", re.I), "contact.country", "contact"),
    (re.compile(r"linkedin", re.I), "links.linkedin", "links"),
    (re.compile(r"github", re.I), "links.github", "links"),
    (re.compile(r"portfolio", re.I), "links.portfolio", "links"),
    (re.compile(r"website|personal site|url$|homepage", re.I), "links.website", "links"),
    (re.compile(r"current (company|employer)|most recent (company|employer)|^company$|^employer$|organization", re.I), "employment.current_company", "employment"),
    (re.compile(r"current (title|role|position)|job title|^title$|^role$|^position$", re.I), "employment.current_title", "employment"),
    (re.compile(r"(school|institution|university|college|campus) (location|city|country)", re.I), "education[0].location", "education"),
    (re.compile(r"school|university|college|institution", re.I), "education[0].school", "education"),
    (re.compile(r"degree|qualification", re.I), "education[0].degree", "education"),
    (re.compile(r"major|field of study|discipline", re.I), "education[0].field_of_study", "education"),
    (re.compile(r"gpa|grade", re.I), "education[0].gpa", "education"),
    (re.compile(r"years? of experience|total experience|experience \(years\)", re.I), "experience_years", "employment"),
    (re.compile(r"\bskills?\b|technologies", re.I), "skills", "skills"),
    (re.compile(r"summary|about you|tell us about yourself|bio\b", re.I), "summary", "other"),
    (re.compile(r"cover letter|motivation", re.I), "cover_letter", "other"),
    (re.compile(r"i (currently|still) work here|currently work(ing)? here|this is my current (job|role|position)|current(ly)? employed here", re.I), "employment.currently_working", "employment"),
    (re.compile(r"role description|job description|describe your (role|responsibilities|work|duties)|responsibilities|description of (your )?(role|duties|work)", re.I), "employment[0].description", "employment"),
    (re.compile(r"notice period", re.I), "preferences.notice_period", "preferences"),
    (re.compile(r"earliest start|start date|availability|available (from|to start|to join)|date (available|of joining)|joining date|when can you (start|join)", re.I), "preferences.earliest_start_date", "preferences"),
    # Expected pay only: "current salary / CTC" is a different fact and stays a question for the user.
    (_EXPECTED_SALARY_RE := re.compile(r"(expected|desired) (salary|compensation|pay|ctc)|salary (expectation|requirement)|(compensation|pay) expectation", re.I), "preferences.salary_min", "legal"),
    (re.compile(r"preferred location|location preference|^location$", re.I), "preferences.locations", "preferences"),
]

_LEGAL_TO_KEY = {
    "work_authorization": "legal.work_authorization",
    "requires_sponsorship": "legal.requires_sponsorship",
    "willing_to_relocate": "legal.willing_to_relocate",
    "disability_status": "legal.disability_status",
    "veteran_status": "legal.veteran_status",
    "gender": "legal.gender",
    "ethnicity": "legal.ethnicity",
    "criminal_history": "legal.criminal_history",
    "over_18": "legal.over_18",
    "relatives_at_company": "legal.relatives_at_company",
    "non_compete": "legal.non_compete",
    "previously_employed_here": "legal.previously_employed_here",
    "board_or_official": "legal.board_or_official",
}

_AUTOFILL_BOX_RE = re.compile(r"autofill|auto-?complete your application|apply with (your )?(resume|cv|indeed|linkedin)|easy apply", re.I)
_RESUME_RE = re.compile(r"resume|résumé|\bcv\b|curriculum", re.I)
_ATTACH_HINT_RE = re.compile(r"attach|select (a |the )?file|choose (a )?file|upload|browse|drop", re.I)
_COVER_FILE_RE = re.compile(r"cover letter", re.I)
_CONSENT_RE = re.compile(r"i (agree|consent|acknowledge|certify|confirm)|terms|privacy|gdpr|consent", re.I)
_HOW_HEARD_RE = re.compile(r"how did you (hear|find)|source", re.I)


@dataclass(slots=True)
class FieldPlan:
    node: int
    label: str
    field_type: str
    required: bool
    category: str
    profile_key: str | None = None
    answer_id: str | None = None
    value: str | None = None
    provenance: Provenance = Provenance.MISSING
    confidence: float = 0.0
    status: str = "needs_input"  # auto | needs_review | needs_input | skip | contradiction
    reason: str = ""
    options: list[str] | None = None
    legally_significant: bool = False
    resume_id: str | None = None
    current_value: str | None = None
    # Radio groups: option label → snapshot node, so the chosen option is clicked by exact node.
    option_nodes: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__slots__}  # type: ignore[attr-defined]
        d["provenance"] = self.provenance.value
        return d


@dataclass(slots=True)
class FillPlan:
    fields: list[FieldPlan] = field(default_factory=list)
    submit_node: int | None = None
    next_node: int | None = None

    @property
    def blocking(self) -> list[FieldPlan]:
        return [f for f in self.fields if f.status in ("needs_input", "contradiction") and f.required]

    @property
    def review(self) -> list[FieldPlan]:
        return [f for f in self.fields if f.status == "needs_review"]

    @property
    def auto(self) -> list[FieldPlan]:
        return [f for f in self.fields if f.status == "auto"]

    def to_dict(self) -> dict[str, Any]:
        return {"fields": [f.to_dict() for f in self.fields], "submit_node": self.submit_node, "next_node": self.next_node, "blocking": len(self.blocking), "review": len(self.review), "auto": len(self.auto)}


_MARKERS_RE = re.compile(r"(\s*[*✱†]+\s*|\s*\((required|optional|mandatory)\)\s*|\s*:\s*)+$", re.I)


def clean_label(label: str) -> str:
    """Strip required/optional markers and trailing punctuation before semantic matching."""
    return _MARKERS_RE.sub("", (label or "").strip()).strip()


def _label_of(n: SnapshotNode) -> str:
    return (n.name or n.label or n.placeholder or n.name_attr or n.id or "").strip()


def _answer_match(label: str, answers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick an approved answer whose canonical question matches the label (token overlap)."""
    lt = set(re.findall(r"[a-z0-9]+", label.lower()))
    if not lt:
        return None
    best, best_score = None, 0.0
    for a in answers:
        qt = set(re.findall(r"[a-z0-9]+", str(a.get("question", "")).lower()))
        if not qt:
            continue
        overlap = len(lt & qt) / len(qt)
        if overlap > best_score and overlap >= 0.6:
            best, best_score = a, overlap
    return best


def heuristic_map_fields(fields: list[dict[str, Any]], fact_keys: list[str], answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure function used by the heuristic provider. Returns model-schema mappings."""
    out = []
    keys = set(fact_keys)
    for f in fields:
        label = clean_label(f.get("label", ""))
        mapping: dict[str, Any] = {"node": f["node"], "profile_key": None, "answer_id": None, "value": None, "confidence": 0.0, "category": "other", "legally_significant": False, "reason": None}
        legal = legal_category_for(label)
        if legal:
            key = _LEGAL_TO_KEY[legal]
            ans = _answer_match(label, answers)
            if ans and countries_in(label) and countries_in(label) != countries_in(str(ans.get("question", ""))):
                ans = None  # an answer approved for one country never answers another country's question
            if key in keys:
                mapping.update({"category": "legal", "legally_significant": True, "profile_key": key, "confidence": 0.9, "reason": f"legal:{legal}"})
            elif ans:
                # An approved answer for this exact question is explicit user approval.
                mapping.update({"category": "legal", "legally_significant": True, "answer_id": ans["id"], "value": ans["answer"], "confidence": 0.9, "reason": f"legal:{legal} approved answer"})
            else:
                mapping.update({"category": "legal", "legally_significant": True, "profile_key": None, "confidence": 0.2, "reason": f"legal:{legal}"})
            out.append(mapping)
            continue
        if f.get("type") == "file":
            mapping.update({"category": "upload", "confidence": 0.9 if _RESUME_RE.search(label) else 0.4, "reason": "resume upload" if _RESUME_RE.search(label) else "file upload"})
            out.append(mapping)
            continue
        ans = _answer_match(label, answers)
        if f.get("type") == "checkbox" and _CONSENT_RE.search(label):
            if ans and str(ans.get("category")) == "consent":
                mapping.update({"category": "consent", "answer_id": ans["id"], "value": ans["answer"], "confidence": 0.9, "reason": "approved consent answer"})
            else:
                mapping.update({"category": "consent", "confidence": 0.8, "reason": "consent checkbox"})
            out.append(mapping)
            continue
        if ans:
            mapping.update({"category": "question", "answer_id": ans["id"], "value": ans["answer"], "confidence": 0.85, "reason": "approved answer"})
            out.append(mapping)
            continue
        for pat, key, cat in _RULES:
            if pat.search(label):
                mapping.update({"category": cat, "profile_key": key if key in keys else None, "confidence": 0.85 if key in keys else 0.3, "reason": f"rule:{pat.pattern[:30]}"})
                break
        out.append(mapping)
    return out


_MARKETING_RE = re.compile(r"stay in (the know|touch)|newsletter|marketing|keep me (informed|updated|posted)|talent (community|network|pool)|job alerts|receive (updates|news|communications|sms|text|emails?)|subscribe|\bsms\b|text messages?|whatsapp|consider(ed)? (me |you )?for (other|future|similar) (roles|positions|opportunities|openings)|future (job )?(opportunities|openings|roles)|keep my (data|details|information|profile|cv|resume)|contact me (about|regarding|for) (other |future |new |relevant )?(job )?(opportunities|roles|positions|openings)", re.I)


def _opt_in_answer(facts: dict[str, tuple[str, Provenance]]) -> str:
    return "Yes" if facts.get("preferences.marketing_opt_in", ("No", None))[0] == "Yes" else "No"
_RESIDENCE_RE = re.compile(r"\b(are you|do you) (currently )?(based|located|living|live|residing|reside)\b|\bcurrently (based|located|living|reside|residing) in\b", re.I)
_WHICH_RE = re.compile(r"^(in )?(which|what|where)\b|\b(in )?which (country|city|state|location)\b", re.I)  # asks for a place, not Yes/No
_INDIA_PLACES_RE =re.compile(r"\b(india|gurugram|gurgaon|delhi|ncr|noida|bengaluru|bangalore|mumbai|pune|hyderabad|chennai|kolkata|ahmedabad|jaipur|chandigarh)\b", re.I)
_ARRANGEMENT_KINDS = {
    "onsite": re.compile(r"on-?site|in[- ]office|in[- ]person|(work|be) (from|in|at) (the |our )?office|\d\s*days? (a|per|in the) week|commute", re.I),
    "hybrid": re.compile(r"hybrid", re.I),
    "remote": re.compile(r"remote(ly)?|work from home|wfh", re.I),
}
_ARRANGEMENT_RE = re.compile("|".join(p.pattern for p in _ARRANGEMENT_KINDS.values()), re.I)
_WILLING_RE = re.compile(r"\b(able|willing|comfortable|open|okay|ok|agree|happy|prepared|can you|could you|would you|are you fine|requires?|model|expected)\b", re.I)


_WIDGET_ARTEFACT_RE = re.compile(r"^(items selected|options expanded|\d+ (items?|options?) selected)$", re.I)
_SEARCH_BOX_RE = re.compile(r"^(search|filter|type to (search|filter)|find a|search by)\b", re.I)
# Privacy notices and the usual declarations ("I hereby declare the above is true", "I have read
# and agree to the terms"): agreed to automatically when the user chose to always agree.
_PRIVACY_CONSENT_RE = re.compile(r"privacy (notice|policy|statement)|personal data|data (protection|processing|privacy)|\bgdpr\b|processing of (my|your) (personal )?data|applicant privacy|confirm (that )?you have read|\bhereby\b|i (declare|certify|attest|affirm)|true (and|&) (correct|complete|accurate)|best of my knowledge|terms (and|&) conditions|i have read|i agree to", re.I)
_HAS_EXPERIENCE_RE = re.compile(r"\b(do|have) you (have )?(an? |any |good |solid )?(hands-on|practical |professional |working )?(experience|understanding|knowledge|familiarity|exposure|worked)\b", re.I)


def _mentions_topic(label: str, topics: str) -> bool:
    low = label.lower()
    return any(t.strip() and re.search(r"(?<![a-z])" + re.escape(t.strip().lower()) + r"(?![a-z])", low) for t in topics.split(","))


def _agree_option(options: list[str]) -> str | None:
    """The 'I agree / Yes / I accept' option of a consent question (never 'I do not agree')."""
    for o in options:
        if re.search(r"\b(not|don['’]?t|disagree|decline|no)\b", o, re.I):
            continue
        if re.search(r"^yes\b|^confirm\b|\bi (agree|accept|consent|acknowledge|confirm)|\bagree\b|\baccept", o, re.I):
            return o
    return None


_REFERRAL_SYNONYMS = {
    "google": re.compile(r"\bgoogle\b", re.I),
    # Only options that really mean the employer's own site; never "Google", "Other"…
    "website": re.compile(r"\bwebsite\b|career(s)? (site|page|website|portal)|company (site|page|website)|\bcareers page\b", re.I),
    "linkedin": re.compile(r"linkedin", re.I),
    "job board": re.compile(r"job board|indeed|glassdoor|naukri|monster|job site|online job", re.I),
}


def _closest_referral(options: list[str], value: str) -> str | None:
    """Option meaning the same source as ``value`` (e.g. "Company website" → "Careers Page")."""
    v = value.lower()
    for key, pat in _REFERRAL_SYNONYMS.items():
        if key in v or pat.search(v):
            hit = next((o for o in options if pat.search(o)), None)
            if hit:
                return hit
    return None


_GRADUATED_RE = re.compile(r"have you (already )?graduated|graduated yet|completed your (degree|graduation)|are you a (recent )?graduate", re.I)


def _date_passed(text: str) -> bool | None:
    """Whether a 'YYYY-MM' / 'YYYY' / 'MM/YYYY' date lies in the past."""
    from datetime import date

    m = re.search(r"(\d{4})(?:[-/](\d{1,2}))?", text or "") or None
    m2 = re.search(r"(\d{1,2})/(\d{4})", text or "")
    if m2:
        year, month = int(m2.group(2)), int(m2.group(1))
    elif m:
        year, month = int(m.group(1)), int(m.group(2) or 12)
    else:
        return None
    today = date.today()
    return (year, month) <= (today.year, today.month)


def _eeo_category_from_options(options: list[str] | None) -> str | None:
    """EEO category implied by a question's options when its wording is vague."""
    if not options:
        return None
    low = {o.strip().lower() for o in options}
    if {"male", "female"} <= low or {"man", "woman"} <= low:
        return "gender"
    if any("hispanic" in o or "latino" in o for o in low) or any("asian" in o for o in low) and any("white" in o for o in low):
        return "ethnicity"
    if any("veteran" in o for o in low):
        return "veteran_status"
    if any("disabilit" in o for o in low):
        return "disability_status"
    return None


def _mentions_home(label: str, home: str) -> bool:
    if home.strip().lower() == "india":
        return bool(_INDIA_PLACES_RE.search(label))
    return home.strip().lower() in label.lower()


_DIAL_CODE_RE = re.compile(r"country code|dial(ing)? code|phone (country|prefix)|calling code|^\+\d{1,3}$|^[a-z]{2} \+\d{1,3}$", re.I)


def _phone_for_field(phone: str, n: SnapshotNode, nodes: list[SnapshotNode]) -> str:
    """Full international number by default; the national number (e.g. 9315605606) when the
    form takes the country code separately or the box only fits the national digits."""
    digits = re.sub(r"\D", "", phone)
    national = digits[2:] if digits.startswith("91") and len(digits) == 12 else digits[-10:] if len(digits) > 10 else digits
    if len(national) == len(digits):
        return phone
    separate_code = any(
        x.i != n.i and abs(x.i - n.i) <= 4 and (_DIAL_CODE_RE.search(x.name or "") or _DIAL_CODE_RE.search(x.placeholder or ""))
        for x in nodes
    )
    short_box = bool(n.maxlength and n.maxlength < len(phone.replace(" ", "")))
    local_placeholder = bool(n.placeholder and re.search(r"\d", n.placeholder) and "+" not in n.placeholder)
    return national if (separate_code or short_box or local_placeholder) else phone


def _choose_range(options: list[str], amount: float) -> str | None:
    """Option of a salary-range dropdown containing ``amount`` ("€40,000 - €50,000", "40k-50k",
    "Up to 30.000 €", "100,000+")."""

    def nums(text: str) -> list[float]:
        # "5-10 LPA" / "40k-50k": a unit written once applies to every number of the option.
        found: list[tuple[float, str]] = []
        for m in re.finditer(r"(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:\.\d+)?)\s*(k|m|lpa|lakhs?|lacs?|cr(?:ore)?s?|l\b)?", text, re.I):
            raw, unit = m.group(1), (m.group(2) or "").lower()
            v = float(re.sub(r"[.,\s]", "", raw)) if re.search(r"[.,\s]\d{3}$", raw) else float(raw)
            found.append((v, unit))
        shared = next((u for _, u in reversed(found) if u), "")
        scale = {"k": 1e3, "m": 1e6, "lpa": 1e5, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5, "cr": 1e7, "crore": 1e7, "crores": 1e7}
        return [v * scale.get(u or shared, 1) for v, u in found]

    singles: list[tuple[float, str]] = []
    for o in options:
        low = o.lower()
        v = nums(o)
        if len(v) >= 2 and v[0] <= amount <= v[1]:
            return o
        if len(v) == 1:
            if re.search(r"up to|under|less than|below|<", low) and amount <= v[0]:
                return o
            if re.search(r"\+|above|over|more than|or more|>", low) and amount >= v[0]:
                return o
            singles.append((v[0], o))
    # Lists of single amounts ("35,000€", "40,000€"): the exact one, else the nearest at or above.
    exact = [o for val, o in singles if val == amount]
    if exact:
        return exact[0]
    above = sorted((val, o) for val, o in singles if val > amount)
    return above[0][1] if above else None


def _languages_answer(value: str, label: str, options: list[str] | None) -> str | None:
    """Languages for a language question: only fluent ones when it asks about fluency; with known
    options, each matched to an option (one per line, for multi-select dropdowns)."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if "fluent" in label.lower() and any("(" in p for p in parts):
        parts = [p for p in parts if "fluent" in p.lower() or "native" in p.lower()]
    names = [re.sub(r"\s*\(.*?\)\s*", "", p).strip() for p in parts]
    if not options:
        return ", ".join(names) if names else None
    matched = [o for o in (_choose_option(options, nm) for nm in names) if o]
    return "\n".join(dict.fromkeys(matched)) if matched else None


_DATE_KEYS = {"contact.date_of_birth", "education[0].start_date", "education[0].end_date", "employment[0].start_date", "employment[0].end_date"}
_GENERIC_DATE_RE = re.compile(r"^(start|from|begin|joining|end|to|until|finish|completion|graduation|passing|passout)( (date|month|year|month/year))?\b|^(date|month|year)( of)? (start|joining|end|completion|graduation|passing)", re.I)


def _section_date_key(label: str, section: str) -> str | None:
    """Profile key for a bare "Start date" / "End date" from the block it sits in."""
    if not _GENERIC_DATE_RE.search(label):
        return None
    where = section.lower()
    end = bool(re.search(r"^(end|to|until|finish|completion|graduation|passing|passout)|(end|completion|graduation|passing)\b", label, re.I))
    if re.search(r"educat|school|college|universit|degree|qualification|academic", where) or re.search(r"graduation|passing|passout", label, re.I):
        return "education[0].end_date" if end else "education[0].start_date"
    if re.search(r"work|employ|experience|company|job|position|career", where):
        return "employment[0].end_date" if end else "employment[0].start_date"
    return None


def _date_for_field(iso_or_text: str, label: str, input_type: str | None, placeholder: str | None) -> str:
    """A stored date (YYYY-MM-DD or YYYY-MM) in the format the field asks for."""
    m = re.match(r"^\s*(\d{4})-(\d{1,2})(?:-(\d{1,2}))?\s*$", iso_or_text or "")
    if not m:
        return iso_or_text
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
    hint = f"{label} {placeholder or ''}".lower()
    if input_type == "date":
        return f"{y:04d}-{mo:02d}-{d:02d}"
    if input_type == "month":
        return f"{y:04d}-{mo:02d}"
    if "mm/dd/yyyy" in hint:
        return f"{mo:02d}/{d:02d}/{y:04d}"
    if "yyyy-mm-dd" in hint:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    if "dd/mm/yyyy" in hint or "dd-mm-yyyy" in hint or "dd.mm.yyyy" in hint:
        sep = "/" if "dd/mm" in hint else "-" if "dd-mm" in hint else "."
        return f"{d:02d}{sep}{mo:02d}{sep}{y:04d}"
    if re.search(r"mm/yyyy|month/year|mm-yyyy", hint):
        return f"{mo:02d}/{y:04d}"
    if re.search(r"\byyyy\b|\byear\b", hint) and not re.search(r"\bdd\b|\bday\b|\bmm\b|month", hint):
        return f"{y:04d}"
    if "dd-mm-yyyy" in hint:
        return f"{d:02d}-{mo:02d}-{y:04d}"
    return f"{d:02d}/{mo:02d}/{y:04d}"  # India / most forms: DD/MM/YYYY


def _nearest_monday():  # type: ignore[no-untyped-def]
    """The coming Monday: an "as soon as possible" start that a hiring team can plan around."""
    from datetime import date, timedelta

    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def _start_date_text(label: str, input_type: str | None) -> str:
    """"As soon as possible" as a concrete date: the nearest Monday, in the field's format."""
    start = _nearest_monday()
    if input_type == "date":
        return start.isoformat()
    low = label.lower()
    if "dd/mm/yyyy" in low or "dd-mm-yyyy" in low:
        return start.strftime("%d/%m/%Y" if "/" in low else "%d-%m-%Y")
    if "mm/dd/yyyy" in low:
        return start.strftime("%m/%d/%Y")
    if "yyyy-mm-dd" in low:
        return start.isoformat()
    return start.strftime("%d %B %Y") + " (available immediately)"


def _salary_text(amount: float, currency: str, *, numeric: bool) -> str:
    """'40,000 EUR per year' for text boxes, '40000' for number inputs; INR also in LPA."""
    if numeric:
        return f"{amount:.0f}"
    if currency == "INR" and amount >= 100_000:
        lpa = amount / 100_000
        return f"{lpa:g} LPA (INR {amount:,.0f} per year)"
    return f"{amount:,.0f} {currency} per year"


# Same meaning, different wording across sites: (what the answer looks like, options to try in order).
_EQUIVALENTS: list[tuple[re.Pattern[str], list[re.Pattern[str]]]] = [
    (re.compile(r"\bb\.?\s?tech\b|bachelor of technology", re.I), [re.compile(r"\bb\.?\s?tech\b|bachelor of technology", re.I), re.compile(r"\bb\.?\s?e\b\.?|bachelor of engineering", re.I), re.compile(r"\bgraduate\b|under-?graduate|\bug\b|bachelor", re.I), re.compile(r"\bbachelor", re.I), re.compile(r"under ?graduate|\bgraduate\b|\bdegree\b", re.I)]),
    (re.compile(r"information technology|^it$", re.I), [re.compile(r"information technology", re.I), re.compile(r"(^|\W)it(\W|$)", re.I)]),
    (re.compile(r"guru gobind singh indraprastha|ggsipu?|\bipu\b|indraprastha university", re.I), [re.compile(r"guru gobind singh|ggsipu?|\bipu\b|indraprastha", re.I)]),
    (re.compile(r"\bgurugram\b|\bgurgaon\b", re.I), [re.compile(r"\bgurugram\b", re.I), re.compile(r"\bgurgaon\b", re.I)]),
]


def _equivalent_option(options: list[str], value: str) -> str | None:
    for when, tries in _EQUIVALENTS:
        if when.search(value):
            for pat in tries:
                hit = next((o for o in options if pat.search(o)), None)
                if hit:
                    return hit
    return None


def _choose_option(options: list[str] | None, value: str) -> str | None:
    chosen = _choose_option_exactish(options, value)
    if chosen is None and options:
        chosen = _equivalent_option(options, value)
    return chosen


def _choose_option_exactish(options: list[str] | None, value: str) -> str | None:
    if not options:
        return value
    v = " ".join(value.strip().lower().split())
    for o in options:
        if " ".join(o.strip().lower().split()) == v:
            return o
    yes_no = {"yes": ("yes", "y", "true"), "no": ("no", "n", "false")}
    for canon, aliases in yes_no.items():
        if v in aliases:
            for o in options:
                if _starts_with_word(o, canon):
                    return o
            if canon == "no":
                # EEO-style negatives: "I am not a protected veteran", "I don't have a disability".
                negatives = [o for o in options if re.search(r"\b(not|don['’]?t|do not|no)\b", o, re.I) and not _DECLINE_RE.search(o)]
                if len(negatives) == 1:
                    return negatives[0]
            return None
    if _DECLINE_RE.search(v):
        for o in options:
            if _DECLINE_RE.search(o.lower()):
                return o
        return None
    # The option begins with the whole answer ("India" → "India +91", "Gurugram" → "Gurugram, Haryana").
    # Never a match somewhere inside an option: "India" must not pick "British Indian Ocean Territory".
    for o in options:
        if v and _starts_with_word(o, v):
            return o
    return None


def _starts_with_word(option: str, value: str) -> bool:
    o = " ".join(option.strip().lower().split())
    v = " ".join(value.strip().lower().split())
    return bool(v) and o.startswith(v) and (len(o) == len(v) or not o[len(v)].isalnum())


# "Decline to self-identify" in all its spellings across ATSs.
_DECLINE_RE = re.compile(r"declin|prefer not|rather not|do(n['’]?t| not) (wish|want) to|not (to )?(answer|disclose|say|specify)|choose not", re.I)



_ANY_LOCATION_RE = re.compile(r"\b(any|anywhere|all|open|flexible|no preference|pan[- ]india|remote)\b", re.I)
_LOCATION_ORDER = [re.compile(r"bengaluru|bangalore", re.I), re.compile(r"hyderabad", re.I), re.compile(r"gurugram|gurgaon", re.I)]
_IMMEDIATE_RE = re.compile(r"immediate|right away|\b0 days?\b|^0\b|join(ing)? now|no notice", re.I)


def _first_number(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(m.group(0)) if m else None


def _range_of(text: str) -> tuple[float, float] | None:
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text or "")]
    if not nums:
        return None
    if re.search(r"\+|or more|and above|above|more than|over", text, re.I):
        return (nums[0], float("inf"))
    if re.search(r"less than|under|below|up to", text, re.I) and len(nums) == 1:
        return (0.0, nums[0])
    return (nums[0], nums[1]) if len(nums) >= 2 else (nums[0], nums[0])


_CURRENT_SALARY_RE = re.compile(r"current (ctc|salary|compensation|pay|package|annual)|present (ctc|salary)|ctc \(fixed\)|existing (ctc|salary)", re.I)


def _assign_month_year_boxes(plan: FillPlan, nodes: list[SnapshotNode], facts: dict[str, tuple[str, Provenance]]) -> None:
    """Workday splits dates into bare "Month" and "Year" boxes: within a work block the first
    Month/Year pair is the start date and the second the end date (left empty for the current
    job); within an education block likewise from the education entry."""
    by_node = {n.i: n for n in nodes}
    bare = [f for f in plan.fields if clean_label(f.label).lower() in ("month", "year") and f.status != "auto"]
    if not bare:
        return
    has_job_fields = any(re.search(r"job title|company|employer", clean_label(f.label), re.I) for f in plan.fields)

    def block(f: FieldPlan) -> str:
        sec = (by_node.get(f.node).section if by_node.get(f.node) else "") or ""
        if re.search(r"educat|school|universit|degree|academic", sec, re.I):
            return "education"
        if re.search(r"work|employ|experience|job|position|company", sec, re.I) or has_job_fields:
            return "employment"
        return ""

    for kind in ("employment", "education"):
        fields = [f for f in bare if block(f) == kind]
        if not fields:
            continue
        start = facts.get(f"{kind}[0].start_date", ("", None))[0]
        end = facts.get(f"{kind}[0].end_date", ("", None))[0]
        current = kind == "employment" and facts.get("employment.currently_working", ("", None))[0] == "Yes"
        pair_index = 0
        seen_in_pair: set[str] = set()
        for f in fields:  # document order: Month, Year, Month, Year…
            name = clean_label(f.label).lower()
            if name in seen_in_pair:
                pair_index += 1
                seen_in_pair = set()
            seen_in_pair.add(name)
            date = start if pair_index == 0 else (None if current else end)
            m = re.match(r"^\s*(\d{4})-(\d{1,2})", date or "")
            if not m:
                if pair_index == 1 and current:
                    f.status, f.reason = "skip", "end date: current job"
                continue
            f.value = m.group(2).zfill(2) if name == "month" else m.group(1)
            f.status, f.reason = "auto", f"{kind} {'start' if pair_index == 0 else 'end'} date ({name})"
            f.provenance = Provenance.VERIFIED_FACT
            f.confidence = max(f.confidence, 0.9)

def pick_by_preference(key: str, options: list[str], *, job_location: str | None, experience_years: float | None) -> str | None:
    """The user's standing preferences for list questions: work location = the job's own place,
    else Bengaluru > Hyderabad > Gurugram > any; notice period = immediate, else the shortest;
    experience = the band holding the real figure (lower bound preferred)."""
    opts = [o for o in options if o and o.strip()]
    if not opts:
        return None
    if key == "preferences.locations":
        loc = (job_location or "").lower()
        for o in opts:
            words = [w for w in re.findall(r"[a-z]+", o.lower()) if len(w) > 3]
            if words and any(w in loc for w in words):
                return o
        for pat in _LOCATION_ORDER:
            hit = next((o for o in opts if pat.search(o)), None)
            if hit:
                return hit
        return next((o for o in opts if _ANY_LOCATION_RE.search(o)), None)
    if key == "preferences.notice_period":
        hit = next((o for o in opts if _IMMEDIATE_RE.search(o)), None)
        if hit:
            return hit
        numbered = [(n, o) for o in opts if (n := _first_number(o)) is not None]
        return min(numbered)[1] if numbered else None
    if key == "experience_years" and experience_years is not None:
        bands = [(r, o) for o in opts if (r := _range_of(o)) is not None]
        holding = [(r, o) for r, o in bands if r[0] <= experience_years <= r[1]]
        if holding:
            exact = [o for r, o in holding if r[0] == experience_years]
            return exact[0] if exact else holding[0][1]
        below = [(r, o) for r, o in bands if r[0] <= experience_years]
        return max(below, key=lambda x: x[0][0])[1] if below else None
    return None

def build_fill_plan(
    nodes: list[SnapshotNode],
    facts: dict[str, tuple[str, Provenance]],
    answers: list[dict[str, Any]],
    model_mappings: list[dict[str, Any]] | None,
    *,
    resumes: list[dict[str, Any]],
    default_resume_id: str | None,
    submit_names: list[str],
    job_location: str | None = None,
) -> FillPlan:
    plan = FillPlan()
    authorized = [c.strip() for c in (facts.get("legal.work_authorized_countries", ("", None))[0] or "").split(",") if c.strip()]
    outside_sponsorship = (facts.get("legal.requires_sponsorship", ("", None))[0] or "")
    answers_by_id = {a["id"]: a for a in answers}
    fields = [n for n in nodes if n.role in {"textbox", "searchbox", "combobox", "listbox", "checkbox", "radio", "switch", "spinbutton", "file", "date", "slider"} and not n.disabled and n.visible]
    field_inputs = [{"node": n.i, "label": _label_of(n) + (f" ({n.section})" if n.section and n.role == "radio" else ""), "type": n.type or n.role, "options": n.options, "required": n.required} for n in fields]
    rule_maps = {m["node"]: m for m in heuristic_map_fields(field_inputs, list(facts.keys()), answers)}
    model_maps = {m["node"]: m for m in (model_mappings or [])}

    seen_radio_groups: set[str] = set()
    resume_planned = False
    # SmartRecruiters & co. show an "autofill from your resume" drop box above the form AND a
    # Resume field inside it. The resume belongs in the Resume field: the autofill box parses the
    # file and redraws the whole form (every planned node goes stale, entries get duplicated).
    file_nodes = [x for x in fields if (x.type or "") == "file" or x.role == "file"]
    has_named_resume_slot = any(_RESUME_RE.search(f"{_label_of(x)} {x.section or ''}") and not _AUTOFILL_BOX_RE.search(f"{_label_of(x)} {x.section or ''}") for x in file_nodes)
    for n in fields:
        label = _label_of(n)
        m = rule_maps.get(n.i, {})
        mm = model_maps.get(n.i)
        # Use the model mapping only when the rules found nothing and the model points at a real key/answer.
        if (not m.get("profile_key") and not m.get("answer_id")) and mm and (mm.get("profile_key") in facts or mm.get("answer_id") in answers_by_id):
            m = {**m, **{k: v for k, v in mm.items() if v is not None}, "confidence": min(float(mm.get("confidence", 0.5)), 0.7), "reason": "model:" + str(mm.get("reason") or "")}
            model_sourced = True
        else:
            model_sourced = False
        # Searchable dropdowns (react-select & co.) are <input role="combobox">: pick an option, never just type.
        # Custom dropdowns built from buttons/divs (role combobox/listbox, not a native <select>) too.
        ftype = "combobox" if n.role in ("combobox", "listbox") and n.tag != "select" else (n.type or n.role)
        # Styled uploads: a read-only "Resume*" box ("Select the attachment") + Browse button in front
        # of a hidden <input type=file>. Treat it as the resume upload; the browser attaches the file
        # to the hidden input next to it.
        attach_box = n.role == "textbox" and bool(_RESUME_RE.search(label)) and (bool(_ATTACH_HINT_RE.search(n.placeholder or "")) or len(clean_label(label)) <= 12)
        if attach_box:
            ftype = "file"
            m = {**m, "category": "upload"}
        fp = FieldPlan(node=n.i, label=label, field_type=ftype, required=n.required, category=m.get("category", "other"), options=n.options, legally_significant=bool(m.get("legally_significant")), current_value=(n.value if n.checked is None else ("Yes" if n.checked else "No")))
        fp.profile_key = m.get("profile_key")
        fp.answer_id = m.get("answer_id")
        fp.confidence = float(m.get("confidence", 0.0))
        fp.reason = str(m.get("reason") or "")

        if _WIDGET_ARTEFACT_RE.match(clean_label(label)):
            fp.status, fp.reason = "skip", "dropdown widget state, not a question"
            plan.fields.append(fp)
            continue
        if n.role in ("combobox", "searchbox", "textbox") and (_SEARCH_BOX_RE.search(clean_label(label)) or (_SEARCH_BOX_RE.search(n.placeholder or "") and not clean_label(label))):
            # The search box inside an open dropdown (SmartRecruiters "Search by country/region or
            # code") or the page's own search: not a question about the candidate.
            fp.status = "skip"
            fp.reason = "search box, not an application question"
            plan.fields.append(fp)
            continue

        # Radio groups: plan once per group.
        if n.role == "radio":
            group = f"{n.name_attr or ''}|{n.section or ''}"
            if group in seen_radio_groups:
                fp.status = "skip"
                fp.reason = "radio group handled by sibling"
                plan.fields.append(fp)
                continue
            seen_radio_groups.add(group)
            group_nodes = [x for x in fields if x.role == "radio" and f"{x.name_attr or ''}|{x.section or ''}" == group]
            fp.options = [_label_of(x) for x in group_nodes]
            fp.option_nodes = {_label_of(x): x.i for x in group_nodes}
            fp.label = n.section or label
            label = fp.label  # legal/sensitive checks below read the question, not the option text
            checked = [_label_of(x) for x in group_nodes if x.checked]
            fp.current_value = checked[0] if checked else None

        if fp.category == "upload":
            resume = next((r for r in resumes if r["id"] == default_resume_id), None) or (resumes[0] if resumes else None)
            # A bare "Attach" button is identified by its section ("Resume/CV" vs "Cover Letter").
            cover_section = bool(n.section and _COVER_FILE_RE.search(n.section) and not _RESUME_RE.search(n.section))
            named_resume = bool(_RESUME_RE.search(label)) or bool(n.section and _RESUME_RE.search(n.section) and not cover_section)
            # An unnamed slot gets the resume only if no other slot already has it (forms list
            # Resume, Cover Letter and "Other documents" as identical "Attach" buttons).
            if has_named_resume_slot and _AUTOFILL_BOX_RE.search(f"{label} {n.section or ''}"):
                fp.status, fp.reason = "skip", "autofill box; the resume goes in the Resume field"
                plan.fields.append(fp)
                continue
            is_resume = named_resume or (not _COVER_FILE_RE.search(label) and not cover_section and resume is not None and not resume_planned)
            if is_resume:
                resume_planned = True
                if resume:
                    fp.resume_id = resume["id"]
                    fp.value = resume["file_name"]
                    fp.provenance = Provenance.VERIFIED_FACT
                    fp.status = "auto"
                    fp.reason = f"resume '{resume['label']}'"
                else:
                    fp.status = "needs_input"
                    fp.reason = "no resume registered"
            else:
                fp.status = "needs_input" if n.required else "skip"
                fp.reason = "cover letter / other file upload requires user"
            plan.fields.append(fp)
            continue

        privacy_ok = facts.get("preferences.agree_to_privacy_notices", ("No", None))[0] == "Yes"
        if privacy_ok and _PRIVACY_CONSENT_RE.search(clean_label(label)) and not _MARKETING_RE.search(clean_label(label)):
            # The user agreed to required privacy / data-processing notices.
            agree = _agree_option(n.options) if n.options else "Yes"
            if agree:
                fp.value, fp.provenance, fp.status = agree, Provenance.APPROVED_ANSWER, "auto"
                fp.category, fp.confidence = "consent", max(fp.confidence, 0.9)
                fp.reason = "privacy / data-processing notice accepted (your standing consent)"
                plan.fields.append(fp)
                continue

        if _MARKETING_RE.search(clean_label(label)) and n.role in ("checkbox", "switch") and not n.options:
            # "Yes, <company> can contact me about future job opportunities": the user's own choice.
            fp.value, fp.provenance, fp.status = _opt_in_answer(facts), Provenance.VERIFIED_FACT, "auto"
            fp.category, fp.confidence = "consent", max(fp.confidence, 0.9)
            fp.reason = f"stay-in-touch / future roles: {fp.value} (your preference)"
            plan.fields.append(fp)
            continue

        if fp.category == "consent" and not (fp.answer_id and fp.answer_id in answers_by_id):
            fp.status = "needs_review"
            fp.reason = "consent checkbox requires user review (approve it once in the profile to automate)"
            plan.fields.append(fp)
            continue

        section_key = _section_date_key(clean_label(label), n.section or "")
        if section_key and section_key in facts:
            # "Start date" inside a Work / Education block is that entry's date, not availability.
            fp.profile_key, fp.confidence = section_key, max(fp.confidence, 0.9)
        if clean_label(label).lower().startswith("are you a fresher") and facts.get("employment.has_experience"):
            fp.profile_key = None
            value_override = "No" if facts["employment.has_experience"][0] == "Yes" else "Yes"
        else:
            value_override = None

        value: str | None = None
        prov = Provenance.MISSING
        legal_cat = legal_category_for(clean_label(label)) or _eeo_category_from_options(n.options)
        if legal_cat and not fp.profile_key and _LEGAL_TO_KEY.get(legal_cat) in facts and legal_cat not in ("work_authorization", "requires_sponsorship"):
            # Question wording gave nothing away ("Which of the following describes you?") but the
            # options (Male / Female / Prefer not to say) did: answer it like that EEO question.
            fp.profile_key = _LEGAL_TO_KEY[legal_cat]
            fp.category = "legal"
            fp.legally_significant = True
            fp.confidence = max(fp.confidence, 0.9)
        per_country = None
        if legal_cat in ("work_authorization", "requires_sponsorship") and authorized:
            per_country = country_answer(legal_cat, label, authorized, job_location, outside_sponsorship)
        if legal_cat in ("work_authorization", "requires_sponsorship") and authorized:
            # With a country list, only a per-country answer is trusted; unknown country → ask.
            value, prov = (per_country, Provenance.VERIFIED_FACT) if per_country else (None, Provenance.MISSING)
            fp.legally_significant = True
            if per_country:
                fp.reason = f"per-country answer ({legal_cat})"
                fp.confidence = max(fp.confidence, 0.95)  # derived from verified facts, not a fuzzy mapping
                fp.category = "legal"
        elif _EXPECTED_SALARY_RE.search(clean_label(label)) and any(k.startswith("preferences.salary_expectations.") for k in facts):
            # Currency from the question, else the dropdown's options ("€40,000 - €50,000"), else the job country.
            cur = salary_currency(label + " " + " ".join((n.options or [])[:8]), job_location)
            if not cur:
                # Nothing names a currency and the job's place is unknown: the candidate's own
                # country's currency (INR for someone in India), never a USD guess.
                cur = _COUNTRY_CURRENCY.get(str(facts.get("contact.country", ("", None))[0] or ""))
            amount = facts.get(f"preferences.salary_expectations.{cur}") if cur else None
            usd = facts.get("preferences.salary_expectations.USD")
            if cur and amount is None and usd and cur in USD_RATES and facts.get("preferences.salary_convert_missing_from_usd", ("No", None))[0] == "Yes":
                # The user asked: currencies without a saved figure are converted from the USD one.
                converted = round(float(usd[0]) * USD_RATES[cur], -3)
                amount = (f"{converted:.0f}", Provenance.VERIFIED_FACT)
            if amount and n.options:
                chosen_range = _choose_range(n.options, float(amount[0]))
                if chosen_range:
                    value, prov = chosen_range, Provenance.VERIFIED_FACT
                    fp.confidence = max(fp.confidence, 0.95)
                    fp.reason = f"salary range containing {float(amount[0]):,.0f} {cur}"
                else:
                    fp.reason = f"no option contains {float(amount[0]):,.0f} {cur}"
            elif amount:
                value, prov = _salary_text(float(amount[0]), cur, numeric=(n.type == "number")), Provenance.VERIFIED_FACT  # type: ignore[arg-type]
                fp.confidence = max(fp.confidence, 0.95)
                fp.reason = f"expected salary in {cur}"
            else:
                fp.reason = "salary currency unknown for this job" if not cur else f"no expected salary saved in {cur}"
        elif _RESIDENCE_RE.search(clean_label(label)) and not _WHICH_RE.search(clean_label(label)) and (not n.options or _choose_option(n.options, "Yes")) and facts.get("contact.country"):
            # "Are you based in / located in / living in X?": Yes only where you actually live.
            home = facts["contact.country"][0]
            value, prov = ("Yes" if _mentions_home(label, home) else "No"), Provenance.VERIFIED_FACT
            fp.confidence = max(fp.confidence, 0.9)
            fp.reason = f"you live in {home}"
        elif _ARRANGEMENT_RE.search(clean_label(label)) and _WILLING_RE.search(clean_label(label)) and facts.get("preferences.remote_types"):
            # "Are you able to work onsite 5 days / hybrid / remotely?" against your accepted arrangements.
            accepted = facts["preferences.remote_types"][0].lower()
            asked = {k for k, pat in _ARRANGEMENT_KINDS.items() if pat.search(label)}
            if asked and all(k in accepted for k in asked):
                value, prov = "Yes", Provenance.VERIFIED_FACT
                fp.confidence = max(fp.confidence, 0.9)
                fp.reason = f"you accept {', '.join(sorted(asked))} work"
        elif _HAS_EXPERIENCE_RE.search(clean_label(label)) and facts.get("preferences.experienced_with") and _mentions_topic(label, facts["preferences.experienced_with"][0]):
            # "Do you have experience with Docker/Kubernetes…?" for topics the user confirmed.
            value, prov = "Yes", Provenance.VERIFIED_FACT
            fp.confidence = max(fp.confidence, 0.9)
            fp.reason = "you confirmed experience with this"
        elif _GRADUATED_RE.search(clean_label(label)) and facts.get("education[0].end_date"):
            graduated = _date_passed(facts["education[0].end_date"][0])
            if graduated is not None:
                value, prov = ("Yes" if graduated else "No"), Provenance.VERIFIED_FACT
                fp.confidence = max(fp.confidence, 0.9)
                fp.reason = f"degree ended {facts['education[0].end_date'][0]}"
        elif _MARKETING_RE.search(clean_label(label)) and (not n.options or _choose_option(n.options, _opt_in_answer(facts))):
            # Newsletters / talent-community / SMS-email updates / other roles: the user's own
            # choice (preferences.marketing_opt_in); "No" when they never said.
            value, prov = _opt_in_answer(facts), Provenance.VERIFIED_FACT
            fp.confidence = max(fp.confidence, 0.9)
            fp.reason = f"stay-in-touch / other roles: {value} (your preference)"
        elif fp.answer_id and fp.answer_id in answers_by_id:
            a = answers_by_id[fp.answer_id]
            value = a["answer"]
            prov = Provenance(a.get("provenance", "approved_answer"))
            fp.legally_significant = fp.legally_significant or bool(a.get("legally_significant"))
        elif fp.profile_key and fp.profile_key in facts:
            value, prov = facts[fp.profile_key]

        if value_override and value is None:
            value, prov = value_override, Provenance.VERIFIED_FACT
            fp.confidence = max(fp.confidence, 0.9)
            fp.reason = "from your work experience"

        if value and fp.profile_key == "preferences.earliest_start_date" and value.strip().lower() in ("immediately", "asap", "now", "immediate"):
            value = _start_date_text(label, n.type)
        if value and fp.profile_key in _DATE_KEYS:
            value = _date_for_field(value, label, n.type, n.placeholder)

        if value and fp.profile_key == "contact.phone":
            value = _phone_for_field(value, n, nodes)

        if value and fp.profile_key == "languages":
            value = _languages_answer(value, label, n.options)
            if value is None:
                fp.reason = "none of your languages is among the options"

        sensitive = fp.legally_significant or is_sensitive_question(clean_label(label)) or fp.category == "legal"
        if value is None:
            fp.status = "needs_input" if (n.required or sensitive) else "skip"
            fp.reason = fp.reason or ("no matching profile fact" if not sensitive else "sensitive question without approved answer")
            plan.fields.append(fp)
            continue

        if (n.options is not None or n.role == "radio") and "\n" not in value:
            chosen = _choose_option(fp.options or n.options, value)
            if chosen is None and fp.profile_key == "preferences.referral_source":
                opts = fp.options or n.options or []
                chosen = _closest_referral(opts, value)
                fallback = facts.get("preferences.referral_fallback", ("", None))[0]
                if chosen is None and fallback:
                    chosen = _choose_option(opts, fallback) or _closest_referral(opts, fallback)
                    if chosen:
                        fp.reason = f"no '{value}' option; your fallback '{fallback}'"
            if chosen is None and n.role == "combobox" and n.tag == "input" and len(n.options or []) >= 25:
                # Long searchable lists (countries…) only render part of their options until you
                # type: keep the exact answer; the browser types it and must find that option.
                chosen = value
            if chosen is None:
                fp.status = "needs_review"
                fp.reason = f"value '{value}' does not match any option"
                fp.value = value
                fp.provenance = prov
                plan.fields.append(fp)
                continue
            value = chosen
        if n.role == "checkbox" and n.type == "checkbox":
            value = "Yes" if value.strip().lower() in ("yes", "true", "y", "1") else "No"

        fp.value = value
        fp.provenance = prov
        if prov in (Provenance.VERIFIED_FACT, Provenance.APPROVED_ANSWER) and not model_sourced and fp.confidence >= 0.8:
            fp.status = "auto"
        elif prov == Provenance.CONTRADICTORY:
            fp.status = "contradiction"
        else:
            fp.status = "needs_review"
            fp.reason = fp.reason or ("model-proposed mapping" if model_sourced else "low-confidence mapping")
        plan.fields.append(fp)

    plan.submit_node, plan.next_node = find_submit_and_next(nodes, submit_names)
    # One education entry and one job, like the profile: when the form already lists an entry
    # (SmartRecruiters' resume autofill adds "Edit education …" / "Edit experience …" rows), the
    # blank "add another" fields of that section stay untouched instead of creating a duplicate.
    listed = " ".join((n.name or "") for n in nodes if n.role == "button")
    has_education = bool(re.search(r"\b(edit|delete|remove) (education|school|degree)\b", listed, re.I))
    has_employment = bool(re.search(r"\b(edit|delete|remove) (experience|employment|work|job|position)\b", listed, re.I))
    _assign_month_year_boxes(plan, nodes, facts)
    exp_years = _first_number(str(facts.get("experience_years", ("", None))[0] or ""))
    current_inr = facts.get("preferences.current_salary.INR")
    for f in plan.fields:
        if f.options and current_inr and _CURRENT_SALARY_RE.search(clean_label(f.label)) and f.status in ("auto", "needs_input", "needs_review"):
            band = _choose_range(f.options, float(current_inr[0]))
            if band:
                f.value, f.status, f.reason = band, "auto", f"current salary band containing {float(current_inr[0]):,.0f} INR"
                f.provenance = Provenance.VERIFIED_FACT
                f.confidence = max(f.confidence, 0.9)
                continue
        if f.options and f.profile_key in ("preferences.locations", "preferences.notice_period", "experience_years") and f.status in ("auto", "needs_input", "needs_review"):
            pick = pick_by_preference(f.profile_key, f.options, job_location=job_location, experience_years=exp_years)
            if pick and pick != f.value:
                f.value, f.status, f.reason = pick, "auto", "preference order"
                f.provenance = Provenance.VERIFIED_FACT
                f.confidence = max(f.confidence, 0.9)
    sections = {n.i: (n.section or "") for n in nodes}
    for f in plan.fields:
        key = f.profile_key or ""
        if f.status != "auto":
            continue
        in_block = bool(re.search(r"work|employ|experience|company|job|position|career|educat|school|academic", sections.get(f.node, ""), re.I))
        if has_education and (key.startswith("education[") or (key.startswith("education") and in_block)):
            f.status, f.reason = "skip", "an education entry is already on the form"
        elif has_employment and (key.startswith("employment[") or (key.startswith("employment") and in_block)):
            f.status, f.reason = "skip", "a work-experience entry is already on the form"

    return plan


def find_submit_and_next(nodes: list[SnapshotNode], submit_names: list[str]) -> tuple[int | None, int | None]:
    """Submit / next buttons of a snapshot. Node indices are only valid for the snapshot they came
    from, so callers re-run this on a fresh snapshot right before clicking."""
    submit_node: int | None = None
    next_node: int | None = None
    buttons = [n for n in nodes if n.role == "button" and not n.disabled and n.visible]
    for b in buttons:
        nm = (b.name or "").strip().lower()
        if any(nm == s.lower() or nm.startswith(s.lower()) for s in submit_names):
            submit_node = b.i
            break
    for b in buttons:
        nm = (b.name or "").strip().lower()
        if nm in ("next", "continue", "next step", "save and continue", "save & continue") or nm.startswith("next"):
            next_node = b.i
            break
    return submit_node, next_node
