$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Set-Location $projectRoot
foreach ($name in @('.env', 'google-oauth-client.json', 'work', 'reports')) {
    if (-not (Test-Path (Join-Path $projectRoot $name))) {
        throw "$name is missing. Import the private migration data before starting Docker."
    }
}

# Open the editable environment file before starting services. The dashboard reads
# .env (not .emv), and blank model keys simply leave those providers disabled.
# Waiting lets newly added keys be saved before Docker reads the configuration.
Write-Host 'Opening .env. Save and close Notepad to continue starting the dashboard.'
Start-Process notepad.exe -ArgumentList (Join-Path $projectRoot '.env') -Wait

$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCommand) {
    throw 'Docker Desktop is not installed or its command-line tools are unavailable.'
}
$dockerPath = $dockerCommand.Source

$dockerReady = $false
try { & $dockerPath info *> $null; $dockerReady = $LASTEXITCODE -eq 0 } catch {}
if (-not $dockerReady) {
    # Docker's CLI lives under DockerDesktop\resources\bin. Derive its installation
    # directory first, because per-user installs do not use Docker's older paths.
    $dockerDesktopRoot = Split-Path (Split-Path (Split-Path $dockerPath -Parent) -Parent) -Parent
    $desktopCandidates = @(
        (Join-Path $dockerDesktopRoot 'Docker Desktop.exe'),
        (Join-Path $dockerDesktopRoot 'resources\Docker desktop.exe'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\frontend\Docker Desktop.exe')
    ) | Where-Object { Test-Path $_ }
    if (-not $desktopCandidates) { throw 'Docker Desktop is installed but its executable could not be found.' }
    Start-Process -FilePath $desktopCandidates[0]
    Write-Host 'Starting Docker Desktop; waiting for its engine...'
    for ($attempt = 1; $attempt -le 120; $attempt++) {
        Start-Sleep -Seconds 1
        try { & $dockerPath info *> $null; if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break } } catch {}
    }
    if (-not $dockerReady) { throw 'Docker Desktop did not become ready within two minutes.' }
}

& $dockerPath compose -f deploy/windows/compose.yml up -d --build
if ($LASTEXITCODE -ne 0) { throw 'Docker could not start the dashboard. Review the Docker Desktop output above.' }
& $dockerPath compose -f deploy/windows/compose.yml ps
if ($LASTEXITCODE -ne 0) { throw 'Docker could not read the dashboard status.' }
Write-Host "Dashboard is starting at http://localhost:8765"

for ($attempt = 1; $attempt -le 90; $attempt++) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8765/healthz' -TimeoutSec 2 | Out-Null
        Start-Process 'http://localhost:8765'
        Write-Host 'Dashboard is ready.'
        exit 0
    } catch { Start-Sleep -Seconds 1 }
}
throw 'The dashboard container started but did not pass its health check within 90 seconds. Run deploy/windows/stop.ps1, then start again.'
