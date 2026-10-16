# Troubleshooting

## The dashboard says the local agent is not reachable
- Start it: `scripts\start.ps1` (Windows) or `scripts/start.sh`. It prints `LFA agent listening on http://127.0.0.1:8765`.
- Port in use? Set `LFA_PORT` in `agent/.env` and update the extension's agent URL in its options page.
- `GET http://127.0.0.1:8765/api/health` without the `X-LFA-Client` header returns 401 by design; the dashboard sends it automatically.

## Extension shows "not paired" / "Pairing token rejected"
- Dashboard → Settings → Generate pairing code → enter it in the extension options within 5 minutes. Codes are single-use.
- After 5 wrong codes within a minute, claims are refused for a minute.
- If you revoked the pairing in the dashboard, unpair in the extension and pair again.

## Extension says "connected" but tasks fail with PERMISSION_DENIED
- Open the side panel on the tab and click **Allow this site**. The extension never acts on sites you did not allow.
- The agent also has its own allow-list (Settings → Allowed domains). Tasks started from the side panel on the current tab add that domain automatically for that task.

## The side panel never updates while a task runs
- The service worker reconnects on an alarm (every minute) if the socket dropped. Click **Reconnect** in the side panel.
- Chrome suspends service workers after inactivity; the heartbeat keeps the socket alive only while connected. Check `chrome://extensions` → the extension → "Service worker" link for errors.

## Application stuck in AUTH_REQUIRED
- Log in on the site in the same tab, then press **I have done it – resume** in the side panel (or Applications → Resume). The agent never types passwords, OTPs or solves CAPTCHAs.

## Application in NEEDS_USER_INPUT or READY_FOR_REVIEW
- Open the application in the dashboard; answer the pending questions (they are saved as approved answers), or review the flagged fields. Then **Save answers & re-queue**.
- Legally significant questions (work authorization, sponsorship, disability, veteran, salary …) only auto-fill from the profile's legal section or from an approved answer for that exact question.

## Submission outcome is "uncertain"
- The agent could not find a confirmation message or reference number after clicking submit. Check the site yourself and record the result with **I verified it was submitted** / **It was not submitted**. Retry is blocked until you do, to avoid duplicate applications.

## "SITE_UNSUPPORTED" failures
- The form lives in a cross-origin iframe, a closed shadow root, or a canvas-based UI, or no submit/next button was recognised. Apply manually; see `SUPPORT_MATRIX.md`.

## Model-related errors (MODEL_TIMEOUT, MODEL_OUTPUT_INVALID)
- With no runtime installed all roles use the deterministic `heuristic` provider; nothing times out but free-form planning asks for your guidance.
- With Ollama: check `GET /api/settings/models` in the dashboard (Settings → Model selection shows provider health). Pull the model (`ollama pull <name>`), lower `max_output_tokens`, or raise `LFA_MODEL_REQUEST_TIMEOUT_SECONDS`.
- Repeated invalid JSON from a model: choose a model with reliable structured output, or keep that role on `heuristic`.

## Playwright errors in tests or extraction (`Executable doesn't exist`)
- Run `agent\.venv\Scripts\python.exe -m playwright install chromium-headless-shell`. The Playwright package version pins a specific Chromium build.
- Browser tests are skipped automatically when `extension/dist/dom-agent.js` is missing: run `npm run build` in `extension/`.

## Service restarted during a task
- Running tasks are marked failed with `BROWSER_DISCONNECTED` and their applications go to `RETRY_SCHEDULED` with no automatic retry time; review them in the Applications page before retrying.

## Resource pressure
- Tasks pause when the agent's RSS exceeds the configured budget or system memory > 92%. Lower concurrency, choose a smaller model, or raise the budget in Settings → Resource budgets.

## Data disappears after a restart / extension says "token rejected" after pairing
- Make sure every way of starting the agent uses the same data directory. `agent/.env` pins
  `LFA_DATA_DIR=D:\MANIK\Projects\Ext\data` (inside the project) for exactly this reason: on the
  development machine, processes launched from the AI coding session saw a virtualised copy of
  `%LOCALAPPDATA%\.lfa`, so the scheduled task and the session's server each wrote to a different
  file and a restart looked like a wipe. The startup log line `database …: N jobs, …` shows what
  the running process actually sees.
- The extension recovers on its own: a rejected token is discarded and the build loaded from
  `extension/dist` re-pairs without a code. Other builds need a new pairing code once.

## Where are the logs?
- `<data dir>\logs\agent.log` (JSON lines, rotated at 5 MB × 3). The audit log is in the dashboard (Activity) and in the database (`audit_events`). Exports are redacted.

## Reset everything
- Dashboard → Activity → Delete all data, or stop the agent and delete `%LOCALAPPDATA%\.lfa`.
