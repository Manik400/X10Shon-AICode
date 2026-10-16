from __future__ import annotations

import pytest

from app.core.enums import ApplicationState as S
from app.core.state_machine import TRANSITIONS, InvalidTransition, SCHEDULABLE, WAITING_ON_USER, assert_transition, can_transition


def test_every_state_has_a_row() -> None:
    assert set(TRANSITIONS) == set(S)


def test_terminal_states_have_no_exits() -> None:
    for s in (S.APPLIED_CONFIRMED, S.DUPLICATE, S.CANCELLED):
        assert TRANSITIONS[s] == frozenset()


def test_happy_path() -> None:
    path = [S.DISCOVERED, S.IMPORTED, S.SCREENING, S.ELIGIBLE, S.QUEUED, S.IN_PROGRESS, S.READY_FOR_REVIEW, S.SUBMISSION_PENDING, S.APPLIED_CONFIRMED]
    for a, b in zip(path, path[1:]):
        assert can_transition(a, b), f"{a}->{b}"


def test_invalid_transition_raises() -> None:
    with pytest.raises(InvalidTransition):
        assert_transition(S.QUEUED, S.APPLIED_CONFIRMED)
    with pytest.raises(InvalidTransition):
        assert_transition(S.APPLIED_CONFIRMED, S.QUEUED)


def test_waiting_states_are_not_schedulable() -> None:
    assert WAITING_ON_USER.isdisjoint(SCHEDULABLE)


@pytest.mark.asyncio
async def test_repository_persists_transitions_and_rejects_invalid(ctx) -> None:
    job = await ctx.repos.jobs.insert({"original_url": "https://example.com/j/1", "canonical_url": "https://example.com/j/1", "dedup_key": "k1", "source_kind": "url", "company": "X", "title": "Y"})
    app = await ctx.repos.applications.create(job["id"], "id-1", S.IMPORTED, application_url=job["canonical_url"])
    app = await ctx.repos.applications.transition(app["id"], S.SCREENING, reason="s")
    app = await ctx.repos.applications.transition(app["id"], S.ELIGIBLE, reason="e")
    with pytest.raises(InvalidTransition):
        await ctx.repos.applications.transition(app["id"], S.APPLIED_CONFIRMED, reason="nope")
    hist = await ctx.repos.applications.history(app["id"])
    assert [h["to_state"] for h in hist] == ["IMPORTED", "SCREENING", "ELIGIBLE"]
    assert (await ctx.repos.applications.get(app["id"]))["state"] == "ELIGIBLE"


@pytest.mark.asyncio
async def test_identity_uniqueness_prevents_second_application_record(ctx) -> None:
    from app.core.errors import DatabaseError

    job = await ctx.repos.jobs.insert({"original_url": "https://example.com/j/2", "canonical_url": "https://example.com/j/2", "dedup_key": "k2", "source_kind": "url"})
    await ctx.repos.applications.create(job["id"], "same-identity", S.IMPORTED, application_url=None)
    job2 = await ctx.repos.jobs.insert({"original_url": "https://example.com/j/3", "canonical_url": "https://example.com/j/3", "dedup_key": "k3", "source_kind": "url"})
    with pytest.raises(DatabaseError):
        await ctx.repos.applications.create(job2["id"], "same-identity", S.IMPORTED, application_url=None)
