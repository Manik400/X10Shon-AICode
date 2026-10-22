from __future__ import annotations

from app.agent.policy import PolicyEngine
from app.core.actions import BrowserAction, Target
from app.core.enums import RiskLevel


def engine(**kw) -> PolicyEngine:
    return PolicyEngine(allowed_domains=["boards.greenhouse.io", "example.com"], per_domain_delay=0, **kw)


def test_domain_allow_list() -> None:
    e = engine()
    assert e.domain_allowed("https://boards.greenhouse.io/x")
    assert e.domain_allowed("https://jobs.example.com/x")
    assert not e.domain_allowed("https://evil.example.net/x")
    assert e.domain_allowed("http://127.0.0.1:8765/")
    assert e.domain_allowed("file:///C:/fixtures/a.html")


def test_wildcard_allows_every_site_and_survives_updates() -> None:
    e = PolicyEngine(allowed_domains=["*"], per_domain_delay=0)
    assert e.domain_allowed("https://jobs.ashbyhq.com/clera/x")
    e.set_allowed_domains(["*", "manik400.github.io", "*.example.com"])
    assert e.domain_allowed("https://coke.wd1.myworkdayjobs.com/x") and "*" in e.allowed_domains
    assert "example.com" in e.allowed_domains
    e.set_allowed_domains(["boards.greenhouse.io"])
    assert not e.domain_allowed("https://jobs.ashbyhq.com/clera/x")


def test_navigate_outside_allow_list_denied() -> None:
    v = engine().evaluate(BrowserAction(type="navigate", args={"url": "https://evil.example.net/"}), current_url="https://example.com")
    assert v.decision == "deny"


def test_actions_on_unlisted_current_page_denied_but_reads_allowed() -> None:
    e = engine()
    click = BrowserAction(type="click", target=Target(strategy="role", value="button", name="Next"))
    assert e.evaluate(click, current_url="https://evil.example.net").decision == "deny"
    assert e.evaluate(BrowserAction(type="extract"), current_url="https://evil.example.net").decision == "allow"


def test_submit_requires_approval_by_default() -> None:
    e = engine()
    click = BrowserAction(type="click", target=Target(strategy="role", value="button", name="Submit application"))
    v = e.evaluate(click, current_url="https://example.com/apply")
    assert v.decision == "needs_approval" and v.risk == RiskLevel.HIGH


def test_submit_policy_never_allows_when_explicit() -> None:
    e = engine(submit_policy="never")
    click = BrowserAction(type="click", target=Target(strategy="role", value="button", name="Submit"))
    assert e.evaluate(click, current_url="https://example.com/apply", explicit_submit=True).decision == "allow"
    # A non-submit high-risk button (e.g. delete) still needs approval.
    delete = BrowserAction(type="click", target=Target(strategy="role", value="button", name="Delete account"))
    assert e.evaluate(delete, current_url="https://example.com/apply").decision == "needs_approval"


def test_typing_into_password_fields_is_never_automated() -> None:
    e = engine()
    a = BrowserAction(type="type", target=Target(strategy="label", value="Password"), args={"text": "x"})
    assert e.evaluate(a, current_url="https://example.com").decision == "deny"
    b = BrowserAction(type="type", target=Target(strategy="node", value="3"), args={"text": "x"})
    assert e.evaluate(b, current_url="https://example.com", field_label="One-time code").decision == "deny"


def test_model_declared_risk_only_raises() -> None:
    e = engine()
    low_click_declared_high = BrowserAction(type="click", target=Target(strategy="role", value="button", name="Next"), risk=RiskLevel.HIGH)
    assert e.evaluate(low_click_declared_high, current_url="https://example.com").decision == "needs_approval"
    submit_declared_low = BrowserAction(type="click", target=Target(strategy="role", value="button", name="Submit"), risk=RiskLevel.LOW)
    assert e.evaluate(submit_declared_low, current_url="https://example.com").risk == RiskLevel.HIGH


def test_pause_denies_everything() -> None:
    e = engine()
    e.paused = True
    assert e.evaluate(BrowserAction(type="extract"), current_url="https://example.com").decision == "deny"


def test_rate_limit_wait_applies_per_domain() -> None:
    e = PolicyEngine(allowed_domains=["example.com"], per_domain_delay=5)
    e.record_action("https://example.com/a")
    v = e.evaluate(BrowserAction(type="navigate", args={"url": "https://example.com/b"}), current_url="https://example.com/a")
    assert v.decision == "allow" and v.wait_seconds > 0
    assert e.evaluate(BrowserAction(type="scroll"), current_url="https://other.example.com/a").wait_seconds == 0


def test_action_schema_rejects_code_execution() -> None:
    import pytest

    with pytest.raises(ValueError):
        BrowserAction(type="click", target=Target(strategy="css", value="button"), args={"script": "alert(1)"})
    with pytest.raises(ValueError):
        BrowserAction(type="navigate", args={"url": "javascript:alert(1)"})
    with pytest.raises(ValueError):
        BrowserAction(type="upload", target=Target(strategy="css", value="input"), args={"path": "C:/secret.pdf"})
