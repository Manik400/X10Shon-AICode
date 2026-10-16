"""RecoveryManager: failure classification, retry eligibility and backoff."""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..core.actions import ActionResult, BrowserAction
from ..core.enums import RETRYABLE_CATEGORIES, ErrorCategory
from ..core.errors import FailureInfo, AgentError


@dataclass(slots=True)
class RecoveryDecision:
    retry: bool
    wait_seconds: float
    strategy: str  # retry_same | resnapshot | relocate | abort | handoff
    failure: FailureInfo


def backoff_seconds(attempt: int, *, base: float = 2.0, cap: float = 300.0, retry_after: float | None = None) -> float:
    """Exponential backoff with full jitter; honours server guidance when provided."""
    if retry_after is not None:
        return max(retry_after, 1.0)
    exp = min(cap, base * (2 ** max(0, attempt - 1)))
    return round(random.uniform(exp * 0.5, exp), 2)


class RecoveryManager:
    def __init__(self, max_attempts: int = 3):
        self._max = max_attempts

    def classify_result(self, action: BrowserAction, result: ActionResult) -> FailureInfo:
        try:
            cat = ErrorCategory(result.error_category or "UNKNOWN")
        except ValueError:
            cat = ErrorCategory.UNKNOWN
        err = AgentError(result.message or "action failed", category=cat, details={"action": action.type, "matched": result.matched_count})
        return err.to_failure()

    def decide(self, failure: FailureInfo, attempt: int) -> RecoveryDecision:
        cat = failure.category
        if cat in (ErrorCategory.AUTH_REQUIRED, ErrorCategory.HUMAN_VERIFICATION_REQUIRED, ErrorCategory.NEEDS_USER_INPUT):
            return RecoveryDecision(False, 0, "handoff", failure)
        if cat in (ErrorCategory.SUBMISSION_UNCERTAIN, ErrorCategory.SUBMISSION_REJECTED, ErrorCategory.PERMISSION_DENIED, ErrorCategory.USER_CANCELLED, ErrorCategory.SITE_UNSUPPORTED, ErrorCategory.AMBIGUOUS_TARGET, ErrorCategory.MODEL_OUTPUT_INVALID, ErrorCategory.FORM_VALIDATION_FAILED):
            return RecoveryDecision(False, 0, "abort", failure)
        if attempt >= self._max:
            return RecoveryDecision(False, 0, "abort", failure)
        if cat == ErrorCategory.ELEMENT_NOT_FOUND:
            return RecoveryDecision(True, 0.5, "resnapshot", failure)
        if cat in RETRYABLE_CATEGORIES:
            ra = failure.details.get("retry_after_seconds")
            return RecoveryDecision(True, backoff_seconds(attempt, retry_after=float(ra) if ra else None), "retry_same", failure)
        return RecoveryDecision(False, 0, "abort", failure)
