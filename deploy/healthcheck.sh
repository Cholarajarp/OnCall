#!/usr/bin/env bash
# Post-install / on-demand check of every component. Exit 1 if any check fails.
set -uo pipefail
fail=0
check() {  # label command...
  if "${@:2}" >/dev/null 2>&1; then echo "  ok    $1"; else echo "  FAIL  $1"; fail=1; fi
}
echo "OnCall stack health"
for unit in prometheus alertmanager oncall-releases payments payments-traffic oncall caddy; do
  check "systemd $unit" systemctl is-active --quiet "$unit"
done
check "payments /health"        curl -fsS http://127.0.0.1:9100/health
check "releases /health"        curl -fsS http://127.0.0.1:8400/health
check "prometheus ready"        curl -fsS http://127.0.0.1:9090/-/ready
check "payments scraped (up=1)" sh -c "curl -fsS 'http://127.0.0.1:9090/api/v1/query?query=up%7Bjob%3D%22payments%22%7D' | grep -q '\"1\"\]'"
check "alertmanager ready"      curl -fsS http://127.0.0.1:9093/-/ready
check "oncall api /health"      curl -fsS http://127.0.0.1:8200/health
check "oncall mcp /health"      curl -fsS http://127.0.0.1:8000/health
check "public status endpoint"  curl -fsS http://127.0.0.1:8200/api/public/status
exit $fail
