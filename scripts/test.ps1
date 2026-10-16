# Runs every test suite.
$root = Split-Path -Parent $PSScriptRoot
Push-Location "$root\extension"; npm run typecheck; npm test; Pop-Location
Push-Location "$root\dashboard"; npm run typecheck; npm test; Pop-Location
Push-Location "$root\agent"; $env:PYTHONIOENCODING = "utf-8"; .\.venv\Scripts\python.exe -m pytest -q; Pop-Location
