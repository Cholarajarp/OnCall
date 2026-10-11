"""Start the OnCall stack; the historical filename also supports live mode."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def service_files(mode: str) -> list[str]:
    return (['target_service/app.py'] if mode == 'sandbox' else []) + ['server.py', 'brain.py']


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def assert_ports_available(ports: list[int], grace: float | None = None) -> None:
    """Refuse occupied ports, but wait briefly first: on a service restart the previous
    instance may still be releasing them. Never stops any existing process."""
    if len(set(ports)) != len(ports):
        raise RuntimeError('Each OnCall service needs a distinct port.')
    grace = float(os.environ.get('ONCALL_PORT_WAIT_SECONDS', 20)) if grace is None else grace
    deadline = time.monotonic() + max(grace, 0)
    busy = [p for p in ports if not _port_free(p)]
    while busy and time.monotonic() < deadline:
        time.sleep(0.5)
        busy = [p for p in busy if not _port_free(p)]
    if busy:
        raise RuntimeError(f'Port {busy[0]} is already in use; no existing process was stopped.')


def stop_children(children: list[subprocess.Popen]) -> None:
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def wait_ready(children: list[subprocess.Popen], ports: list[int], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    remaining = list(dict.fromkeys(ports))
    while time.monotonic() < deadline:
        for child in children:
            if child.poll() is not None:
                raise RuntimeError(f'OnCall child process {child.pid} exited with code {child.returncode}. Inspect the service logs.')
        for port in list(remaining):
            try:
                with urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                    if response.status == 200:
                        remaining.remove(port)
            except (OSError, URLError):
                pass
        if not remaining:
            return
        time.sleep(0.15)
    unavailable = ', '.join(str(port) for port in sorted(remaining))
    raise RuntimeError(f'OnCall did not become ready on ports {unavailable} within {timeout}s.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Run OnCall sandbox or configured live services.')
    parser.add_argument('--log-dir', type=Path, help='Write child service logs to this directory.')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from config import settings

    ports = [settings.mcp_port, settings.port]
    if settings.mode == 'sandbox':
        ports.append(settings.target_port)
    assert_ports_available(ports)
    children: list[subprocess.Popen] = []
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    with ExitStack() as stack:
        try:
            for filename in service_files(settings.mode):
                output = None
                if args.log_dir:
                    args.log_dir.mkdir(parents=True, exist_ok=True)
                    output = stack.enter_context((args.log_dir / f'{Path(filename).stem}.log').open('a', encoding='utf-8'))
                env = os.environ.copy()
                env['PYTHONUNBUFFERED'] = '1'
                children.append(subprocess.Popen(
                    [sys.executable, str(ROOT / filename)], cwd=ROOT, env=env,
                    stdout=output, stderr=subprocess.STDOUT if output else None,
                ))
            wait_ready(children, ports)
            print(f'OnCall {settings.mode} ready: http://127.0.0.1:{settings.port}', flush=True)
            print('Press Ctrl+C to stop only the services started by this launcher.', flush=True)
            while not stopping:
                for child in children:
                    if child.poll() is not None:
                        raise RuntimeError(f'OnCall service {child.pid} exited ({child.returncode}).')
                time.sleep(0.3)
            return 0
        finally:
            stop_children(children)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f'OnCall startup failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
