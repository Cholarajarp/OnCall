#!/usr/bin/env bash
# OnCall production installer for one Ubuntu 24.04 host (EC2 t4g.micro via
# deploy/aws/provision.sh, or any small VM). Idempotent: re-run to upgrade. No Docker.
#
#   sudo ONCALL_DOMAIN=oncall.example.com ./deploy/install.sh
#
# Optional: ONCALL_NOTIFY_URL=https://ntfy.sh/<private-topic> ONCALL_NOTIFY_FORMAT=ntfy
#           ONCALL_BEDROCK_MODEL_ID=<model id>  (needs AWS credentials, see deploy/README.md)
set -euo pipefail

PROM_VERSION=3.15.0
AM_VERSION=0.34.1
CADDY_VERSION=2.11.7
APP_DIR=/opt/oncall/app
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
[[ -n "${ONCALL_DOMAIN:-}" ]] || { echo "Set ONCALL_DOMAIN (DNS A record must point at this host)" >&2; exit 1; }
[[ "$ONCALL_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "Invalid ONCALL_DOMAIN" >&2; exit 1; }
ARCH=$(dpkg --print-architecture)   # amd64 or arm64
[[ "$ARCH" == amd64 || "$ARCH" == arm64 ]] || { echo "Unsupported arch $ARCH" >&2; exit 1; }

echo "==> Swap (512 MB hosts need it)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Packages"
# The Cloudsmith Caddy apt repo now returns 402; remove it (from older installs) before any apt update.
rm -f /etc/apt/sources.list.d/caddy-stable.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl ufw sqlite3 >/dev/null
# Caddy from its official GitHub release .deb (ships the caddy user and systemd unit),
# sha512-verified against the release's checksums file.
if ! command -v caddy >/dev/null || [[ "$(caddy version 2>/dev/null)" != "v$CADDY_VERSION"* ]]; then
  tmp=$(mktemp -d) deb="caddy_${CADDY_VERSION}_linux_$ARCH.deb"
  base="https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION"
  curl -fsSL "$base/$deb" -o "$tmp/$deb"
  curl -fsSL "$base/caddy_${CADDY_VERSION}_checksums.txt" -o "$tmp/sums.txt"
  # Caddy publishes sha512 sums
  (cd "$tmp" && grep " $deb\$" sums.txt | sha512sum -c -)
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$tmp/$deb" >/dev/null
  rm -rf "$tmp"
fi

fetch_verified() {  # name version  -> extracts official release, verifies sha256 from the release's own sums file
  local name=$1 version=$2 file="$1-$2.linux-$ARCH.tar.gz" base="https://github.com/prometheus/$1/releases/download/v$2"
  [[ -x "/opt/$name/$name" && "$("/opt/$name/$name" --version 2>&1 | head -1)" == *"$version"* ]] && return
  local tmp; tmp=$(mktemp -d)
  curl -fsSL "$base/$file" -o "$tmp/$file"
  curl -fsSL "$base/sha256sums.txt" -o "$tmp/sha256sums.txt"
  (cd "$tmp" && grep " $file\$" sha256sums.txt | sha256sum -c -)
  mkdir -p "/opt/$name" && tar -xzf "$tmp/$file" -C "/opt/$name" --strip-components=1
  rm -rf "$tmp"
}
echo "==> Prometheus $PROM_VERSION, Alertmanager $AM_VERSION (checksum verified)"
fetch_verified prometheus "$PROM_VERSION"
fetch_verified alertmanager "$AM_VERSION"

echo "==> Users"
for u in oncall payments releases prometheus alertmanager; do
  id "$u" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$u"
done
usermod -aG payments releases

echo "==> Application"
mkdir -p "$APP_DIR" /var/lib/oncall /var/lib/payments /var/lib/prometheus /var/lib/alertmanager /etc/oncall /var/log/caddy
cp -r "$SRC_DIR"/{brain.py,server.py,oncall_core.py,integrations.py,config.py,agent.py,correlate.py,oauth.py,requirements.txt} "$APP_DIR"/
rm -rf "$APP_DIR"/{app,target_service,production,scripts}
cp -r "$SRC_DIR"/{app,target_service,production,scripts} "$APP_DIR"/
[[ -d /opt/oncall/venv ]] || python3 -m venv /opt/oncall/venv
/opt/oncall/venv/bin/pip install -q --upgrade pip
/opt/oncall/venv/bin/pip install -q -r "$APP_DIR/requirements.txt"
chown -R root:root /opt/oncall && chmod -R a+rX /opt/oncall
chown oncall:oncall /var/lib/oncall && chmod 700 /var/lib/oncall
chown releases:payments /var/lib/payments && chmod 2770 /var/lib/payments
chown prometheus:prometheus /var/lib/prometheus
chown alertmanager:alertmanager /var/lib/alertmanager

echo "==> Secrets (generated once, never printed)"
secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(40))'; }
if [[ ! -f /etc/oncall/releases.env ]]; then
  READ=$(secret); WRITE=$(secret)
  umask 077
  printf 'LEDGER_READ_TOKEN=%s\nROLLBACK_TOKEN=%s\nRELEASES_SERVICE=payments\n' "$READ" "$WRITE" > /etc/oncall/releases.env
  cat > /etc/oncall/oncall.env <<EOF
ONCALL_MODE=live
ONCALL_HOST=127.0.0.1
ONCALL_API_KEY=$(secret)
ONCALL_DB_PATH=/var/lib/oncall/live.db
ONCALL_SERVICE=payments
ONCALL_PUBLIC_URL=https://$ONCALL_DOMAIN
PROMETHEUS_URL=http://127.0.0.1:9090
ONCALL_TELEMETRY_MAX_AGE_SECONDS=120
ONCALL_DEPLOYS_SOURCE=ledger
ONCALL_DEPLOYS_URL=http://127.0.0.1:8400/deployments
ONCALL_DEPLOYS_TOKEN=$READ
ONCALL_MUTATION_ENABLED=true
ONCALL_ROLLBACK_URL=http://127.0.0.1:8400/rollback
ONCALL_ROLLBACK_TOKEN=$WRITE
ONCALL_PUBLIC_STATUS=true
ONCALL_DEMO_CONTROLS=true
ONCALL_OAUTH_ENABLED=true
ONCALL_NOTIFY_URL=${ONCALL_NOTIFY_URL:-}
ONCALL_NOTIFY_FORMAT=${ONCALL_NOTIFY_FORMAT:-json}
ONCALL_BEDROCK_MODEL_ID=${ONCALL_BEDROCK_MODEL_ID:-}
AWS_REGION=${AWS_REGION:-us-east-1}
EOF
  umask 022
fi
# Static OAuth client for Alexa+ (it does not support dynamic registration). Kept
# out of oncall.env until the redirect URIs from `alexa-ai configure-account-linking`
# are known: see deploy/README.md "Connect Alexa+". Root-only; never printed.
if [[ ! -f /etc/oncall/alexa-client.env ]]; then
  umask 077
  printf 'ONCALL_OAUTH_CLIENT_ID=oncall-alexa-%s\nONCALL_OAUTH_CLIENT_SECRET=%s\n' \
    "$(python3 -c 'import secrets; print(secrets.token_hex(6))')" "$(secret)" > /etc/oncall/alexa-client.env
  umask 022
fi
chown root:root /etc/oncall/alexa-client.env && chmod 600 /etc/oncall/alexa-client.env
chown root:releases /etc/oncall/releases.env && chmod 640 /etc/oncall/releases.env
chown root:oncall /etc/oncall/oncall.env && chmod 640 /etc/oncall/oncall.env
# Alertmanager authenticates to OnCall with the same operator key.
grep '^ONCALL_API_KEY=' /etc/oncall/oncall.env | cut -d= -f2- > /etc/oncall/alertmanager-bearer
chown root:alertmanager /etc/oncall/alertmanager-bearer && chmod 640 /etc/oncall/alertmanager-bearer
# Upgrades: apply explicitly passed non-secret settings to an existing env file.
set_env() {  # KEY VALUE (value is validated by config.py at start-up)
  [[ "$2" =~ ^[A-Za-z0-9._:/@=+-]*$ ]] || { echo "Refusing unsafe value for $1" >&2; exit 1; }
  if grep -q "^$1=" /etc/oncall/oncall.env; then sed -i "s|^$1=.*|$1=$2|" /etc/oncall/oncall.env
  else echo "$1=$2" >> /etc/oncall/oncall.env; fi
}
[[ -n "${ONCALL_BEDROCK_MODEL_ID:-}" ]] && set_env ONCALL_BEDROCK_MODEL_ID "$ONCALL_BEDROCK_MODEL_ID"
[[ -n "${ONCALL_NOTIFY_URL:-}" ]] && set_env ONCALL_NOTIFY_URL "$ONCALL_NOTIFY_URL" && set_env ONCALL_NOTIFY_FORMAT "${ONCALL_NOTIFY_FORMAT:-json}"
set_env ONCALL_PUBLIC_URL "https://$ONCALL_DOMAIN"

echo "==> Config and units"
cp "$SRC_DIR"/deploy/{prometheus.yml,alert-rules.yml,alertmanager.yml} /etc/oncall/
chmod 644 /etc/oncall/{prometheus.yml,alert-rules.yml,alertmanager.yml}
/opt/prometheus/promtool check config /etc/oncall/prometheus.yml >/dev/null
/opt/alertmanager/amtool check-config /etc/oncall/alertmanager.yml >/dev/null
cp "$SRC_DIR"/deploy/systemd/*.service /etc/systemd/system/
mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironment=ONCALL_DOMAIN=%s\n' "$ONCALL_DOMAIN" > /etc/systemd/system/caddy.service.d/oncall.conf
cp "$SRC_DIR"/deploy/Caddyfile /etc/caddy/Caddyfile
chown caddy:caddy /var/log/caddy
systemctl daemon-reload

echo "==> First release (only if nothing is deployed yet)"
if [[ ! -f /var/lib/payments/current.json ]]; then
  (cd "$APP_DIR" && sudo -u releases env PAYMENTS_STATE_DIR=/var/lib/payments \
     /opt/oncall/venv/bin/python -m production.releases deploy 3f9a1c2e7b4d --note "Initial stable release" >/dev/null)
fi

echo "==> Firewall: only SSH, HTTP, HTTPS"
ufw --force default deny incoming >/dev/null
ufw allow OpenSSH >/dev/null && ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> Start"
for unit in prometheus alertmanager oncall-releases payments payments-traffic oncall caddy; do
  systemctl enable --now "$unit" >/dev/null 2>&1 || true
  systemctl restart "$unit"
done

echo "==> Backups: nightly SQLite online backup, 7 kept"
cat > /etc/cron.daily/oncall-backup <<'EOF'
#!/bin/sh
d=/var/backups/oncall; mkdir -p $d; chmod 700 $d; t=$(date +%F)
sqlite3 /var/lib/oncall/live.db ".backup $d/live-$t.db"
sqlite3 /var/lib/payments/releases.db ".backup $d/releases-$t.db"
find $d -name '*.db' -mtime +7 -delete
EOF
chmod 755 /etc/cron.daily/oncall-backup

sleep 5
"$SRC_DIR"/deploy/healthcheck.sh || true
echo
echo "OnCall is live at https://$ONCALL_DOMAIN  (public status: https://$ONCALL_DOMAIN/status)"
echo "Operator key: sudo grep ONCALL_API_KEY /etc/oncall/oncall.env"
