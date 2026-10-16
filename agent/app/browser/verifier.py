"""ResultVerifier / PostActionVerifier.

Two responsibilities:

1. Verify that an executed action produced its declared expected outcome.
2. Decide, from authoritative page evidence only, whether a submission succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.actions import ActionResult, BrowserAction
from ..core.enums import VerificationStatus
from .page_state import classify_page
from .snapshot import PageSnapshot


@dataclass(slots=True)
class VerificationOutcome:
    ok: bool
    strategy: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


def verify_action(action: BrowserAction, result: ActionResult, before: PageSnapshot | None, after: PageSnapshot | None) -> VerificationOutcome:
    strat = action.verification
    if not result.ok:
        return VerificationOutcome(False, strat, f"action failed: {result.message}")
    if strat == "none":
        return VerificationOutcome(True, strat, "no verification requested")
    if strat == "value_set":
        expected = action.args.get("text") if action.type == "type" else action.args.get("value")
        if action.type == "check":
            expected = action.args.get("checked", True)
        observed = result.value
        if action.type == "upload":
            ok = bool(observed)
        elif isinstance(expected, bool):
            ok = observed is expected or str(observed).lower() == str(expected).lower()
        else:
            ok = observed is not None and str(observed).strip() == str(expected).strip()
        return VerificationOutcome(ok, strat, "value set" if ok else "value mismatch", {"expected": expected, "observed": observed})
    if after is None:
        return VerificationOutcome(False, strat, "no post-action snapshot available")
    arg = (action.verification_arg or "").strip()
    if strat == "url_change":
        changed = before is not None and before.url != after.url
        return VerificationOutcome(changed, strat, "url changed" if changed else "url did not change", {"before": before.url if before else None, "after": after.url})
    if strat == "navigation_complete":
        return VerificationOutcome(bool(after.url), strat, "page loaded", {"url": after.url})
    if strat == "text_present":
        hay = (after.text_excerpt + " " + after.title).lower()
        found = bool(arg) and arg.lower() in hay
        return VerificationOutcome(found, strat, "text found" if found else f"text '{arg[:60]}' not found")
    if strat == "element_visible":
        found = any(_node_matches(n, arg) and n.visible for n in after.nodes)
        return VerificationOutcome(found, strat, "element visible" if found else f"element '{arg[:60]}' not visible")
    if strat == "element_hidden":
        found = any(_node_matches(n, arg) and n.visible for n in after.nodes)
        return VerificationOutcome(not found, strat, "element hidden" if not found else f"element '{arg[:60]}' still visible")
    return VerificationOutcome(False, strat, f"unknown strategy {strat}")


def _node_matches(node, needle: str) -> bool:  # type: ignore[no-untyped-def]
    if not needle:
        return False
    n = needle.lower()
    return n in (node.name or "").lower() or n in (node.text or "").lower() or n == (node.testid or "").lower() or n == (node.id or "").lower()


@dataclass(slots=True)
class SubmissionVerdict:
    status: VerificationStatus
    reference_number: str | None
    evidence: dict[str, Any]
    message: str


def verify_submission(before: PageSnapshot, after: PageSnapshot, *, expected_confirmation_text: str | None = None, expected_url_fragment: str | None = None) -> SubmissionVerdict:
    """A click is not proof. Success requires a confirmation page/message or a reference number.
    Errors or still-present validation messages mean rejection. Anything else is uncertain."""
    cls = classify_page(after)
    evidence: dict[str, Any] = {
        "url_before": before.url,
        "url_after": after.url,
        "title_after": after.title,
        "page_kind": cls.kind,
        "reasons": cls.reasons,
        "success_messages": after.signals.success_messages[:5],
        "error_messages": after.signals.error_messages[:5],
    }
    if cls.kind == "challenge" or cls.kind == "login":
        return SubmissionVerdict(VerificationStatus.UNCERTAIN, None, evidence, "submission interrupted by authentication or a challenge")
    if after.signals.error_messages:
        return SubmissionVerdict(VerificationStatus.REJECTED, None, evidence, "site reported errors after submit: " + "; ".join(after.signals.error_messages[:3]))
    ref = cls.reference_numbers[0] if cls.reference_numbers else None
    text_hit = bool(expected_confirmation_text) and expected_confirmation_text.lower() in (after.text_excerpt + " " + after.title).lower()  # type: ignore[union-attr]
    url_hit = bool(expected_url_fragment) and expected_url_fragment.lower() in after.url.lower()  # type: ignore[union-attr]
    if cls.kind == "confirmation" or ref or text_hit or url_hit:
        evidence["matched"] = {"confirmation_page": cls.kind == "confirmation", "reference": ref, "expected_text": text_hit, "expected_url": url_hit}
        return SubmissionVerdict(VerificationStatus.CONFIRMED, ref, evidence, "confirmation evidence found")
    # Form still present and unchanged → likely nothing happened (uncertain, not rejected).
    if after.form_fields() and after.url == before.url:
        return SubmissionVerdict(VerificationStatus.UNCERTAIN, None, evidence, "form still present with no confirmation or error")
    return SubmissionVerdict(VerificationStatus.UNCERTAIN, None, evidence, "no authoritative confirmation found")
