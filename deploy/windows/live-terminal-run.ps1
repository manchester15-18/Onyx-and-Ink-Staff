# Live terminal wrapper for Donald's SSH runs on the Windows PC.
# Usage (from PowerShell on the PC, or via SSH):
#   powershell -ExecutionPolicy Bypass -File deploy/windows/live-terminal-run.ps1 -Command "git status"
# Writes, under work/live-terminal/ (git-ignored, mounted into the dashboard container as /app/work/live-terminal):
#   current.json - {status: running|done, command, startedAt, endedAt, exitCode}
#   output.log   - stdout+stderr, appended by the OS as the command runs, so a polling
#                  dashboard tab sees output grow live, not only at the end.
# Never run a command through this wrapper that prints a secret (.env, keys, tokens).
param([Parameter(Mandatory=$true)][string]$Command)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $root) { $root = Get-Location }
$dir = Join-Path $root 'work\live-terminal'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$current = Join-Path $dir 'current.json'
$log = Join-Path $dir 'output.log'
if (Test-Path $log) { Remove-Item $log -Force }
$started = (Get-Date).ToUniversalTime().ToString('o')
# Write JSON with UTF-8 *without* BOM: PowerShell's -Encoding utf8 adds a BOM, which
# breaks json.loads() in the dashboard container (it would read status as idle).
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($current, (@{status='running'; command=$Command; startedAt=$started; endedAt=$null; exitCode=$null} | ConvertTo-Json), $utf8NoBom)
# cmd redirection writes to the file incrementally, so polling sees it live.
& cmd.exe /c "$Command > `"$log`" 2>&1"
$code = $LASTEXITCODE
$ended = (Get-Date).ToUniversalTime().ToString('o')
[System.IO.File]::WriteAllText($current, (@{status='done'; command=$Command; startedAt=$started; endedAt=$ended; exitCode=$code} | ConvertTo-Json), $utf8NoBom)
exit $code
