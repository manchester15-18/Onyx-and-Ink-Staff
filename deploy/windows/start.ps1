$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Set-Location $projectRoot
foreach ($name in @('.env', 'google-oauth-client.json', 'work', 'reports')) {
    if (-not (Test-Path (Join-Path $projectRoot $name))) {
        throw "$name is missing. Import the private migration data before starting Docker."
    }
}
docker compose -f deploy/windows/compose.yml up -d --build
docker compose -f deploy/windows/compose.yml ps
Write-Host "Dashboard is starting at http://localhost:8765"
