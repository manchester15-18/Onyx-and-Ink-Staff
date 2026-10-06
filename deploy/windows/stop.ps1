$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Set-Location $projectRoot
docker compose -f deploy/windows/compose.yml down
