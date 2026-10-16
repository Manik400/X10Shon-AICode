# Launches the dedicated LFA browser (Playwright's Chromium build, which honours --load-extension;
# Google Chrome 137+ ignores it) with the extension loaded and the dashboard open.
# The profile lives in %LOCALAPPDATA%\.lfa\chrome-profile: sign in to job sites once there.
$root = Split-Path -Parent $PSScriptRoot
$ext = Join-Path $root "extension\dist"
$profile = Join-Path $env:LOCALAPPDATA ".lfa\chrome-profile"
$pw = Join-Path $env:LOCALAPPDATA "ms-playwright"
$browser = Get-ChildItem -Path $pw -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike "*headless*" } | Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName "chrome-win64\chrome.exe"; Join-Path $_.FullName "chrome-win\chrome.exe" } | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) { Write-Error "Chromium not found. Run: agent\.venv\Scripts\python.exe -m playwright install chromium"; exit 1 }
New-Item -ItemType Directory -Force $profile | Out-Null
Start-Process -FilePath $browser -ArgumentList @("--user-data-dir=`"$profile`"", "--load-extension=`"$ext`"", "--disable-extensions-except=`"$ext`"", "--no-first-run", "--no-default-browser-check", "http://127.0.0.1:8765")
