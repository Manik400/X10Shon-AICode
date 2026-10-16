# Starts the local agent (serves the dashboard at http://127.0.0.1:8765).
$root = Split-Path -Parent $PSScriptRoot
& "$root\agent\.venv\Scripts\python.exe" "$root\agent\run.py"
