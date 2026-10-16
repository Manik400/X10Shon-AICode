# Support Matrix

Status legend: **tested** = covered by automated tests in this repository; **implemented** = code
exists and was exercised manually against fixtures only; **unsupported** = detected and reported.

## Job sources

| Source | Discovery | Extraction | Auth needed | Access method | Status |
| --- | --- | --- | --- | --- | --- |
| User-provided URL | n/a | JSON-LD → DOM heuristics → model | none | Page fetch via extension tab or Playwright | tested (fixtures) |
| CSV import (`url,title,company,location`) | file | as above per URL (lazy) | none | local file | tested |
| Manual add from extension side panel | current tab | as above | user's session | extension | implemented |
| Greenhouse job board (`boards.greenhouse.io/<board>`) | public Job Board API | public Job Board API (`questions=true`) | none for listing | official public JSON API, rate-limited client | tested with recorded fixture |
| Lever postings (`jobs.lever.co/<company>`) | public Postings API | public Postings API | none for listing | official public JSON API | tested with recorded fixture |
| LinkedIn, Indeed, Glassdoor | — | — | — | not implemented: terms restrict automated access; no official public API for applicants | unsupported |

## Application workflows

| Pattern | Fixture | Fill | Validate | Verify submission | Status |
| --- | --- | --- | --- | --- | --- |
| Single-page form with labeled inputs, select, radio, checkbox, file upload | `tests/fixtures/forms/simple_application.html` | yes | yes | confirmation text + reference id | tested |
| Greenhouse-style hosted form (`#application_form`, custom questions, EEOC block) | `tests/fixtures/forms/greenhouse_like.html` | yes | yes | "Thank you for applying" heading | tested |
| Lever-style hosted form (`.application-form`, `cards[...]` names) | `tests/fixtures/forms/lever_like.html` | yes | yes | confirmation URL `/thanks` | tested |
| Multi-step wizard (next/back, dynamic fields appear on selection) | `tests/fixtures/forms/multi_step.html` | yes | yes | final page text | tested |
| Duplicate labels (two "Name" fields in different sections) | `tests/fixtures/forms/duplicate_labels.html` | disambiguated by fieldset/section | yes | — | tested |
| Login wall before form | `tests/fixtures/auth/login_required.html` | pauses `AUTH_REQUIRED` | — | — | tested |
| CAPTCHA / OTP interstitial | `tests/fixtures/auth/captcha.html`, `otp.html` | pauses `HUMAN_VERIFICATION_REQUIRED` | — | — | tested |
| Server-side validation error after submit | `tests/fixtures/forms/rejects_submission.html` | — | — | `SUBMISSION_REJECTED` | tested |
| Ambiguous outcome (no confirmation, no error) | `tests/fixtures/forms/ambiguous_outcome.html` | — | — | `SUBMISSION_UNCERTAIN` | tested |
| Workday, Taleo, SuccessFactors, iCIMS | — | — | — | — | unsupported (not tested; generic filler may partially work; adapters declare `supported=false`) |

## Browser interaction capabilities

| Capability | Status | Notes |
| --- | --- | --- |
| Accessible role + name locators | tested | `extension/src/dom/locator.ts` |
| Label association (`for`, wrapping, `aria-labelledby`, `aria-label`, placeholder) | tested | |
| `data-testid`, `data-qa`, stable `id`/`name` | tested | |
| Visible text with context | tested | |
| Scoped CSS fallback | tested | only when adapter supplies it |
| Text inputs, textarea, select, radio, checkbox, date, file | tested | file upload via `DataTransfer` in extension, `set_input_files` in Playwright |
| React/Vue/Angular controlled inputs | tested (fixture uses native value setter + input events) | |
| Same-origin iframes | implemented | content script runs in `all_frames` |
| Cross-origin iframes | unsupported | reported as `SITE_UNSUPPORTED` |
| Open shadow DOM | implemented | traversed by snapshot |
| Closed shadow DOM, canvas UIs, virtualized lists | unsupported | reported |
| Modal dialogs | implemented | snapshot prefers `role=dialog` subtree when present |

## Model runtimes

| Runtime | Provider | Status |
| --- | --- | --- |
| Ollama 0.40.5 (`/api/chat` with JSON schema `format`), `qwen2.5:3b-instruct`, CPU | `OllamaProvider` | installed and measured on this machine: eval score 0.26 vs 0.80 for rules, p50 21 s, p95 85 s (see `PERFORMANCE.md`). Used only where rules abstain: extractor (no title found), field_mapper (unmapped fields), matcher (semantic blend). |
| llama.cpp server / LM Studio (`/v1/chat/completions`) | `OpenAICompatibleProvider` | implemented; not benchmarked |
| No runtime | `HeuristicProvider` | tested; deterministic, fast; default for intent, planner and classifier roles |
