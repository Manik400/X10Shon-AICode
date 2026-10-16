# Implementation Plan

Progress is recorded in `IMPLEMENTATION_LOG.md`.

| Phase | Deliverable | Depends on | Acceptance criteria | Tests |
| --- | --- | --- | --- | --- |
| 0 | Audit, boundaries, support matrix, architecture, this plan | — | docs exist and reflect the actual machine | — |
| 1 | FastAPI service, SQLite migrations, pairing, health, extension skeleton, dashboard skeleton | 0 | `GET /api/health` works; extension pairs with a code and reports `connected`; dashboard loads real counts | `tests/test_api_auth.py`, `tests/test_migrations.py`, `extension/tests/protocol.test.ts` |
| 2 | dom-agent library (snapshot, locators, actions, verification), ExtensionDriver, PlaywrightDriver, policy engine | 1 | one end-to-end task on a local fixture through PlaywrightDriver; domain restriction blocks a navigation | `tests/test_browser_actions.py`, `tests/test_policy.py`, `extension/tests/locator.test.ts` |
| 3 | Model gateway with Ollama/OpenAI-compatible/heuristic providers, schema validation, metrics, eval harness | 2 | the planner proposes a schema-valid, policy-allowed action on a fixture page using the heuristic provider; invalid model JSON is rejected and repaired once | `tests/test_model_gateway.py`, `tests/test_planner.py` |
| 4 | Profile, approved answers, resumes, job import (URL/CSV/Greenhouse/Lever), canonicalization, dedup, filters, scoring | 1, 3 | importing the same job twice yields `DUPLICATE`; scoring explains evidence; mandatory filters reject | `tests/test_jobs_dedup.py`, `tests/test_matching.py`, `tests/test_profile.py` |
| 5 | Form detection, semantic field mapping, approved-answer reuse, validation, missing-info pause | 2, 3, 4 | fixtures are filled with provenance per field; missing required field pauses `NEEDS_USER_INPUT` | `tests/test_form_mapping.py`, `tests/test_form_fill_e2e.py` |
| 6 | Application state machine, auth interruption, review, approval, authoritative verification, duplicate prevention, recovery | 5 | login wall → `AUTH_REQUIRED`; submit requires approval; success recorded only with evidence; uncertain outcome blocks retry | `tests/test_state_machine.py`, `tests/test_workflow_e2e.py`, `tests/test_auth_interruption.py` |
| 7 | Dashboard pages, scheduler, retries, cancellation, CSV export | 6 | queue runs within configured limits; pause-all responds during a running task | `tests/test_scheduler.py`, `dashboard/tests/*.test.tsx` |
| 8 | Benchmarks, leak checks, polling removal | 7 | `PERFORMANCE.md` has measured numbers from this machine | `agent/benchmarks/` |
| 9 | Install docs, scripts, security docs, troubleshooting | 8 | clean setup instructions work | `scripts/` |

## Test strategy

- **Unit** (pytest, vitest): pure logic, policy, state machine, dedup, mapping, schema validation.
- **Integration** (pytest + FastAPI `TestClient`/`httpx`): API auth, pairing, CRUD, workflow transitions.
- **Browser** (pytest + Playwright, local HTML fixtures under `tests/fixtures/`): the dom-agent bundle is injected, so these tests cover the same code the extension runs.
- **Extension** (vitest + jsdom): dom-agent locator/snapshot/actions, protocol handling, service-worker message routing with a fake `chrome` API.
- **Dashboard** (vitest + testing-library): data formatting, rate denominators, API client error handling.
- **Benchmarks** (scripts in `agent/benchmarks/`): produce JSON reports consumed by `PERFORMANCE.md`.

All automated tests use synthetic candidate data. No test submits to a real website.

## First milestone

Phase 1: service starts, migrations apply, extension pairs, dashboard shows live counts of zero
jobs and zero applications read from SQLite.
