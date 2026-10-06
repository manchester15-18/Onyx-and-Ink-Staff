#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/an-encrypted-removable-drive"
  exit 2
fi

project_root="$(cd "$(dirname "$0")/../.." && pwd)"
target_root="$1/Onyx-Ink-Private-Migration-$(date +%Y%m%d-%H%M%S)"
if [[ -e "$target_root" ]]; then
  echo "Migration folder already exists; choose a different destination."
  exit 1
fi

mkdir -p "$target_root"
cp "$project_root/.env" "$target_root/app.env"
cp "$project_root/google-oauth-client.json" "$target_root/google-oauth-client.json"
cp -R "$project_root/work" "$target_root/work"
cp -R "$project_root/reports" "$target_root/reports"
chmod -R go-rwx "$target_root" 2>/dev/null || true
echo "Private migration data copied to $target_root. Transfer it only using encrypted storage, then remove it from that storage after Windows is verified."
