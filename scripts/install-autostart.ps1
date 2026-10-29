# Registers the LFA agent to run in the background without a console window:
#   - at logon, when the workstation is unlocked (covers wake from sleep), and every 10 minutes as a
#     watchdog (run.py exits at once if the agent is already up);
#   - restarts on failure.
# Also makes sure Ollama's tray app starts at logon (it keeps the local model served with the GPU
# settings stored in the user environment) and puts a "LFA Chrome" shortcut on the Desktop.
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root "agent\.venv\Scripts\pythonw.exe"
$run = Join-Path $root "agent\run.py"
$taskName = "LFA Agent"
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$run`"" -WorkingDirectory (Join-Path $root "agent")
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
# Unlock (session state change 8) is not exposed by the cmdlets; build it from the CIM class.
$unlockClass = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
$unlock = New-CimInstance -CimClass $unlockClass -ClientOnly -Property @{ StateChange = 8; UserId = $user; Enabled = $true }
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logon, $unlock, $watchdog) -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Scheduled task '$taskName' registered (logon, unlock/wake, 10-minute watchdog; no console window)."

# Ollama tray app at logon (the installer normally adds this; re-create it if missing).
$startup = [Environment]::GetFolderPath("Startup")
$ollamaApp = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
if ((Test-Path $ollamaApp) -and -not (Get-ChildItem $startup -Filter "Ollama*.lnk" -ErrorAction SilentlyContinue)) {
  $shell = New-Object -ComObject WScript.Shell
  $l = $shell.CreateShortcut((Join-Path $startup "Ollama.lnk")); $l.TargetPath = $ollamaApp; $l.Save()
  Write-Host "Ollama added to Startup."
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop "LFA Chrome.lnk"))
$lnk.TargetPath = "powershell.exe"
$lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $root 'scripts\launch-chrome.ps1')`""
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe,0"
$lnk.Save()
Write-Host "Desktop shortcut 'LFA Chrome' created."
# "LFA Start": starts the agent and Ollama when they are down and opens the dashboard.
& (Join-Path $root "scripts\start-all.ps1") -NoBrowser -CreateShortcut
Start-ScheduledTask -TaskName $taskName
Write-Host "Agent started."
