"""ResultVerifier / PostActionVerifier.

Two responsibilities:

1. Verify that an executed action produced its declared expected outcome.
2. Decide, from authoritative page evidence only, whether a submission succeeded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.actions import ActionResult, BrowserAction
from ..core.enums import VerificationStatus
from .page_state import _CONFIRM_RE, classify_page
from .snapshot import PageSnapshot


@dataclass(slots=True)
class VerificationOutcome:
    ok: bool
    strategy: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,}$")
_DECLINE_WORDS_RE = re.compile(r"declin|prefer not|rather not|do(n['’]?t| not) (wish|want) to|not (to )?(answer|disclose|say|specify)|choose not", re.I)


_DIAL_CODES = {"india": "+91", "united states": "+1", "united kingdom": "+44", "germany": "+49", "ireland": "+353", "netherlands": "+31", "france": "+33", "spain": "+34", "canada": "+1", "australia": "+61", "singapore": "+65", "united arab emirates": "+971"}


def values_match(kind: str, expected: str, observed: str) -> bool:
    """Whether a field now holds the intended value, tolerating site formatting.

    * phone numbers: same digits (sites reformat "+91 9315605606" as "+91 93156 05606"); a
      national number typed into a field with a separate country code also matches;
    * dropdown picks (``select``): the picked option names the value ("Gurugram" →
      "Gurugram, Haryana, India", "Yes" → "Yes, I will", decline → any decline wording);
    * everything else: equal after whitespace normalisation.
    """
    e = " ".join((expected or "").split())
    o = " ".join((observed or "").split())
    if not o:
        return not e
    if e == o:
        return True
    if _PHONE_RE.match(e) and _PHONE_RE.match(o):
        de, do = re.sub(r"\D", "", e), re.sub(r"\D", "", o)
        return de == do or (len(do) >= 7 and de.endswith(do)) or (len(de) >= 7 and do.endswith(de))
    if kind == "select":
        el, ol = e.lower(), o.lower()
        if _DIAL_CODES.get(el) and ol.replace(" ", "") in (_DIAL_CODES[el], f"{el}{_DIAL_CODES[el]}".replace(" ", "")):
            return True  # phone-country pickers display only the code: "India" shows "+91"
        code = re.search(r"\+\d{1,4}\b", el)
        if code and ol.replace(" ", "") == code.group(0):
            return True  # picked "India +91", picker shows "+91"
        if _DECLINE_WORDS_RE.search(el):
            return bool(_DECLINE_WORDS_RE.search(ol))
        # The picked option must BEGIN with the whole answer ("India" → "India +91", "Yes" →
        # "Yes, I will"); an option that merely contains it ("British Indian Ocean Territory") or
        # a different one entirely ("North Korea") is a mismatch, which stops the submission.
        return ol.startswith(el) and (len(ol) == len(el) or not ol[len(el)].isalnum())
    return False


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
            ok = observed is not None and values_match("select" if action.type == "select" else "text", str(expected or ""), str(observed))
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


_ERROR_WORDS_RE = re.compile(r"\b(error|invalid|required|missing|failed|could not|couldn't|unable|please (enter|select|complete|provide|fix|correct))\b", re.I)


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
    # Some sites show the thank-you in an alert/status box, which reads as an "error" element.
    errors = [m for m in after.signals.error_messages if not (_CONFIRM_RE.search(m) and not _ERROR_WORDS_RE.search(m))]
    if errors:
        return SubmissionVerdict(VerificationStatus.REJECTED, None, evidence, "site reported errors after submit: " + "; ".join(errors[:3]))
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
