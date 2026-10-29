"""Which button is the Submit / Next of a form, and the rules-first fill that applies the model's
late picks afterwards."""

from __future__ import annotations

import asyncio

import pytest

from app.applications.field_mapping import find_submit_and_next
from app.browser.snapshot import SnapshotNode

NAMES = ["Submit application", "Submit", "Apply", "Send application"]


def _field(i: int, name: str, form: int | None = 0) -> SnapshotNode:
    return SnapshotNode(i=i, role="textbox", name=name, tag="input", type="text", form=form, visible=True)


def _button(i: int, name: str, *, form: int | None = None, type_: str | None = None) -> SnapshotNode:
    return SnapshotNode(i=i, role="button", name=name, tag="button", type=type_, form=form, visible=True)


def test_submit_in_the_fields_form_beats_the_page_apply_button() -> None:
    # Darize: "Apply now" at the top only opens the form; the real one is type=submit at its end.
    nodes = [_button(2, "Apply now"), _field(8, "First name"), _field(10, "Last name"), _field(18, "Mobile"), _button(19, "Apply now", form=0, type_="submit")]
    assert find_submit_and_next(nodes, NAMES) == (19, None)


def test_button_below_the_fields_beats_one_above_when_no_form_info() -> None:
    nodes = [_button(2, "Apply"), _field(8, "First name", form=None), _field(10, "Email", form=None), _button(19, "Apply")]
    assert find_submit_and_next(nodes, NAMES) == (19, None)


def test_first_match_when_nothing_else_tells_them_apart() -> None:
    nodes = [_field(3, "Email"), _button(5, "Submit"), _button(7, "Submit")]
    assert find_submit_and_next(nodes, NAMES) == (5, None)


def test_wizard_step_next_in_form_wins_over_a_stray_submit_elsewhere() -> None:
    nodes = [_button(1, "Submit"), _field(4, "First name"), _field(5, "Last name"), _button(9, "Save and Continue", form=0, type_="button")]
    assert find_submit_and_next(nodes, NAMES) == (None, 9)


def test_next_names_in_other_languages() -> None:
    nodes = [_field(4, "Vorname"), _button(9, "Weiter", form=0)]
    assert find_submit_and_next(nodes, NAMES) == (None, 9)


class _SlowGateway:
    """A model gateway whose field mapping takes a while (the real one takes 40-120 s)."""

    def __init__(self, delay: float, mappings: list[dict]):
        self.delay = delay
        self.mappings = mappings
        self.calls = 0

    class _Cfg:
        provider = "ollama"
        model = "x"

    def role_config(self, role: str):
        return self._Cfg()

    async def complete(self, role, request, output_model, **kw):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return output_model.model_validate({"mappings": self.mappings})


@pytest.mark.asyncio
async def test_rules_fill_first_and_model_picks_follow() -> None:
    from app.applications.form_filler import FormFiller
    from app.applications.profile import CandidateProfile
    from app.browser.snapshot import PageSnapshot

    profile = CandidateProfile.model_validate({"contact": {"first_name": "Manik", "last_name": "Goyal", "email": "m@example.com", "country": "India"}})
    nodes = [
        _field(1, "First name"),
        _field(2, "Email"),
        SnapshotNode(i=3, role="combobox", name="Poland", tag="div", form=0, visible=True, options=["Poland", "India", "Germany"]),
        _button(9, "Apply now", form=0, type_="submit"),
    ]
    snap = PageSnapshot(url="https://example.test/apply", title="Apply", nodes=nodes)
    gateway = _SlowGateway(0.5, [{"node": 3, "profile_key": "contact.country", "category": "contact", "confidence": 0.9}])
    filler = FormFiller(gateway)  # type: ignore[arg-type]

    plan = await filler.plan(snap, profile, [], [], submit_names=NAMES, model_budget_seconds=0.05)
    assert plan.pending_model is not None, "the model is still working: the plan must not wait for it"
    assert {f.label for f in plan.auto} >= {"First name", "Email"}
    assert plan.submit_node == 9

    extra = await filler.finish_plan(plan, snap, profile, [], [], submit_names=NAMES, extra_wait=5)
    assert extra is not None and [f.node for f in extra.fields] == [3]
    assert extra.fields[0].value == "India"
    assert extra.fields[0].status in ("auto", "needs_review")  # model picks are reviewed unless the run promotes them
    assert next(f for f in plan.fields if f.node == 3).value == "India"
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_too_late_model_is_dropped_and_cancelled() -> None:
    from app.applications.form_filler import FormFiller
    from app.applications.profile import CandidateProfile
    from app.browser.snapshot import PageSnapshot

    profile = CandidateProfile.model_validate({"contact": {"first_name": "Manik", "email": "m@example.com"}})
    nodes = [_field(1, "First name"), SnapshotNode(i=3, role="combobox", name="Poland", tag="div", form=0, visible=True, options=["Poland", "India"]), _button(9, "Submit", form=0, type_="submit")]
    snap = PageSnapshot(url="https://example.test/apply", title="Apply", nodes=nodes)
    gateway = _SlowGateway(30, [])
    filler = FormFiller(gateway)  # type: ignore[arg-type]
    plan = await filler.plan(snap, profile, [], [], submit_names=NAMES, model_budget_seconds=0.05)
    task = plan.pending_model.task
    assert await filler.finish_plan(plan, snap, profile, [], [], submit_names=NAMES, extra_wait=0.1) is None
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
