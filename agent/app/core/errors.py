"""Structured errors. Every failure carries a category, a safe message and recovery guidance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import RETRYABLE_CATEGORIES, ErrorCategory


@dataclass(slots=True)
class FailureInfo:
    category: ErrorCategory
    message: str
    retryable: bool
    recommended_action: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "message": self.message,
            "retryable": self.retryable,
            "recommended_action": self.recommended_action,
            "details": self.details,
        }


_RECOMMENDATIONS: dict[ErrorCategory, str] = {
    ErrorCategory.NAVIGATION_FAILED: "Retry with backoff; check the URL and network.",
    ErrorCategory.ELEMENT_NOT_FOUND: "Re-snapshot the page and retry with a semantic locator; otherwise mark site unsupported.",
    ErrorCategory.AMBIGUOUS_TARGET: "Narrow the locator with section context or ask the user.",
    ErrorCategory.FORM_VALIDATION_FAILED: "Review the reported field errors and correct the profile data.",
    ErrorCategory.AUTH_REQUIRED: "Log in manually in the browser tab, then resume the task.",
    ErrorCategory.HUMAN_VERIFICATION_REQUIRED: "Complete the CAPTCHA/OTP/MFA challenge manually, then resume.",
    ErrorCategory.PERMISSION_DENIED: "Grant the required permission or add the domain to the allow-list.",
    ErrorCategory.MODEL_TIMEOUT: "Retry; consider a smaller model or a shorter context.",
    ErrorCategory.MODEL_OUTPUT_INVALID: "The model returned invalid output; a repair attempt failed. Review model settings.",
    ErrorCategory.RATE_LIMITED: "Wait for the server-provided retry window before retrying.",
    ErrorCategory.SITE_UNSUPPORTED: "Apply manually; the page uses an unsupported structure.",
    ErrorCategory.SUBMISSION_UNCERTAIN: "Check the site for a confirmation and reconcile the outcome before any retry.",
    ErrorCategory.SUBMISSION_REJECTED: "Read the rejection message; fix the data; do not retry blindly.",
    ErrorCategory.DATABASE_ERROR: "Retry; if persistent, check disk space and file permissions.",
    ErrorCategory.RESOURCE_LIMIT_REACHED: "Wait for resources to free up or lower the configured budgets.",
    ErrorCategory.USER_CANCELLED: "No action needed.",
    ErrorCategory.NEEDS_USER_INPUT: "Provide the missing answers in the dashboard, then resume.",
    ErrorCategory.BROWSER_DISCONNECTED: "Reconnect the extension or restart the browser, then resume.",
    ErrorCategory.UNKNOWN: "Inspect the diagnostic log.",
}


class AgentError(Exception):
    """Base error with a category. Subclasses are thin and exist for ``except`` clarity."""

    category: ErrorCategory = ErrorCategory.UNKNOWN

    def __init__(self, message: str, *, category: ErrorCategory | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        if category is not None:
            self.category = category
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES

    def to_failure(self) -> FailureInfo:
        return FailureInfo(
            category=self.category,
            message=str(self),
            retryable=self.retryable,
            recommended_action=_RECOMMENDATIONS.get(self.category, _RECOMMENDATIONS[ErrorCategory.UNKNOWN]),
            details=self.details,
        )


class NavigationError(AgentError):
    category = ErrorCategory.NAVIGATION_FAILED


class ElementNotFoundError(AgentError):
    category = ErrorCategory.ELEMENT_NOT_FOUND


class AmbiguousTargetError(AgentError):
    category = ErrorCategory.AMBIGUOUS_TARGET


class FormValidationError(AgentError):
    category = ErrorCategory.FORM_VALIDATION_FAILED


class AuthRequiredError(AgentError):
    category = ErrorCategory.AUTH_REQUIRED


class HumanVerificationError(AgentError):
    category = ErrorCategory.HUMAN_VERIFICATION_REQUIRED


class PermissionDeniedError(AgentError):
    category = ErrorCategory.PERMISSION_DENIED


class ModelTimeoutError(AgentError):
    category = ErrorCategory.MODEL_TIMEOUT


class ModelOutputInvalidError(AgentError):
    category = ErrorCategory.MODEL_OUTPUT_INVALID


class RateLimitedError(AgentError):
    category = ErrorCategory.RATE_LIMITED

    def __init__(self, message: str, *, retry_after_seconds: float | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after_seconds = retry_after_seconds
        if retry_after_seconds is not None:
            self.details["retry_after_seconds"] = retry_after_seconds


class SiteUnsupportedError(AgentError):
    category = ErrorCategory.SITE_UNSUPPORTED


class SubmissionUncertainError(AgentError):
    category = ErrorCategory.SUBMISSION_UNCERTAIN


class SubmissionRejectedError(AgentError):
    category = ErrorCategory.SUBMISSION_REJECTED


class DatabaseError(AgentError):
    category = ErrorCategory.DATABASE_ERROR


class ResourceLimitError(AgentError):
    category = ErrorCategory.RESOURCE_LIMIT_REACHED


class UserCancelledError(AgentError):
    category = ErrorCategory.USER_CANCELLED


class NeedsUserInputError(AgentError):
    category = ErrorCategory.NEEDS_USER_INPUT


class BrowserDisconnectedError(AgentError):
    category = ErrorCategory.BROWSER_DISCONNECTED


def classify_exception(exc: BaseException) -> FailureInfo:
    """Map any exception to a FailureInfo without leaking stack traces or secrets."""
    if isinstance(exc, AgentError):
        return exc.to_failure()
    if isinstance(exc, TimeoutError):
        return AgentError("Operation timed out", category=ErrorCategory.NAVIGATION_FAILED).to_failure()
    return AgentError(f"{type(exc).__name__}: {str(exc)[:300]}", category=ErrorCategory.UNKNOWN).to_failure()
