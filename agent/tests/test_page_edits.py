"""Instructions about the form in front of the user become edits (not answers), and generic
link boxes each get a different link."""

from __future__ import annotations

from app.applications.field_mapping import FieldPlan, FillPlan, _spread_links
from app.applications.page_edits import looks_like_edit, rule_edits
from app.browser.snapshot import SnapshotNode
from app.core.enums import Provenance

FACTS = {
    "links.github": ("https://github.com/Manik400", Provenance.VERIFIED_FACT),
    "links.linkedin": ("https://linkedin.com/in/manik400", Provenance.VERIFIED_FACT),
    "links.other": ("https://leetcode.com/u/manik_23_", Provenance.VERIFIED_FACT),
    "links.portfolio": ("https://manik400.github.io/portfolio/", Provenance.VERIFIED_FACT),
    "links.website": ("https://manik400.github.io/portfolio/", Provenance.VERIFIED_FACT),
}


def _box(i: int, name: str, value: str = "") -> SnapshotNode:
    return SnapshotNode(i=i, role="textbox", name=name, tag="input", type="text", value=value, visible=True)


def test_corrections_are_edits_and_questions_are_not() -> None:
    assert looks_like_edit("wht the hell in link you have filled all 3 links same ??\nfil one pottfolio nad one github nad one leetcode")
    assert looks_like_edit("put my GitHub in link 2")
    assert looks_like_edit("clear the salary box")
    assert not looks_like_edit("What is the application deadline?")
    assert not looks_like_edit("Is this job remote?")


def test_named_links_are_handed_out_in_order() -> None:
    fields = [_box(10, "Link 1", "https://leetcode.com/u/manik_23_"), _box(11, "Link 2", "https://leetcode.com/u/manik_23_"), _box(12, "Link 3", "https://leetcode.com/u/manik_23_")]
    edits = rule_edits("fil one pottfolio nad one github nad one leetcode", fields, FACTS)
    assert [(e.node, e.value) for e in edits] == [
        (10, "https://manik400.github.io/portfolio/"),
        (11, "https://github.com/Manik400"),
        (12, "https://leetcode.com/u/manik_23_"),
    ]


def test_a_box_labelled_with_the_site_takes_that_link() -> None:
    fields = [_box(5, "GitHub profile"), _box(6, "Link 1")]
    edits = rule_edits("put my github and my portfolio", fields, FACTS)
    assert {(e.node, e.value) for e in edits} == {(5, "https://github.com/Manik400"), (6, "https://manik400.github.io/portfolio/")}


def test_no_link_words_means_no_rule_edits() -> None:
    assert rule_edits("change the city to Delhi", [_box(1, "City")], FACTS) == []


def test_generic_link_boxes_get_different_links() -> None:
    same = "https://leetcode.com/u/manik_23_"
    plan = FillPlan(fields=[FieldPlan(node=i, label=f"Link {i}", field_type="text", required=False, category="links", profile_key="links.other", value=same, status="auto", provenance=Provenance.VERIFIED_FACT) for i in (1, 2, 3)])
    _spread_links(plan, FACTS)
    values = [f.value for f in plan.fields]
    assert values[0] == same and len(set(values)) == 3
    assert "github.com" in " ".join(values) and "portfolio" in " ".join(values)


def test_site_named_boxes_are_left_alone() -> None:
    plan = FillPlan(fields=[
        FieldPlan(node=1, label="GitHub", field_type="text", required=False, category="links", profile_key="links.github", value="https://github.com/Manik400", status="auto", provenance=Provenance.VERIFIED_FACT),
        FieldPlan(node=2, label="GitHub URL", field_type="text", required=False, category="links", profile_key="links.github", value="https://github.com/Manik400", status="auto", provenance=Provenance.VERIFIED_FACT),
    ])
    _spread_links(plan, FACTS)
    assert [f.value for f in plan.fields] == ["https://github.com/Manik400"] * 2
