# Starts everything the Local-First Agent needs and opens the dashboard:
#   - the agent (via the "LFA Agent" scheduled task when it is registered, else agent\run.py
#     in the background) if nothing answers on 127.0.0.1:8765;
#   - the Ollama tray app if nothing answers on 127.0.0.1:11434 (skipped when Ollama is not installed);
#   - then waits for both (bounded) and opens http://127.0.0.1:8765 in the default browser.
# Safe to run when everything is already up: it only reports.
#
#   scripts\start-all.ps1                  start what is down, open the dashboard
#   scripts\start-all.ps1 -NoBrowser       same, without opening the browser
#   scripts\start-all.ps1 -CreateShortcut  also put an "LFA Start" shortcut on the Desktop
param(
  [switch]$NoBrowser,
  [switch]$CreateShortcut
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$agentUrl = "http://127.0.0.1:8765"
$ollamaUrl = "http://127.0.0.1:11434"

function Test-Port([int]$port) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $ar = $client.BeginConnect("127.0.0.1", $port, $null, $null)
    if (-not $ar.AsyncWaitHandle.WaitOne(500)) { return $false }
    $client.EndConnect($ar)
    return $true
  } catch { return $false } finally { $client.Close() }
}

function Wait-Port([int]$port, [string]$what, [int]$seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Port $port) { return $true }
    Start-Sleep -Milliseconds 700
  }
  Write-Host "$what did not answer on port $port within $seconds s." -ForegroundColor Yellow
  return $false
}

# ---------------------------------------------------------------- agent
if (Test-Port 8765) {
  Write-Host "Agent: already running ($agentUrl)." -ForegroundColor Green
} else {
  $task = Get-ScheduledTask -TaskName "LFA Agent" -ErrorAction SilentlyContinue
  if ($task) {
    Write-Host "Agent: starting the 'LFA Agent' scheduled task..."
    Start-ScheduledTask -TaskName "LFA Agent"
  } else {
    $pyw = Join-Path $root "agent\.venv\Scripts\pythonw.exe"
    if (-not (Test-Path $pyw)) { throw "agent\.venv not found. Run scripts\setup.ps1 first." }
    Write-Host "Agent: no scheduled task registered (scripts\install-autostart.ps1 creates one); starting agent\run.py in the background..."
    Start-Process -FilePath $pyw -ArgumentList "`"$(Join-Path $root 'agent\run.py')`"" -WorkingDirectory (Join-Path $root "agent") -WindowStyle Hidden
  }
  if (Wait-Port 8765 "The agent" 40) { Write-Host "Agent: up ($agentUrl)." -ForegroundColor Green }
  else { Write-Host "Check <data dir>\logs\agent.log (data dir: LFA_DATA_DIR in agent\.env)." -ForegroundColor Yellow }
}

# ---------------------------------------------------------------- ollama
$ollamaApp = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
if (Test-Port 11434) {
  Write-Host "Ollama: already running ($ollamaUrl)." -ForegroundColor Green
} elseif (-not (Test-Path $ollamaApp)) {
  Write-Host "Ollama: not installed (winget install Ollama.Ollama); the agent keeps working with the heuristic provider." -ForegroundColor Yellow
} else {
  Write-Host "Ollama: starting the tray app..."
  Start-Process -FilePath $ollamaApp
  if (Wait-Port 11434 "Ollama" 60) { Write-Host "Ollama: up ($ollamaUrl). The model loads on the agent's first request." -ForegroundColor Green }
}

# ---------------------------------------------------------------- dashboard
if (-not $NoBrowser) {
  Write-Host "Opening the dashboard..."
  Start-Process $agentUrl
}

# ---------------------------------------------------------------- desktop shortcut
if ($CreateShortcut) {
  $desktop = [Environment]::GetFolderPath("Desktop")
  $shell = New-Object -ComObject WScript.Shell
  $lnk = $shell.CreateShortcut((Join-Path $desktop "LFA Start.lnk"))
  $lnk.TargetPath = "powershell.exe"
  $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
  $lnk.WorkingDirectory = $root
  $lnk.Description = "Start the Local-First Agent and Ollama, open the dashboard"
  $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,137"
  $lnk.Save()
  Write-Host "Desktop shortcut 'LFA Start' created." -ForegroundColor Green
}
