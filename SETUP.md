# Setting up the Local-First Agent on a new machine

Step-by-step guide for installing the agent, the dashboard and the Chrome extension from scratch
on another computer. Windows is the primary platform (the autostart task is Windows-only); macOS
and Linux notes are given where the steps differ.

Three parts are installed:

| Part | What it is | Where it runs |
| --- | --- | --- |
| Agent | Python/FastAPI service: plans, fills forms, keeps the SQLite database, serves the dashboard | `http://127.0.0.1:8765` (loopback only) |
| Dashboard | React app (job queue, application tracker, profile, settings) | served by the agent at the same address |
| Extension | Chrome Manifest V3 extension that drives your own browser tabs | loaded unpacked from `extension/dist` |

Local models (Ollama) are optional: without one, the deterministic `heuristic` provider keeps
every workflow working, with weaker job matching and free-text answers.

---

## 1. Prerequisites

Install these first and make sure each is on `PATH` (open a new terminal after installing).

| Tool | Version | Install (Windows) | Check |
| --- | --- | --- | --- |
| Git | any recent | `winget install Git.Git` | `git --version` |
| Python | 3.12.x (3.13 not tested) | `winget install Python.Python.3.12` | `python --version` |
| Node.js | 18 or newer (18.20 is what the dev machine uses) | `winget install OpenJS.NodeJS.LTS` | `node --version`, `npm --version` |
| Google Chrome | 116 or newer | <https://www.google.com/chrome/> | `chrome://version` |
| Ollama (optional, for local models) | latest | `winget install Ollama.Ollama` | `ollama --version` |

On macOS/Linux install the same tools with Homebrew / your package manager; use `python3`
instead of `python` where this guide says `python`.

Hardware: the agent itself is light (about 100 MB RAM). Models are what cost memory:
`qwen2.5:7b-instruct` needs roughly 5 GB free RAM (or VRAM), `qwen2.5:3b-instruct` about 2.5 GB.

---

## 2. Get the code

```powershell
git clone <your-repository-url> Ext
cd Ext
```

Keep the folder path stable afterwards. The agent trusts the extension loaded from **this
folder's** `extension/dist` without a pairing code (the extension id is derived from that path),
so moving the folder later means pairing again (see step 7).

---

## 3. Configure the agent (`agent/.env`)

Copy the example file and edit it:

```powershell
Copy-Item agent\.env.example agent\.env
```

Recommended keys for a fresh machine (everything else can stay at its default):

```ini
# Keep all data (database, resumes, logs) inside the project so every way of starting the agent
# sees the same files. Use the absolute path of your checkout.
LFA_DATA_DIR=C:\path\to\Ext\data

LFA_LOG_LEVEL=INFO

# Ollama desktop app (loopback). Leave as is if you installed Ollama; harmless otherwise.
LFA_OLLAMA_BASE_URL=http://127.0.0.1:11434
LFA_OLLAMA_AUTOSTART=false
LFA_MODEL_REQUEST_TIMEOUT_SECONDS=120

# Big multi-step forms need more actions per job than the default 60.
LFA_MAX_ACTIONS_PER_TASK=150

# Only when Ollama should use a GPU (see step 5).
# LFA_GPU_ENABLED=true
```

Notes:

- Without `LFA_DATA_DIR` the data goes to `%LOCALAPPDATA%\.lfa` (Windows) or `~/.lfa`.
- The agent only ever binds to `127.0.0.1`. Binding to all interfaces is refused on purpose.
- Scheduler limits, active hours, allowed domains and model roles are edited later in the
  dashboard, not in `.env`.

---

## 4. Install dependencies and build

From the repository root:

```powershell
scripts\setup.ps1
```

```bash
scripts/setup.sh        # macOS / Linux
```

The script does, in order:

1. creates the virtual environment `agent/.venv` and installs `agent/requirements.txt`;
2. installs Playwright's headless Chromium (used only for unauthenticated page extraction and the
   test suite, never for applying with your logins);
3. `npm install` and `npm run build` in `extension/` (produces `extension/dist`);
4. `npm install` and `npm run build` in `dashboard/` (produces `dashboard/dist`, served by the agent).

If PowerShell refuses to run scripts, start it with
`powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`.

Manual equivalent, if you prefer to run the steps yourself:

```powershell
python -m venv agent\.venv
agent\.venv\Scripts\python.exe -m pip install --upgrade pip
agent\.venv\Scripts\python.exe -m pip install -r agent\requirements.txt
agent\.venv\Scripts\python.exe -m playwright install chromium-headless-shell
cd extension; npm install; npm run build; cd ..
cd dashboard; npm install; npm run build; cd ..
```

---

## 5. Local models with Ollama (optional but recommended)

1. Install Ollama (step 1) and let its tray app run. It starts with Windows by itself.
2. Pull the model(s). The dev machine uses the 7B model for every role; the 3B one is the
   fallback for machines with less memory:

   ```powershell
   ollama pull qwen2.5:7b-instruct
   ollama pull qwen2.5:3b-instruct
   ```

3. Check it answers:

   ```powershell
   ollama list
   curl http://127.0.0.1:11434/api/tags
   ```

4. GPU (optional). Ollama uses an NVIDIA GPU automatically when the driver is installed. For an
   Intel iGPU via Vulkan the dev machine sets these **user** environment variables (System
   Properties -> Environment Variables, then restart the Ollama tray app):

   ```text
   OLLAMA_KEEP_ALIVE=24h      # keep the model loaded between jobs
   OLLAMA_VULKAN=1            # Intel / AMD GPUs through Vulkan
   OLLAMA_IGPU_ENABLE=1
   ```

   and `LFA_GPU_ENABLED=true` in `agent/.env`. Skip this on a CPU-only machine.

Model roles (which model does intent routing, planning, extraction, matching, field mapping,
classification) are chosen in the dashboard after the first start: **Settings -> Model
selection**, provider `ollama`, model `qwen2.5:7b-instruct` (or `3b`). Until then every role
uses the `heuristic` provider.

---

## 6. Start the agent

First start, in a terminal so you can see the log:

```powershell
scripts\start.ps1
```

```bash
scripts/start.sh        # macOS / Linux
```

Then open <http://127.0.0.1:8765>. The dashboard loads from `dashboard/dist`; the startup log
prints `database ...: N jobs, N applications` so you can confirm the data directory it uses.

Quick health check from another terminal:

```powershell
curl -H "X-LFA-Client: dashboard" http://127.0.0.1:8765/api/health
```

### Start automatically at logon (Windows)

```powershell
scripts\install-autostart.ps1
```

This registers the scheduled task **LFA Agent** (runs `agent\.venv\Scripts\pythonw.exe
agent\run.py` with no console window at logon, on unlock/wake, and as a 10-minute watchdog;
`run.py` exits at once when the agent is already up), makes sure Ollama's tray app is in Startup,
and puts an "LFA Chrome" shortcut on the Desktop. Useful commands afterwards:

```powershell
Get-ScheduledTask -TaskName "LFA Agent"            # state
Stop-ScheduledTask -TaskName "LFA Agent"           # stop the agent
Start-ScheduledTask -TaskName "LFA Agent"          # start it again (also after a code update)
Unregister-ScheduledTask -TaskName "LFA Agent"     # remove the autostart
```

### One click to start everything

```powershell
scripts\start-all.ps1                  # start the agent and Ollama if they are down, open the dashboard
scripts\start-all.ps1 -CreateShortcut  # also put an "LFA Start" shortcut on the Desktop
```

`install-autostart.ps1` creates that shortcut too. After a reboot the agent and Ollama start by
themselves, but the agent has no window and Ollama needs a minute or two: run **LFA Start** (or
open <http://127.0.0.1:8765>) to see that both are up.

On macOS/Linux there is no autostart script; run `scripts/start.sh` in a terminal, or wrap it in
a `launchd` agent / `systemd --user` service that runs `agent/.venv/bin/python agent/run.py`.

---

## 7. Load and pair the Chrome extension

1. In Chrome open `chrome://extensions`, switch on **Developer mode** (top right).
2. **Load unpacked** -> select the folder `extension\dist` inside your checkout.
3. Pin the extension (puzzle icon -> pin) so its button is visible.
4. Pairing:
   - **Loaded from this checkout's `extension/dist` on the machine where the agent runs**: it
     pairs itself the first time it connects. The side panel shows *connected* and *paired*.
   - **Any other path, or a different machine**: Dashboard -> **Settings -> Extension pairing ->
     Generate pairing code**, then open the extension's options page (right-click the extension
     icon -> Options) and enter the 8-character code. Codes are single-use and expire after a day.
5. Open the side panel: click the extension icon on any page. The first time on a site Chrome asks
   you to allow it; say yes, or use **Allow this site** in the panel. The agent learns the domain.

Google Chrome 137+ ignores `--load-extension`, so the "dedicated browser" shortcut
(`scripts\launch-chrome.ps1`) only works with Playwright's Chromium build. Loading the extension
by hand into your normal Chrome, as above, is the supported path.

---

## 8. First-time dashboard setup

Open <http://127.0.0.1:8765> and work through:

1. **Candidate profile**: contact details, skills, experience, education, employment, links,
   legal answers (work authorization, sponsorship, countries you may work in), preferences,
   and upload at least one resume (PDF). The agent only fills forms from these verified facts
   and from answers you approve.
2. **Settings -> Model selection**: pick the Ollama models per role (step 5), or leave
   `heuristic`.
3. **Settings -> Scheduler & limits**:
   - *Scheduler enabled*: off until the profile is complete.
   - *Max concurrent background tasks*: 1-5 (start with 1). Tasks you start yourself on a tab
     never wait behind this limit.
   - *Automatic retries after a transient failure*: 0-3 (default 1). A job that fails for its
     own reasons is never retried automatically; use *Retry now* on it in the dashboard.
   - *Daily application preparation limit*, active hours and days, allowed domains (`*` allows
     every http(s) site), and *Submission approval* (`always ask me` until you trust it).
   - *Home list URL* (optional, API only for now): the page that holds your job list; "Apply"
     started on that page works through every job on it. Set it with the settings API, keeping
     the other values as they are:

     ```powershell
     $s = (Invoke-RestMethod http://127.0.0.1:8765/api/settings -Headers @{ "X-LFA-Client" = "dashboard" }).scheduler
     $s.home_list_url = "https://example.com/my-job-list"
     Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8765/api/settings -Headers @{ "X-LFA-Client" = "dashboard" } -ContentType "application/json" -Body ($s | ConvertTo-Json -Depth 5)
     ```
4. **Job queue**: paste a job URL or import a CSV to check that import, screening and scoring
   work. *Empty queue* cancels everything that is waiting without touching sent applications.

---

## 9. Verify the installation

Run every test suite (extension, dashboard, agent; the agent tests drive local HTML fixtures
through Playwright and never touch a real job site):

```powershell
scripts\test.ps1
```

```bash
scripts/test.sh        # macOS / Linux
```

Then try one real job: open a posting in Chrome, click the extension icon, press **Apply to the
job on this page**. With *Submission approval = always*, the dashboard asks you before anything is
submitted.

---

## 10. Moving data from another machine (optional)

Everything the agent knows lives in the data directory (`LFA_DATA_DIR`):

| Item | Where |
| --- | --- |
| Database (jobs, applications, profile, approved answers, pairings, audit log) | `lfa.db` (+ `lfa.db-wal`, `lfa.db-shm`) |
| Uploaded resumes | `resumes/` |
| Logs | `logs/agent.log` |

To move: stop the agent on both machines, copy the whole data directory, point the new
machine's `agent/.env` at it. The extension on the new machine pairs again (automatic from this
checkout's `extension/dist`, otherwise with a new code). Resume file paths stored in the database
are absolute; keep the same `LFA_DATA_DIR` layout or re-upload the resumes.

---

## 11. Updating after a code change

```powershell
git pull
agent\.venv\Scripts\python.exe -m pip install -r agent\requirements.txt   # if requirements changed
cd dashboard; npm run build; cd ..          # dashboard changes
cd extension; npm run build; cd ..          # extension changes
Stop-ScheduledTask -TaskName "LFA Agent"; Start-ScheduledTask -TaskName "LFA Agent"   # or re-run scripts\start.ps1
```

After an extension rebuild, press **Reload** on the extension's card in `chrome://extensions`
(or ask the agent to do it: `POST /api/control/extension/reload` with the `X-LFA-Client: dashboard`
header). Database schema changes are applied automatically at the next start.

---

## 12. Troubleshooting the setup

| Symptom | Fix |
| --- | --- |
| Dashboard says the agent is not reachable | Agent not running: `scripts\start.ps1` or `Start-ScheduledTask "LFA Agent"`. Check nothing else uses port 8765. |
| Extension stays *not paired* | Loaded from a path other than this checkout's `extension/dist`: pair with a code (step 7). After a database reset the build from `extension/dist` re-pairs by itself. |
| Tasks fail with `PERMISSION_DENIED` | Allow the site in the side panel, and make sure the domain is allowed in Settings (or `*`). |
| `Executable doesn't exist` in tests / extraction | `agent\.venv\Scripts\python.exe -m playwright install chromium-headless-shell` |
| `MODEL_TIMEOUT` / model errors | Ollama not running or the model not pulled (`ollama list`); raise `LFA_MODEL_REQUEST_TIMEOUT_SECONDS`, or switch roles to `heuristic` / the 3B model. |
| Data "disappears" after a restart | Two processes use different data directories. Pin `LFA_DATA_DIR` in `agent/.env` and check the startup log line `database ...`. |
| Scheduler status says *waiting for a browser* | Extension not connected (Chrome closed or extension unloaded). Open Chrome; the queue continues by itself. |
| Reset everything | Dashboard -> Activity -> *Delete all data*, or stop the agent and delete the data directory. |

More in `docs/TROUBLESHOOTING.md`, `docs/ARCHITECTURE.md` and `docs/SECURITY.md`.
