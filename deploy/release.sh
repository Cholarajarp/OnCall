#!/usr/bin/env bash
# Ship a release through the release controller (what CI would call).
#   sudo ./deploy/release.sh 8c2d5e91a0f3 "Tighten payment gateway timeout"
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
commit=${1:?commit required}; note=${2:-}
[[ "$commit" =~ ^[0-9a-f]{7,64}$ ]] || { echo "commit must be lowercase hex" >&2; exit 1; }
cd /opt/oncall/app
sudo -u releases env PAYMENTS_STATE_DIR=/var/lib/payments \
  /opt/oncall/venv/bin/python -m production.releases deploy "$commit" --note "$note"
