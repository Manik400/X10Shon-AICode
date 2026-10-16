#!/usr/bin/env bash
root="$(cd "$(dirname "$0")/.." && pwd)"
exec "$root/agent/.venv/bin/python" "$root/agent/run.py"
