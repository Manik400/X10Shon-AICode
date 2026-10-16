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

## Next implementation step
Install a local runtime (e.g. Ollama with a 1.5–3B instruct model), set the `extractor`,
`field_mapper` and `matcher` roles to it in Settings, run `POST /api/settings/models/evaluate`
and the benchmark with `--with-model`, and record the model figures in `PERFORMANCE.md`.
