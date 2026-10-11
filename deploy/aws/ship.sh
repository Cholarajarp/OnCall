#!/usr/bin/env bash
# Copy the working tree to the instance and run the installer. Re-run to upgrade;
# secrets and data on the server are preserved by install.sh.
#
#   ./deploy/aws/ship.sh [ip]     (defaults to the IP saved by provision.sh)
set -euo pipefail
export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IP=${1:-$(cat "$(dirname "$0")/.last-ip" 2>/dev/null || true)}
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Usage: $0 <elastic-ip>" >&2; exit 1; }
HOST="${IP//./-}.sslip.io"
KEY_FILE="$HOME/.ssh/oncall-key.pem"
SSH=(ssh -i "$KEY_FILE" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "ubuntu@$IP")
MODEL=${ONCALL_BEDROCK_MODEL_ID:-us.amazon.nova-pro-v1:0}

echo "==> Waiting for SSH on $IP"
for _ in $(seq 1 30); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done

echo "==> Packaging source (no venv, data, secrets or node_modules)"
TAR=$(mktemp -t oncall-XXXX.tgz)
tar -C "$ROOT" -czf "$TAR" \
  --exclude=.venv --exclude=node_modules --exclude=data --exclude=.env --exclude='.env.*' --exclude=.remember \
  --exclude=__pycache__ --exclude='*.pyc' --exclude=.kiro --exclude=.claude --exclude='deploy/aws/.last-ip' \
  --exclude=docs/screenshots --exclude='*.db' --exclude='*.log' .
scp -i "$KEY_FILE" -o StrictHostKeyChecking=accept-new -q "$TAR" "ubuntu@$IP:/tmp/oncall.tgz"
rm -f "$TAR"

echo "==> Installing on the server"
"${SSH[@]}" "set -e; rm -rf ~/oncall && mkdir -p ~/oncall && tar -xzf /tmp/oncall.tgz -C ~/oncall && rm /tmp/oncall.tgz
  cd ~/oncall && sudo ONCALL_DOMAIN='$HOST' ONCALL_BEDROCK_MODEL_ID='$MODEL' AWS_REGION=us-east-1 \
    ONCALL_NOTIFY_URL='${ONCALL_NOTIFY_URL:-}' ONCALL_NOTIFY_FORMAT='${ONCALL_NOTIFY_FORMAT:-json}' ./deploy/install.sh"

echo
echo "Live:   https://$HOST"
echo "Status: https://$HOST/status"
echo "Alexa:  https://$HOST/alexa.html"
echo "MCP:    https://$HOST/mcp"
echo "Key:    ssh -i $KEY_FILE ubuntu@$IP sudo grep ONCALL_API_KEY /etc/oncall/oncall.env"
