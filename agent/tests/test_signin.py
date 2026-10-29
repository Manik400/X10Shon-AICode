"""Automatic sign-in on a site's own login wall, account creation where the site offers it, and
the policy exception that lets a stored password be typed."""

from __future__ import annotations

import re

import pytest

from app.agent.policy import PolicyEngine
from app.applications.profile import CandidateProfile, SiteAccount
from app.applications.signin import SignInHelper, generate_password, host_of
from app.browser.snapshot import PageSignals, PageSnapshot, SnapshotNode
from app.core.actions import ActionResult, BrowserAction, Target

HOST = "acme.wd5.myworkdayjobs.com"
BASE = f"https://{HOST}/en-US/Careers/job/City/Engineer_R1/apply/applyManually"


def _n(i: int, role: str, name: str, **kw) -> SnapshotNode:
    return SnapshotNode(i=i, role=role, name=name, visible=True, **kw)


def pages() -> dict[str, PageSnapshot]:
    chooser = PageSnapshot(url=BASE, title="Sign In", signals=PageSignals(login_cta_present=True), nodes=[
        _n(1, "button", "Sign In", tag="button"),
        _n(2, "heading", "Sign In", tag="h2", text="Sign In"),
        _n(3, "button", "Sign in with Google", tag="button", type="button"),
        _n(4, "button", "Sign in with email", tag="button"),
    ])
    signin = PageSnapshot(url=BASE + "#signin", title="Sign In", signals=PageSignals(has_password_field=True, login_cta_present=True), nodes=[
        _n(10, "textbox", "Email Address*", tag="input", type="text", form=0),
        _n(11, "textbox", "Password*", tag="input", type="password", form=0),
        _n(12, "button", "Sign In", tag="button", type="submit", form=0),
        _n(13, "button", "Create Account", tag="button"),
        _n(14, "button", "Forgot your password?", tag="button"),
        _n(15, "textbox", "Enter website. This input is for robots only, do not enter", tag="input", type="text", name_attr="website", form=0),
    ])
    create = PageSnapshot(url=BASE + "#create", title="Create Account", signals=PageSignals(has_password_field=True, signup_cta_present=True), nodes=[
        _n(20, "textbox", "Email Address*", tag="input", type="text", form=0),
        _n(21, "textbox", "Password*", tag="input", type="password", form=0),
        _n(22, "textbox", "Verify New Password*", tag="input", type="password", form=0),
        _n(23, "checkbox", "I Agree", tag="input", type="checkbox", required=True, form=0),
        _n(24, "button", "Create Account", tag="button", type="submit", form=0),
        _n(25, "button", "Sign In", tag="button"),
        _n(26, "textbox", "Enter website. This input is for robots only", tag="input", type="text", name_attr="website", form=0),
    ])
    app = PageSnapshot(url=f"https://{HOST}/en-US/Careers/job/City/Engineer_R1/apply/applyManually?step=2", title="My Information", nodes=[
        _n(30, "textbox", "First Name*", tag="input", type="text", form=0),
        _n(31, "textbox", "Last Name*", tag="input", type="text", form=0),
        _n(32, "button", "Save and Continue", tag="button", type="button", form=0),
    ])
    return {"chooser": chooser, "signin": signin, "create": create, "app": app}


class FakeCtx:
    def __init__(self, start: str, transitions: dict[tuple[str, str], str]):
        self.pages = pages()
        self.current = start
        self.transitions = transitions
        self.actions: list[BrowserAction] = []
        self.task_id = "t1"

    async def snapshot(self) -> PageSnapshot:
        return self.pages[self.current]

    async def execute(self, action: BrowserAction, payload=None) -> ActionResult:  # type: ignore[no-untyped-def]
        self.actions.append(action)
        if action.type == "click" and action.target is not None:
            node = self.pages[self.current].node(int(action.target.value))
            key = (self.current, node.name if node else "")
            if key in self.transitions:
                self.current = self.transitions[key]
        return ActionResult(ok=True, action_type=action.type, message="")


class FakeProfileRepo:
    def __init__(self, data: dict):
        self.data = data

    async def get(self):
        return self.data

    async def save(self, data: dict) -> None:
        self.data = data


class FakeRepos:
    def __init__(self, profile: dict):
        self.profile = FakeProfileRepo(profile)


class FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def log(self, event: str, **kw) -> None:  # type: ignore[no-untyped-def]
        self.events.append((event, kw))


TRANSITIONS = {("chooser", "Sign in with email"): "signin", ("signin", "Sign In"): "app", ("signin", "Create Account"): "create", ("create", "Create Account"): "app"}


def _typed(ctx: FakeCtx, node: int) -> list[BrowserAction]:
    return [a for a in ctx.actions if a.type == "type" and a.target is not None and a.target.value == str(node)]


@pytest.mark.asyncio
async def test_signs_in_with_the_stored_account_through_the_email_option() -> None:
    profile = CandidateProfile.model_validate({"contact": {"email": "me@example.com"}, "accounts": [{"host": HOST, "email": "me@example.com", "password": "Secr3t!pass"}]})
    ctx = FakeCtx("chooser", TRANSITIONS)
    res = await SignInHelper(FakeRepos(profile.model_dump()), FakeAudit()).attempt(ctx, ctx.pages["chooser"], profile)
    assert res.status == "signed_in", res.message
    assert ctx.current == "app" and res.snap.title == "My Information"
    assert _typed(ctx, 10)[0].args["text"] == "me@example.com"
    pw = _typed(ctx, 11)[0]
    assert pw.args["text"] == "Secr3t!pass" and pw.args.get("credential") is True
    assert not _typed(ctx, 15), "the honeypot box must stay empty"


@pytest.mark.asyncio
async def test_creates_an_account_when_none_is_stored_and_saves_the_password() -> None:
    profile = CandidateProfile.model_validate({"contact": {"email": "me@example.com"}})
    repos = FakeRepos(profile.model_dump())
    audit = FakeAudit()
    ctx = FakeCtx("chooser", TRANSITIONS)
    res = await SignInHelper(repos, audit, create_accounts=True).attempt(ctx, ctx.pages["chooser"], profile)
    assert res.status == "created", res.message
    assert ctx.current == "app"
    saved = CandidateProfile.model_validate(repos.profile.data)
    acc = saved.account_for(HOST)
    assert acc is not None and acc.created_by_agent and acc.email == "me@example.com"
    assert re.search(r"[A-Z]", acc.password) and re.search(r"[a-z]", acc.password) and re.search(r"\d", acc.password) and re.search(r"[^A-Za-z0-9]", acc.password) and len(acc.password) >= 12
    assert _typed(ctx, 21)[0].args["text"] == acc.password and _typed(ctx, 22)[0].args["text"] == acc.password
    assert any(a.type == "check" and a.target.value == "23" for a in ctx.actions), "the consent box is ticked"
    assert not _typed(ctx, 26)
    assert profile.account_for(HOST) is not None, "the in-memory profile learns the account too"
    assert ("signin.account_created", {"host": HOST, "email": "me@example.com"}) in audit.events


@pytest.mark.asyncio
async def test_without_account_and_without_creation_it_reports_no_credentials() -> None:
    profile = CandidateProfile.model_validate({"contact": {"email": "me@example.com"}})
    ctx = FakeCtx("chooser", TRANSITIONS)
    res = await SignInHelper(FakeRepos(profile.model_dump()), FakeAudit(), create_accounts=False).attempt(ctx, ctx.pages["chooser"], profile)
    assert res.status == "no_credentials"
    assert "Site accounts" in res.message
    assert not [a for a in ctx.actions if a.type == "type"]


@pytest.mark.asyncio
async def test_rejected_password_is_reported_not_retried() -> None:
    profile = CandidateProfile.model_validate({"contact": {"email": "me@example.com"}, "accounts": [{"host": HOST, "email": "me@example.com", "password": "old"}]})
    ctx = FakeCtx("signin", {})
    bad = ctx.pages["signin"].model_copy(update={"signals": PageSignals(has_password_field=True, error_messages=["Invalid email or password"])})
    ctx.pages["signin"] = bad
    res = await SignInHelper(FakeRepos(profile.model_dump()), FakeAudit()).attempt(ctx, ctx.pages["signin"], profile)
    assert res.status == "failed" and "rejected the stored password" in res.message


def test_account_lookup_prefers_the_closest_host() -> None:
    p = CandidateProfile.model_validate({"accounts": [{"host": "myworkdayjobs.com", "email": "a", "password": "x"}, {"host": "acme.wd5.myworkdayjobs.com", "email": "b", "password": "y"}, {"host": "wellfound.com", "email": "c", "password": ""}]})
    assert p.account_for("acme.wd5.myworkdayjobs.com").email == "b"
    assert p.account_for("other.wd1.myworkdayjobs.com").email == "a"
    assert p.account_for("www.wellfound.com") is None, "an entry without a password is not usable"
    assert host_of("https://www.Wellfound.com/jobs/1") == "wellfound.com"
    assert "password" not in " ".join(p.facts().keys())


def test_generated_passwords_meet_the_usual_rules() -> None:
    for _ in range(20):
        pw = generate_password()
        assert len(pw) >= 14 and re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw) and re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)


def test_policy_allows_a_stored_password_only_with_the_credential_flag() -> None:
    policy = PolicyEngine(allowed_domains=["*"])
    plain = BrowserAction(type="type", target=Target(strategy="node", value="11"), args={"text": "x"})
    flagged = BrowserAction(type="type", target=Target(strategy="node", value="11"), args={"text": "x", "credential": True})
    assert policy.evaluate(plain, current_url="https://a.test/login", field_label="Password*").decision == "deny"
    assert policy.evaluate(flagged, current_url="https://a.test/login", field_label="Password*").decision == "allow"
    assert policy.evaluate(flagged, current_url="https://a.test/login", field_label="One-time passcode").decision == "deny"
    policy.allow_stored_credentials = False
    assert policy.evaluate(flagged, current_url="https://a.test/login", field_label="Password*").decision == "deny"


@pytest.mark.asyncio
async def test_a_second_signup_keeps_the_first_password_and_signin_tries_it() -> None:
    profile = CandidateProfile.model_validate({"contact": {"email": "me@example.com"}, "accounts": [{"host": HOST, "email": "me@example.com", "password": "Second!pw7x", "created_by_agent": True, "previous_passwords": ["First!pw7x"]}]})
    ctx = FakeCtx("signin", {("signin", "Sign In"): "app"})
    calls = {"n": 0}
    orig = ctx.execute

    async def execute(action, payload=None):  # the site rejects the first (newer) password only
        res = await orig(action, payload)
        if action.type == "click" and calls["n"] == 0:
            calls["n"] += 1
            ctx.current = "signin"
            ctx.pages["signin"] = ctx.pages["signin"].model_copy(update={"signals": PageSignals(has_password_field=True, error_messages=["Invalid email or password"])})
        elif action.type == "click":
            ctx.pages["signin"] = ctx.pages["signin"].model_copy(update={"signals": PageSignals(has_password_field=True)})
            ctx.current = "app"
        return res

    ctx.execute = execute  # type: ignore[method-assign]
    res = await SignInHelper(FakeRepos(profile.model_dump()), FakeAudit()).attempt(ctx, ctx.pages["signin"], profile)
    assert res.status == "signed_in", res.message
    typed = [a.args["text"] for a in ctx.actions if a.type == "type" and a.args.get("credential")]
    assert typed == ["Second!pw7x", "First!pw7x"]
