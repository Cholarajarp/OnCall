"""Run the production stack locally (loopback only) for a pre-deploy preview.

Same components as deploy/: payments service, release controller, traffic,
Prometheus (deploy/alert-rules.yml), Alertmanager -> OnCall /api/alerts, and
OnCall in LIVE mode with OAuth and the public status page. No Caddy/TLS: plain
http://127.0.0.1. State lives in data/preview/ and survives restarts.

    python scripts/preview.py            # start; Ctrl+C stops everything it started
    python scripts/preview.py --reset    # wipe preview state first
    python scripts/preview.py break      # (second terminal) ship the bad release
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_demo import assert_ports_available, stop_children, wait_ready  # noqa: E402

STATE = ROOT / 'data' / 'preview'
TOOLS = ROOT / 'tools'
PROM = TOOLS / 'prometheus-3.15.0.windows-amd64' / 'prometheus.exe'
AM = TOOLS / 'alertmanager-0.34.1.windows-amd64' / 'alertmanager.exe'
# The OnCall MCP server runs on 8080 in the preview (override with --mcp-port).
PORTS = {'api': 8200, 'mcp': 8080, 'releases': 8400, 'payments': 9100, 'prometheus': 9090, 'alertmanager': 9093}


def stop_running():
    """Stop a preview started earlier (launcher and every service it spawned)."""
    marker = STATE / 'preview.json'
    if not marker.exists():
        print('No running preview recorded.')
        return
    pids = json.loads(marker.read_text()).get('pids', [])
    for pid in pids:
        if os.name == 'nt':
            # Kill the whole tree: venv python.exe is a launcher whose real
            # interpreter is a child process.
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass  # already exited
    marker.unlink(missing_ok=True)
    print(f'Stopped {len(pids)} preview processes.')


def assert_free(ports):
    """Bind-test on 0.0.0.0 too: a process listening on all interfaces does not block
    a 127.0.0.1-only test bind on Windows, but it does take the requests."""
    import socket
    for port in ports:
        for host in ('127.0.0.1', '0.0.0.0'):
            with socket.socket() as probe:
                if os.name == 'nt':
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind((host, port))
                except OSError:
                    raise RuntimeError(f'Port {port} is already in use by another program; nothing was stopped.') from None
GOOD, BAD = '3f9a1c2e7b4d', '8c2d5e91a0f3'


def secrets_file():
    path = STATE / 'secrets.json'
    if path.exists():
        return json.loads(path.read_text())
    values = {k: secrets.token_urlsafe(40) for k in ('api_key', 'ledger_read', 'rollback')}
    path.write_text(json.dumps(values))
    return values


def controller_env(s):
    return {**os.environ, 'PYTHONUNBUFFERED': '1', 'PAYMENTS_STATE_DIR': str(STATE / 'payments'),
            'LEDGER_READ_TOKEN': s['ledger_read'], 'ROLLBACK_TOKEN': s['rollback'], 'RELEASES_SERVE': '1'}


def release(commit, note):
    s = secrets_file()
    out = subprocess.run([sys.executable, '-m', 'production.releases', 'deploy', commit, '--note', note],
                         cwd=ROOT, env=controller_env(s), capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(out.stderr.strip() or 'release failed')
    print(f'Shipped {commit}: {note}')


def write_configs(api_key):
    rules = (ROOT / 'deploy' / 'alert-rules.yml').as_posix()
    (STATE / 'prometheus.yml').write_text(
        'global: {scrape_interval: 15s, evaluation_interval: 15s}\n'
        f"alerting: {{alertmanagers: [{{static_configs: [{{targets: ['127.0.0.1:{PORTS['alertmanager']}']}}]}}]}}\n"
        f"rule_files: ['{rules}']\n"
        f"scrape_configs: [{{job_name: payments, static_configs: [{{targets: ['127.0.0.1:{PORTS['payments']}']}}]}}]\n")
    bearer = STATE / 'alertmanager-bearer'
    bearer.write_text(api_key)
    am = (ROOT / 'deploy' / 'alertmanager.yml').read_text().replace('/etc/oncall/alertmanager-bearer', bearer.as_posix())
    (STATE / 'alertmanager.yml').write_text(am)


def main():
    parser = argparse.ArgumentParser(description='Local production preview')
    parser.add_argument('action', nargs='?', choices=['start', 'stop', 'break', 'fix'], default='start')
    parser.add_argument('--reset', action='store_true', help='Delete preview state before starting')
    parser.add_argument('--mcp-port', type=int, default=PORTS['mcp'], help='OnCall MCP server port (default 8080)')
    args = parser.parse_args()
    if not 1024 <= args.mcp_port <= 65535:
        raise SystemExit('--mcp-port must be 1024-65535')
    PORTS['mcp'] = args.mcp_port
    if args.action == 'stop':
        return stop_running()
    if args.action == 'break':
        return release(BAD, 'Tighten payment gateway timeout')
    if args.action == 'fix':
        return release(GOOD, 'Manual redeploy of stable release')
    for binary in (PROM, AM):
        if not binary.exists():
            raise SystemExit(f'Missing {binary}. Download the official release into tools/ (see README).')
    if args.reset and STATE.exists():
        shutil.rmtree(STATE)
    (STATE / 'payments').mkdir(parents=True, exist_ok=True)
    s = secrets_file()
    assert_ports_available(list(PORTS.values()))
    assert_free(PORTS.values())
    write_configs(s['api_key'])
    if not (STATE / 'payments' / 'current.json').exists():
        release(GOOD, 'Initial stable release')

    base = {**os.environ, 'PYTHONUNBUFFERED': '1', 'PAYMENTS_STATE_DIR': str(STATE / 'payments')}
    oncall = {**base, 'ONCALL_MODE': 'live', 'ONCALL_API_KEY': s['api_key'], 'ONCALL_HOST': '127.0.0.1',
              'ONCALL_PORT': str(PORTS['api']), 'ONCALL_MCP_PORT': str(PORTS['mcp']),
              'ONCALL_MCP_URL': f"http://127.0.0.1:{PORTS['mcp']}/mcp", 'ONCALL_DB_PATH': str(STATE / 'live.db'),
              'ONCALL_PUBLIC_URL': '', 'ONCALL_OAUTH_ENABLED': 'false', 'ONCALL_PUBLIC_STATUS': 'true',
              'PROMETHEUS_URL': f"http://127.0.0.1:{PORTS['prometheus']}", 'ONCALL_TELEMETRY_MAX_AGE_SECONDS': '120',
              'ONCALL_DEPLOYS_SOURCE': 'ledger', 'ONCALL_DEPLOYS_URL': f"http://127.0.0.1:{PORTS['releases']}/deployments",
              'ONCALL_DEPLOYS_TOKEN': s['ledger_read'], 'ONCALL_MUTATION_ENABLED': 'true',
              'ONCALL_ROLLBACK_URL': f"http://127.0.0.1:{PORTS['releases']}/rollback", 'ONCALL_ROLLBACK_TOKEN': s['rollback'],
              'ONCALL_REPO': '', 'ONCALL_NOTIFY_URL': '', 'ONCALL_BEDROCK_MODEL_ID': '', 'ONCALL_VERIFY_WATCH_SECONDS': '30'}
    py = sys.executable
    procs, stopping = [], False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logs = STATE / 'logs'
    logs.mkdir(exist_ok=True)
    names = {}
    with ExitStack() as stack:
        def spawn(name, cmd, env):
            out = stack.enter_context((logs / f'{name}.log').open('a', encoding='utf-8'))
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
            names[proc.pid] = name
            procs.append(proc)
        try:
            spawn('payments', [py, '-m', 'uvicorn', 'production.payments.app:app', '--host', '127.0.0.1',
                               '--port', str(PORTS['payments']), '--log-level', 'warning'], base)
            spawn('releases', [py, '-m', 'uvicorn', 'production.releases:app', '--host', '127.0.0.1',
                               '--port', str(PORTS['releases']), '--log-level', 'warning'], controller_env(s))
            spawn('traffic', [py, '-m', 'production.traffic', '--url', f"http://127.0.0.1:{PORTS['payments']}/checkout", '--rps', '3'], base)
            spawn('prometheus', [str(PROM), f"--config.file={STATE / 'prometheus.yml'}", f"--storage.tsdb.path={STATE / 'tsdb'}",
                                 f"--web.listen-address=127.0.0.1:{PORTS['prometheus']}", '--storage.tsdb.retention.time=2d'], base)
            spawn('alertmanager', [str(AM), f"--config.file={STATE / 'alertmanager.yml'}", f"--storage.path={STATE / 'am'}",
                                   f"--web.listen-address=127.0.0.1:{PORTS['alertmanager']}", '--cluster.listen-address='], base)
            spawn('oncall-mcp', [py, 'server.py'], oncall)
            spawn('oncall-api', [py, 'brain.py'], oncall)
            wait_ready(procs[-2:], [PORTS['mcp'], PORTS['api']], timeout=60)
            # Children first, launcher last, so `preview.py stop` never orphans a service.
            (STATE / 'preview.json').write_text(json.dumps({'pids': [p.pid for p in procs] + [os.getpid()]}))
            print('OnCall production preview is running (live mode, loopback only)', flush=True)
            print(f"  Operator workspace  http://127.0.0.1:{PORTS['api']}", flush=True)
            print(f"  OnCall MCP server   http://127.0.0.1:{PORTS['mcp']}/mcp", flush=True)
            print(f"  Public status       http://127.0.0.1:{PORTS['api']}/status", flush=True)
            print(f"  Prometheus          http://127.0.0.1:{PORTS['prometheus']}", flush=True)
            print(f"  Alertmanager        http://127.0.0.1:{PORTS['alertmanager']}", flush=True)
            print(f"  Operator key        {STATE / 'secrets.json'} (api_key)", flush=True)
            print('  Break production:   python scripts/preview.py break', flush=True)
            while not stopping:
                for p in procs:
                    if p.poll() is not None:
                        name = names.get(p.pid, 'unknown')
                        raise RuntimeError(f'Preview service "{name}" exited with code {p.returncode}. See {logs / (name + ".log")}.')
                time.sleep(0.5)
        finally:
            stop_children(procs)
            (STATE / 'preview.json').unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(f'Preview failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
