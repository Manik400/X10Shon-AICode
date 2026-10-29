# Local-First Agent (LFA)

A local-first AI browser agent for job applications: a Manifest V3 Chrome extension that drives
your own browser, a Python/FastAPI service that plans, enforces policy and persists state in
SQLite, and a React dashboard. Inference runs locally (Ollama / llama.cpp) or falls back to
deterministic rules. Nothing leaves your machine unless you configure it to.

```
extension/   Chrome extension (TypeScript, MV3) + shared dom-agent library
agent/       FastAPI service, agent loop, job pipeline, workflow, scheduler, tests, benchmarks
dashboard/   React + Vite dashboard (served by the agent at http://127.0.0.1:8765)
tests/       Local HTML/JSON fixtures used by every browser test
docs/        Architecture, boundaries, support matrix, security, performance, troubleshooting
scripts/     setup / start / test helpers
```

## Requirements

- Python 3.12, Node 18+, Chrome 116+.
- [Ollama](https://ollama.com) (installed on this machine with `winget install Ollama.Ollama`, model `qwen2.5:3b-instruct`) or any OpenAI-compatible local server for model-based planning/extraction/matching. Without one, the `heuristic` provider keeps every workflow functional (see `docs/SUPPORT_MATRIX.md`). Ollama starts with Windows and keeps running in the tray.

## Install

```bash
scripts/setup.sh        # macOS/Linux
```

```powershell
scripts\setup.ps1       # Windows
```

This creates `agent/.venv`, installs dependencies and the Playwright headless Chromium, and builds the extension and dashboard.

## Run

```powershell
scripts\start.ps1
```

Then open <http://127.0.0.1:8765>. `scripts\start-all.ps1` (Desktop shortcut **LFA Start**) starts
the agent and Ollama when they are down and opens the dashboard: the one-click check after a reboot.
On the development machine the agent is registered as the
scheduled task **LFA Agent** (starts at logon, restarts on failure; `scripts\install-autostart.ps1`)
and keeps its data in `D:\MANIK\Projects\Ext\data` (`agent/.env`). Local test forms are served at
`/fixtures/...` for end-to-end checks through the real extension.

1. **Load the extension** into your Chrome: `chrome://extensions` → Developer mode → Load unpacked → `extension/dist`. Loaded from this folder on this machine it **pairs itself** with the running agent (the agent trusts the extension id Chrome derives from that path, nothing else). Loaded from any other path, pair by hand: Dashboard → Settings → *Generate pairing code* → extension options → enter the code (or open the options page with `?code=XXXXXXXX`).
2. Note: Google Chrome 137+ ignores `--load-extension`, so a scripted "dedicated browser" (`scripts/pair_chrome.py`, `scripts/launch-chrome.ps1`) only works with a Chromium build; on the development machine Playwright's headed Chromium failed to start, so the manual load above is the supported path.
3. **Start at logon**: `scripts\install-autostart.ps1` registers a scheduled task for the agent and creates the shortcut (Ollama registers its own startup entry).
3. **Profile**: Dashboard → Candidate profile: contact, skills, experience, legal answers, upload a resume.
4. **Take over a tab**: on any job page click the extension icon. Chrome asks once to allow the site, the side panel opens, and the agent learns the domain.
5. **Apply on this page**: press the button in the side panel. The agent finds the job, opens the form, fills it from verified facts and approved answers, clicks through the steps and submits. Turn on *Submit without asking me* for fully unattended runs. If the site needs a login, sign in once (saved password or Google account); the agent notices and resumes by itself.
6. **Queue in bulk**: paste URLs or a CSV in the Job queue, add Greenhouse/Lever boards as sources, enable the scheduler. Success is recorded only with page evidence.

Enable the scheduler (Settings) to work through the queue within your hours and limits.

## Configuration

Copy `agent/.env.example` to `agent/.env`. Everything else (limits, hours, allowed domains, model roles, resource budgets) is editable in the dashboard.

## Tests

```powershell
scripts\test.ps1
```

Runs the extension (vitest), dashboard (vitest) and agent (pytest, including Playwright-driven end-to-end workflows against local fixtures) suites. No test touches a real job site.

## Documentation

- `docs/USE_CASE_BOUNDARIES.md` – what runs autonomously, what needs you, what is unsupported
- `docs/ARCHITECTURE.md` – components, agent loop, security model
- `docs/SUPPORT_MATRIX.md` – sites, workflows, capabilities, test coverage
- `docs/SECURITY.md` – threat model, revocation, data deletion
- `docs/PERFORMANCE.md` – measured baselines and known gaps
- `docs/TROUBLESHOOTING.md`
- `docs/IMPLEMENTATION_LOG.md` – what was built, test results, remaining work
