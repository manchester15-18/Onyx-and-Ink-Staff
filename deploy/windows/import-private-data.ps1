param(
    [Parameter(Mandatory = $true)]
    [string]$SourceFolder
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$source = (Resolve-Path $SourceFolder).Path
foreach ($name in @('app.env', 'google-oauth-client.json', 'work', 'reports')) {
    if (-not (Test-Path (Join-Path $source $name))) {
        throw "Missing $name in the selected migration folder."
    }
}
foreach ($name in @('.env', 'google-oauth-client.json', 'work', 'reports')) {
    if (Test-Path (Join-Path $projectRoot $name)) {
        throw "$name already exists in the Windows project. Import only into a fresh Git clone."
    }
}
Copy-Item (Join-Path $source 'app.env') (Join-Path $projectRoot '.env') -Force
Copy-Item (Join-Path $source 'google-oauth-client.json') (Join-Path $projectRoot 'google-oauth-client.json') -Force
Copy-Item (Join-Path $source 'work') (Join-Path $projectRoot 'work') -Recurse -Force
Copy-Item (Join-Path $source 'reports') (Join-Path $projectRoot 'reports') -Recurse -Force
Write-Host "Private Onyx & Ink data imported. Start the dashboard with deploy/windows/start.ps1."
