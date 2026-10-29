"""Getting past a site's own sign-in wall without the user.

A run that reaches a login page (Workday's "Sign In", a board's "Log in to apply") signs in with
the candidate's stored account for that host, or, where the site offers it and no account is
stored, creates one with the candidate's email and a generated password that is saved to the
profile right away. CAPTCHAs and one-time codes are never touched: they end in
``needs_verification`` and the run hands over as before.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from ..browser.page_state import classify_page, signin_label
from ..browser.snapshot import PageSnapshot, SnapshotNode
from ..core.actions import BrowserAction, Target
from ..core.enums import RiskLevel
from .profile import CandidateProfile, SiteAccount

log = logging.getLogger("lfa.signin")

_EMAIL_FIELD_RE = re.compile(r"e-?mail|user ?name|user id|login id|^login$|^user$|phone (number )?or e-?mail|mobile (number )?or e-?mail", re.I)
_HONEYPOT_RE = re.compile(r"robot|honeypot|do not (enter|fill)|leave (this )?(blank|empty)", re.I)
_SIGNIN_SUBMIT_RE = re.compile(r"^\s*(sign ?in|log ?in|login|continue|next|submit|go)\b", re.I)
_THIRD_PARTY_RE = re.compile(r"\b(google|apple|linkedin|facebook|microsoft|github|sso|okta|indeed|x)\b", re.I)
_EMAIL_OPTION_RE = re.compile(r"(sign ?in|log ?in|continue|sign ?up|register)\s*(with|using|via|by)\s*(your )?e-?mail|use (your )?e-?mail|e-?mail (address )?(and|&) password", re.I)
_CREATE_CTA_RE = re.compile(r"^\s*(create (an |your |a |new )?account|sign ?up|register( now)?|join( now)?|create (a )?profile|new user|don'?t have an account)\b", re.I)
_SIGNIN_LINK_RE = re.compile(r"^\s*(sign ?in|log ?in|login|log ?in to apply|sign ?in to apply)\s*$", re.I)
_FORGOT_RE = re.compile(r"forgot|reset|recover", re.I)
_CONSENT_RE = re.compile(r"agree|accept|consent|terms|privacy|policy", re.I)
_EXISTS_RE = re.compile(r"already (exists|registered|in use|have an account|taken|been used)|account (already )?exists|e-?mail (is )?(already )?(in use|registered|taken)", re.I)
_BAD_CREDS_RE = re.compile(r"invalid|incorrect|wrong|unrecognized|not recognized|do(es)? ?n[o']t match|unable to sign|could ?n[o']t (sign|log)|check your (e-?mail|password)|not found|no account", re.I)
_PASSWORD_RULE_RE = re.compile(r"password (must|should|needs|requires)|at least \d|minimum of \d|characters? long", re.I)


@dataclass(slots=True)
class SignInResult:
    status: str  # signed_in | created | needs_verification | no_credentials | failed
    snap: PageSnapshot
    message: str = ""
    account: SiteAccount | None = None


def host_of(url: str) -> str:
    return urlsplit(url or "").netloc.lower().removeprefix("www.").split(":")[0]


def generate_password() -> str:
    """16+ characters with an upper-case letter, a lower-case letter, a digit and a symbol (the
    classes every site's password rule asks for)."""
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(12))
    return "Lf" + core[:5] + "7" + core[5:] + "!a"


def _label(n: SnapshotNode) -> str:
    return " ".join(x for x in (n.name, n.placeholder, n.name_attr, n.id, n.testid) if x)


def password_fields(snap: PageSnapshot) -> list[SnapshotNode]:
    return [n for n in snap.nodes if n.visible and not n.disabled and (n.type or "").lower() == "password"]


def _is_honeypot(n: SnapshotNode) -> bool:
    return bool(_HONEYPOT_RE.search(_label(n))) or ((n.name_attr or "").lower() == "website" and bool(re.search(r"robot|bot", n.name or "", re.I)))


def email_field(snap: PageSnapshot, *, before: int | None = None) -> SnapshotNode | None:
    """The username/email box of a login or sign-up form: typed first, above the password box."""
    cands = [n for n in snap.nodes if n.visible and not n.disabled and n.role in ("textbox", "combobox") and (n.type or "") not in ("password", "search") and not _is_honeypot(n)]
    typed = [n for n in cands if (n.type or "").lower() == "email" or _EMAIL_FIELD_RE.search(_label(n))]
    pool = typed or [n for n in cands if (n.type or "text").lower() in ("text", "email", "tel")]
    if before is not None:
        above = [n for n in pool if n.i < before]
        if above:
            return above[-1]
    return pool[0] if pool else None


def _buttons(snap: PageSnapshot) -> list[SnapshotNode]:
    return [n for n in snap.nodes if n.visible and not n.disabled and n.role in ("button", "link") and n.name]


def email_option_control(snap: PageSnapshot) -> SnapshotNode | None:
    """"Sign in with email" on a chooser page that otherwise only offers Google/Apple sign-in."""
    for n in _buttons(snap):
        if _EMAIL_OPTION_RE.search(n.name) and not _THIRD_PARTY_RE.search(n.name):
            return n
    return None


def signin_link(snap: PageSnapshot) -> SnapshotNode | None:
    """A page's own "Log in" / "Sign in" control (a board's header, a gate popup)."""
    dialog = [n for n in _buttons(snap) if n.in_dialog]
    for pool in (dialog, _buttons(snap)):
        for n in pool:
            if _SIGNIN_LINK_RE.match(n.name) and not _THIRD_PARTY_RE.search(n.name):
                return n
    return None


def create_control(snap: PageSnapshot) -> SnapshotNode | None:
    for n in _buttons(snap):
        if _CREATE_CTA_RE.match(n.name) and not _THIRD_PARTY_RE.search(n.name) and (n.type or "").lower() != "submit":
            return n
    for n in _buttons(snap):
        if _CREATE_CTA_RE.match(n.name) and not _THIRD_PARTY_RE.search(n.name):
            return n
    return None


def is_create_form(snap: PageSnapshot) -> bool:
    """Two password boxes (password + verify), or one with a create/register submit button."""
    pws = password_fields(snap)
    if len(pws) >= 2:
        return True
    if len(pws) == 1:
        return any(_CREATE_CTA_RE.match(n.name) and (n.type or "").lower() == "submit" for n in _buttons(snap))
    return False


def submit_control(snap: PageSnapshot, *, after: int, kind: str) -> SnapshotNode | None:
    """The form's own submit: a submit-typed button below the password box, else the first
    sign-in-/create-named button below it, else anywhere."""
    rx = _CREATE_CTA_RE if kind == "create" else _SIGNIN_SUBMIT_RE
    btns = [n for n in _buttons(snap) if n.role == "button" and not _FORGOT_RE.search(n.name) and not _THIRD_PARTY_RE.search(n.name)]
    below = [n for n in btns if n.i > after]
    for pool in (below, btns):
        for n in pool:
            if (n.type or "").lower() == "submit" and rx.match(n.name):
                return n
        for n in pool:
            if rx.match(n.name) and not (kind == "signin" and _CREATE_CTA_RE.match(n.name)) and not (kind == "create" and _SIGNIN_LINK_RE.match(n.name)):
                return n
    for n in below:
        if (n.type or "").lower() == "submit":
            return n
    return None


def consent_boxes(snap: PageSnapshot, *, near: int) -> list[SnapshotNode]:
    """Unchecked terms/consent checkboxes of a sign-up form."""
    out = []
    for n in snap.nodes:
        if n.role == "checkbox" and n.visible and not n.disabled and not n.checked and n.i > near - 40:
            if n.required or _CONSENT_RE.search(f"{n.name or ''} {n.section or ''}"):
                out.append(n)
    return out


class SignInHelper:
    def __init__(self, repos: Any, audit: Any, *, create_accounts: bool = True, settle_seconds: float = 20.0):
        self._repos = repos
        self._audit = audit
        self._create = create_accounts
        self._settle_seconds = settle_seconds

    # ------------------------------------------------------------------ entry
    async def attempt(self, ctx: Any, snap: PageSnapshot, profile: CandidateProfile, *, task_id: str | None = None) -> SignInResult:
        host = host_of(snap.url)
        account = profile.account_for(host)
        page = snap
        if not password_fields(page):
            opt = email_option_control(page)
            if opt is not None:
                page = await self._click(ctx, page, opt)
        if not password_fields(page):
            link = signin_link(page)
            if link is not None and (account is not None or not self._create or create_control(page) is None):
                page = await self._click(ctx, page, link)
        if account is not None:
            if is_create_form(page) and signin_link(page) is not None:
                page = await self._click(ctx, page, signin_link(page))  # type: ignore[arg-type]
            if not password_fields(page):
                return SignInResult("failed", page, f"could not reach the password box on {host}")
            res = await self._sign_in(ctx, page, account)
            if res.status == "failed":
                await self._audit.log("signin.failed", host=host, note=res.message[:200])
            if res.status == "failed" and account.created_by_agent and self._create and profile.contact.email and "rejected" in res.message:
                # The agent's own earlier sign-up evidently never went through: sign up again.
                page = res.snap
                if not is_create_form(page):
                    ctl = create_control(page)
                    if ctl is not None:
                        page = await self._click(ctx, page, ctl)
                if is_create_form(page):
                    return await self._create_account(ctx, page, profile, host)
            return res
        if self._create and profile.contact.email:
            if not is_create_form(page):
                ctl = create_control(page)
                if ctl is not None:
                    page = await self._click(ctx, page, ctl)
            if is_create_form(page):
                return await self._create_account(ctx, page, profile, host)
        if password_fields(page):
            return SignInResult("no_credentials", page, f"no stored account for {host}: add it under Profile → Site accounts")
        return SignInResult("no_credentials", page, f"no stored account for {host} and no account sign-up on the page")

    # ------------------------------------------------------------------ flows
    async def _sign_in(self, ctx: Any, page: PageSnapshot, account: SiteAccount) -> SignInResult:
        """Sign in with the stored password; when the site rejects it, with each earlier password
        kept for this host (a later sign-up may have replaced the one that works)."""
        res: SignInResult | None = None
        for attempt, password in enumerate([account.password, *account.previous_passwords[:3]]):
            if not password_fields(page):
                break
            pw = password_fields(page)[0]
            email = email_field(page, before=pw.i)
            if email is None:
                return SignInResult("failed", page, "login form without an email/username box")
            await self._type(ctx, email, account.email)
            await self._type(ctx, pw, password, credential=True)
            if not await self._click_submit(ctx, page, after=pw.i, kind="signin"):
                await ctx.execute(BrowserAction(type="press_key", args={"key": "Enter"}, risk=RiskLevel.LOW, verification="none", timeout_ms=5_000), None)
            after = await self._settle(ctx, page)
            res = self._judge(after, page, account=account, created=False)
            if res.status != "failed" or "rejected" not in res.message:
                break
            await self._audit.log("signin.password_rejected", host=account.host, attempt=attempt + 1, note=res.message[:160])
            page = after
        assert res is not None
        return res

    async def _create_account(self, ctx: Any, page: PageSnapshot, profile: CandidateProfile, host: str) -> SignInResult:
        pws = password_fields(page)
        email = email_field(page, before=pws[0].i)
        if email is None:
            return SignInResult("failed", page, "sign-up form without an email box")
        account = SiteAccount(host=host, email=profile.contact.email, password=generate_password(), created_by_agent=True, note="created by the agent")
        # Saved before the site is told: whatever happens next, the password is not lost.
        await self._store(profile, account)
        await self._type(ctx, email, account.email)
        for n in pws[:2]:
            await self._type(ctx, n, account.password, credential=True)
        for box in consent_boxes(page, near=pws[0].i):
            try:
                await ctx.execute(BrowserAction(type="check", target=Target(strategy="node", value=str(box.i)), args={"checked": True}, risk=RiskLevel.MEDIUM, verification="none", timeout_ms=8_000), None)
            except Exception as exc:  # noqa: BLE001 - a decorative box must not stop the sign-up
                log.info("could not tick %r: %s", box.name, exc)
        clicked = await self._click_submit(ctx, page, after=pws[-1].i, kind="create")
        if not clicked:
            return SignInResult("failed", page, "sign-up form without a Create Account button", account=account)
        after = await self._settle(ctx, page)
        return self._judge(after, page, account=account, created=True)

    async def _click_submit(self, ctx: Any, page: PageSnapshot, *, after: int, kind: str) -> bool:
        """Click the form's submit on a FRESH snapshot: typing re-renders React forms (Workday),
        so a node number remembered from before the typing may now point at another element."""
        fresh = await ctx.snapshot()
        pws = password_fields(fresh)
        anchor = pws[-1].i if pws else after
        btn = submit_control(fresh, after=anchor, kind=kind) or submit_control(page, after=after, kind=kind)
        if btn is None:
            return False
        if btn.testid:
            target = Target(strategy="testid", value=btn.testid)
        else:
            target = Target(strategy="node", value=str(btn.i))
        await ctx.execute(BrowserAction(type="click", target=target, args={}, risk=RiskLevel.MEDIUM, verification="none", timeout_ms=20_000, expected_outcome="account created" if kind == "create" else "signed in"), None)
        return True

    # ------------------------------------------------------------------ pieces
    def _judge(self, after: PageSnapshot, before: PageSnapshot, *, account: SiteAccount, created: bool) -> SignInResult:
        from .page_state_helpers import seen_summary

        cls = classify_page(after)
        errors = " ".join(after.signals.error_messages)
        if not errors:
            # Sites that print their complaint in plain text rather than an alert element.
            hints = [m.group(0).strip() for m in re.finditer(r"[^.!?]{0,80}\b(already (?:exists|registered|in use|taken|have an account)|invalid|incorrect|do(?:es)? ?n[o']t match|not match|verification|verify your|sent (?:you )?an? ?e-?mail|an e-?mail has been sent|try again|required)\b[^.!?]{0,80}", after.text_excerpt or "", re.I)]
            errors = " ".join(dict.fromkeys(h for h in hints if not re.search(r"password requirements|use the sign in option|if your account already exists", h, re.I)))[:400]
        what = seen_summary(after) + (f" [{errors[:160]}]" if errors else "")
        if cls.kind == "challenge":
            return SignInResult("needs_verification", after, "the site asks for a verification code or CAPTCHA after signing " + ("up" if created else "in") + what, account=account)
        if not password_fields(after) and not after.form_fields() and after.url == before.url and (signin_label(after.title) or any(n.role == "heading" and n.visible and signin_label(n.text or n.name) for n in after.nodes)):
            # Same address, still titled "Create Account" / "Sign In", and the boxes vanished for
            # a moment (a re-render): not done. Look again before deciding.
            return SignInResult("failed", after, "the sign-" + ("up" if created else "in") + " page is still showing" + what, account=account)
        if password_fields(after):
            if created and _EXISTS_RE.search(errors):
                return SignInResult("failed", after, f"an account for {account.email} already exists on {account.host}: add its password under Profile → Site accounts", account=account)
            if created and _PASSWORD_RULE_RE.search(errors):
                return SignInResult("failed", after, "the site rejected the generated password: " + errors[:120], account=account)
            if errors and _BAD_CREDS_RE.search(errors):
                return SignInResult("failed", after, ("the site rejected the stored password" if not created else "sign-up refused") + ": " + errors[:120], account=account)
            if after.url == before.url and after.content_hash == before.content_hash:
                return SignInResult("failed", after, "the sign-" + ("up" if created else "in") + " click changed nothing", account=account)
            if errors:
                return SignInResult("failed", after, errors[:160], account=account)
            return SignInResult("failed", after, "still on the sign-" + ("up" if created else "in") + " page", account=account)
        if cls.kind == "login":
            return SignInResult("failed", after, "still on a sign-in page after submitting" + what, account=account)
        return SignInResult("created" if created else "signed_in", after, ("account created and signed in" if created else "signed in") + what, account=account)

    async def _store(self, profile: CandidateProfile, account: SiteAccount) -> None:
        raw = await self._repos.profile.get() or {}
        data = {k: v for k, v in raw.items() if k != "updated_at"}
        current = CandidateProfile.model_validate(data)
        old = [a for a in current.accounts if a.host.lower() == account.host.lower()]
        for a in old:
            # Keep every password this host was ever given: if the new sign-up is refused because
            # the account already exists, the earlier password is the one that works.
            for p in [a.password, *a.previous_passwords]:
                if p and p != account.password and p not in account.previous_passwords:
                    account.previous_passwords.append(p)
        current.accounts = [a for a in current.accounts if a.host.lower() != account.host.lower()] + [account]
        await self._repos.profile.save(current.model_dump())
        profile.accounts = list(current.accounts)
        await self._audit.log("signin.account_created", host=account.host, email=account.email)

    async def _type(self, ctx: Any, node: SnapshotNode, text: str, *, credential: bool = False) -> None:
        args: dict[str, Any] = {"text": text}
        if credential:
            args["credential"] = True
        await ctx.execute(BrowserAction(type="type", target=Target(strategy="node", value=str(node.i)), args=args, risk=RiskLevel.LOW, verification="none", timeout_ms=10_000), None)

    async def _click(self, ctx: Any, page: PageSnapshot, node: SnapshotNode) -> PageSnapshot:
        await ctx.execute(BrowserAction(type="click", target=Target(strategy="node", value=str(node.i)), args={}, risk=RiskLevel.LOW, verification="none", timeout_ms=15_000, expected_outcome="sign-in form"), None)
        return await self._settle(ctx, page)

    async def _settle(self, ctx: Any, before: PageSnapshot) -> PageSnapshot:
        """Wait until the page moved on (new address, password box gone or appeared, an error
        shown) or it stopped changing. A page that merely went blank for a moment (React
        re-render after a click) is read again until it has content."""
        waited = 0.0
        snap = await ctx.snapshot()
        last = snap.content_hash
        same = 0
        had_pw = bool(password_fields(before))
        while waited < self._settle_seconds:
            moved = snap.url != before.url or bool(password_fields(snap)) != had_pw or snap.signals.error_messages or classify_page(snap).kind == "challenge"
            if moved and (snap.url != before.url or len(snap.nodes) >= 8 or waited >= 4):
                return snap
            await asyncio.sleep(1.0)
            waited += 1.0
            snap = await ctx.snapshot()
            same = same + 1 if snap.content_hash == last else 0
            last = snap.content_hash
            if same >= 3 and waited >= 4:
                return snap
        return snap


SignInFn = Callable[[Any, PageSnapshot, CandidateProfile], Any]
