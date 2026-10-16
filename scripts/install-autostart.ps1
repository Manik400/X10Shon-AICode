# Registers the LFA agent to start at logon (hidden) and puts a "LFA Chrome" shortcut on the Desktop.
# Ollama already registers its own startup entry when installed.
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root "agent\.venv\Scripts\python.exe"
$run = Join-Path $root "agent\run.py"
$taskName = "LFA Agent"
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$run`"" -WorkingDirectory (Join-Path $root "agent")
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Scheduled task '$taskName' registered (at logon, restarts on failure)."

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop "LFA Chrome.lnk"))
$lnk.TargetPath = "powershell.exe"
$lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $root 'scripts\launch-chrome.ps1')`""
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe,0"
$lnk.Description = "Chrome with the Local-First Agent extension and dashboard"
$lnk.Save()
Write-Host "Desktop shortcut 'LFA Chrome' created."
