from __future__ import annotations

from app.applications.profile import CandidateProfile, is_sensitive_question, legal_category_for
from app.core.enums import Provenance


def test_facts_are_verified_and_skip_empty(sample_profile) -> None:
    p = CandidateProfile.model_validate(sample_profile)
    facts = p.facts()
    assert facts["contact.first_name"] == ("Ada", Provenance.VERIFIED_FACT)
    assert facts["contact.full_name"][0] == "Ada Testperson"
    assert facts["legal.requires_sponsorship"][0] == "No"
    assert "legal.disability_status" not in facts  # empty → not a fact → must pause
    assert "employment.current_company" in facts


def test_completeness_reports_missing(sample_profile) -> None:
    p = CandidateProfile.model_validate({**sample_profile, "contact": {"first_name": "A"}})
    c = p.completeness()
    assert "contact.last_name" in c["missing_required"]
    assert c["score"] < 1


def test_skills_deduplicated() -> None:
    p = CandidateProfile.model_validate({"skills": ["Python", "python ", "Go"]})
    assert p.skills == ["Python", "Go"]


def test_legal_detection() -> None:
    assert legal_category_for("Are you legally authorized to work in the US?") == "work_authorization"
    assert legal_category_for("Will you require visa sponsorship?") == "requires_sponsorship"
    assert legal_category_for("Veteran status") == "veteran_status"
    assert legal_category_for("Favourite colour") is None
    assert is_sensitive_question("Expected salary (USD)")
    assert is_sensitive_question("Notice period")
    assert not is_sensitive_question("LinkedIn profile")
