"""Release controller: the single source of truth for what is deployed.

- `deploy` (CLI, operator/CI only) switches the service to a release and records it.
- `GET /deployments` is OnCall's read-only deployment evidence (ONCALL_DEPLOYS_SOURCE=ledger).
- `POST /rollback` is OnCall's rollback webhook (ONCALL_ROLLBACK_URL), implementing the
  contract: bearer auth, service allowlist, Idempotency-Key == action_id, durable
  at-most-once per action ID, echo only on a completed rollback.

Rollback semantics: `target_commit` is the *active* release the operator approved
rolling back. It must still be the active release (otherwise 409, no change). The
service is switched to the most recent earlier release that is not itself rolled back.

Two tokens: LEDGER_READ_TOKEN (read evidence) and ROLLBACK_TOKEN (mutation) so a
leaked read credential cannot change production.

    python -m production.releases deploy 8c2d5e91a0f3 --note "Tighten gateway timeout"
    uvicorn production.releases:app --port 8400
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

COMMIT = re.compile(r'^[0-9a-f]{7,64}$')


class _Closing(sqlite3.Connection):
    """`with connect()` closes the connection (sqlite3's default only ends a transaction)."""
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class Controller:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.service = env.get('RELEASES_SERVICE', 'payments')
        self.state_dir = Path(env.get('PAYMENTS_STATE_DIR', 'data/payments'))
        self.releases_dir = Path(env.get('PAYMENTS_RELEASES_DIR', Path(__file__).resolve().parent / 'payments' / 'releases'))
        self.db_path = Path(env.get('RELEASES_DB', self.state_dir / 'releases.db'))
        self.read_token = env.get('LEDGER_READ_TOKEN', '')
        self.rollback_token = env.get('ROLLBACK_TOKEN', '')
        # HTTP /deploy may only ship releases that exist in releases_dir, further limited by
        # this comma-separated allowlist when set. The CLI `deploy` is unrestricted (operator/CI).
        self.deploy_allowlist = tuple(c.strip() for c in env.get('RELEASES_DEPLOY_ALLOWLIST', '').split(',') if c.strip())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS deployments (
              id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, commit_sha TEXT NOT NULL,
              deployed_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
              rolled_back INTEGER NOT NULL DEFAULT 0, actor TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions (
              action_id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
              at TEXT NOT NULL, detail TEXT);''')

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None, factory=_Closing)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def _switch(self, commit):
        # Atomic pointer swap: the service never reads a half-written file.
        if not (self.releases_dir / f'{commit}.json').is_file():
            raise ValueError(f'Unknown release {commit}')
        temp = self.state_dir / 'current.json.tmp'
        temp.write_text(json.dumps({'commit': commit, 'at': _now()}))
        os.replace(temp, self.state_dir / 'current.json')

    def deploy(self, commit, note='', actor='operator'):
        if not COMMIT.fullmatch(commit):
            raise ValueError('Commit must be lowercase hex')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._switch(commit)
            db.execute("UPDATE deployments SET status='inactive' WHERE service=? AND status='active'", (self.service,))
            db.execute('INSERT INTO deployments(service,commit_sha,deployed_at,note,status,actor) VALUES(?,?,?,?,?,?)',
                       (self.service, commit, _now(), note[:200], 'active', actor))
            db.execute('COMMIT')
        return self.active()

    def deployable(self, commit):
        known = COMMIT.fullmatch(commit) and (self.releases_dir / f'{commit}.json').is_file()
        return bool(known and (not self.deploy_allowlist or commit in self.deploy_allowlist))

    def active(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM deployments WHERE service=? AND status='active' ORDER BY id DESC LIMIT 1",
                             (self.service,)).fetchone()
        return dict(row) if row else None

    def ledger(self):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM deployments WHERE service=? ORDER BY id DESC LIMIT 50', (self.service,)).fetchall()
        return [{'deployment_id': r['id'], 'service': r['service'], 'commit': r['commit_sha'],
                 'deployed_at': r['deployed_at'], 'note': r['note'], 'status': r['status'],
                 'active': r['status'] == 'active', 'rolled_back': bool(r['rolled_back']), 'actor': r['actor']}
                for r in reversed(rows)]

    def rollback(self, commit, action_id):
        """Returns (status, detail). Runs under one writer lock; never partially applies."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                active = db.execute("SELECT * FROM deployments WHERE service=? AND status='active' ORDER BY id DESC LIMIT 1",
                                    (self.service,)).fetchone()
                if not active or active['commit_sha'] != commit:
                    db.execute('ROLLBACK')
                    return 'stale', 'Approved target is no longer the active release'
                previous = db.execute('''SELECT * FROM deployments WHERE service=? AND id<? AND rolled_back=0
                                         AND commit_sha!=? ORDER BY id DESC LIMIT 1''',
                                      (self.service, active['id'], commit)).fetchone()
                if not previous:
                    db.execute('ROLLBACK')
                    return 'no_previous', 'No earlier healthy release to roll back to'
                self._switch(previous['commit_sha'])
                db.execute("UPDATE deployments SET status='inactive', rolled_back=1 WHERE id=?", (active['id'],))
                db.execute('INSERT INTO deployments(service,commit_sha,deployed_at,note,status,actor) VALUES(?,?,?,?,?,?)',
                           (self.service, previous['commit_sha'], _now(), f'Rollback of {commit} (action {action_id[:12]})',
                            'active', 'oncall'))
                db.execute('COMMIT')
                return 'rolled_back', f"Now serving {previous['commit_sha']}"
            except Exception:
                db.execute('ROLLBACK')
                raise


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    target_commit: str = Field(pattern=r'^[0-9a-fA-F]{7,64}$')
    action_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


class DeployBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    commit: str = Field(pattern=r'^[0-9a-f]{7,64}$')
    note: str = Field(default='', max_length=200)


def _auth(header, token):
    if len(token) < 32 or not hmac.compare_digest(header, f'Bearer {token}'):
        raise HTTPException(401, 'unauthorized', headers={'WWW-Authenticate': 'Bearer'})


def create_app(controller: Controller | None = None) -> FastAPI:
    ctl = controller or Controller()
    api = FastAPI(title='Release controller', docs_url=None, redoc_url=None)

    @api.get('/health')
    def health():
        return {'status': 'ok', 'active': (ctl.active() or {}).get('commit_sha')}

    @api.get('/deployments')
    def deployments(service: str = Query(max_length=64), authorization: str = Header(default='')):
        _auth(authorization, ctl.read_token)
        if service != ctl.service:
            raise HTTPException(403, 'service not allowed')
        return {'deployments': ctl.ledger()}

    @api.post('/deploy')
    def deploy(body: DeployBody, authorization: str = Header(default='')):
        """Demo controls: ship one known release (rollback token, allowlisted commits only)."""
        _auth(authorization, ctl.rollback_token)
        if body.service != ctl.service:
            raise HTTPException(403, 'service not allowed')
        if not ctl.deployable(body.commit):
            raise HTTPException(403, 'release not on the deploy allowlist')
        active = ctl.active()
        if active and active['commit_sha'] == body.commit:
            return {'status': 'already_active', 'service': ctl.service, 'commit': body.commit}
        ctl.deploy(body.commit, body.note or 'Demo control deploy', actor='oncall-demo')
        return {'status': 'deployed', 'service': ctl.service, 'commit': body.commit}

    @api.post('/rollback')
    def rollback(body: RollbackBody, authorization: str = Header(default=''), idempotency_key: str = Header(default='')):
        _auth(authorization, ctl.rollback_token)
        if idempotency_key != body.action_id:
            raise HTTPException(400, 'Idempotency-Key must equal action_id')
        if body.service != ctl.service:
            raise HTTPException(403, 'service not allowed')
        digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        with ctl.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,status FROM actions WHERE action_id=?', (body.action_id,)).fetchone()
            if row:
                db.execute('COMMIT')
                if row['digest'] != digest:
                    raise HTTPException(409, 'action_id reused with a different target')
                if row['status'] == 'rolled_back':
                    return {'status': 'rolled_back', **body.model_dump(), 'replayed': True}
                return {'status': row['status'], 'action_id': body.action_id, 'replayed': True}
            db.execute('INSERT INTO actions VALUES(?,?,?,?,NULL)', (body.action_id, digest, 'running', _now()))
            db.execute('COMMIT')
        try:
            status, detail = ctl.rollback(body.target_commit.lower(), body.action_id)
        except Exception:
            status, detail = 'failed', 'Controller error; reconcile before any new action'
        with ctl.connect() as db:
            db.execute('UPDATE actions SET status=?,detail=? WHERE action_id=?', (status, detail, body.action_id))
        if status != 'rolled_back':
            return {'status': status, 'action_id': body.action_id, 'detail': detail}
        return {'status': 'rolled_back', **body.model_dump(), 'detail': detail}

    return api


def main(argv=None):
    parser = argparse.ArgumentParser(description='Release controller CLI')
    sub = parser.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('deploy', help='Switch the service to a release and record it')
    d.add_argument('commit')
    d.add_argument('--note', default='')
    sub.add_parser('status', help='Show the ledger')
    args = parser.parse_args(argv)
    ctl = Controller()
    if args.cmd == 'deploy':
        print(json.dumps(ctl.deploy(args.commit, args.note), indent=2))
    else:
        print(json.dumps(ctl.ledger(), indent=2))
    return 0


app = create_app() if os.environ.get('RELEASES_SERVE') == '1' else None

if __name__ == '__main__':
    sys.exit(main())
