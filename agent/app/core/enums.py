"""Shared enumerations: application states, error categories, risk levels, provenance."""

from __future__ import annotations

from enum import StrEnum


class ApplicationState(StrEnum):
    DISCOVERED = "DISCOVERED"
    IMPORTED = "IMPORTED"
    SCREENING = "SCREENING"
    ELIGIBLE = "ELIGIBLE"
    SKIPPED = "SKIPPED"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    APPLIED_CONFIRMED = "APPLIED_CONFIRMED"
    APPLICATION_FAILED = "APPLICATION_FAILED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    DUPLICATE = "DUPLICATE"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset(
    {
        ApplicationState.APPLIED_CONFIRMED,
        ApplicationState.DUPLICATE,
        ApplicationState.CANCELLED,
    }
)


class ErrorCategory(StrEnum):
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    FORM_VALIDATION_FAILED = "FORM_VALIDATION_FAILED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    HUMAN_VERIFICATION_REQUIRED = "HUMAN_VERIFICATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    SITE_UNSUPPORTED = "SITE_UNSUPPORTED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    SUBMISSION_REJECTED = "SUBMISSION_REJECTED"
    DATABASE_ERROR = "DATABASE_ERROR"
    RESOURCE_LIMIT_REACHED = "RESOURCE_LIMIT_REACHED"
    USER_CANCELLED = "USER_CANCELLED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    BROWSER_DISCONNECTED = "BROWSER_DISCONNECTED"
    UNKNOWN = "UNKNOWN"


# Failures that may be retried automatically with backoff. Everything else needs a human or a
# reconciliation step first.
RETRYABLE_CATEGORIES = frozenset(
    {
        ErrorCategory.NAVIGATION_FAILED,
        ErrorCategory.MODEL_TIMEOUT,
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.DATABASE_ERROR,
        ErrorCategory.RESOURCE_LIMIT_REACHED,
        ErrorCategory.BROWSER_DISCONNECTED,
        ErrorCategory.ELEMENT_NOT_FOUND,
    }
)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Provenance(StrEnum):
    """Where a candidate answer came from, and therefore how it may be used."""

    VERIFIED_FACT = "verified_fact"  # typed by the user into the profile; auto-fill
    APPROVED_ANSWER = "approved_answer"  # explicitly approved reusable answer; auto-fill
    INFERRED = "inferred"  # derived by the model/heuristics; requires review
    MISSING = "missing"  # nothing known; requires user input
    CONTRADICTORY = "contradictory"  # conflicting sources; stop


class AuthState(StrEnum):
    AUTHENTICATED = "authenticated"
    LOGGED_OUT = "logged_out"
    NEW_ACCOUNT_FLOW = "new_account_flow"
    PASSWORD_RECOVERY = "password_recovery"
    CHALLENGE = "challenge"  # OTP / CAPTCHA / MFA / passkey
    LOCKED = "locked"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    REJECTED = "rejected"
    NOT_ATTEMPTED = "not_attempted"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
