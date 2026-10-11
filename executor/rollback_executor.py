"""Reference rollback executor for OnCall's live webhook contract.

Runs on the host that owns the deployment (for example the EC2 instance running
your app). OnCall POSTs here once, after a human approves an exact target:

    POST /rollback
    Authorization: Bearer <ONCALL_ROLLBACK_TOKEN>
    Idempotency-Key: <action_id>
    {"service": "payments", "target_commit": "<sha>", "action_id": "<action_id>"}

Guarantees this file implements (and OnCall relies on):
- constant-time bearer check; service allowlist; strict SHA / action ID formats
- durable idempotency: an action ID runs at most once, a replay returns the
  recorded outcome, and a different body under the same ID is rejected
- one command attempt with a timeout, no shell, arguments passed as a list
- echoes service/target_commit/action_id only when the command exited 0

Semantics: OnCall sends the commit of the *active* deployment being rolled back
(the reviewed target). Your command decides what "roll back" means, e.g. redeploy
the previous image tag. The command receives the commit as an argument and via
ONCALL_TARGET_COMMIT. Example:
    EXECUTOR_COMMAND="/opt/deploy/rollback.sh"   # rollback.sh <service> <target_commit>

Run:  EXECUTOR_TOKEN=... EXECUTOR_SERVICE=payments EXECUTOR_COMMAND=... \
      python executor/rollback_executor.py         (binds 127.0.0.1:8300 by default)
Put TLS in front (ALB, Caddy, nginx) if OnCall reaches it over a network.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.token = env.get('EXECUTOR_TOKEN', '')
        self.service = env.get('EXECUTOR_SERVICE', '')
        self.command = shlex.split(env.get('EXECUTOR_COMMAND', ''))
        self.db = Path(env.get('EXECUTOR_DB', 'executor-actions.db'))
        self.timeout = int(env.get('EXECUTOR_TIMEOUT_SECONDS', '120'))
        if len(self.token) < 32:
            raise ValueError('EXECUTOR_TOKEN must be at least 32 characters')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.service):
            raise ValueError('EXECUTOR_SERVICE must be one service identifier')
        if not self.command:
            raise ValueError('EXECUTOR_COMMAND is required')
        if not 1 <= self.timeout <= 900:
            raise ValueError('EXECUTOR_TIMEOUT_SECONDS must be 1-900')


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    target_commit: str = Field(pattern=r'^[0-9a-fA-F]{7,64}$')
    action_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


def _connect(path):
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.execute('CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY, digest TEXT NOT NULL, '
               'status TEXT NOT NULL, at TEXT NOT NULL, detail TEXT)')
    return db


def create_app(config: Config | None = None, run=subprocess.run) -> FastAPI:
    cfg = config or Config()
    app = FastAPI(title='OnCall reference rollback executor', docs_url=None, redoc_url=None)

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/rollback')
    def rollback(body: RollbackBody, authorization: str = Header(default=''),
                 idempotency_key: str = Header(default='')):
        if not hmac.compare_digest(authorization, f'Bearer {cfg.token}'):
            raise HTTPException(401, 'unauthorized')
        if idempotency_key != body.action_id:
            raise HTTPException(400, 'Idempotency-Key must equal action_id')
        if body.service != cfg.service:
            raise HTTPException(403, 'service not allowed')
        digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        db = _connect(cfg.db)
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,status FROM actions WHERE action_id=?', (body.action_id,)).fetchone()
            if row:
                db.execute('COMMIT')
                if row[0] != digest:
                    raise HTTPException(409, 'action_id reused with a different target')
                if row[1] == 'rolled_back':
                    return {'status': 'rolled_back', **body.model_dump(), 'replayed': True}
                # 'running' or 'failed': never run twice; operator reconciles.
                return {'status': row[1], 'action_id': body.action_id, 'replayed': True}
            db.execute('INSERT INTO actions VALUES(?,?,?,?,NULL)', (body.action_id, digest, 'running',
                       datetime.now(timezone.utc).isoformat()))
            db.execute('COMMIT')
        finally:
            db.close()
        env = {**os.environ, 'ONCALL_SERVICE': body.service, 'ONCALL_TARGET_COMMIT': body.target_commit,
               'ONCALL_ACTION_ID': body.action_id}
        try:
            proc = run([*cfg.command, body.service, body.target_commit], env=env, timeout=cfg.timeout,
                       capture_output=True, text=True, shell=False)
            status = 'rolled_back' if proc.returncode == 0 else 'failed'
            detail = f'exit {proc.returncode}'
        except subprocess.TimeoutExpired:
            status, detail = 'failed', 'timeout; outcome unknown, reconcile manually'
        except OSError:
            status, detail = 'failed', 'command could not start'
        db = _connect(cfg.db)
        try:
            db.execute('UPDATE actions SET status=?,detail=? WHERE action_id=?', (status, detail, body.action_id))
        finally:
            db.close()
        if status != 'rolled_back':
            # Non-matching body => OnCall records the outcome as unknown and never retries.
            return {'status': status, 'action_id': body.action_id, 'detail': detail}
        return {'status': 'rolled_back', **body.model_dump()}

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host=os.environ.get('EXECUTOR_HOST', '127.0.0.1'),
                port=int(os.environ.get('EXECUTOR_PORT', '8300')))
