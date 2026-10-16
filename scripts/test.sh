#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
(cd "$root/extension" && npm run typecheck && npm test)
(cd "$root/dashboard" && npm run typecheck && npm test)
(cd "$root/agent" && PYTHONIOENCODING=utf-8 ./.venv/bin/python -m pytest -q)
