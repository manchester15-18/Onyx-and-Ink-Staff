#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_URL="${REPOSITORY_URL:-https://github.com/manchester15-18/Onyx-and-Ink-Staff.git}"
INSTALL_ROOT="/opt/onyx"

sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git rsync
sudo systemctl enable --now docker
sudo install -d -m 0750 -o "$USER" -g "$USER" "$INSTALL_ROOT" "$INSTALL_ROOT/data/work" "$INSTALL_ROOT/data/reports" "$INSTALL_ROOT/secrets"

if [[ ! -d "$INSTALL_ROOT/app/.git" ]]; then
  git clone "$REPOSITORY_URL" "$INSTALL_ROOT/app"
else
  git -C "$INSTALL_ROOT/app" fetch origin main
  git -C "$INSTALL_ROOT/app" merge --ff-only origin/main
fi

if [[ ! -f "$INSTALL_ROOT/secrets/app.env" ]]; then
  cp "$INSTALL_ROOT/app/.env.example" "$INSTALL_ROOT/secrets/app.env"
fi
if [[ ! -f "$INSTALL_ROOT/secrets/cloudflared.env" ]]; then
  printf 'TUNNEL_TOKEN=\n' > "$INSTALL_ROOT/secrets/cloudflared.env"
fi
if [[ ! -f "$INSTALL_ROOT/secrets/google-oauth-client.json" ]]; then
  printf '{}\n' > "$INSTALL_ROOT/secrets/google-oauth-client.json"
fi
chmod 0600 "$INSTALL_ROOT/secrets/app.env" "$INSTALL_ROOT/secrets/cloudflared.env" "$INSTALL_ROOT/secrets/google-oauth-client.json"

echo "Oracle host prepared. Add the private values under /opt/onyx/secrets, then run deploy/oracle/deploy.sh."
