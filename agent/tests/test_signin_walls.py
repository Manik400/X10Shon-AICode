"""Sign-in walls that carry no password box (Workday's "Sign In" chooser page) and the
page-level account gate behind an "Apply" click."""

from __future__ import annotations

from app.applications.workflow import _account_gate, find_apply_button
from app.browser.page_state import classify_page, signin_label
from app.browser.snapshot import PageSignals, PageSnapshot, SnapshotNode


def _btn(i: int, name: str, *, tag: str = "button", href: str | None = None, in_dialog: bool = False, role: str = "button") -> SnapshotNode:
    return SnapshotNode(i=i, role=role, name=name, tag=tag, href=href, visible=True, in_dialog=in_dialog)


def _workday_signin() -> PageSnapshot:
    """Workday after "Apply Manually": the page asks to sign in; the password box only comes later."""
    nodes = [
        SnapshotNode(i=0, role="link", name="Skip to main content", tag="a", visible=True),
        _btn(1, "Sign In"),
        _btn(2, "Search Careers"),
        SnapshotNode(i=3, role="heading", name="Sign In", text="Sign In", tag="h2", visible=True),
        SnapshotNode(i=4, role="link", name="Back to Job Posting", tag="a", visible=True),
        _btn(5, "Sign in with Google"),
        _btn(6, "Sign in with email"),
        SnapshotNode(i=7, role="link", name="Accenture Privacy Policy", tag="a", visible=True),
    ]
    return PageSnapshot(url="https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/City/Engineer_R1/apply/applyManually", title="Sign In", nodes=nodes, signals=PageSignals(login_cta_present=True, has_jsonld_jobposting=True), text_excerpt="Sign In Back to Job Posting Sign in with Google Sign in with email")


def test_signin_label_matches_only_whole_prompts() -> None:
    assert signin_label("Sign In")
    assert signin_label("Log in to apply")
    assert signin_label("Sign In | Acme Careers")
    assert not signin_label("Sign up for job alerts")
    assert not signin_label("Login Engineer (Remote)")
    assert not signin_label("Software Engineer")


def test_workday_signin_chooser_is_a_login_page() -> None:
    cls = classify_page(_workday_signin())
    assert cls.kind == "login"
    assert cls.auth_state.value == "logged_out"


def test_workday_signin_chooser_is_an_account_gate() -> None:
    assert _account_gate(_workday_signin())


def test_job_page_with_header_signin_is_not_a_gate_or_login() -> None:
    nodes = [
        _btn(0, "Sign In"),
        SnapshotNode(i=1, role="heading", name="Custom Software Engineer", text="Custom Software Engineer", tag="h2", visible=True),
        _btn(2, "Apply", tag="a", href="https://acme.wd5.myworkdayjobs.com/Careers/job/City/Engineer_R1/apply"),
    ]
    snap = PageSnapshot(url="https://acme.wd5.myworkdayjobs.com/Careers/job/City/Engineer_R1", title="Custom Software Engineer", nodes=nodes, signals=PageSignals(login_cta_present=True, has_jsonld_jobposting=True), text_excerpt="Responsibilities Qualifications")
    assert not _account_gate(snap)
    assert classify_page(snap).kind == "job_description"
    apply = find_apply_button(snap)
    assert apply is not None and apply.name == "Apply" and apply.tag == "a" and apply.href


def test_workday_start_popup_prefers_apply_manually() -> None:
    nodes = [
        _btn(0, "Sign In"),
        _btn(1, "Apply", tag="a", href="https://acme.wd5.myworkdayjobs.com/Careers/job/City/Engineer_R1/apply"),
        _btn(2, "Close", in_dialog=True),
        _btn(3, "Autofill with Resume", tag="a", href="https://acme.wd5.myworkdayjobs.com/Careers/job/City/Engineer_R1/apply/autofillWithResume", in_dialog=True),
        _btn(4, "Apply Manually", tag="a", href="https://acme.wd5.myworkdayjobs.com/Careers/job/City/Engineer_R1/apply/applyManually", in_dialog=True),
        _btn(5, "Use My Last Application", tag="a", href="https://acme.wd5.myworkdayjobs.com/Careers/job/City/Engineer_R1/apply/useMyLastApplication", in_dialog=True),
    ]
    snap = PageSnapshot(url="https://acme.wd5.myworkdayjobs.com/Careers/job/City/Engineer_R1", title="Custom Software Engineer", nodes=nodes, signals=PageSignals(login_cta_present=True))
    apply = find_apply_button(snap)
    assert apply is not None and apply.name == "Apply Manually"
    assert not _account_gate(snap)
