"""Application workflow state machine with an explicit transition table."""

from __future__ import annotations

from .enums import ApplicationState as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.DISCOVERED: frozenset({S.IMPORTED, S.DUPLICATE, S.CANCELLED}),
    S.IMPORTED: frozenset({S.SCREENING, S.DUPLICATE, S.CANCELLED}),
    S.SCREENING: frozenset({S.ELIGIBLE, S.SKIPPED, S.DUPLICATE, S.CANCELLED}),
    S.ELIGIBLE: frozenset({S.QUEUED, S.SKIPPED, S.SCREENING, S.CANCELLED, S.DUPLICATE}),  # SCREENING = re-screen after filter change
    S.SKIPPED: frozenset({S.ELIGIBLE, S.QUEUED, S.SCREENING, S.CANCELLED}),  # user may override a skip
    S.QUEUED: frozenset({S.IN_PROGRESS, S.CANCELLED, S.SKIPPED, S.DUPLICATE}),
    S.IN_PROGRESS: frozenset(
        {
            S.AUTH_REQUIRED,
            S.NEEDS_USER_INPUT,
            S.READY_FOR_REVIEW,
            S.SUBMISSION_PENDING,
            S.APPLICATION_FAILED,
            S.RETRY_SCHEDULED,
            S.CANCELLED,
            S.DUPLICATE,
        }
    ),
    S.AUTH_REQUIRED: frozenset({S.IN_PROGRESS, S.QUEUED, S.CANCELLED, S.APPLICATION_FAILED, S.APPLIED_CONFIRMED}),  # APPLIED_CONFIRMED: the user finished it by hand
    S.NEEDS_USER_INPUT: frozenset({S.IN_PROGRESS, S.QUEUED, S.CANCELLED, S.APPLICATION_FAILED, S.APPLIED_CONFIRMED}),  # APPLIED_CONFIRMED: the user finished it by hand
    S.READY_FOR_REVIEW: frozenset({S.SUBMISSION_PENDING, S.IN_PROGRESS, S.NEEDS_USER_INPUT, S.QUEUED, S.CANCELLED, S.APPLIED_CONFIRMED}),  # QUEUED: re-run after a rejected submit
    S.SUBMISSION_PENDING: frozenset({S.APPLIED_CONFIRMED, S.APPLICATION_FAILED, S.READY_FOR_REVIEW, S.CANCELLED}),
    S.APPLICATION_FAILED: frozenset({S.RETRY_SCHEDULED, S.QUEUED, S.CANCELLED, S.APPLIED_CONFIRMED}),  # manual reconcile may confirm
    S.RETRY_SCHEDULED: frozenset({S.QUEUED, S.IN_PROGRESS, S.CANCELLED, S.APPLICATION_FAILED}),
    S.APPLIED_CONFIRMED: frozenset(),
    S.DUPLICATE: frozenset(),
    S.CANCELLED: frozenset(),
}

# States in which the workflow is waiting on a human and must not be picked up by the scheduler.
WAITING_ON_USER = frozenset({S.AUTH_REQUIRED, S.NEEDS_USER_INPUT, S.READY_FOR_REVIEW})

# States the scheduler may pick up.
SCHEDULABLE = frozenset({S.QUEUED, S.RETRY_SCHEDULED})


class InvalidTransition(ValueError):
    def __init__(self, current: S, target: S):
        super().__init__(f"Transition {current} -> {target} is not permitted")
        self.current = current
        self.target = target


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
