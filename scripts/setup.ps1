# One-time setup on Windows (PowerShell). Run from the repository root.
$ErrorActionPreference = "Stop"
Write-Host "== Python agent =="
python -m venv agent\.venv
agent\.venv\Scripts\python.exe -m pip install --upgrade pip
agent\.venv\Scripts\python.exe -m pip install -r agent\requirements.txt
agent\.venv\Scripts\python.exe -m playwright install chromium-headless-shell
Write-Host "== Extension =="
Push-Location extension; npm install; npm run build; Pop-Location
Write-Host "== Dashboard =="
Push-Location dashboard; npm install; npm run build; Pop-Location
Write-Host "Done. Start the agent with scripts\start.ps1, then load extension\dist in chrome://extensions (Developer mode > Load unpacked)."
