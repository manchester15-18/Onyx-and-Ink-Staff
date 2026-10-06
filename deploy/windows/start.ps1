$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Set-Location $projectRoot
docker compose -f deploy/windows/compose.yml up -d --build
docker compose -f deploy/windows/compose.yml ps
Write-Host "Dashboard is starting at http://localhost:8765"
