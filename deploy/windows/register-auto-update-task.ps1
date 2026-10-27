# Register the Onyx and Ink Staff dashboard auto-update as a Windows
# scheduled task. Run this ONCE on the Windows PC, in PowerShell.
#
#   Right-click PowerShell -> Run as administrator is NOT required for a
#   task that runs only when you are signed in, but use an elevated
#   prompt if Windows asks for permission to create the task.
#
# What it creates:
#   Task name:  "Onyx Staff Dashboard Auto-Update"
#   Action:     powershell -ExecutionPolicy Bypass -File
#               <project>\deploy\windows\auto-update.ps1
#   Trigger:    Every 30 minutes, for 1 year, starting now.
#               (Pass -IntervalMinutes to change it, e.g. 60.)
#               Task Scheduler rejects an "indefinite" repetition
#               duration (it becomes a ~27-million-day XML duration,
#               out of range, error 0x80041318). A 365-day duration is
#               valid; re-run this script once a year to renew it.
#   Settings:   Runs only when you are signed in, does not wake the PC,
#               stops if it runs longer than 15 minutes.
#
# To remove it later:
#   Unregister-ScheduledTask -TaskName "Onyx Staff Dashboard Auto-Update" -Confirm:$false
# To check it:
#   Get-ScheduledTask -TaskName "Onyx Staff Dashboard Auto-Update"
#   Get-ScheduledTaskInfo -TaskName "Onyx Staff Dashboard Auto-Update"
# To run it once right now, without waiting for the schedule:
#   Start-ScheduledTask -TaskName "Onyx Staff Dashboard Auto-Update"
#   # then read work/auto-update.log in the project root.
#
# This script only registers the task. It does not run an update itself,
# does not touch .env, and does not print any secret.

param(
    [int]$IntervalMinutes = 30,
    [string]$ComposeFile = "deploy/windows/compose.tailscale.yml"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$scriptPath = Join-Path $PSScriptRoot "auto-update.ps1"
if (-not (Test-Path $scriptPath)) {
    throw "auto-update.ps1 was not found next to this script."
}

$taskName = "Onyx Staff Dashboard Auto-Update"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -ComposeFile `"$ComposeFile`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 365)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Registered '$taskName' every $IntervalMinutes minutes, for 365 days (re-run this script yearly to renew)."
Write-Host "Script: $scriptPath"
Write-Host "Compose file: $ComposeFile"
Write-Host "Log: $(Join-Path $projectRoot 'work\auto-update.log')"
Write-Host "Run it once now with: Start-ScheduledTask -TaskName '$taskName'"
