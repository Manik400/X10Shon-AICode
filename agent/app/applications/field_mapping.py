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
from .profile import is_sensitive_question, legal_category_for

# label regex → (profile key, category)
_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^(first|given)\s*name|^first$|fname|firstname", re.I), "contact.first_name", "contact"),
    (re.compile(r"^(last|family|sur)\s*name|^last$|lname|lastname", re.I), "contact.last_name", "contact"),
    (re.compile(r"^(full\s*)?name$|^your name|^legal name|^candidate name", re.I), "contact.full_name", "contact"),
    (re.compile(r"e-?mail", re.I), "contact.email", "contact"),
    (re.compile(r"phone|mobile|telephone|cell", re.I), "contact.phone", "contact"),
    (re.compile(r"address line ?2|apt|suite|unit", re.I), "contact.address_line2", "contact"),
    (re.compile(r"street|address( line ?1)?$|^address$", re.I), "contact.address_line1", "contact"),
    (re.compile(r"\bcity\b|town", re.I), "contact.city", "contact"),
    (re.compile(r"\bstate\b|province|region", re.I), "contact.state", "contact"),
    (re.compile(r"zip|postal|pin ?code", re.I), "contact.postal_code", "contact"),
    (re.compile(r"country", re.I), "contact.country", "contact"),
    (re.compile(r"linkedin", re.I), "links.linkedin", "links"),
    (re.compile(r"github", re.I), "links.github", "links"),
    (re.compile(r"portfolio", re.I), "links.portfolio", "links"),
    (re.compile(r"website|personal site|url$|homepage", re.I), "links.website", "links"),
    (re.compile(r"current (company|employer)|most recent (company|employer)|^company$|^employer$|organization", re.I), "employment.current_company", "employment"),
    (re.compile(r"current (title|role|position)|job title|^title$|^role$|^position$", re.I), "employment.current_title", "employment"),
    (re.compile(r"school|university|college|institution", re.I), "education[0].school", "education"),
    (re.compile(r"degree|qualification", re.I), "education[0].degree", "education"),
    (re.compile(r"major|field of study|discipline", re.I), "education[0].field_of_study", "education"),
    (re.compile(r"gpa|grade", re.I), "education[0].gpa", "education"),
    (re.compile(r"years? of experience|total experience|experience \(years\)", re.I), "experience_years", "employment"),
    (re.compile(r"\bskills?\b|technologies", re.I), "skills", "skills"),
    (re.compile(r"summary|about you|tell us about yourself|bio\b", re.I), "summary", "other"),
    (re.compile(r"cover letter|why (do you want|are you interested)|motivation", re.I), "cover_letter", "other"),
    (re.compile(r"notice period", re.I), "preferences.notice_period", "preferences"),
    (re.compile(r"earliest start|start date|availability|available (from|to start)", re.I), "preferences.earliest_start_date", "preferences"),
    (re.compile(r"(expected|desired|current) (salary|compensation|pay|ctc)|salary expectation", re.I), "preferences.salary_min", "legal"),
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
}

_RESUME_RE = re.compile(r"resume|résumé|\bcv\b|curriculum", re.I)
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


def _choose_option(options: list[str] | None, value: str) -> str | None:
    if not options:
        return value
    v = value.strip().lower()
    for o in options:
        if o.strip().lower() == v:
            return o
    yes_no = {"yes": ("yes", "y", "true"), "no": ("no", "n", "false")}
    for canon, aliases in yes_no.items():
        if v in aliases:
            for o in options:
                if o.strip().lower().startswith(canon):
                    return o
    for o in options:
        if v and (v in o.lower() or o.lower() in v) and len(v) >= 3:
            return o
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
) -> FillPlan:
    plan = FillPlan()
    answers_by_id = {a["id"]: a for a in answers}
    fields = [n for n in nodes if n.role in {"textbox", "searchbox", "combobox", "listbox", "checkbox", "radio", "switch", "spinbutton", "file", "date", "slider"} and not n.disabled and n.visible]
    field_inputs = [{"node": n.i, "label": _label_of(n) + (f" ({n.section})" if n.section and n.role == "radio" else ""), "type": n.type or n.role, "options": n.options, "required": n.required} for n in fields]
    rule_maps = {m["node"]: m for m in heuristic_map_fields(field_inputs, list(facts.keys()), answers)}
    model_maps = {m["node"]: m for m in (model_mappings or [])}

    seen_radio_groups: set[str] = set()
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
        fp = FieldPlan(node=n.i, label=label, field_type=n.type or n.role, required=n.required, category=m.get("category", "other"), options=n.options, legally_significant=bool(m.get("legally_significant")), current_value=(n.value if n.checked is None else ("Yes" if n.checked else "No")))
        fp.profile_key = m.get("profile_key")
        fp.answer_id = m.get("answer_id")
        fp.confidence = float(m.get("confidence", 0.0))
        fp.reason = str(m.get("reason") or "")

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
            fp.label = n.section or label
            checked = [_label_of(x) for x in group_nodes if x.checked]
            fp.current_value = checked[0] if checked else None

        if fp.category == "upload":
            resume = next((r for r in resumes if r["id"] == default_resume_id), None) or (resumes[0] if resumes else None)
            if _RESUME_RE.search(label) or (not _COVER_FILE_RE.search(label) and resume):
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

        if fp.category == "consent" and not (fp.answer_id and fp.answer_id in answers_by_id):
            fp.status = "needs_review"
            fp.reason = "consent checkbox requires user review (approve it once in the profile to automate)"
            plan.fields.append(fp)
            continue

        value: str | None = None
        prov = Provenance.MISSING
        if fp.answer_id and fp.answer_id in answers_by_id:
            a = answers_by_id[fp.answer_id]
            value = a["answer"]
            prov = Provenance(a.get("provenance", "approved_answer"))
            fp.legally_significant = fp.legally_significant or bool(a.get("legally_significant"))
        elif fp.profile_key and fp.profile_key in facts:
            value, prov = facts[fp.profile_key]

        sensitive = fp.legally_significant or is_sensitive_question(clean_label(label)) or fp.category == "legal"
        if value is None:
            fp.status = "needs_input" if (n.required or sensitive) else "skip"
            fp.reason = fp.reason or ("no matching profile fact" if not sensitive else "sensitive question without approved answer")
            plan.fields.append(fp)
            continue

        if n.options is not None or n.role == "radio":
            chosen = _choose_option(fp.options or n.options, value)
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

    # Submit / next buttons.
    buttons = [n for n in nodes if n.role == "button" and not n.disabled and n.visible]
    for b in buttons:
        nm = (b.name or "").strip().lower()
        if any(nm == s.lower() or nm.startswith(s.lower()) for s in submit_names):
            plan.submit_node = b.i
            break
    for b in buttons:
        nm = (b.name or "").strip().lower()
        if nm in ("next", "continue", "next step", "save and continue", "save & continue") or nm.startswith("next"):
            plan.next_node = b.i
            break
    return plan
