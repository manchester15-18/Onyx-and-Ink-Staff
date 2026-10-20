$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Set-Location $projectRoot
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCommand) {
    throw 'Docker Desktop is not installed or its command-line tools are unavailable.'
}
& $dockerCommand.Source compose -f deploy/windows/compose.yml down
if ($LASTEXITCODE -ne 0) { throw 'Docker could not stop the dashboard. Review the Docker Desktop output above.' }
Write-Host 'Dashboard stopped. Docker Desktop remains open for other applications.'
