#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="/opt/onyx"
APP_ROOT="$INSTALL_ROOT/app"

cd "$APP_ROOT"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Deployment stopped: tracked files on the server have local changes."
  exit 1
fi
git fetch origin main
git merge --ff-only origin/main
docker compose -f deploy/oracle/compose.yml build dashboard
docker run --rm onyx-and-ink-staff:latest python -m unittest discover -s tests -p 'test_*.py'
docker compose -f deploy/oracle/compose.yml up -d --remove-orphans
docker compose -f deploy/oracle/compose.yml ps
