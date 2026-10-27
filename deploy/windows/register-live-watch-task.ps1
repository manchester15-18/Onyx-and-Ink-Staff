# Register the Live terminal watcher as a Scheduled Task so it survives SSH disconnects.
# A Start-Process watcher dies when its SSH session closes (verified Oct 27: heartbeat froze).
# A Scheduled Task process is owned by Task Scheduler, not by an SSH session.
# Task: "Onyx Live Terminal Watcher", runs at logon and starts now, no time limit,
# ignores a second start while one instance is already running.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path (Get-Location) 'deploy\windows\live-terminal-watch.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 365) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopOnIdleEnd
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
try { Unregister-ScheduledTask -TaskName 'Onyx Live Terminal Watcher' -Confirm:$false -ErrorAction Stop } catch {}
Register-ScheduledTask -TaskName 'Onyx Live Terminal Watcher' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Watches work/live-terminal/request.json and runs queued Live command box commands on this PC.' | Out-Null
Start-ScheduledTask -TaskName 'Onyx Live Terminal Watcher'
Write-Host 'REGISTERED and STARTED Onyx Live Terminal Watcher task'
