# PC-side watcher for the Live command box.
# Polls work/live-terminal/request.json (written by the dashboard container via
# POST /api/live-terminal-run) and runs each queued command through
# deploy/windows/live-terminal-run.ps1 on this PC, so the polling Live tab
# shows real PC output, not container output.
# Start (hidden, survives SSH disconnect): 
#   Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File deploy\windows\live-terminal-watch.ps1" -WindowStyle Hidden
# Stop: create work\live-terminal\watcher.stop (the watcher exits and deletes it).
# It is NOT a service: it stops on reboot/sleep. The Live tab shows
# "PC watcher: offline" from its heartbeat when it is not running.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $root) { $root = Get-Location }
$dir = Join-Path $root 'work\live-terminal'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
# Drop a stale in-flight request from a crashed run; do not re-run it.
$runningFile = Join-Path $dir 'request-running.json'
if (Test-Path $runningFile) { Remove-Item $runningFile -Force -ErrorAction SilentlyContinue }
while ($true) {
    try {
        [System.IO.File]::WriteAllText((Join-Path $dir 'watcher.json'), (@{lastSeen=(Get-Date).ToUniversalTime().ToString('o')} | ConvertTo-Json), $utf8NoBom)
        $stopFile = Join-Path $dir 'watcher.stop'
        if (Test-Path $stopFile) { Remove-Item $stopFile -Force; break }
        $reqFile = Join-Path $dir 'request.json'
        if (Test-Path $reqFile) {
            $req = Get-Content $reqFile -Raw | ConvertFrom-Json
            if ($req.status -eq 'pending') {
                $command = [string]$req.command
                Move-Item $reqFile $runningFile -Force
                $lower = $command.ToLower()
                $blocked = ($command -match '[&|;><`$()]') -or ($lower -match '\.env|authorized_keys|wifi-password|api_key|password|secret|token|ts_authkey|del |remove-item|format |shutdown|restart-computer')
                if ($blocked) {
                    $log = Join-Path $dir 'output.log'
                    [System.IO.File]::WriteAllText($log, 'Blocked: command was not run from the Live command box.', $utf8NoBom)
                    $now = (Get-Date).ToUniversalTime().ToString('o')
                    [System.IO.File]::WriteAllText((Join-Path $dir 'current.json'), (@{status='done'; command=$command; startedAt=$now; endedAt=$now; exitCode=1} | ConvertTo-Json), $utf8NoBom)
                } else {
                    & powershell -ExecutionPolicy Bypass -File (Join-Path $root 'deploy\windows\live-terminal-run.ps1') -Command $command
                }
                if (Test-Path $runningFile) { Remove-Item $runningFile -Force -ErrorAction SilentlyContinue }
            }
        }
    } catch {
        try { Add-Content -Path (Join-Path $dir 'watch.log') -Value ("$((Get-Date).ToUniversalTime().ToString('o')) watcher error: $($_.Exception.Message)") -ErrorAction SilentlyContinue } catch {}
    }
    Start-Sleep -Seconds 2
}
