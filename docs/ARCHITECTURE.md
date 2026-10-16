# Architecture

## Components

```
┌──────────────────────────────┐      WebSocket (token, origin-checked)      ┌──────────────────────────────┐
│ Chrome extension (MV3, TS)   │ ◄──────────────────────────────────────────► │ Local agent (Python/FastAPI)  │
│  service worker  side panel  │         http://127.0.0.1:8765               │  api/   agent/   browser/     │
│  content script (dom-agent)  │ ◄──────────────────────────────────────────► │  models/ jobs/ applications/  │
└──────────────────────────────┘      REST (bearer, custom header, CORS)      │  persistence/ scheduler/      │
                                                                             │  security/ observability/      │
┌──────────────────────────────┐      REST (same origin, custom header)       │                               │
│ Dashboard (React, TS, Vite)  │ ◄──────────────────────────────────────────► │  serves dashboard/dist at /   │
└──────────────────────────────┘                                              └───────────────┬──────────────┘
                                                                                              │
                                                       ┌──────────────────────────────────────┼─────────────┐
                                                       │ SQLite (WAL, migrations)             │ Ollama /    │
                                                       │ ~/.lfa/lfa.db (0600)                 │ llama.cpp   │
                                                       └──────────────────────────────────────┴─────────────┘
```

## Primary browser-control strategy (decision)

**The extension drives the user's own Chrome.** That is the only way to reuse the user's existing
authenticated sessions without collecting cookies or passwords. The agent sends typed `BrowserAction`
messages over the WebSocket; the extension's content script executes them with the shared
`dom-agent` library and returns a typed `ActionResult` plus a compact page snapshot.

**Playwright is a second transport for the same action schema**, not a competing system:

- it injects the *same* compiled `dom-agent.js` bundle, so locator and action semantics are identical;
- it is used for automated tests against local fixtures and for unauthenticated job-page extraction
  when the extension is not connected;
- it is never used for application submission in the first release (no user sessions).

`BrowserDriver` (`agent/app/browser/driver.py`) is the interface; `ExtensionDriver` and
`PlaywrightDriver` implement it.

## Agent loop

`Observe → Normalize → Classify → Plan → Validate Policy → Execute → Verify → Persist`

| Module | File | Responsibility |
| --- | --- | --- |
| `IntentRouter` | `agent/app/agent/intent.py` | Classifies a natural-language task into a task kind (`apply`, `extract_job`, `navigate`, `read`, `general`) using deterministic rules first and the model when ambiguous |
| `ContextBuilder` | `agent/app/agent/context.py` | Builds a bounded prompt context from the compact snapshot, task state and profile facts (no secrets, no full HTML) |
| `TaskPlanner` | `agent/app/agent/planner.py` | Asks the model gateway for the next structured action; validates it against the schema; falls back to deterministic planners for known page states |
| `PolicyEngine` | `agent/app/agent/policy.py` | Domain allow-list, risk classification, approval requirements, per-domain rate limits, prompt-injection guard (page text never becomes instructions) |
| `ApprovalManager` | `agent/app/agent/approvals.py` | Creates approval requests, persists them, resolves them from dashboard/extension |
| `BrowserExecutor` | `agent/app/agent/executor.py` | Executes one validated action via the driver with timeout, duplicate and loop detection |
| `ResultVerifier` | `agent/app/browser/verifier.py` | Verifies the expected outcome (URL change, element visible, text present, value set); authoritative submission verification |
| `RecoveryManager` | `agent/app/agent/recovery.py` | Maps failures to error categories, decides retry eligibility and backoff |
| `AuditLogger` | `agent/app/observability/audit.py` | Persists every action, transition, approval and failure with redaction |
| `TaskRunner` | `agent/app/agent/runner.py` | The bounded loop: max actions, per-action timeout, deadline, cancellation |

The model only ever returns JSON that must validate against `BrowserAction`
(`agent/app/core/actions.py`). It cannot run code.

## Model gateway

`ModelGateway` (`agent/app/models/gateway.py`) wraps one `ModelProvider` per role
(`intent`, `planner`, `extractor`, `matcher`, `field_mapper`, `classifier`). Providers:

- `OllamaProvider` – `POST /api/chat` with `format: <json schema>`; one `httpx.AsyncClient` reused;
- `OpenAICompatibleProvider` – llama.cpp server / LM Studio `/v1/chat/completions` with `response_format`;
- `HeuristicProvider` – deterministic rules, no network, used in tests and when no runtime is configured.

Gateway features: concurrency semaphore, timeouts, max response size, schema validation with one
bounded repair retry, content-hash cache with TTL, latency/token metrics persisted to `model_metrics`.

## Persistence

SQLite via `aiosqlite`, WAL mode, foreign keys on, numbered SQL migrations in
`agent/migrations/`. Repositories in `agent/app/persistence/repositories/` expose typed methods;
business logic never writes SQL. Tables: `candidate_profile`, `approved_answers`, `resumes`,
`job_sources`, `jobs`, `applications`, `application_transitions`, `execution_attempts`,
`approvals`, `audit_events`, `model_metrics`, `scheduler_config`, `settings`, `pairings`, `tasks`.

Secrets policy: no passwords, cookies, OTPs, tokens or session secrets are ever written. The
extension token is stored hashed (`sha256`) in `pairings`.

## Security model

- API binds `127.0.0.1:8765` (configurable, never `0.0.0.0` by default).
- All `/api/*` requests require header `X-LFA-Client` (forces CORS preflight; a cross-origin web
  page cannot send it without an allow-listed CORS response).
- CORS allow-list: the dashboard origin(s) and the paired `chrome-extension://<id>` origin.
- Extension requests and the WebSocket carry `Authorization: Bearer <token>`; the token comes from a
  short-lived (5 minutes) 8-character pairing code shown in the dashboard.
- WebSocket upgrade validates `Origin` is the paired extension origin.
- Payload size limit 2 MB; snapshots are truncated to a configured node budget.
- Actions are restricted to tabs the extension registered and to allow-listed domains.
- Data directory and database created with owner-only permissions where the OS supports it.

## Dashboard

React 18 + TypeScript + Vite 5, pages: Overview, Jobs, Applications, Profile, Settings, Activity.
All data comes from `/api/*`. Built output (`dashboard/dist`) is served by the agent at `/`.
The API client is a single module so a remote deployment only needs a base URL and an auth header.
