# Use-Case Boundaries

This document is the authority on what the Local-First Agent (LFA) does on its own, what it hands
to the user, and what it will never do. The `PolicyEngine` in `agent/app/agent/policy.py` enforces
these rules in code; this document explains them.

## A. Supported autonomous tasks

The agent performs these without asking, inside the configured domain allow-list and rate limits:

- Reading visible page content and the compact accessibility snapshot of the active tab.
- Following links and ordinary website navigation (same-site and allow-listed domains).
- Searching configured job sources (Greenhouse and Lever public posting APIs, user URLs, CSV imports, manual adds from the extension).
- Extracting job descriptions and requirements (schema.org `JobPosting` JSON-LD first, DOM heuristics second, model extraction third).
- Scoring and ranking jobs against the candidate profile with explainable evidence.
- Filling supported form fields with **verified** profile facts and **approved** reusable answers.
- Selecting radio/select/checkbox answers only when the option meaning is unambiguous and the answer is approved.
- Saving drafts where the site exposes a save/draft control.
- Validating completed forms (required fields, formats, uploads, consistency).
- Capturing confirmation messages and application reference numbers as evidence.
- Maintaining the application history and the audit log.
- Retrying transient failures (`NAVIGATION_FAILED`, `MODEL_TIMEOUT`, `RATE_LIMITED`, `DATABASE_ERROR`) within the bounded retry policy.
- Running scheduled work inside the user-configured hours, daily limits and concurrency limits.

### Unattended operation

- **All sites by default**: the extension ships with host permissions for every http(s) site and the agent's allow-list defaults to `*`, so nothing prompts per site. Restrict by replacing `*` with specific domains in Settings. Clicking the extension icon still opens the side panel on that tab; "Apply on this page" then runs the whole workflow there.
- **Never gives up connecting**: the extension stores its pairing token locally and reconnects with unbounded exponential backoff (1 s to 60 s between attempts, plus a one-minute alarm after service-worker suspension). A rejected token (agent database reset, revoked pairing) triggers automatic re-pairing for the build loaded from this project's folder.
- **Auto-submit**: with the side panel's "Submit without asking me" switch (approval policy `never`) the agent submits without a confirmation prompt, and fills review-flagged fields that already hold a verified or approved value. Fields with no known value still pause.
- **Auto-resume**: when the agent hands a tab to the user for a login or challenge, it inspects the next page load of that tab and resumes by itself once the page no longer needs a human. One click on a saved password or a Google account is therefore the only human step on sites that require an account.

## B. Tasks requiring user intervention

The workflow moves to `AUTH_REQUIRED`, `NEEDS_USER_INPUT` or `READY_FOR_REVIEW` and waits:

- Initial extension permissions and pairing with the local service (pairing code entry).
- Manual login when no authenticated session is detected.
- Password reset and account recovery of any kind.
- CAPTCHA, OTP, passkey, email verification, multifactor and any other human verification challenge.
- Ambiguous questions about personal circumstances.
- Salary expectations, work authorization, sponsorship, relocation, notice period, disability, veteran status, criminal history, references, and any legally significant declaration **unless** an approved answer exists in the profile for that exact question category.
- Account creation when the site requires a new credential or a verification step (the user types credentials; the agent never generates or stores them). This includes "Sign in / Sign up with Google": the agent never operates identity-provider pages. It detects the signed-in state afterwards and continues on its own.
- Final submission whenever the approval policy is `always` (default) or the site adapter declares `requires_submit_confirmation`.
- Any action classified `high` risk by the policy engine (payment, deletion, irreversible declarations, sending messages).

The agent preserves whatever authenticated session the user already has in Chrome. It never reads
cookies, local storage, passwords or tokens into its own database.

The agent never bypasses website security controls, never hides the fact that automation is in use,
never evades rate limits, and never defeats anti-bot systems.

## C. Explicitly unsupported behavior

- Universal compatibility with every website. Support is per adapter and per tested pattern; see `SUPPORT_MATRIX.md`.
- Guaranteed accuracy or guaranteed application success.
- Bypassing authentication, CAPTCHA, OTP or access restrictions.
- Creating accounts through automation the site prohibits.
- Inventing candidate information, qualifications, dates, eligibility or answers.
- Reporting an application as submitted without verifiable page evidence.
- Indefinite retries or uncontrolled mass submissions. Retries are bounded; daily limits apply; uncertain outcomes block retries until reconciled.
- Unrestricted access to local files, cookies, passwords or browser profiles. The only files the agent reads are resumes the user registered and the CSV files the user imports.
- Executing model-generated Python, shell, SQL or JavaScript. The model proposes typed actions only.
- Cross-origin iframes, closed shadow DOM, canvas-rendered forms and fully virtualized controls (detected and reported, not worked around).

## Action risk levels

| Risk | Examples | Policy |
| --- | --- | --- |
| low | read, scroll, wait, navigate within allow-list, type into a non-sensitive field | auto |
| medium | select/check answers, upload resume, click non-final buttons, save draft | auto when the value is verified or approved; otherwise pause |
| high | submit application, confirm, delete, pay, send message, declare legal status | approval required per policy (default: always) |
