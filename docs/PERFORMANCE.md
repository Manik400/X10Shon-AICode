# Performance

All numbers below were measured on the development machine described in `REPOSITORY_AUDIT.md`
(i7-1355U, 32 GB RAM, Windows 11, Python 3.12, Playwright headless Chromium 156) with the
benchmark suite in `agent/benchmarks/run_benchmarks.py`. Raw JSON results are written to
`agent/benchmarks/results/`. Re-run with:

```bash
agent/.venv/Scripts/python.exe -m benchmarks.run_benchmarks --with-model
```

## What was measured (run `20261016-074225`, idle machine)

An earlier run (`20261016-073045`) executed while the full pytest suite was running and showed
roughly 2× slower cold start, database and snapshot figures; the numbers below are from an idle
machine and are the baseline.

| Metric | Value | Notes |
| --- | --- | --- |
| Service cold start (connect + migrate + wire services) | 1.1 s | includes Playwright import; API answers immediately after |
| Agent process RSS, idle | 64 MB | no browser, no model loaded |
| Agent process RSS, browser open | 75 MB | headless Chromium is a separate process (~150–250 MB per Chromium, read from Task Manager, not in the JSON) |
| Agent process RSS, during workflow | 76 MB | |
| Import + screen 200 jobs (deterministic scoring) | 3.2 s (16 ms/job) | each import = dedup lookup + insert + filter + score + 3 transitions |
| Job list query (search + sort, 50 rows), p50 / p95 | 4.9 ms / 5.5 ms | indexed on `created_at`, `match_score`, `dedup_key`, `fuzzy_key` |
| Overview counts, p50 / p95 | 1.3 ms / 1.9 ms | |
| Headless browser launch | 1.3 s | once per process; pages are reused |
| Page snapshot (18 interactive nodes), p50 / p95 | 16 ms / 84 ms | p95 is the first snapshot after load |
| `type` action incl. verification, p50 / p95 | 41 ms / 45 ms | |
| Full application workflow on `simple_application.html` | 5.4 s mean (5.3–5.5 s), 12 actions | navigate, snapshot, 9 fills incl. file upload, approval round-trip, submit, verification |
| Model calls per completed workflow | 0 | heuristic provider: rules handle every field on the fixture; no inference needed |
| Model gateway (heuristic), eval harness 9 cases | avg score 0.80, p50 4 ms, p95 6 ms | the two failing expectations are model-grade cases (generic planning) the rules decline by design |
| Extension service-worker activity | event-driven only | no polling loops; one heartbeat frame / 20 s while connected; alarm every 60 s only while disconnected |

## Local model (Ollama 0.40.5, `qwen2.5:3b-instruct`, Q4 GGUF, CPU only)

Installed with `winget install Ollama.Ollama`; the 2 GB MX550 cannot hold the model, so Ollama
runs it on the CPU (`num_gpu=0`). Measured with `python -m benchmarks.configure_model` (eval
harness, `benchmarks/results/eval_qwen2.5_3b-instruct.json`):

| Metric | Rules (`heuristic`) | `qwen2.5:3b-instruct` on CPU |
| --- | --- | --- |
| Eval score, 9 workflow cases | 0.80 | 0.26 |
| p50 latency per call | 4 ms | 21.1 s |
| p95 latency per call | 6 ms | 85.3 s |
| Total for 9 cases | 37 ms | 299 s |
| Failures | 0 | 1 timeout (60 s, 12 k-char extraction prompt), 1 invalid plan (proposed typing into a login form; rejected by the action schema) |
| Cold start (first call after `ollama serve`) | — | ~20 s of the first call's 21 s |
| RAM (Ollama process while loaded) | — | ~2.4 GB resident (Task Manager) |

Mixed configuration actually shipped (`extractor`, `field_mapper`, `matcher` on the model, the rest
on rules), run `20261016-114322` with `--roles extractor,field_mapper`:

| Metric | Value | Notes |
| --- | --- | --- |
| Eval score, 9 cases | 0.66 | rules answer intent/plan instantly; model cases pull the average down |
| Eval p50 / p95 | 5 ms / 18.0 s | |
| Workflow on `simple_application.html`, first run | 25.7 s, 11 actions | one model call (20 s) for the two fields the rules could not place; the model-proposed mappings are flagged for review, so with the default approval policy the run stops at `READY_FOR_REVIEW` instead of submitting |
| Same workflow, second run | 0.8 s | the mapping answer came from the gateway cache |
| Model calls per workflow | 0.33 | |

Consequences, applied in code and settings:
- Rules stay primary for every role. The model is consulted only where rules abstain: job
  extraction when no title is found, field mapping for fields the rules could not place,
  matching as a 40 % blend. Intent, planning and recovery classification stay on rules.
- Default inference timeout raised to 150 s and the context bound lowered to 8 000 chars so CPU
  prompt evaluation fits; extraction prompts are cut to 6 000 chars.
- Model-proposed field mappings are never auto-filled under the default approval policy; with
  auto-submit (`never`) they are filled only when the value comes from a verified fact or an
  approved answer.
- No model is "100 % accurate"; this one is markedly worse than the rules on structured tasks
  and its outputs are always schema-validated and policy-checked before use. A larger model
  (7B+) needs a GPU with ≥ 6 GB or a much longer latency budget on this CPU.

## Not measured (and why)

- **GPU memory**: the model does not fit the 2 GB GPU; nothing was offloaded. **llama.cpp /
  LM Studio** are implemented but not benchmarked. Model figures above are from a single
  machine and one quantization; the gateway records latency, prompt/output tokens and cache hits
  per call in `model_metrics` (dashboard: Activity → Model latency) for any configuration.
- **Browser memory per page** is reported by Chromium, not the agent process; use Task Manager
  / `chrome://system` for the user's Chrome, or `--with-model` runs with Playwright to compare.

## Optimizations already in place

- One Playwright browser/context per process; tabs opened by a task are closed when it ends
  (the first benchmark run exposed a tab leak that hit the `max_concurrent_pages` bound; fixed).
- Snapshots are bounded (`snapshot_max_nodes`), contain no HTML and never include password
  values; the planner context is further compacted to one line per interactive node.
- Deterministic extraction (JSON-LD, adapters' official APIs, text rules) runs before any model;
  deterministic field mapping covers contact/education/links/legal fields so a model is only
  asked about leftovers.
- Model gateway: single reusable HTTP client per provider, `keep_alive` for Ollama, concurrency
  semaphore, content-hash cache with TTL, strict output size and context limits, one bounded
  repair attempt.
- Database: WAL mode, single writer with `BEGIN IMMEDIATE`, indexes on every filtered/sorted
  column, no duplicate page snapshots stored (only fill reports and evidence summaries).
- Scheduler wakes on an interval or explicit kicks; the dashboard polls only visible tabs every
  3–5 s; the extension uses alarms, not timers, for reconnects.
- Waits are state-based: actions poll a page fingerprint for at most 4 s instead of fixed sleeps;
  the only fixed wait is a single 1.5 s grace period after submit when nothing changed.

## Known bottlenecks / next steps

1. Workflow wall-clock (5.4 s for 12 actions, ~450 ms per action while a single `type` costs
   ~40 ms) is dominated by fixed waits: the in-page change-wait after clicks (up to 4 s when the
   page fingerprint does not change), the 1.5 s post-submit grace period, and the approval
   round-trip. Profiling per-action timings from `execution_attempts` and batching consecutive
   `type` actions into one content-script call are the next steps.
2. `import_200_jobs` is write-bound (three transitions per job); batching screening inside one
   transaction would roughly halve it.
3. Model-based roles are untested for latency on this hardware; the 2 GB MX550 will not hold a
   7B model, so CPU inference with a 1.5–3B quantized model is the realistic target for
   intent/classification roles.
