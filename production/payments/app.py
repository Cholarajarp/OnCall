"""Production payments service watched by OnCall.

A small but real HTTP service: it serves checkout traffic, exports Prometheus
metrics, and runs whichever *release* the release controller marks current.
A release is a commit plus its configuration (`releases/<commit>.json`). One
shipped release has a genuinely bad configuration (gateway timeout lower than
the gateway's real latency), which makes /checkout fail exactly the way a real
bad config push does. Nothing here is simulated by OnCall; OnCall only observes
this service through Prometheus and the release ledger.

Run:  PAYMENTS_STATE_DIR=/var/lib/payments uvicorn production.payments.app:app --port 9100
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

SERVICE = os.environ.get('PAYMENTS_SERVICE', 'payments')
STATE_DIR = Path(os.environ.get('PAYMENTS_STATE_DIR', 'data/payments'))
RELEASES_DIR = Path(os.environ.get('PAYMENTS_RELEASES_DIR', Path(__file__).resolve().parent / 'releases'))

registry = CollectorRegistry()
REQUESTS = Counter('http_requests_total', 'HTTP requests', ['service', 'route', 'status'], registry=registry)
LATENCY = Histogram('http_request_duration_seconds', 'Request latency', ['service', 'route'], registry=registry,
                    buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5))
RELEASE = Gauge('payments_release_info', 'Currently loaded release', ['service', 'commit'], registry=registry)

# Initialize 5xx/2xx series at zero: OnCall treats a missing error counter as UNKNOWN.
for status in ('200', '500', '503'):
    REQUESTS.labels(SERVICE, '/checkout', status)

app = FastAPI(title=f'{SERVICE} service', docs_url=None, redoc_url=None)
_cache = {'pointer': None, 'release': None}


def current_release() -> dict:
    """Read the controller-owned pointer (tiny file) and reload when its content changes.

    Content, not mtime: two deploys within one filesystem timestamp tick must not
    leave the service running the previous release.
    """
    try:
        raw = (STATE_DIR / 'current.json').read_text()
        if raw != _cache['pointer']:
            commit = json.loads(raw)['commit']
            config = json.loads((RELEASES_DIR / f'{commit}.json').read_text())
            RELEASE.clear()
            RELEASE.labels(SERVICE, commit).set(1)
            _cache.update(pointer=raw, release={'commit': commit, **config})
    except (OSError, ValueError, KeyError):
        # No valid release: refuse traffic rather than run unknown code.
        _cache.update(pointer=None, release=None)
    return _cache['release']


async def call_gateway(timeout_ms: int) -> None:
    """The payment gateway answers in 40-120 ms. A timeout below that fails."""
    latency_ms = random.uniform(40, 120)
    await asyncio.sleep(min(latency_ms, timeout_ms) / 1000)
    if timeout_ms < latency_ms:
        raise TimeoutError(f'payment gateway deadline ({timeout_ms}ms) exceeded after {latency_ms:.0f}ms')


@app.get('/health')
async def health():
    release = current_release()
    return JSONResponse({'service': SERVICE, 'status': 'ok' if release else 'no_release',
                         'commit': release and release['commit']}, status_code=200 if release else 503)


@app.post('/checkout')
async def checkout():
    started = time.perf_counter()
    release = current_release()
    status = 503
    try:
        if not release:
            return JSONResponse({'error': 'no_release'}, status_code=503)
        try:
            await call_gateway(int(release['gateway_timeout_ms']))
            status = 200
            return {'status': 'charged', 'release': release['commit']}
        except TimeoutError as exc:
            status = 500
            # Structured stdout log line; shipped to Loki/journald like any real service.
            print(json.dumps({'level': 'ERROR', 'service': SERVICE, 'route': '/checkout',
                              'release': release['commit'], 'detail': f'TimeoutError: {exc}'}), flush=True)
            return JSONResponse({'error': 'payment_gateway_timeout'}, status_code=500)
    finally:
        REQUESTS.labels(SERVICE, '/checkout', str(status)).inc()
        LATENCY.labels(SERVICE, '/checkout').observe(time.perf_counter() - started)


@app.get('/metrics')
async def metrics():
    current_release()
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
