#!/usr/bin/env bash
# One-time setup on macOS/Linux. Run from the repository root.
set -euo pipefail
echo "== Python agent =="
python3 -m venv agent/.venv
agent/.venv/bin/python -m pip install --upgrade pip
agent/.venv/bin/python -m pip install -r agent/requirements.txt
agent/.venv/bin/python -m playwright install chromium-headless-shell
echo "== Extension =="
(cd extension && npm install && npm run build)
echo "== Dashboard =="
(cd dashboard && npm install && npm run build)
echo "Done. Start the agent with scripts/start.sh, then load extension/dist in chrome://extensions (Developer mode > Load unpacked)."
