# Implementation Log

Date: 2026-10-16. Single build session on the machine described in `REPOSITORY_AUDIT.md`.

## Phase 0 — audit and scope
- Repository was empty; no existing code or Playwright tooling to reuse.
- Wrote `REPOSITORY_AUDIT.md`, `USE_CASE_BOUNDARIES.md`, `SUPPORT_MATRIX.md`, `ARCHITECTURE.md`, `IMPLEMENTATION_PLAN.md`.
- Decision: the extension in the user's own Chrome is the primary browser-control path (reuses real sessions); Playwright is a second transport for the *same* action schema, used for tests and unauthenticated extraction. Both execute the one compiled `dom-agent` bundle.

## Phase 1 — functional skeleton
- `agent/`: FastAPI app factory with lifespan wiring (`app/state.py`), typed settings (`app/config.py`), SQLite with numbered migrations and a single serialized writer (`app/persistence/db.py`, `migrations/0001_initial.sql`, `0002_jobs_fuzzy_key.sql`), repositories for every table, structured JSON logging with redaction, audit logger, resource monitor.
- Security: loopback bind (wildcard refused), `X-LFA-Client` header + CORS allow-list + Host check for the dashboard, pairing codes (8 chars, 5 min, single use, 5 failures/min), hashed bearer tokens for the extension, extension-origin pinning, WebSocket origin/token checks, 2 MB request limit.
- `extension/`: MV3 manifest with **no** blanket host permissions (per-site grants from the side panel), service worker with alarm-driven reconnect and 20 s heartbeat, content script, side panel, popup, options (pairing, site revocation), esbuild build, generated icons.
- `dashboard/`: Vite + React + TypeScript, one API client module, pages Overview / Job queue / Applications / Profile / Settings / Activity; built output is served by the agent at `/`.
- Acceptance: `GET /api/health` with and without auth, pairing claim, WebSocket connect, dashboard shows live counts — verified by `tests/test_api_auth.py` and manually in the built-in browser (served dashboard renders real `/api/overview` data).

## Phase 2 — browser-control foundation
- `extension/src/dom/`: accessibility names/roles, compact snapshot with sections, forms, signals (password/OTP/CAPTCHA/login-logout CTAs/errors/success/reference numbers/unsupported structures/JSON-LD), locator with ranked strategies and explicit ambiguity, actions (type with native setters + events, select incl. custom comboboxes, check, upload via `DataTransfer`, press key, scroll, extract), bounded state-change waits.
- `agent/app/browser/`: `BrowserDriver` interface, `ExtensionHub` + `ExtensionDriver`, `PlaywrightDriver` injecting the bundle, deterministic `classify_page`, `verify_action`, `verify_submission`.
- `PolicyEngine`: domain allow-list, risk classification (model-declared risk can only raise), credential fields never typed, high-risk clicks need approval, per-domain delay/concurrency, pause.
- Acceptance: `tests/test_browser_actions.py` (16 tests in real headless Chromium) and `tests/test_policy.py`.

## Phase 3 — local model integration
- Provider-independent `ModelGateway` with `OllamaProvider` (JSON-schema `format`), `OpenAICompatibleProvider` (llama.cpp / LM Studio), `HeuristicProvider` (deterministic, offline). Concurrency semaphore, timeouts, response-size and context bounds, strict pydantic validation with one repair attempt, content-hash cache, per-call metrics persisted.
- Evaluation harness (`app/models/evaluation.py`, `benchmarks/eval_cases.json`) exposed via `POST /api/settings/models/evaluate` and the Settings page.
- Acceptance: `tests/test_model_gateway.py`; planner proposes schema-valid, policy-checked actions (`test_read_task_uses_bounded_general_loop`). Invalid model JSON is rejected after one repair (`test_invalid_output_twice_raises`), forbidden actions rejected at schema level.
- Blocker: no model runtime is installed on this machine, so Ollama/OpenAI-compatible providers are implemented and unit-tested at the protocol level but not benchmarked against a real model. Everything runs end to end on the heuristic provider.

## Phase 4 — profile and job ingestion
- `CandidateProfile` with provenance (`verified_fact`, `approved_answer`, `inferred`, `missing`, `contradictory`), completeness report, legal-question detection.
- Import from URL (adapter API → headless page fetch → JSON-LD → text rules → model), CSV, Greenhouse and Lever boards via their official public APIs, manual add from the side panel. Canonical URLs, requisition-id dedup key (unique index), persisted fuzzy key (company|title|location) for secondary duplicates.
- Deterministic filters with reasons, weighted scoring with evidence, optional model semantic score; re-screen on filter change.
- Acceptance: `tests/test_jobs_dedup.py`, `test_matching.py`, `test_profile.py`.

## Phase 5 — form-filling engine
- Rule-based semantic field mapping (synonym tables, legal categories, consent, uploads, approved-answer matching with label normalisation), model mapping only for leftovers and only referencing existing fact keys/answer ids (values are always copied from facts). Fill plan with per-field status (`auto`, `needs_review`, `needs_input`, `skip`, `contradiction`), radio-group planning, option matching, resume selection and SHA-256 check before upload, dynamic-form re-planning, already-filled skipping.
- Acceptance: `tests/test_form_mapping.py` and the e2e fixtures (simple, Greenhouse-like, Lever-like, multi-step wizard with a dynamic field).

## Phase 6 — application workflow
- Explicit state machine with a transition table and persisted history; `ApplicationWorkflow` persists every transition before continuing. Login/challenge → `AUTH_REQUIRED` with manual handoff and resume; blocking questions → `NEEDS_USER_INPUT`; review-only → `READY_FOR_REVIEW`; approval before submit (policy `always` by default); authoritative verification (`confirmation` page kind, success text, reference number, adapter hints) → `APPLIED_CONFIRMED`, else `SUBMISSION_REJECTED`/`SUBMISSION_UNCERTAIN`. Uncertain outcomes block retry until the user reconciles. Composite identity prevents a second application record.
- Acceptance: `tests/test_workflow_e2e.py` (17 scenarios, all in real Chromium), `test_state_machine.py`.

## Phase 7 — dashboard and scheduler
- Scheduler with persisted config: enable, daily limit, concurrency, per-domain limits, allowed domains, active hours/days, retries, approval policy, resource budgets, model roles; restart-safe (interrupted tasks are marked and their applications go to `RETRY_SCHEDULED` for reconciliation).
- Dashboard pages implement every item of the requirements list, with search, sorting, date filters, status filters, pagination and CSV export; approvals and pause-all are available on every page; pending questions can be answered inline (saved as approved answers).
- Acceptance: `tests/test_recovery_and_scheduler.py`, `dashboard/tests/dashboard.test.tsx`, manual check in the built-in browser.

## Phase 8 — optimization and reliability
- Benchmarks: `agent/benchmarks/run_benchmarks.py`; results in `docs/PERFORMANCE.md`. The first benchmark run found a tab leak (runner never closed tabs it opened) which hit the `max_concurrent_pages` bound on the third workflow; fixed and re-measured.
- Removed polling: extension uses alarms + heartbeat; dashboard refreshes only visible tabs; scheduler wakes on interval/kick.

## Phase 9 — release readiness
- `README.md`, `scripts/setup.*`, `scripts/start.*`, `scripts/test.*`, `agent/.env.example`, `agent/run.py`, `.claude/launch.json`, `docs/SECURITY.md`, `docs/TROUBLESHOOTING.md`.

## Commands executed (abridged)
```
python -m venv agent/.venv && pip install -r agent/requirements.txt && playwright install chromium-headless-shell
cd extension && npm install && npm run typecheck && npm run build && npm test
cd dashboard && npm install && npm run typecheck && npm test && npx vite build
cd agent && pytest -q            # 112 tests
cd agent && python -m benchmarks.run_benchmarks
```

## Test results (final run, 2026-10-16)
| Suite | Result |
| --- | --- |
| agent (pytest, incl. 33 Playwright-driven browser/e2e tests) | 112 passed, 0 failed |
| extension (vitest, jsdom + fake chrome API) | 20 passed |
| dashboard (vitest + testing-library) | 7 passed |
| TypeScript strict typecheck (extension, dashboard) | 0 errors |

## Definition-of-done check
1. Extension installs and communicates with the agent — pairing/WebSocket verified by tests; manual end-to-end with a real Chrome profile still to be exercised by the user (needs the unpacked extension loaded).
2. Dashboard shows persisted data — verified in the built-in browser.
3. Profile + approved answers — yes.
4. Import, dedup, filter, score — yes.
5–11. Navigation, form interpretation, safe pauses, auth handoff, approval policy, verified success, duplicate prevention — all covered by the e2e suite on local fixtures.
12. Errors/retries/uncertain outcomes visible — Applications page + Activity.
13. Tests pass — yes.
14. Restart safety — `test_restart_marks_running_tasks_interrupted_and_reconciles`.
15. Performance and limitations documented — `PERFORMANCE.md`, `SUPPORT_MATRIX.md`.
16. Install instructions — `README.md` + scripts (exercised on this machine; a second clean machine has not been tried).

## Remaining work / known gaps
- No local model runtime on this machine: Ollama / OpenAI-compatible providers are untested against real models; model latency/memory figures are missing (`PERFORMANCE.md`).
- Real-site validation: Greenhouse/Lever adapters are tested against recorded API fixtures and look-alike forms, not live submissions (by design, no real mass submissions).
- The extension's side panel approval list and status pushes were tested through the protocol and fake chrome API, not in an interactive Chrome session.
- Workday/Taleo/iCIMS/SuccessFactors are unsupported.
- Possible optimizations listed in `PERFORMANCE.md` (batched fills, batched screening transactions).

## Iteration 2 — unattended operation and a local model (2026-10-16)

Requested: autonomous account creation / login (incl. Google), full permissions, a local model,
and "click the icon, the extension takes the tab, then Apply does the rest".

Declined, with reasons recorded in `USE_CASE_BOUNDARIES.md` and `SECURITY.md`: automated account
creation, password entry, Google sign-in, OTP and CAPTCHA handling. The agent never types
credentials or operates identity-provider pages.

Built instead:
- **Tab takeover**: `chrome.action.onClicked` opens the side panel on that tab, requests the
  per-site grant inside the click gesture, and calls `POST /api/control/allow-domain` so the
  agent's persisted allow-list learns the domain. The popup was removed.
- **Apply on this page / Import & score only** buttons in the side panel start an `apply` /
  `extract_job` task on the current tab; `POST /api/tasks` now imports the job from the tab's own
  snapshot (authenticated pages work) before starting the workflow.
- **Auto-submit switch** (`POST /api/control/approval-policy`): approval policy `never` submits
  without a prompt and fills review-flagged fields that already hold verified/approved values.
  Settings changes propagate to the running workflow through a scheduler config listener.
- **Auto-resume**: the extension hub now feeds `tab_event`s to a handler that, for tasks waiting
  on `AUTH_REQUIRED`/`HUMAN_VERIFICATION_REQUIRED` in that tab, snapshots the page after each
  load and resumes the task once the deterministic classifier no longer sees a login/challenge.
- **Local model**: Ollama 0.40.5 installed with `winget install Ollama.Ollama` (runs in the tray,
  starts with Windows); `qwen2.5:3b-instruct` pulled; `benchmarks/configure_model.py` points
  roles at it and runs the eval harness. Measured: score 0.26 vs 0.80 for the rules, p50 21 s,
  p95 85 s on CPU (details in `PERFORMANCE.md`). Consequently extraction and field mapping were
  made rules-first with the model used only for what the rules cannot place; the persisted role
  configuration is `extractor`/`field_mapper`/`matcher` → Ollama, `intent`/`planner`/`classifier`
  → rules. There is no "100 % accurate" model; outputs stay schema-validated and policy-checked.

Tests added: `tests/test_takeover_and_resume.py` (allow-domain, approval policy propagation,
unattended submit without approval, auto-resume with a fake extension socket), extension
takeover test. Totals after this iteration: agent 117 passed, extension 21 passed, dashboard 7
passed.

## Iteration 3 — turnkey setup for this user (2026-10-16)

- Candidate profile created from `Manik_Goyal_Resume.pdf` (text extracted with pypdf): contact,
  summary, 60 skills, education, employment, project, links; resume uploaded and set as default.
  Completeness 0.87; work-authorization and sponsorship left empty on purpose (not in the resume),
  so forms asking them pause once until answered.
- Unattended settings persisted: approval policy `never`, scheduler on 24×7, 30 applications/day,
  8 s per-domain delay, 2 retries; model roles as in iteration 2.
- **Code-less pairing**: the agent computes the id Chrome assigns to an extension loaded unpacked
  from `extension/dist` (SHA-256 of the lower-cased UTF-16 path, hex→a-p) and accepts a single
  `AUTOPAIR` claim from exactly that origin (`app/security/extension_id.py`,
  `tests/test_autopair.py`). The service worker tries it on install/startup; the options page also
  accepts `?code=`.
- Dead ends recorded: Google Chrome 137+ ignores `--load-extension`, so a scripted Chrome profile
  cannot carry the extension; Playwright's headed Chromium build fails to start on this machine
  (side-by-side configuration error), so `scripts/pair_chrome.py` / `launch-chrome.ps1` are kept but
  unsupported here. Registering the logon scheduled task and restarting the running agent were
  blocked by the session's permission classifier; `scripts/install-autostart.ps1` is ready to run.

## Iteration 4 — "allow everything, never stop" (2026-10-16)

- Extension: host permissions for all http(s) sites in the manifest (no per-site prompts);
  reconnect loop with unbounded exponential backoff (1–60 s) plus a one-minute alarm safety net;
  a rejected token is discarded and the project build re-pairs without a code (`AUTOPAIR`, which
  now replaces any stale pairing for the same id). Pairing tokens never expire; pairing codes are
  single-use and valid for a day.
- Agent: `*` in the allow-list permits every site (new default); scheduler retries raised to 10 for
  this user; WAL checkpoint at startup, every scheduler tick and on close; startup logs the row
  counts the process actually sees.
- Root cause of "paired but not connected" and "data gone after restart": processes launched from
  the AI coding session saw a virtualised copy of `%LOCALAPPDATA%\.lfa`, while the user's scheduled
  task saw the real file, so they diverged. Fixed by pinning `LFA_DATA_DIR` to the project folder
  (`agent/.env`, loaded regardless of working directory). Verified: profile, resume, settings and
  events survive `Stop-ScheduledTask`/`Start-ScheduledTask`.
- Extension id derivation corrected against a real Chrome 155: UTF-16 bytes of the path in its
  original case (`ceddcadeclnpgbjdcfikacbhmmbemckf` for this checkout).

### Verified in the user's real Chrome (2026-10-16, 12:58 UTC)
- The installed extension (loaded unpacked from `extension/dist`) connected with its existing
  token after the token was re-registered against the moved database.
- Found and fixed: `canonicalize_url` rewrote every URL to https, which sent the extension to an
  unreachable https address for http sites; the scheme is now preserved and dedup keys ignore it.
  Tasks that fail before the workflow starts now back off (`QUEUED` → `RETRY_SCHEDULED`) instead
  of being retried every scheduler tick. Extension `open_tab`/`navigate` timeouts were raised
  above the service worker's own 30 s page-load wait. The agent now serves `tests/fixtures` at
  `/fixtures` so the real extension can be exercised locally.
- `POST /api/tasks` on `http://127.0.0.1:8765/fixtures/auth/logged_in_form.html`: the extension
  opened the tab, typed first name and email, clicked submit, the confirmation page was verified
  and the application recorded as `APPLIED_CONFIRMED` (3 actions, ~6 s).

## Iteration 5 — visible cursor and "apply to all on this list" (2026-10-16)

- **On-page cursor and banner** (`extension/src/dom/overlay.ts`): a cursor glides to each target,
  the target is outlined, clicks pulse, and a banner names the current step ("Filling 'Email'",
  "Found 50 jobs; applying one by one"). It lives in a closed shadow root, is skipped by the
  snapshot, and is served from disk, so new tabs get it without an extension reload.
- **List apply** (`apply_list` intent, `TaskRunner._run_apply_list`, `app/jobs/listing.py`): from a
  list page the agent collects job links (ATS/job URLs or "Open"/"Apply" links; generic links such
  as "About" ignored), opens each in a focused new tab, imports and scores it, runs the full
  application workflow, closes the tab, returns focus to the list and moves on. Per-job bounds;
  one job's failure never stops the rest; at most 5 tabs are left open for sign-in/answers (each
  with a waiting task the auto-resume hook continues). On the user's Job Hunt page the finder
  returns 50 career links (Workday, Ashby, Greenhouse-embedded).
- **Reliability**: content-script ping/injection bounded (1.5 s / 8 s), tab activation and
  window focus, new-tab events with opener, `GET /api/control/tabs`, `GET /api/control/tabs/{id}/snapshot`.
- **Self-updating extension**: build 0.2.0 handles `reload_extension`; the agent compares the
  connected build's version with `extension/dist/manifest.json` on every hello and asks older
  builds to reload (once per 5 minutes). Chrome blocks tools from opening `chrome://extensions`,
  so the first reload into 0.2.0 has to be clicked by the user once.
- 7B model: download failed repeatedly (`lookup registry.ollama.ai: no such host`); the machine's
  DNS/internet drops intermittently. Not installed; 3B remains.
- Tests: agent 123 passed (new `tests/test_apply_list.py`, end-to-end list apply over fixtures:
  2 applied, 1 correctly failed), extension 21 passed.

## 2026-10-19 — first real ATS run (Ashby) fixes
- **False CAPTCHA** (`signals.ts` `hasBlockingCaptcha`): reCAPTCHA pre-injects its challenge
  `bframe` inside a `visibility:hidden` container at `top:-10000px`; it counted as a visible
  challenge. Widgets now must be on screen (no hidden/transparent ancestor, ≥60×60, on the page).
- **Re-rendered fields** (`snapshot.ts` fingerprints, `locator.ts` `refind`): React forms swap
  inputs after hydration/autofill, leaving `strategy:"node"` targets disconnected. Each snapshot
  node keeps a fingerprint (id, tag, role, accessible name, name attr, type, nth); a disconnected
  node is re-found by it, never guessed.
- **Yes/No answer buttons** (`dom/choice.ts`): Ashby renders questions as a label plus Yes/No
  buttons. These are reported as a radio group (section = question, `name_attr` = `choice:<hash>`,
  checked from aria/`_active_` class), planned per country like any legal question, and clicked by
  exact option node (`FieldPlan.option_nodes`). Rows like "Open"/"Apply" are never choices.
- **Wrong submit button**: snapshots taken while filling renumber the page; the saved submit index
  hit "Replace" (resume upload). Submit/next are now re-resolved on a fresh snapshot
  (`find_submit_and_next`) right before clicking.
- Profile legal answers set by the user: work rights India; sponsorship needed elsewhere; willing
  to relocate; EEO "Decline to self-identify" (matched to any decline wording by `_choose_option`).
- Per-country answers carry confidence 0.95 (derived from verified facts).

## 2026-10-19 (later) — searchable dropdowns, strict matching, unattended reliability
- **Dropdowns** (`actions.ts`): `read_options` opens a searchable dropdown read-only so plans use
  real options (salary ranges/amounts, multi-select languages, EEO wording). `select` types the
  answer first, picks an option that BEGINS with it (never one that merely contains it: "India"
  once picked "British Indian Ocean Territory"; a recycled option element once gave "North
  Korea"), and returns what the dropdown DISPLAYS; the agent verifies that strictly
  (`values_match`), so a wrong pick fails the field and blocks submission.
- **Background tabs** (`dom/timing.ts`): pauses use MessageChannel in hidden tabs (setTimeout is
  throttled to 1/s, then 1/min); cursor animation skipped when hidden; a frozen tab is brought to
  the front and the action/snapshot retried once.
- **Submission safety**: confirmation polled for ~12 s; "Thanks for your application" and status
  notices recognised; an uncertain submission is never retried automatically (tab left open).
- **Eligibility**: TS/SCI, US-citizen-only and "hire only within the U.S." roles are skipped when
  the user has no US work rights (posting text or form question).
- **Answers** (profile): per-currency salary (+ conversion from USD), referral source/fallback,
  marketing opt-in, why-join / reason-for-leaving texts, residence (Yes only for India), work
  arrangements, graduation from education dates, citizenship, languages (fluent only when asked),
  relatives / non-compete / criminal / veteran / disability / gender, privacy-notice consent,
  experience topics, addresses, alternate phone, national phone number where the form takes the
  country code separately.
- **Runner**: per-job action budget 150; approval wait time no longer counts against the job
  deadline; stale IN_PROGRESS states from cancelled runs are retried; memory pressure waits up to
  2 min instead of failing; private loopback Ollama (127.0.0.1:11435) supervised by the agent.
- Result on the Job Hunt list: 7 confirmed (6 Clera, 1 Granola), 6 ineligible skipped, 3 closed
  postings, 2 uncertain (Trivago, Granola Full Stack: check email), Minitab waiting on US-only
  questions.

## Next implementation step
Install a local runtime (e.g. Ollama with a 1.5–3B instruct model), set the `extractor`,
`field_mapper` and `matcher` roles to it in Settings, run `POST /api/settings/models/evaluate`
and the benchmark with `--with-model`, and record the model figures in `PERFORMANCE.md`.

## 2026-10-21 — Shared Ollama, styled radios, thank-you alerts

- Memory: the agent now uses the Ollama desktop server (127.0.0.1:11434) instead of a private copy on 11435, so qwen2.5:7b is loaded once (RAM 93% → 87%). `LFA_OLLAMA_AUTOSTART=false`.
- Extension 0.2.13: radios/checkboxes hidden with `opacity: 0` (or similar) behind a visible `<label>` are now in the snapshot. Comeet's required "previously worked for…" question was invisible to the agent, which caused a rejected submit.
- Opt-in rule also matches "keep my data … contact me about job opportunities".
- Verifier: a thank-you message rendered in an alert/status box is no longer counted as a site error (Comeet showed "Your application has been submitted" in an alert).
- Cognyte (NI Pune Backend Engineer Junior) submitted and reconciled as confirmed.

## 2026-10-21 (later) — 59-job run analysis: Workday, BambooHR, SmartRecruiters

Of 59 jobs, 50 failed; the causes were three sites, not fifty problems:

- **Workday (~33 jobs)** — the job page is a single-page app that draws its Apply button seconds
  after the URL loads; the agent snapshotted at ~3 s, saw nothing and gave up. The workflow now
  waits for the page to settle (`_settled_snapshot`: re-reads until a form/Apply appears or the
  node count stops changing, ≤12 s) before deciding. Workday's "Apply Manually" then lands on a
  Create Account page; after settling, sign-in walls are re-checked so the job ends as
  AUTH_REQUIRED with a notification (accounts are never created by the agent).
- **BambooHR (15 jobs)** — the whole site was down ("BambooHR temporarily unavailable", later a bare
  `{"error_msg":"404 Route Not Found"}`). `_site_down` recognises outage pages and raises a
  retryable NavigationError → RETRY_SCHEDULED instead of a permanent failure.
- **SmartRecruiters (4 jobs)** — three widget problems in its Lit web components: the resume box
  clears its `<input type=file>` after reading the file (upload now counts as attached when the page
  shows the file name), the country-code list lives in shadow DOM with slotted option text (option
  lookup is now shadow-DOM aware, uses the list's own search box, and `pressOn` sends composed
  PointerEvents), and uploading a resume to the "autofill" box redraws the form (the fill loop now
  re-reads the page and fills what is still empty once before asking the user; attached files are
  not re-uploaded).
- Extension 0.2.14. Field rules: "Preferred First Name" → first name; "Acknowledge" counts as the
  agree option of a privacy-notice dropdown; "keep my data / contact me about opportunities" follows
  the marketing opt-in.
- Memory guard: `LFA_MEMORY_PRESSURE_PERCENT` / `LFA_MEMORY_MIN_AVAILABLE_MB` (96 % / 1.5 GB here):
  this PC sits at 88-95 % because the desktop Ollama keeps qwen2.5:7b pinned (keep-alive ∞) for
  other tools, plus Chrome. `POST /api/tasks` with `application_id` and no URL now takes the
  application's own URL as the task domain.
- Follow-up (same day): a CAPTCHA sitting on an application form (BambooHR) no longer stops the
  job before anything is filled — the page counts as a form, every field is filled, and right
  before submitting the job pauses as AUTH_REQUIRED ("complete the verification, then Resume");
  Resume submits (the hand-off is remembered in `fill_plan.captcha_handoff`). The agent never
  solves or bypasses the check itself. Dropdown search boxes ("Search by country/region or code")
  are skipped by the planner; "School location" maps to the new `education[].location`; values a
  site already holds (resume-parsed "GGSIP University") are kept when equivalent to the profile's
  rather than cleared and re-typed; a type-ahead that offers no suggestion at all keeps the typed
  text. Extension 0.2.15 follows slotted options (`<slot>` assigned nodes) when reading lists.
- Extension 0.2.16: job tabs open in the agent's own window — next to the list tab that started
  the run, else the window the side panel / the previous job tab was in — never in whichever
  Chrome window has focus, and bringing a frozen tab forward no longer focuses its window. The user
  can keep working in a second Chrome window while a list runs.
- Extension 0.2.17: the side panel is scoped per tab (`sidePanel.setOptions`): off by default, on
  for the tab the toolbar icon was clicked on and for every tab the agent opens for its work; tabs
  the user opens by hand never show it. Events the agent fires are `composed`, so inputs inside web
  components are heard by their host (SmartRecruiters upload box); option lists are resolved once
  per selection and found by walking the enclosing shadow trees instead of the whole page (a
  whole-page search through ~1800 shadow roots per poll was timing selections out).

## 2026-10-22 — speed, duplicate entries, hand-finished applications

Timing of a SmartRecruiters fill (25 actions, 170 s): ~50 s opening every dropdown to read its
options, ~50 s in two model calls for the fill plan (one per pass), 7-20 s per custom-dropdown
pick, 0.45 s per typed field. Changes:
- Type-ahead pickers the rules answer with a plain value (city, country, school, employer,
  country code) are no longer opened to read their options; typing the value is the selection.
- Model field mappings are cached per task by label: the redraw/refill pass and later wizard steps
  cost no second model call.
- One education entry and one job: when the form already lists an entry ("Edit education …",
  "Edit experience …" from the resume autofill), that section's blank fields are left alone.
- A paused application (NEEDS_USER_INPUT / AUTH_REQUIRED / READY_FOR_REVIEW) can be reconciled as
  APPLIED_CONFIRMED when the user finished it by hand, so no run reopens it.
- Profile: website/portfolio = https://manik400.github.io/portfolio/.
- Extension 0.2.18: the agent's window is remembered in local storage (session storage was wiped
  by every reload, after which the first job landed in the focused window and dragged all later
  ones there). When nothing is remembered, the window holding the job-list tab is used (the agent
  passes `home_url`; API/dashboard tasks open next to that tab), and failing that a new unfocused
  window is created — the user's current window is never borrowed.
- Verification boxes (BambooHR's visible reCAPTCHA): the form is filled, ONE desktop notification
  covers every waiting job (replaced in place with a count), and a read-only watcher polls the
  paused tab; the moment the user has ticked the box (the widget's response field is filled) the
  task resumes and submits by itself — no Resume click. The agent never ticks or bypasses a check.
- "Apply to each job" works the list on the page the user is on (a saved report, any site); the
  Job Hunt default list is only a fallback when that page holds no jobs. The navigator also
  follows "Open" / "View job" / "Details" controls on the way to the real Apply (8 steps max).
- Fill rules: "Country/Region" is the country (it used to match the state rule via "region");
  Indian cities/states (Mohali, Chandigarh, Punjab, Haryana, Kolkata, Jaipur, …) count as India for
  salary currency and per-country answers; when neither the question nor the job's place names a
  currency, the candidate's own country's currency is used (INR here), never USD by default;
  "as soon as possible" start dates become the nearest Monday in the field's date format.
  Side panel: the guide's ✕ works again (`[hidden]` now wins over the `.stack` flex rule).
- Extension 0.2.20: a select whose action carries no timeout no longer gets a NaN budget (one
  poll); the option poll always has 3-6 s. The resume is planned into the form's Resume field,
  never the "autofill from your resume" drop box (which parses the file and redraws the whole
  form); an upload counts as taken when the box re-renders after receiving the file. Replica
  tests of SmartRecruiters' upload box and country-code picker (shadow DOM, slotted options,
  pointer-only opening) now drive the real action code in tests/webcomponents.test.ts.
- On startup the agent re-arms the verification watchers for every task still waiting on a tab
  (they are asyncio tasks and die with the process), so a tick given after a restart still submits.
- Standing answers (2026-10-22): preferred work location = the job's own place when offered, else
  Bengaluru > Hyderabad > Gurugram > "Any"; notice period = immediate, else the shortest band;
  experience = the band holding the real figure; qualification options Graduate / UG / Bachelor's
  are equivalents of B.Tech; "associated with <company> before" → previously-employed answer (No);
  "connected with associates/employees" → relatives answer (No); approved answers added for
  "offer in hand" (Yes). Chrome's Memory Saver discarded filled tabs waiting for the user (the
  form reloads blank): tabs handed to the user are now set `autoDiscardable: false`
  (extension 0.2.22, `protect_tab`), and the verification watcher survives a briefly
  unresponsive tab instead of giving up.
- Standing answers batch 2: preferred/family name = Goyal (rule), current CTC 9 LPA
  (`preferences.current_salary`, band picker understands a shared "LPA"/"k"/"L" unit, case-
  insensitive), declarations ("I hereby declare…", "I have read and agree…") agreed automatically
  like privacy notices, former employee / currently employed with <company> → No, nationality →
  Indian; approved answers for graduation year/type, right-to-work verification after offer (Yes).
- SmartRecruiters root causes from the in-page trace: the picked option's own textContent is empty
  (slotted) so the pick reported "" → now read through `optionText`; the resume went into the
  "autofill" box because section titles across shadow roots were invisible → `sectionContext`
  now climbs through shadow hosts and accepts a short plain-text block title ("Resume").
- Standing answers batch 3 (approved answers): contract role Yes, managed a team Yes, WFH night
  shift Yes, join in a week Yes, big-enterprise B2B Yes, start-up Yes, US/global clients No,
  marketing-automation features "Workflows, scheduling and dashboards", current/desired annual
  salary in local currency 900000 / 1800000. Referral: "Company careers website", else LinkedIn.
  "Date available to join" follows the nearest-Monday rule. "Why us / what motivates you" boxes are
  now written per job by the local model from the posting (title, company, description) and the
  candidate's background (`FormFiller._compose_open_answers`, cached per task); the stored stance
  is the fallback when no model is configured.
- Workday "Start Your Application" page (Capital One): the Apply click tears the page down and
  Chrome now reports "message channel closed before a response was received" — recognised as a
  navigation (extension 0.2.24) instead of a failed click; the settle wait allows 20 s and needs
  four identical readings over 6 s before concluding a page has nothing, since Workday draws its
  Autofill / Apply Manually links long after its header.
- Workday application steps (Capital One, signed-in session): "Given Name(s)" / "Family Name" /
  address fields count as an application; name fields answered from approved answers count as
  identity; the phone rule no longer catches "Phone Extension" / "Phone Device Type" / country
  phone code; Workday dropdowns whose placeholder says "Search" are no longer skipped as search
  boxes when they carry a label; "phone device type" → Mobile (approved answer); redundant
  preferred/family-name answers removed (the rules cover them).
- Workday wizard (My Experience, Application Questions): bare "Month"/"Year" boxes of a work or
  education block are the start pair then the end pair (end left empty for the current job);
  "I currently work here" → Yes; "Role Description" → the job's description; Workday's listbox
  state texts ("items selected", "Options Expanded") are not questions; after filling a step the
  agent presses Escape so an open type-ahead (skills) cannot push "Save and Continue" out of the
  snapshot; a wizard step reached by Next (or resumed on an open tab) is never judged "not an
  application" for lacking a name/email; "previously, worked at <company>", "personal or business
  ties with anyone employed", "basic qualifications", "senior government official" wordings answered.
- Resync with the user's page: a task started on a tab the user handed over (`WorkflowContext.
  user_tab`) runs as a resume — no reload, their typed answers kept, continue from the step shown;
  the progress banner is taken down when a run ends on a tab that stays with the user. After a
  Next / Save and Continue click the agent waits (≤15 s) until the fields, URL or errors change
  before reading the page: Workday redraws a step slowly and an early snapshot still showed the
  completed step, so the agent re-filled it and pressed Save on an untouched questions page.
- Queue and retries (2026-10-22): the Job queue page has an "Empty queue" button (`GET/POST
  /api/applications/queue[/clear]`): every QUEUED / RETRY_SCHEDULED application is cancelled,
  tasks waiting their turn are cancelled and running list passes / scheduled applications stopped;
  sent applications and tasks on the user's own tab are untouched. Retry storm fixed: a saved
  config of 100 concurrent tasks and 10 retries made the scheduler start dozens of apply tasks
  per tick that died at once on the browser page limit, each death counted as a failed attempt
  and rescheduled; now `max_concurrent_tasks` is 1-5 and `max_retries` 0-3 (default 1; saved
  values load clamped), the RecoveryManager follows that setting, the scheduler asks
  `browser_ready()` before creating work and holds for 5 min when a task finds no browser, and a
  BROWSER_DISCONNECTED failure before the workflow starts no longer counts as the job's attempt.
  A list pass settles each failed job through the same rule as single applies (one more try only
  for a transient failure within the limit, otherwise APPLICATION_FAILED) and skips jobs that
  failed in an earlier pass (note: "failed earlier; use Retry now"), except ones the user paused.
  Tasks started on the user's own tab (tab_id set) run at once instead of waiting behind the
  background queue, and the concurrency limit follows the saved setting at start-up and on change.
  A page where the site says the candidate cannot apply ("You are not eligible for this job",
  "your profile does not match", "not accepting applications from your location") is closed out
  as SKIPPED (CANCELLED, with the notice as evidence) before anything is filled and never retried
  (`page_state.ineligible_notice`; description hypotheticals like "if you do not meet..." ignored).
