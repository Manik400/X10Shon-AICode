"""Structured output schemas for each model task. Every model answer is validated against one."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IntentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["apply", "apply_list", "extract_job", "navigate", "read", "contacts", "track", "general", "ask", "fill_page"]
    confidence: float = Field(ge=0, le=1)
    url: str | None = None
    notes: str | None = Field(default=None, max_length=300)


class AnswerOutput(BaseModel):
    """Answer to a free-form question about the current page / PDF (read-only assistant mode)."""

    model_config = ConfigDict(extra="allow")

    answer: str = Field(max_length=8000)

    def full_text(self) -> str:
        """The answer plus anything the model put in other fields (lists of skills, steps…)."""
        parts = [self.answer.strip()]
        for key, value in (self.model_extra or {}).items():
            if isinstance(value, list):
                items = "\n".join(f"- {v if not isinstance(v, dict) else ', '.join(f'{k}: {x}' for k, x in v.items())}" for v in value)
                parts.append(f"{key.replace('_', ' ').capitalize()}:\n{items}")
            elif isinstance(value, str) and value.strip():
                parts.append(f"{key.replace('_', ' ').capitalize()}: {value.strip()}")
        return "\n\n".join(p for p in parts if p)


class JobExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    company: str | None = None
    location: str | None = None
    remote_type: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    employment_type: str | None = None
    experience_min_years: float | None = None
    experience_max_years: float | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    requirements: list[str] = Field(default_factory=list, max_length=40)
    description: str | None = Field(default=None, max_length=8000)
    apply_url: str | None = None
    requisition_id: str | None = None
    posted_at: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: int
    profile_key: str | None = Field(default=None, description="Dot path into the profile facts, or null when no fact fits")
    answer_id: str | None = Field(default=None, description="Approved answer id when the field is a question")
    value: str | None = Field(default=None, description="Only values copied verbatim from facts/answers")
    confidence: float = Field(ge=0, le=1)
    category: Literal["contact", "education", "employment", "skills", "links", "preferences", "legal", "question", "upload", "consent", "other"] = "other"
    legally_significant: bool = False
    reason: str | None = Field(default=None, max_length=200)


class FieldMappingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: list[FieldMapping] = Field(max_length=120)


class MatchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_score: float = Field(ge=0, le=1)
    matched_skills: list[str] = Field(default_factory=list, max_length=30)
    missing_skills: list[str] = Field(default_factory=list, max_length=30)
    rationale: str = Field(max_length=600)


class RecoveryClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal[
        "NAVIGATION_FAILED",
        "ELEMENT_NOT_FOUND",
        "AMBIGUOUS_TARGET",
        "FORM_VALIDATION_FAILED",
        "AUTH_REQUIRED",
        "HUMAN_VERIFICATION_REQUIRED",
        "SITE_UNSUPPORTED",
        "SUBMISSION_UNCERTAIN",
        "SUBMISSION_REJECTED",
        "UNKNOWN",
    ]
    retryable: bool
    suggestion: str = Field(max_length=300)


def schema_of(model: type[BaseModel]) -> dict:
    return model.model_json_schema()
