"""Browser-level tests of the shared dom-agent bundle through the Playwright driver."""

from __future__ import annotations

import base64

import pytest

from app.browser.page_state import classify_page
from app.browser.verifier import verify_action
from app.core.actions import BrowserAction, Target
from app.core.enums import AuthState

from .conftest import fixture_url, requires_browser

pytestmark = [requires_browser, pytest.mark.asyncio]


async def open_fixture(driver, rel: str) -> str:
    return await driver.open_tab(fixture_url(rel))


async def test_snapshot_exposes_labels_roles_sections_and_forms(driver) -> None:
    tab = await open_fixture(driver, "forms/simple_application.html")
    snap = await driver.snapshot(tab, max_nodes=300)
    by_name = {n.name: n for n in snap.nodes}
    assert by_name["First name *"].role == "textbox" and by_name["First name *"].required
    assert by_name["First name *"].section == "Contact"
    assert by_name["Email *"].type == "email"
    sel = by_name["Are you legally authorized to work in the United States? *"]
    assert sel.role == "combobox" and sel.options == ["Select…", "Yes", "No"]
    radios = [n for n in snap.nodes if n.role == "radio"]
    assert {r.name for r in radios} == {"Remote", "Hybrid", "On-site"} and radios[0].section == "Preferred work arrangement"
    assert by_name["Resume / CV *"].role == "file"
    assert any(n.role == "button" and n.name == "Submit application" for n in snap.nodes)
    assert snap.forms and len(snap.forms[0].fields) >= 10 and snap.forms[0].submit_nodes
    assert classify_page(snap).kind == "form"
    await driver.close_tab(tab)


async def test_locator_strategies_and_ambiguity(driver) -> None:
    tab = await open_fixture(driver, "forms/duplicate_labels.html")
    snap = await driver.snapshot(tab, max_nodes=100)
    # Two visible "Name" fields: a bare label locator is ambiguous …
    res = await driver.execute(tab, BrowserAction(type="type", target=Target(strategy="label", value="Name"), args={"text": "x"}))
    assert not res.ok and res.error_category == "AMBIGUOUS_TARGET" and res.matched_count == 2
    # … section context disambiguates …
    res = await driver.execute(tab, BrowserAction(type="type", target=Target(strategy="label", value="Name", section="Emergency contact"), args={"text": "Eve"}))
    assert res.ok and res.value == "Eve"
    # … and so does nth / name attribute.
    res = await driver.execute(tab, BrowserAction(type="type", target=Target(strategy="name", value="applicant_name"), args={"text": "Ada"}))
    assert res.ok
    snap2 = await driver.snapshot(tab, max_nodes=100)
    vals = {n.name_attr: n.value for n in snap2.nodes if n.role == "textbox"}
    assert vals["applicant_name"] == "Ada" and vals["ec_name"] == "Eve"
    assert "dialog_name" not in vals  # hidden dialog is not snapshotted
    missing = await driver.execute(tab, BrowserAction(type="click", target=Target(strategy="role", value="button", name="Does not exist")))
    assert missing.error_category == "ELEMENT_NOT_FOUND"
    assert snap.generation >= 1
    await driver.close_tab(tab)


async def test_type_select_check_upload_and_value_verification(driver) -> None:
    tab = await open_fixture(driver, "forms/simple_application.html")
    snap = await driver.snapshot(tab, max_nodes=300)
    idx = {n.name: n.i for n in snap.nodes}
    typed = await driver.execute(tab, BrowserAction(type="type", target=Target(strategy="node", value=str(idx["First name *"])), args={"text": "Ada"}, verification="value_set"))
    assert typed.ok and typed.value == "Ada"
    assert verify_action(BrowserAction(type="type", target=Target(strategy="node", value="0"), args={"text": "Ada"}, verification="value_set"), typed, None, None).ok
    sel = await driver.execute(tab, BrowserAction(type="select", target=Target(strategy="node", value=str(idx["Are you legally authorized to work in the United States? *"])), args={"value": "yes"}))
    assert sel.ok and sel.value == "Yes"
    bad = await driver.execute(tab, BrowserAction(type="select", target=Target(strategy="label", value="Will you now or in the future require sponsorship"), args={"value": "Maybe"}))
    assert not bad.ok and bad.error_category == "ELEMENT_NOT_FOUND" and "options" in bad.evidence
    radio = await driver.execute(tab, BrowserAction(type="check", target=Target(strategy="label", value="Hybrid", section="Preferred work arrangement"), args={"checked": True}))
    assert radio.ok and radio.value is True
    box = await driver.execute(tab, BrowserAction(type="check", target=Target(strategy="id", value="consent"), args={"checked": True}))
    assert box.ok and box.value is True
    payload = {"file_name": "ada.pdf", "mime_type": "application/pdf", "bytes_b64": base64.b64encode(b"%PDF-1.4 test").decode()}
    up = await driver.execute(tab, BrowserAction(type="upload", target=Target(strategy="label", value="Resume / CV"), args={"resume_id": "r1"}), payload=payload)
    assert up.ok and up.value == "ada.pdf"
    no_payload = await driver.execute(tab, BrowserAction(type="upload", target=Target(strategy="label", value="Resume / CV"), args={"resume_id": "r1"}))
    assert not no_payload.ok and no_payload.error_category == "PERMISSION_DENIED"
    after = await driver.snapshot(tab, max_nodes=300)
    byname = {n.name: n for n in after.nodes}
    assert byname["First name *"].value == "Ada"
    assert byname["Are you legally authorized to work in the United States? *"].value == "Yes"
    assert byname["Hybrid"].checked is True
    await driver.close_tab(tab)


async def test_controlled_input_receives_value(driver) -> None:
    tab = await open_fixture(driver, "forms/react_controlled.html")
    res = await driver.execute(tab, BrowserAction(type="type", target=Target(strategy="label", value="Preferred name"), args={"text": "Grace"}))
    assert res.ok
    snap = await driver.snapshot(tab, max_nodes=50)
    assert '"name":"Grace"' in snap.text_excerpt.replace(" ", "")
    await driver.close_tab(tab)


async def test_hidden_file_input_behind_styled_label(driver) -> None:
    tab = await open_fixture(driver, "forms/lever_like.html")
    payload = {"file_name": "cv.pdf", "mime_type": "application/pdf", "bytes_b64": base64.b64encode(b"%PDF cv").decode()}
    res = await driver.execute(tab, BrowserAction(type="upload", target=Target(strategy="label", value="ATTACH RESUME/CV"), args={"resume_id": "r"}), payload=payload)
    assert res.ok and res.value == "cv.pdf"
    await driver.close_tab(tab)


async def test_submit_click_reveals_errors_and_confirmation(driver) -> None:
    tab = await open_fixture(driver, "forms/simple_application.html")
    res = await driver.execute(tab, BrowserAction(type="click", target=Target(strategy="role", value="button", name="Submit application")))
    assert res.ok
    snap = await driver.snapshot(tab, max_nodes=300)
    assert snap.signals.error_messages and "Please complete" in snap.signals.error_messages[0]
    assert classify_page(snap).kind == "form"
    await driver.close_tab(tab)


@pytest.mark.parametrize(
    "rel,kind,auth",
    [
        ("auth/login_required.html", "login", AuthState.LOGGED_OUT),
        ("auth/captcha.html", "challenge", AuthState.CHALLENGE),
        ("auth/otp.html", "challenge", AuthState.CHALLENGE),
        ("auth/locked.html", "login", AuthState.LOCKED),
        ("auth/logged_in_form.html", "form", AuthState.AUTHENTICATED),
        ("jobs/jsonld_posting.html", "job_description", AuthState.UNKNOWN),
        ("forms/multi_step.html", "form", AuthState.UNKNOWN),
    ],
)
async def test_page_state_classification(driver, rel: str, kind: str, auth: AuthState) -> None:
    tab = await open_fixture(driver, rel)
    snap = await driver.snapshot(tab, max_nodes=200)
    cls = classify_page(snap)
    assert (cls.kind, cls.auth_state) == (kind, auth), cls.reasons
    await driver.close_tab(tab)


async def test_jsonld_job_extraction(ctx, driver) -> None:
    tab = await open_fixture(driver, "jobs/jsonld_posting.html")
    snap = await driver.snapshot(tab, max_nodes=200)
    assert snap.signals.has_jsonld_jobposting and snap.jsonld
    from app.jobs.extraction import JobExtractor

    ex, method = await JobExtractor(ctx.gateway).extract(snap)
    assert method.startswith("jsonld")
    assert ex.title == "Senior Python Developer" and ex.company == "Northwind Analytics"
    assert ex.remote_type == "remote" and ex.salary_min == 70000 and ex.salary_currency == "EUR"
    assert ex.requisition_id == "NW-2291" and ex.experience_min_years == 5
    assert ex.apply_url == "https://example.com/jobs/nw-2291"
    await driver.close_tab(tab)


async def test_plain_job_extraction_heuristics(ctx, driver) -> None:
    tab = await open_fixture(driver, "jobs/plain_posting.html")
    snap = await driver.snapshot(tab, max_nodes=200)
    from app.jobs.extraction import JobExtractor

    ex, method = await JobExtractor(ctx.gateway).extract(snap)
    assert method == "dom"
    assert ex.title == "Frontend Engineer" and ex.company == "Lumen Health"
    assert ex.remote_type == "hybrid" and ex.salary_min == 120000 and ex.salary_max == 150000
    assert ex.experience_min_years == 3 and any("React" in r for r in ex.requirements)
    assert "lever.co" in (ex.apply_url or "")
    await driver.close_tab(tab)


async def test_max_pages_bound_and_navigation_failure(driver) -> None:
    t1 = await open_fixture(driver, "forms/ambiguous_outcome.html")
    t2 = await open_fixture(driver, "forms/ambiguous_outcome.html")
    from app.core.errors import BrowserDisconnectedError

    with pytest.raises(BrowserDisconnectedError):
        await driver.open_tab(fixture_url("forms/ambiguous_outcome.html"))
    res = await driver.navigate(t1, "http://127.0.0.1:1/nothing-here", timeout_ms=3000)
    assert not res.ok and res.error_category == "NAVIGATION_FAILED"
    await driver.close_tab(t1)
    await driver.close_tab(t2)
