# Onyx and Ink Staff dashboard auto-update (Windows PC).
#
# Single check, designed to be run by Windows Task Scheduler (see
# register-auto-update-task.ps1), NOT as a loop and NOT via start.ps1.
#
# What it does, in order:
#   1. Fetch origin/main.
#   2. If the PC is already up to date, log and exit 0.
#   3. If the PC is AHEAD of origin/main (remote is an ancestor of HEAD),
#      log that and exit 0 — no pull is needed, even if the working
#      tree is dirty. "Ahead" is not "diverged".
#   4. If the working tree is dirty, staged, mid-merge, on another branch,
#      or truly diverged from origin/main (neither side is an ancestor
#      of the other), log and SKIP (exit 0) — never force a merge, never
#      overwrite local work. This is deliberate: the PC is also the
#      source of the nightly github_sync backup, which stops on remote
#      changes for human review.
#   5. Otherwise, pull with --ff-only.
#   6. Build the dashboard image FIRST. Only if the build succeeds does
#      it run `docker compose up -d`, so a failed build does not take
#      down the running dashboard.
#   7. Log the result to work/auto-update.log (gitignored runtime data).
#
# Parameters:
#   -ComposeFile  Compose file to update. Default is the Tailscale file
#                 the dashboard currently runs under. Pass
#                 "deploy/windows/compose.yml" to update the localhost
#                 stack instead.
#   -Branch       Branch to track. Default "main".
#
# This script never reads, writes, or prints any secret. It never touches
# .env, and it never runs github_sync (which pushes PC changes upward).
# Run it from any directory; it resolves the project root from its own
# location (deploy/windows -> project root).

param(
    [string]$ComposeFile = "deploy/windows/compose.tailscale.yml",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $projectRoot

$logDir = Join-Path $projectRoot "work"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFile = Join-Path $logDir "auto-update.log"

function Write-Log([string]$message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
    Add-Content -Path $logFile -Value $line
    Write-Host $line
}

function Exit-Logged([int]$code, [string]$message) {
    Write-Log $message
    exit $code
}

try {
    if (-not (Test-Path (Join-Path $projectRoot ".git"))) {
        Exit-Logged 1 "SKIP-ERROR: No .git directory in $projectRoot."
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Exit-Logged 1 "SKIP-ERROR: git is not available."
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Exit-Logged 1 "SKIP-ERROR: docker is not available."
    }

    $currentBranch = (git branch --show-current).Trim()
    if ($currentBranch -ne $Branch) {
        Exit-Logged 0 "SKIP: Current branch is '$currentBranch', not '$Branch'."
    }
    if (Test-Path (Join-Path $projectRoot ".git/MERGE_HEAD")) {
        Exit-Logged 0 "SKIP: A merge is in progress. Resolve it manually first."
    }

    git fetch origin 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Exit-Logged 1 "ERROR: git fetch origin failed."
    }

    $localHead = (git rev-parse HEAD).Trim()
    $remoteHead = (git rev-parse "origin/$Branch" 2>&1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $remoteHead) {
        Exit-Logged 1 "ERROR: Could not resolve origin/$Branch."
    }
    if ($localHead -eq $remoteHead) {
        Exit-Logged 0 "OK: Already up to date at $localHead."
    }

    # Ahead: remote is already contained in HEAD, so no pull is needed.
    # Check this BEFORE the clean-tree check, so a dirty tree does not
    # mask an ahead state with a misleading "diverged" message.
    git merge-base --is-ancestor "origin/$Branch" HEAD 2>$null
    if ($LASTEXITCODE -eq 0) {
        $aheadCount = (git rev-list --count "origin/$Branch..HEAD").Trim()
        Exit-Logged 0 "OK: Ahead of origin/$Branch by $aheadCount commit(s); already contains remote, no pull needed."
    }

    # Truly diverged: neither side is an ancestor of the other.
    # Skip, do not merge automatically.
    git merge-base --is-ancestor HEAD "origin/$Branch" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Exit-Logged 0 "SKIP: Local and origin/$Branch have diverged (neither is an ancestor of the other). Review manually; no automatic merge."
    }

    $dirty = git status --porcelain
    if ($dirty) {
        Exit-Logged 0 "SKIP: Working tree is not clean. Commit, stash, or discard local changes first."
    }

    Write-Log "UPDATE: Pulling $localHead -> $remoteHead (fast-forward only)."
    git pull --ff-only origin $Branch 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Exit-Logged 1 "ERROR: git pull --ff-only failed. Dashboard was not restarted."
    }

    Write-Log "UPDATE: Building with compose file '$ComposeFile'."
    docker compose -f $ComposeFile build
    if ($LASTEXITCODE -ne 0) {
        Exit-Logged 1 "ERROR: docker compose build failed. Running dashboard was not replaced."
    }

    Write-Log "UPDATE: Build succeeded. Starting updated dashboard."
    docker compose -f $ComposeFile up -d
    if ($LASTEXITCODE -ne 0) {
        Exit-Logged 1 "ERROR: docker compose up -d failed after a successful build."
    }

    docker compose -f $ComposeFile ps
    Exit-Logged 0 "OK: Updated to $remoteHead and restarted dashboard."
}
catch {
    Exit-Logged 1 "ERROR: $($_.Exception.Message)"
}
