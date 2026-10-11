"""Persisted incident policy shared by REST and MCP. Models never authorize actions.

SQLite reservations are exactly-once. External delivery is deliberately at-most-once:
a timed-out/crashed webhook is uncertain and is never automatically retried.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from target_service import store

http_client = httpx.AsyncClient
SERVICE = 'payments'
APPROVAL_SECONDS = 120


def settings():
    from config import settings as configured
    return configured


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def init():
    store.init_db()
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY, session TEXT NOT NULL, type TEXT NOT NULL,
          service TEXT NOT NULL, commit_sha TEXT NOT NULL, target TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
          result TEXT);
        CREATE TABLE IF NOT EXISTS incidents (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
          started_at TEXT NOT NULL, resolved_at TEXT, source TEXT NOT NULL DEFAULT 'manual',
          acknowledged_at TEXT, acknowledged_by TEXT, last_notified_at TEXT);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
          title TEXT NOT NULL, detail TEXT NOT NULL, kind TEXT NOT NULL,
          incident_id TEXT, session TEXT);
        CREATE TABLE IF NOT EXISTS workflow_locks (
          name TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, markdown TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS executions (
          target TEXT PRIMARY KEY, action_id TEXT NOT NULL, completed_at TEXT);
        CREATE TABLE IF NOT EXISTS alerts (
          fingerprint TEXT NOT NULL, incident_id TEXT NOT NULL, name TEXT NOT NULL,
          severity TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (fingerprint, incident_id));
        CREATE TABLE IF NOT EXISTS agent_threads (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, incident_id TEXT,
          mode TEXT NOT NULL, speech TEXT NOT NULL, trace TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_turns (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, at TEXT NOT NULL,
          user_text TEXT NOT NULL, reply TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS chat_turns_session ON chat_turns(session, id);
        CREATE TABLE IF NOT EXISTS voice_challenges (
          action_id TEXT PRIMARY KEY, session TEXT NOT NULL, code_hash TEXT NOT NULL,
          delivery TEXT NOT NULL, display_code TEXT, created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL);
        ''')
        # Additive migration for databases created by earlier versions, serialized
        # under the writer lock so concurrent processes cannot add a column twice.
        wanted = (('source', "TEXT NOT NULL DEFAULT 'manual'"), ('acknowledged_at', 'TEXT'),
                  ('acknowledged_by', 'TEXT'), ('last_notified_at', 'TEXT'), ('escalated_at', 'TEXT'))
        if {name for name, _ in wanted} - {row[1] for row in db.execute('PRAGMA table_info(incidents)')}:
            db.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in db.execute('PRAGMA table_info(incidents)')}
            for name, ddl in wanted:
                if name not in columns:
                    db.execute(f'ALTER TABLE incidents ADD COLUMN {name} {ddl}')
            db.execute('COMMIT')


def session_key(session):
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', session):
        raise ValueError('Session must contain 1-128 letters, digits, dots, colons, underscores or hyphens')
    return hashlib.sha256(session.encode()).hexdigest()


def validate_service(service):
    if not isinstance(service, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', service):
        raise ValueError('Invalid service')
    if settings().mode == 'sandbox' and service != SERVICE:
        raise ValueError('Sandbox supports payments only')
    if settings().mode == 'live' and service != settings().service:
        raise ValueError('Service is outside the configured operational scope')
    return service


def public_action(row):
    return {"id": row['id'], "type": row['type'], "service": row['service'],
            "commit": row['commit_sha'], "expires_at": row['expires_at']}


def pending(session):
    init()
    key = session_key(session)
    with store.connect() as db:
        db.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND expires_at<=?", (now(),))
        row = db.execute("SELECT * FROM approvals WHERE session=? AND status='pending' ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
    return public_action(row) if row else None


def incident():
    init()
    with store.connect() as db:
        row = db.execute('SELECT * FROM incidents ORDER BY started_at DESC LIMIT 1').fetchone()
    return dict(row) if row else None


def audit(title, detail='', kind='info', session=None):
    init()
    current = incident()
    with store.connect() as db:
        db.execute('INSERT INTO audit(at,title,detail,kind,incident_id,session) VALUES(?,?,?,?,?,?)',
                   (now(), title, str(detail)[:8000], kind, current['id'] if current else None,
                    session_key(session) if session else None))


def audit_entries(incident_id=None):
    init()
    with store.connect() as db:
        if incident_id is None:
            rows = db.execute('SELECT at,title,detail,kind FROM audit ORDER BY id DESC LIMIT 100').fetchall()
        else:
            rows = db.execute('SELECT at,title,detail,kind FROM audit WHERE incident_id=? ORDER BY id DESC LIMIT 100',
                              (incident_id,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def open_incident(title, source='manual', db=None):
    if db is None:
        init()  # never from inside a caller's write transaction (would self-block)
    incident_id = secrets.token_hex(8)
    started_at = now()
    def run(conn):
        current = conn.execute("SELECT * FROM incidents WHERE status!='resolved' ORDER BY started_at DESC LIMIT 1").fetchone()
        if current:
            return dict(current)
        conn.execute('INSERT INTO incidents(id,title,status,started_at,source) VALUES(?,?,?,?,?)',
                     (incident_id, title, 'investigating', started_at, source))
        return dict(conn.execute('SELECT * FROM incidents WHERE id=?', (incident_id,)).fetchone())
    if db is not None:
        return run(db)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return run(conn)


# ── alert ingestion, acknowledgement and paging ──────────────────────────

MAX_ALERTS = 50
_TEXT = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def _clean(value, limit):
    """Alert text is untrusted evidence: strip control characters and bound length."""
    return _TEXT.sub(' ', str(value or ''))[:limit].strip()


def ingest_alert(payload):
    """Accept an Alertmanager-shaped webhook (EventBridge/CloudWatch relays can map to it).

    Firing alerts open or attach to the single active incident and are deduplicated
    by fingerprint. A 'resolved' alert is a signal only: it never resolves the
    incident, because recovery must be verified with fresh evidence.
    """
    init()
    alerts = payload.get('alerts') if isinstance(payload, dict) else None
    if not isinstance(alerts, list) or not alerts or len(alerts) > MAX_ALERTS:
        raise ValueError(f'Payload needs 1-{MAX_ALERTS} alerts')
    service = settings().service
    parsed = []
    for item in alerts:
        if not isinstance(item, dict) or not isinstance(item.get('labels'), dict):
            raise ValueError('Each alert needs a labels object')
        labels, notes = item['labels'], item.get('annotations') if isinstance(item.get('annotations'), dict) else {}
        status = item.get('status', payload.get('status'))
        if status not in ('firing', 'resolved'):
            raise ValueError('Alert status must be firing or resolved')
        if labels.get('service') != service:
            raise ValueError('Alert service is outside the configured operational scope')
        name = _clean(labels.get('alertname') or 'alert', 120) or 'alert'
        fingerprint = item.get('fingerprint')
        if not isinstance(fingerprint, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', fingerprint):
            fingerprint = hashlib.sha256(json.dumps(sorted((str(k), str(v)) for k, v in labels.items())).encode()).hexdigest()[:32]
        parsed.append({'status': status, 'fingerprint': fingerprint, 'name': name,
                       'severity': _clean(labels.get('severity') or 'unknown', 32),
                       'summary': _clean(notes.get('summary') or notes.get('description') or name, 500)})
    counts = {'created': 0, 'deduplicated': 0, 'resolved_signals': 0}
    opened = False
    current = None
    stamp = now()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for alert in parsed:
            if alert['status'] == 'resolved':
                counts['resolved_signals'] += 1
                row = db.execute("SELECT * FROM incidents WHERE status!='resolved' ORDER BY started_at DESC LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE alerts SET status='resolved',last_seen=? WHERE fingerprint=? AND incident_id=?",
                               (stamp, alert['fingerprint'], row['id']))
                continue
            before = db.execute("SELECT id FROM incidents WHERE status!='resolved' LIMIT 1").fetchone()
            current = open_incident(f"{service.capitalize()}: {alert['name']}", 'alert', db=db)
            opened = opened or before is None
            changed = db.execute('UPDATE alerts SET last_seen=?,count=count+1,status=? WHERE fingerprint=? AND incident_id=?',
                                 (stamp, 'firing', alert['fingerprint'], current['id'])).rowcount
            if changed:
                counts['deduplicated'] += 1
            else:
                db.execute('INSERT INTO alerts VALUES(?,?,?,?,?,?,?,?,1)', (alert['fingerprint'], current['id'], alert['name'],
                           alert['severity'], alert['summary'], 'firing', stamp, stamp))
                counts['created'] += 1
    for alert in parsed:
        if alert['status'] == 'firing':
            audit('Alert received', f"{alert['name']} ({alert['severity']}): {alert['summary']}", 'alert')
        else:
            audit('Alert resolved signal', f"{alert['name']} reported resolved by the monitor; verification still required.", 'alert')
    current = incident() if (current or counts['resolved_signals']) else None
    if current and current['status'] == 'resolved':
        current = None
    return {'status': 'accepted', **counts, 'opened': opened, 'incident': current}


def acknowledge(session, incident_id):
    init()
    key = session_key(session)
    if not isinstance(incident_id, str) or not re.fullmatch(r'[a-f0-9]{16}', incident_id):
        return {'status': 'rejected', 'speech': 'An exact incident ID is required.'}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM incidents WHERE id=? AND status!='resolved'", (incident_id,)).fetchone()
        if not row:
            return {'status': 'rejected', 'speech': 'No open incident matches that ID.'}
        if row['acknowledged_at']:
            return {'status': 'already_acknowledged', 'speech': 'Already acknowledged.', 'acknowledged_at': row['acknowledged_at']}
        stamp = now()
        db.execute('UPDATE incidents SET acknowledged_at=?,acknowledged_by=? WHERE id=?', (stamp, key[:16], incident_id))
    audit('Incident acknowledged', 'An operator owns this incident; re-notification stops.', 'ack', session)
    return {'status': 'acknowledged', 'speech': 'Acknowledged. You own this incident.', 'acknowledged_at': stamp}


def renotify_due(at=None):
    """True when the active incident is unacknowledged and the re-page interval elapsed."""
    minutes = getattr(settings(), 'renotify_minutes', 0)
    current = incident()
    if not minutes or not current or current['status'] == 'resolved' or current.get('acknowledged_at'):
        return False
    last = current.get('last_notified_at') or current['started_at']
    at = at or datetime.now(timezone.utc)
    return at >= datetime.fromisoformat(last) + timedelta(minutes=minutes)


def mark_notified(at=None):
    stamp = at.isoformat(timespec='microseconds') if at else now()
    with store.connect() as db:
        db.execute("UPDATE incidents SET last_notified_at=? WHERE status!='resolved'", (stamp,))


def in_quiet_hours(window, at=None):
    """(start_hour, end_hour) in local server time; wraps midnight when start > end."""
    if not window:
        return False
    hour = (at or datetime.now()).hour
    start, end = window
    return start <= hour < end if start < end else (hour >= start or hour < end)


async def notify(title, detail, *, url=None, token=None, mark=True):
    """One outbound page to the owner-configured webhook. Never raises, never retries.

    Sends only title, short detail, and the workspace link. Approval still requires
    the explicit control in the workspace; a notification is not consent. Quiet hours
    never suppress a page (an outage always pages); they only make the wording urgent.
    """
    cfg = settings()
    url = url or cfg.notify_url
    token = cfg.notify_token if token is None else token
    if not url:
        return {'status': 'disabled'}
    if in_quiet_hours(getattr(cfg, 'quiet_hours', None)):
        title = 'URGENT (quiet hours): ' + str(title)
    link = cfg.public_url or ''
    text = f"OnCall: {_clean(title, 160)} - {_clean(detail, 300)}" + (f" {link}" if link else '')
    body = {'slack': {'text': text}, 'json': {'title': _clean(title, 160), 'detail': _clean(detail, 300), 'url': link, 'text': text}}
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    try:
        async with http_client(timeout=5, follow_redirects=False) as client:
            if cfg.notify_format == 'ntfy':
                response = await client.post(url, content=text.encode(),
                                             headers={**headers, 'Title': 'OnCall incident', 'Priority': 'urgent', **({'Click': link} if link else {})})
            else:
                response = await client.post(url, json=body[cfg.notify_format], headers=headers)
        ok = 200 <= response.status_code < 300
    except httpx.HTTPError:
        ok = False
    if ok and mark:
        mark_notified()
    audit('Page sent' if ok else 'Page failed', 'Notification delivered to the configured endpoint.' if ok
          else 'Notification endpoint did not accept the page; the incident is still open.', 'notify')
    return {'status': 'sent' if ok else 'failed'}


async def deploys(service=SERVICE, window_hours=6.0):
    validate_service(service)
    if not 0 < window_hours <= 168:
        raise ValueError('window_hours must be greater than zero and at most 168')
    if settings().mode == 'live':
        from integrations import provider
        result = await provider.list_deploys(service, window_hours)
        if not isinstance(result, dict) or not isinstance(result.get('deploys'), list):
            raise ValueError('Deployment provider returned invalid evidence')
        return result
    latest = store.latest_deploy(service)
    rows = store.deploys_since(window_hours, service)
    return {'deploys': [dict(d, commit=d['commit_sha'], active=bool(latest and d['id'] == latest['id'] and not d['rolled_back'])) for d in rows],
            'retrieved_at': now(), 'service': service}


def active_target(result):
    if result.get('truncated') or result.get('status') in ('unavailable', 'error'):
        raise ValueError('Complete deployment evidence is required')
    candidates = [d for d in result.get('deploys', []) if (d.get('active') is True or d.get('is_active') is True) and not d.get('rolled_back')]
    if len(candidates) != 1:
        raise ValueError('Exactly one explicitly active deployment is required; refresh deployment evidence')
    d = candidates[0]
    latest = max(result['deploys'], key=lambda row: (row.get('deployed_at', row.get('ts', '')), int(row.get('id', row.get('deployment_id', 0)))))
    if d is not latest:
        raise ValueError('The explicitly active deployment is not the latest deployment')
    commit = d.get('commit') or d.get('commit_sha')
    if not isinstance(commit, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', commit):
        raise ValueError('Active deployment has no valid commit')
    # Deployment identity prevents approvals being reused for a same-commit redeploy.
    identity = d.get('id', d.get('deployment_id'))
    deployed_at = d.get('deployed_at', d.get('ts'))
    if identity is None and not deployed_at:
        raise ValueError('Active deployment needs a deployment ID or timestamp')
    return {'service': d.get('service', settings().service), 'commit': commit,
            'identity': str(identity), 'deployed_at': deployed_at}


async def rates(service=SERVICE, window_minutes=120.0):
    validate_service(service)
    if not 0 < window_minutes <= 1440:
        raise ValueError('window_minutes must be greater than zero and at most 1440')
    if settings().mode == 'live':
        from integrations import provider
        result = await provider.get_error_rates(service, window_minutes)
        if not isinstance(result, dict):
            raise ValueError('Metrics provider returned invalid evidence')
        return result
    init()
    series = store.error_series(window_minutes)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=min(window_minutes, 5))).isoformat()
    with store.connect() as db:
        rows = db.execute("SELECT ts,status FROM requests WHERE ts>=? AND route='/checkout' ORDER BY id DESC LIMIT 8", (cutoff,)).fetchall()
    return {'service': service, 'series': series, 'sample_count': len(rows),
            'current_error_rate': sum(r['status'] >= 500 for r in rows) / len(rows) if rows else None,
            'last_sample_at': rows[0]['ts'] if rows else None, 'retrieved_at': now()}


def assess(metrics):
    cfg = settings()
    if cfg.mode == 'live' and (metrics.get('status') != 'ok' or metrics.get('health') not in ('healthy', 'degraded')):
        return 'unknown'
    count = metrics.get('sample_count', metrics.get('total_requests', 0)) or 0
    rate = metrics.get('current_error_rate')
    stamp = metrics.get('last_sample_at') or metrics.get('observed_at')
    if stamp is None and cfg.mode == 'live':
        series = metrics.get('series') or []
        stamp = series[-1].get('ts') if series else None
    fresh = False
    try:
        if not isinstance(stamp, str):
            raise TypeError('Telemetry timestamp must be a string')
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
        fresh = -30 <= age <= getattr(cfg, 'telemetry_max_age_seconds', 300)
    except (TypeError, ValueError, AttributeError):
        pass
    if not fresh or not isinstance(count, (int, float)) or count <= 0 or not isinstance(rate, (float, int)) or not 0 <= rate <= 1 or metrics.get('error'):
        return 'unknown'
    return 'degraded' if rate > .05 else 'healthy'


async def logs(query='', window_minutes=30.0):
    if not isinstance(query, str) or len(query) > 256 or not 0 < window_minutes <= 1440:
        raise ValueError('Log query/window out of bounds')
    if settings().mode == 'live':
        from integrations import provider
        return await provider.search_logs(query, window_minutes)
    errors = store.recent_errors(window_minutes, query or None)
    return {'query': query, 'count': len(errors), 'errors': errors, 'retrieved_at': now()}


async def state(session='local-cli'):
    init()
    service = settings().service
    errors = []
    try:
        metrics = await rates(service)
    except Exception:
        metrics = {'series': [], 'sample_count': 0, 'current_error_rate': None}
        errors.append('Metrics unavailable; health is unknown')
    try:
        deployments = (await deploys(service)).get('deploys', [])
    except Exception:
        deployments = []
        errors.append('Deployment evidence unavailable')
    health = assess(metrics)
    from integrations import provider
    current = incident()
    impact = await blast_radius(metrics=metrics)
    cfg = settings()
    return {'mode': cfg.mode, 'service': service, 'health': health,
            # Clients show demo start/reset only when the server will accept them.
            'demo_controls': cfg.mode == 'sandbox' or bool(getattr(cfg, 'demo_controls', False)),
            'blast_radius': impact, 'escalation': escalation_status(current),
            'current_error_rate': metrics.get('current_error_rate') if health != 'unknown' else None,
            'sample_count': metrics.get('sample_count', metrics.get('total_requests', 0)) or 0,
            'series': metrics.get('series', []), 'deploys': deployments, 'incident': current,
            'timeline': audit_entries(current['id']) if current else [], 'pending': pending(session),
            'voice_challenge': voice_challenge(session), 'last_updated': now(),
            'integrations': provider.integration_status(), 'errors': errors,
            'contract': {'version': 1, 'approval': 'expiring-session-bound-exact-action-id', 'session_header': 'X-OnCall-Session'}}


# ── blast radius, runbooks, past-incident memory, escalation ─────────────

def _ts(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _counts(row):
    """(timestamp, failed, total) from one series bucket, or None if unusable."""
    if not isinstance(row, dict):
        return None
    at, total = _ts(row.get('ts')), row.get('total_requests')
    failed, rate = row.get('error_count'), row.get('error_rate')
    if not at or isinstance(total, bool) or not isinstance(total, (int, float)) or total != total or total < 0:
        return None
    if isinstance(failed, bool) or not isinstance(failed, (int, float)) or failed != failed:
        failed = rate * total if isinstance(rate, (int, float)) and 0 <= rate <= 1 else None
    if failed is None or not 0 <= failed <= total:
        return None
    return at, float(failed), float(total)


def _money(value):
    return f'${value:,.0f}' if float(value).is_integer() else f'${value:,.2f}'


def _rough(value):
    return round(value, -2) if value >= 1000 else round(value)


def _unknown_impact(reason='fresh checkout telemetry is unavailable'):
    return {'status': 'unknown', 'since': None, 'minutes': None, 'failed_requests': 0, 'total_requests': 0,
            'error_rate': None, 'trend': 'unknown', 'est_revenue_at_risk': None, 'currency': 'USD', 'estimate': True,
            'summary': f'Customer impact is unknown: {reason}.'}


async def blast_radius(series=None, *, metrics=None):
    """Customer impact since the spike began, from measured buckets only.

    Unknown, stale or malformed telemetry yields status 'unknown' (never zero impact).
    Revenue is an estimate: failed checkouts x the configured average order value.
    """
    cfg = settings()
    if series is None:
        if metrics is None:
            try:
                metrics = await rates(cfg.service)
            except Exception:
                return _unknown_impact()
        if assess(metrics) == 'unknown':
            return _unknown_impact()
        series = metrics.get('series') or []
    rows = sorted(filter(None, (_counts(r) for r in (series if isinstance(series, list) else []))), key=lambda r: r[0])
    if not rows:
        return _unknown_impact()
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(rows, rows[1:]) if b[0] > a[0]]
    width = min(gaps) if gaps else 120
    current_time = datetime.now(timezone.utc)
    age = (current_time - rows[-1][0]).total_seconds()
    if not -30 <= age <= getattr(cfg, 'telemetry_max_age_seconds', 120) + width:
        return _unknown_impact('the latest checkout telemetry is stale')
    from correlate import correlate
    spike = _ts(correlate(series, []).get('spike_start'))
    current = incident()
    since = spike or (_ts(current['started_at']) if current and current['status'] != 'resolved' else None)
    latest_rate = round(rows[-1][1] / rows[-1][2], 4) if rows[-1][2] else None
    if since is None:
        return dict(_unknown_impact(), status='measured', trend='steady', error_rate=latest_rate,
                    summary='No checkout failure spike is measured right now.')
    window = [r for r in rows if r[0] + timedelta(seconds=width) > since]
    failed, total = sum(r[1] for r in window), sum(r[2] for r in window)
    if total <= 0:
        return _unknown_impact('no checkout traffic was measured since the spike began')
    shares = [r[1] / r[2] for r in window if r[2] > 0]
    trend = 'unknown' if len(shares) < 2 else ('rising' if shares[-1] > shares[-2] + .05 else
                                               'falling' if shares[-1] < shares[-2] - .05 else 'steady')
    open_incident = bool(current and current['status'] != 'resolved')
    # A past spike still inside the series is history, not ongoing impact: once no incident
    # is open and the latest bucket is back under the degraded threshold, say so plainly
    # instead of "holding steady" (which reads as a live failure rate).
    if not open_incident and latest_rate is not None and latest_rate <= .05:
        trend = 'recovered'
    minutes = max(1, round((current_time - since).total_seconds() / 60))
    failed_count = round(failed)
    aov = float(getattr(cfg, 'avg_order_value', 0) or 0)
    revenue = round(failed_count * aov, 2) if aov > 0 else None
    noun = 'checkout has' if failed_count == 1 else 'checkouts have'
    tail = {'rising': ' and it is still rising', 'falling': ' and it is easing',
            'steady': ' and it is holding steady'}.get(trend, '')
    span = 'minute' if minutes == 1 else f'{minutes} minutes'
    if trend == 'recovered':
        verb = 'checkout' if failed_count == 1 else 'checkouts'
        summary = f'About {failed_count} {verb} failed during the earlier spike; the error rate is back to normal now.'
    else:
        summary = f'About {failed_count} {noun} failed in the last {span}{tail}.'
    if revenue is not None:
        summary += f' At the configured average order of {_money(aov)} that is roughly {_money(_rough(revenue))} at risk (estimate).'
    return {'status': 'measured', 'since': since.isoformat(), 'minutes': minutes, 'failed_requests': failed_count,
            'total_requests': round(total), 'error_rate': round(failed / total, 4), 'trend': trend,
            'est_revenue_at_risk': revenue, 'currency': 'USD', 'estimate': True, 'summary': summary}


def _impact_clause(impact):
    clause = f"About {impact['failed_requests']} checkouts failed so far"
    if impact['est_revenue_at_risk'] is not None:
        clause += f", roughly {_money(_rough(impact['est_revenue_at_risk']))} at risk"
    return clause + '.'


RUNBOOK_DIR = Path(__file__).resolve().parent / 'runbooks'
MAX_RUNBOOK_BYTES = 65536
MAX_RUNBOOKS = 50


def _parse_runbook(text):
    """Front matter of `key: value` lines (alerts as [a, b]) plus numbered steps. No YAML, no code."""
    text = text.replace('\r\n', '\n')
    if not text.startswith('---\n'):
        return None
    end = text.find('\n---', 4)
    if end < 0:
        return None
    meta = {}
    for line in text[4:end].splitlines()[:40]:
        key, sep, value = line.partition(':')
        if sep and re.fullmatch(r'[a-z_]{1,32}', key.strip()):
            meta[key.strip()] = value.strip()[:300]
    alerts = [a.strip().strip('"\'') for a in meta.get('alerts', '').strip('[]').split(',')]
    steps = []
    for line in text[end + 4:].splitlines():
        match = re.match(r'^\d{1,2}[.)]\s+(.+)$', line)
        if match:
            steps.append(_clean(match.group(1), 300))
        if len(steps) >= 20:
            break
    return {'service': meta.get('service', ''), 'title': _clean(meta.get('title', ''), 120),
            'alerts': [a.lower() for a in alerts if a][:20], 'default': meta.get('default', '').lower() == 'true',
            'steps': steps}


def runbook(service, alertname=None):
    """Best matching runbook from runbooks/: exact alert name first, then the service default."""
    if not isinstance(service, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', service):
        return None
    alert = alertname.lower() if isinstance(alertname, str) and len(alertname) <= 120 else None
    fallback = None
    try:
        root = RUNBOOK_DIR.resolve()
        files = sorted(RUNBOOK_DIR.glob('*.md'))[:MAX_RUNBOOKS]
    except OSError:
        return None
    for path in files:
        try:
            # Confined to the runbooks directory: no symlinks, no oversized files.
            if path.is_symlink() or path.resolve().parent != root or path.stat().st_size > MAX_RUNBOOK_BYTES:
                continue
            book = _parse_runbook(path.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            continue
        if not book or book['service'] != service or not book['title'] or not book['steps']:
            continue
        found = {'title': book['title'], 'source': f'runbooks/{path.name}', 'steps': book['steps']}
        if alert and alert in book['alerts']:
            return dict(found, matched_on='alert')
        if book['default'] and fallback is None:
            fallback = dict(found, matched_on='service')
    return fallback


def _latest_alertname(current):
    if not current:
        return None
    with store.connect() as db:
        row = db.execute('SELECT name FROM alerts WHERE incident_id=? ORDER BY last_seen DESC LIMIT 1', (current['id'],)).fetchone()
    return row['name'] if row else None


def _family(title):
    return re.sub(r'[^a-z0-9]+', ' ', str(title).lower()).strip()


ROLLBACK_TITLES = {'rolled_back': 'Rollback completed', 'uncertain': 'Rollback outcome unknown', 'rejected': 'Rollback rejected'}


def _incident_summary(db, row):
    titles = {r['title'] for r in db.execute('SELECT title FROM audit WHERE incident_id=?', (row['id'],))}
    minutes = None
    if row['resolved_at']:
        try:
            minutes = round((datetime.fromisoformat(row['resolved_at']) - datetime.fromisoformat(row['started_at'])).total_seconds() / 60)
        except (TypeError, ValueError):
            minutes = None
    fix = None
    if row['status'] == 'resolved':
        if 'Demo reset' in titles:
            fix = 'closed by demo reset'
        elif titles & {'Rollback completed', 'Rollback rolled_back'}:  # second: records written before the rename
            reserved = db.execute("SELECT detail FROM audit WHERE incident_id=? AND title='Rollback reserved' ORDER BY id DESC LIMIT 1",
                                  (row['id'],)).fetchone()
            commit = re.search(r'@([A-Za-z0-9_-]{1,128})', reserved['detail']) if reserved else None
            fix = f'rolled back {commit.group(1)}' if commit else 'rolled back'
        else:
            fix = 'recovered without action'
    report = db.execute('SELECT 1 FROM reports WHERE instr(markdown, ?) > 0 LIMIT 1',
                        (f"Started: {row['started_at']}",)).fetchone()
    return {'id': row['id'], 'title': row['title'], 'status': row['status'], 'source': row['source'],
            'started_at': row['started_at'], 'resolved_at': row['resolved_at'], 'minutes_to_recover': minutes,
            'fix': fix, 'report_available': report is not None}


def incidents(limit=20):
    """Recent incidents, newest first, with how each one ended."""
    init()
    limit = max(1, min(int(limit), 100))
    with store.connect() as db:
        rows = db.execute('SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
        return [_incident_summary(db, row) for row in rows]


def similar_incidents(limit=3):
    """Resolved incidents in the current incident's title family, newest first.

    Demo resets are excluded: closing an incident by hand is not a recovery to learn from.
    """
    init()
    limit = max(1, min(int(limit), 10))
    current = incident()
    if not current:
        return []
    family = _family(current['title'])
    found = []
    with store.connect() as db:
        rows = db.execute("SELECT * FROM incidents WHERE status='resolved' AND id!=? ORDER BY started_at DESC LIMIT 200",
                          (current['id'],)).fetchall()
        for row in rows:
            if _family(row['title']) != family:
                continue
            item = _incident_summary(db, row)
            if item['fix'] == 'closed by demo reset':
                continue
            found.append({k: v for k, v in item.items() if k not in ('status', 'source')})
            if len(found) >= limit:
                break
    return found


def _similar_clause(item):
    start = _ts(item['started_at'])
    when = f'{start:%b} {start.day}' if start else 'an earlier date'
    minutes = item['minutes_to_recover']
    took = f' in {minutes} minutes' if isinstance(minutes, int) else ''
    if item['fix'] and item['fix'].startswith('rolled back'):
        return f'This matches the incident on {when}; a rollback fixed it{took}.'
    return f'This matches the incident on {when}; it recovered without action{took}.'


def escalation_status(current=None):
    cfg = settings()
    current = incident() if current is None else current
    if not current or current['status'] == 'resolved':
        return {'level': 'primary', 'acknowledged': False, 'next_page_at': None, 'escalated_at': None}
    escalated = current.get('escalated_at')
    level = 'secondary' if escalated else 'primary'
    if current.get('acknowledged_at'):
        return {'level': level, 'acknowledged': True, 'next_page_at': None, 'escalated_at': escalated}
    candidates = []
    minutes = getattr(cfg, 'renotify_minutes', 0)
    started = _ts(current['started_at'])
    if minutes and getattr(cfg, 'notify_url', ''):
        last = _ts(current.get('last_notified_at') or current['started_at'])
        if last:
            candidates.append(last + timedelta(minutes=minutes))
    if not escalated and getattr(cfg, 'secondary_notify_url', '') and started:
        candidates.append(started + timedelta(minutes=getattr(cfg, 'escalation_minutes', 10)))
    return {'level': level, 'acknowledged': False, 'escalated_at': escalated,
            'next_page_at': min(candidates).isoformat() if candidates else None}


async def escalation_tick(at=None):
    """One background pass: re-page the primary when due, escalate once to the secondary.

    Pages are at-most-once: the re-page clock advances on every attempt, success or not,
    so the primary is never paged more often than renotify_minutes. The secondary is
    reserved atomically before sending and is paged at most once per incident.
    """
    init()
    at = at or datetime.now(timezone.utc)
    current = incident()
    if not current or current['status'] == 'resolved' or current.get('acknowledged_at'):
        return {'status': 'idle'}
    cfg = settings()
    out = {'status': 'checked'}
    if getattr(cfg, 'notify_url', '') and renotify_due(at):
        mark_notified(at)
        out['primary'] = (await notify(current['title'], 'Still unacknowledged.', mark=False))['status']
    started = _ts(current['started_at'])
    if (getattr(cfg, 'secondary_notify_url', '') and not current.get('escalated_at') and started
            and at >= started + timedelta(minutes=getattr(cfg, 'escalation_minutes', 10))):
        with store.connect() as db:
            reserved = db.execute("UPDATE incidents SET escalated_at=? WHERE id=? AND escalated_at IS NULL "
                                  "AND acknowledged_at IS NULL AND status!='resolved'",
                                  (at.isoformat(timespec='microseconds'), current['id'])).rowcount
        if reserved == 1:
            minutes = getattr(cfg, 'escalation_minutes', 10)
            sent = await notify(current['title'], f'Escalated: unacknowledged for {minutes} minutes.',
                                url=cfg.secondary_notify_url, token=getattr(cfg, 'secondary_notify_token', ''), mark=False)
            audit('Escalated to secondary', f"Unacknowledged for {minutes} minutes; secondary page {sent['status']}.", 'notify')
            out['secondary'] = sent['status']
    return out


PUBLIC_KINDS = ('alert', 'ack', 'diagnosis', 'investigation', 'approval', 'cancel', 'mutation', 'verification', 'notify')


async def public_status():
    """Redacted, read-only projection for an unauthenticated status page.

    Exposes only measured health, the error-rate series, deployment commits/times,
    incident metadata and event *titles*. No audit details (log lines, model text),
    no action IDs, no integration configuration, no session data.
    """
    full = await state('public-status')
    current = full['incident']
    return {
        'service': full['service'], 'mode': full['mode'], 'health': full['health'],
        'current_error_rate': full['current_error_rate'], 'sample_count': full['sample_count'],
        'series': [{'ts': p.get('ts'), 'error_rate': p.get('error_rate')} for p in full['series']][-60:],
        'deploys': [{'commit': d.get('commit'), 'deployed_at': d.get('deployed_at'), 'active': bool(d.get('active')),
                     'rolled_back': bool(d.get('rolled_back'))} for d in full['deploys']][-10:],
        'incident': {k: current.get(k) for k in ('id', 'title', 'status', 'started_at', 'acknowledged_at', 'resolved_at', 'source')} if current else None,
        'timeline': [{'at': e['at'], 'title': e['title'], 'kind': e['kind']} for e in full['timeline'] if e['kind'] in PUBLIC_KINDS][-30:],
        'approval_waiting': _pending_any(),
        'last_updated': full['last_updated'],
    }


def _pending_any():
    with store.connect() as db:
        return db.execute("SELECT 1 FROM approvals WHERE status='pending' AND expires_at>? LIMIT 1", (now(),)).fetchone() is not None


def _ago(stamp):
    try:
        seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return None
    if seconds >= 120:
        return f'{int(seconds // 60)} minutes ago'
    whole = max(0, int(seconds))
    return 'just now' if whole < 2 else f'{whole} seconds ago'


async def briefing(session='local-cli'):
    """Read-only, voice-sized summary. Never opens incidents or writes audit."""
    full = await state(session)
    current, health, rate = full['incident'], full['health'], full['current_error_rate']
    active = current and current['status'] != 'resolved'
    latest = (full['deploys'] or [None])[-1]
    pend = full['pending']
    if pend:
        step = 'approve_or_cancel'
        speech = f"A rollback of {pend['service']} from {pend['commit']} is waiting for your approval. Say approve to get a one-time code, or cancel."
    elif active and current['status'] == 'verifying':
        # After a rollback, metrics still include pre-rollback failures; never blame the
        # restored release or offer a second rollback. Only fresh traffic can verify.
        step = 'verify'
        speech = (f"The rollback is applied. Recovery isn't verified yet; I'm waiting for fresh {full['service']} traffic. "
                  "Ask me to check recovery.")
    elif health == 'unknown':
        step = 'investigate'
        speech = f"I can't confirm {full['service']} health right now; fresh telemetry is missing. Treat it as unknown, not fine."
    elif health == 'degraded':
        step = 'review_rollback'
        deploy = f" The latest deploy, {latest.get('commit')}, went out {_ago(latest.get('deployed_at')) or 'recently'}." if latest else ''
        speech = f"{full['service'].capitalize()} is failing {round(rate * 100)} percent of requests.{deploy} Want me to prepare a rollback?"
    else:
        step = 'none'
        speech = f"Nothing's on fire. {full['service'].capitalize()} is healthy at {round((rate or 0) * 100, 1)} percent errors."
    impact = full['blast_radius']
    similar = similar_incidents(3)
    book = runbook(full['service'], _latest_alertname(current)) if active else None
    extras = []
    if active and impact['status'] == 'measured' and impact['failed_requests']:
        extras.append(_impact_clause(impact))
    if active and not current.get('acknowledged_at'):
        extras.append('The incident is not acknowledged yet.')
    if active and similar:
        extras.append(_similar_clause(similar[0]))
    for clause in extras:
        # Voice budget: keep the whole answer under ~45 words; drop clauses that do not fit.
        if len((speech + ' ' + clause).split()) <= 45:
            speech += ' ' + clause
    card = {'title': current['title'] if active else f"{full['service']} is {health}", 'health': health,
            'error_rate': rate, 'sample_count': full['sample_count'],
            'incident': {k: current.get(k) for k in ('id', 'status', 'started_at', 'acknowledged_at')} if current else None,
            'latest_deploy': {k: latest.get(k) for k in ('commit', 'deployed_at', 'active', 'rolled_back')} if latest else None,
            'pending': pend, 'next_step': step, 'blast_radius': impact,
            'runbook': dict(book, steps=book['steps'][:3]) if book else None, 'similar': similar}
    return {'speech': speech, 'card': card, 'mode': full['mode'], 'observed_at': full['last_updated']}


async def diagnose(session='local-cli', service=SERVICE):
    validate_service(service)
    metrics = await rates(service)
    deployments = await deploys(service)
    evidence = await logs('', 30)
    health = assess(metrics)
    from correlate import correlate
    timing = correlate(metrics.get('series') or [], deployments.get('deploys') or [])
    if health == 'degraded':
        open_incident(f'{service.capitalize()} request failures')
    if health == 'degraded' and evidence.get('status') == 'unavailable':
        message = 'Errors are elevated. Review the active deployment. Log evidence is unavailable, and correlation does not prove cause.'
    else:
        message = {'unknown': 'Health is unknown: fresh request telemetry is unavailable.',
                   'healthy': 'Recent measured traffic is healthy. This is not a recovery verification.',
                   'degraded': 'Errors are elevated. Review the active deployment and logs; correlation does not prove cause.'}[health]
    if health == 'degraded' and timing['verdict'] in ('deploy_precedes_spike', 'no_recent_deploy'):
        message = 'Errors are elevated. ' + timing['summary']
        top = (evidence.get('errors') or [{}])[0].get('detail') if evidence.get('status') != 'unavailable' else None
        if top:
            message += f" Most recent error: {_clean(top, 160)}"
    audit('Diagnosis', message, 'diagnosis', session)
    return {'speech': message, 'health': health, 'correlation': timing, 'metrics': metrics,
            'deployments': deployments, 'logs': evidence}


async def propose(session='local-cli', service=SERVICE):
    """Stage a rollback of the active release for explicit approval.

    Single choke point for every caller (operator command, Bedrock investigator, any MCP
    client including Alexa+): a rollback is only staged against fresh, measured
    degradation. Healthy or unknown health refuses and stages nothing, because rolling
    back a good release is itself an outage."""
    init()
    validate_service(service)
    key = session_key(session)
    health = assess(await rates(service))
    if health != 'degraded':
        reason = ('Checkout is healthy right now, so there is nothing to roll back.' if health == 'healthy'
                  else "I can't confirm checkout health right now, so I won't stage a rollback blind.")
        audit('Rollback not staged', f'{reason} Measured health: {health}.', 'approval', session)
        return {'status': 'not_warranted', 'health': health, 'pending': pending(session),
                'speech': f'{reason} No change has been made.'}
    # Measured degradation is an incident even if the monitoring alert hasn't fired yet;
    # without one, an approved rollback would have nothing to verify or close, and the
    # late alert would open a second incident. Idempotent: reuses the open incident.
    open_incident(f'{service.capitalize()} request failures', source='rollback')
    target = active_target(await deploys(service, 168))
    if target['service'] != service:
        raise ValueError('Deployment service does not match the proposal')
    created = now()
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=APPROVAL_SECONDS)).isoformat(timespec='microseconds')
    action_id = secrets.token_hex(32)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        # A repeat request for the same exact release reuses the pending action instead of
        # minting a new one. Its original expiry is kept, so asking again never extends the
        # approval window. A different target still supersedes.
        same = db.execute("SELECT id FROM approvals WHERE session=? AND status='pending' AND service=? "
                          "AND commit_sha=? AND target=? AND expires_at>?",
                          (key, service, target['commit'], json.dumps(target, sort_keys=True), created)).fetchone()
        if not same:
            db.execute("UPDATE approvals SET status='superseded' WHERE session=? AND status='pending'", (key,))
            db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                       (action_id, key, 'rollback', service, target['commit'], json.dumps(target, sort_keys=True), created, expiry, 'pending'))
    if same:
        return {'status': 'confirmation_required', 'pending': pending(session),
                'speech': f"A rollback of {service}@{target['commit']} is already waiting for your approval. No change has been made."}
    audit('Rollback proposed', f"Review {service}@{target['commit']}; approval expires in {APPROVAL_SECONDS} seconds.", 'approval', session)
    return {'status': 'confirmation_required', 'pending': pending(session),
            'speech': f"Review rollback of {service}@{target['commit']}. Use the explicit approval control with this action ID. No change has been made."}


async def execute(session, action_id, approved, service=None, commit=None):
    init()
    key = session_key(session)
    if type(approved) is not bool or not isinstance(action_id, str) or not re.fullmatch(r'[a-f0-9]{64}', action_id):
        return {'status': 'rejected', 'speech': 'An exact action ID and explicit boolean approval are required.'}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM approvals WHERE id=?', (action_id,)).fetchone()
        valid = row and hmac.compare_digest(row['session'], key) and row['status'] == 'pending' and row['expires_at'] > now()
        if valid and service is not None:
            valid = row['service'] == service and row['commit_sha'] == commit
        if not valid:
            return {'status': 'rejected', 'speech': 'Approval is expired, consumed, wrong-session or does not match this target.'}
        if approved:
            reserved = db.execute('INSERT OR IGNORE INTO executions(target,action_id) VALUES(?,?)', (row['target'], action_id)).rowcount
            if reserved != 1:
                db.execute("UPDATE approvals SET status='superseded' WHERE id=?", (action_id,))
                return {'status': 'rejected', 'speech': 'This deployment already has a reserved execution. Reconcile its outcome; it cannot be submitted again.'}
        db.execute('UPDATE approvals SET status=? WHERE id=?', ('executing' if approved else 'cancelled', action_id))
    if not approved:
        audit('Rollback cancelled', 'No deployment changed.', 'cancel', session)
        return {'status': 'cancelled', 'speech': 'Cancelled. Nothing changed.'}
    audit('Rollback reserved', f"{row['service']}@{row['commit_sha']}; one execution attempt authorized.", 'approval', session)
    result = {'status': 'rejected', 'speech': 'The active deployment changed. Propose a new action.'}
    try:
        target = active_target(await deploys(row['service'], 168))
        if json.dumps(target, sort_keys=True) == row['target']:
            if settings().mode == 'sandbox':
                # Compare identity and mutate under the same SQLite writer lock.
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    changed = db.execute('''UPDATE deploys SET rolled_back=1
                        WHERE id=? AND id=(SELECT max(id) FROM deploys WHERE service=?)
                        AND commit_sha=? AND rolled_back=0''', (target['identity'], row['service'], row['commit_sha'])).rowcount
                if changed == 1:
                    result = {'status': 'rolled_back', 'speech': 'Rollback applied. Recovery is not yet verified; run fresh probes.'}
            else:
                from integrations import provider
                outcome = await provider.rollback(row['service'], row['commit_sha'], action_id)
                if outcome.get('status') == 'rolled_back':
                    result = {'status': 'rolled_back', 'speech': 'Rollback provider accepted the change. Recovery remains unverified.', 'provider': outcome}
                else:
                    result = {'status': 'uncertain', 'speech': 'Rollback was not confirmed. Inspect the provider before any new action.'}
    except Exception:
        result = {'status': 'uncertain', 'speech': 'Execution evidence unavailable. This action is consumed and will not be retried automatically.'}
    with store.connect() as db:
        db.execute('UPDATE executions SET completed_at=? WHERE action_id=?', (now(), action_id))
        db.execute('UPDATE approvals SET status=?,result=? WHERE id=?', (result['status'], json.dumps(result), action_id))
        if result['status'] == 'rolled_back':
            db.execute("UPDATE incidents SET status='verifying' WHERE status!='resolved'")
    audit(ROLLBACK_TITLES.get(result['status'], 'Rollback ' + result['status']), result['speech'], 'mutation', session)
    return result


# ── Voice approval: a spoken one-time code delivered over a second channel ──────
# Spoken assent ("yes") never authorizes. A voice approval requires a 6-digit code
# bound to one exact action ID, delivered out-of-band (the paging channel), expiring,
# single-use and limited to three attempts. Sandbox without a pager shows the code
# on the workspace screen, which is the second channel for a local operator.
VOICE_CODE_SECONDS = 90
VOICE_MAX_ATTEMPTS = 3
_SPOKEN_DIGITS = {'zero': '0', 'oh': '0', 'o': '0', 'one': '1', 'two': '2', 'to': '2', 'too': '2', 'three': '3',
                  'four': '4', 'for': '4', 'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'ate': '8', 'nine': '9'}


def spoken_code(text):
    """Extract exactly six digits from speech like 'four 2 seven, 1-9-0'. Anything else -> None."""
    if not isinstance(text, str) or len(text) > 200:
        return None
    digits = []
    for token in re.findall(r'[a-z]+|\d', text.lower()):
        if token.isdigit():
            digits.append(token)
        elif token in _SPOKEN_DIGITS:
            digits.append(_SPOKEN_DIGITS[token])
        elif token not in ('code', 'is', 'the', 'my', 'approve', 'approval', 'with', 'confirm', 'and', 'dash'):
            return None
    return ''.join(digits) if len(digits) == 6 else None


def _code_hash(action_id, code):
    return hmac.new(action_id.encode(), code.encode(), hashlib.sha256).hexdigest()


def voice_challenge(session):
    """Public view of the current challenge for this session's pending action."""
    action = pending(session)
    if not action:
        return None
    with store.connect() as db:
        row = db.execute("SELECT * FROM voice_challenges WHERE action_id=? AND session=?",
                         (action['id'], session_key(session))).fetchone()
    if not row:
        return None
    live = row['status'] == 'pending' and row['expires_at'] > now()
    return {'action_id': row['action_id'], 'delivery': row['delivery'], 'expires_at': row['expires_at'],
            'status': row['status'] if live or row['status'] != 'pending' else 'expired',
            'attempts_left': max(0, VOICE_MAX_ATTEMPTS - row['attempts']),
            # Shown only for sandbox screen delivery; a paged code never returns to the caller.
            'display_code': row['display_code'] if live and row['delivery'] == 'screen' else None}


async def request_voice_approval(session, action_id):
    init()
    key = session_key(session)
    action = pending(session)
    if not action or action['id'] != action_id:
        return {'status': 'rejected', 'speech': 'There is no pending rollback with that action ID. Ask me to review a rollback first.'}
    cfg = settings()
    if not cfg.notify_url and cfg.mode != 'sandbox':
        return {'status': 'unavailable', 'speech': 'Voice approval needs a paging channel for the one-time code. Approve in the workspace instead.'}
    code = f'{secrets.randbelow(10**6):06d}'
    expiry = min(datetime.fromisoformat(action['expires_at']),
                 datetime.now(timezone.utc) + timedelta(seconds=VOICE_CODE_SECONDS)).isoformat(timespec='microseconds')
    # The code can't outlive its approval, so speak the real remaining time, not the cap.
    seconds = max(1, int((datetime.fromisoformat(expiry) - datetime.now(timezone.utc)).total_seconds()))
    delivery = 'page' if cfg.notify_url else 'screen'
    if delivery == 'page':
        # The code travels only on the paging channel. It is never stored in plaintext.
        sent = await notify('Rollback approval code', f"Code {' '.join(code)} approves {action['service']}@{action['commit']} for {seconds}s. Ignore if you did not ask.")
        if sent.get('status') != 'sent':
            return {'status': 'unavailable', 'speech': 'I could not deliver the approval code. Nothing changed. Approve in the workspace instead.'}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR REPLACE INTO voice_challenges VALUES(?,?,?,?,?,?,?,0,?)',
                   (action_id, key, _code_hash(action_id, code), delivery, code if delivery == 'screen' else None,
                    now(), expiry, 'pending'))
    audit('Voice approval requested', f"One-time code sent via {delivery} for {action['service']}@{action['commit']}.", 'approval', session)
    where = 'to your pager' if delivery == 'page' else 'on the workspace screen'
    return {'status': 'code_sent', 'delivery': delivery, 'expires_at': expiry,
            'speech': f"To roll back {action['service']} from commit {action['commit']}, read me the six-digit code I just sent {where}. It expires in {seconds} seconds."}


async def approve_with_code(session, action_id, spoken):
    init()
    key = session_key(session)
    code = spoken_code(spoken)
    # Check and consume under one writer lock: a correct code can be used exactly once.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM voice_challenges WHERE action_id=?', (action_id,)).fetchone()
        if not row or not hmac.compare_digest(row['session'], key) or row['status'] != 'pending' or row['expires_at'] <= now():
            return {'status': 'rejected', 'speech': 'That approval code is expired or already used. Nothing changed. Ask for a new code.'}
        matched = code is not None and hmac.compare_digest(row['code_hash'], _code_hash(action_id, code))
        attempts = row['attempts'] + (0 if matched else 1)
        locked = not matched and attempts >= VOICE_MAX_ATTEMPTS
        db.execute('UPDATE voice_challenges SET attempts=?, status=? WHERE action_id=?',
                   (attempts, 'consumed' if matched else 'locked' if locked else 'pending', action_id))
    if not matched:
        audit('Voice code rejected', f'Attempt {attempts} of {VOICE_MAX_ATTEMPTS}.', 'approval', session)
        return {'status': 'locked' if locked else 'wrong_code', 'attempts_left': VOICE_MAX_ATTEMPTS - attempts,
                'speech': 'Too many wrong codes. Voice approval is locked for this action. Nothing changed.' if locked
                else f"That code didn't match. Nothing changed. {VOICE_MAX_ATTEMPTS - attempts} tries left."}
    audit('Voice code accepted', 'Out-of-band code matched; submitting the exact approved action once.', 'approval', session)
    return await execute(session, action_id, True)


def ensure_local_sandbox():
    cfg = settings()
    if cfg.mode != 'sandbox':
        raise ValueError('Sandbox controls are disabled in live mode')
    url = urlparse(cfg.target_url)
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username or url.password:
        raise ValueError('Sandbox requests must use a loopback HTTP target')
    if store.DB_PATH.resolve().name.lower() == 'ops.db':
        raise ValueError('Sandbox controls cannot modify the original ops.db; configure ONCALL_DB_PATH')


async def probes(count=8):
    ensure_local_sandbox()
    if not 1 <= count <= 24:
        raise ValueError('Probe count must be between 1 and 24')
    started = now()
    statuses = []
    failures = 0
    async with http_client(timeout=3, follow_redirects=False) as client:
        for _ in range(count):
            try:
                response = await client.post(settings().target_url.rstrip('/') + '/checkout')
                statuses.append(response.status_code)
            except httpx.HTTPError:
                failures += 1
    health = 'unknown' if failures or len(statuses) != count else ('healthy' if all(200 <= code < 300 for code in statuses) else 'degraded')
    return {'health': health, 'sample_count': len(statuses), 'transport_errors': failures,
            'statuses': statuses, 'started_at': started, 'observed_at': now(),
            'current_error_rate': sum(s >= 500 for s in statuses) / len(statuses) if statuses else None}


SANDBOX_TRAFFIC_BATCH = 4


async def sandbox_traffic_tick():
    """Sandbox only: keep real /checkout traffic flowing while an incident is open.

    Without it the sandbox only sees probe bursts (demo start, verify), so telemetry
    goes stale within minutes and impact/health correctly fall back to 'unknown'.
    Live mode never sends synthetic traffic.
    """
    if settings().mode != 'sandbox':
        return {'status': 'skipped', 'reason': 'live mode'}
    current = incident()
    if not current or current['status'] == 'resolved':
        return {'status': 'idle'}
    result = await probes(SANDBOX_TRAFFIC_BATCH)
    return {'status': 'sent', 'health': result['health'], 'sample_count': result['sample_count']}


def _guard_no_active_scenario():
    with store.connect() as db:
        db.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND expires_at<=?", (now(),))
        if db.execute("SELECT 1 FROM incidents WHERE status!='resolved' LIMIT 1").fetchone():
            raise ValueError('An active incident must be resolved (or reset) before starting another scenario')
        if db.execute("SELECT 1 FROM approvals WHERE status='pending' LIMIT 1").fetchone():
            raise ValueError('A pending approval must be cancelled or expired before starting another scenario')


def _live_demo_allowed():
    cfg = settings()
    if not getattr(cfg, 'demo_controls', False):
        raise ValueError('Demo controls are disabled in live mode (ONCALL_DEMO_CONTROLS=true enables them)')
    if not cfg.rollback_url or not cfg.rollback_token or not cfg.rollback_url.rstrip('/').endswith('/rollback'):
        raise ValueError('Live demo controls need the release controller ONCALL_ROLLBACK_URL (.../rollback) and token')
    return cfg


async def _controller_deploy(commit, note):
    """Ask the release controller to ship one allowlisted release. A timeout is an unknown outcome."""
    cfg = _live_demo_allowed()
    url = cfg.rollback_url.rstrip('/')[:-len('/rollback')] + '/deploy'
    try:
        async with http_client(timeout=10, follow_redirects=False) as client:
            response = await client.post(url, json={'service': cfg.service, 'commit': commit, 'note': note},
                                         headers={'Authorization': f'Bearer {cfg.rollback_token}'})
        body = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        audit('Demo deploy outcome unknown', f'Release controller did not confirm {commit}.', 'error')
        raise ValueError('Deploy outcome unknown; check the release controller before retrying') from None
    if not isinstance(body, dict) or body.get('status') not in ('deployed', 'already_active') or body.get('commit') != commit:
        raise ValueError(f'Release controller refused the demo deploy (HTTP {response.status_code})')
    return {'status': body['status'], 'commit': commit}


async def start_demo(session='local-cli'):
    if settings().mode == 'live':
        _live_demo_allowed()
        init()
        _guard_no_active_scenario()
        outcome = await _controller_deploy(settings().demo_bad_commit, 'Demo: ship the known-bad release')
        audit('Demo release shipped', f"Release controller now serving {outcome['commit']} ({outcome['status']}).", 'scenario', session)
        return dict(await state(session), demo=outcome)
    ensure_local_sandbox()
    init()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_time = now()
        db.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND expires_at<=?", (current_time,))
        lock = db.execute("SELECT expires_at FROM workflow_locks WHERE name='demo'").fetchone()
        if lock and lock['expires_at'] > current_time:
            raise ValueError('A sandbox scenario is already starting')
        if db.execute("SELECT 1 FROM incidents WHERE status!='resolved' LIMIT 1").fetchone():
            raise ValueError('An active incident must be resolved before starting another sandbox scenario')
        if db.execute("SELECT 1 FROM approvals WHERE status='pending' LIMIT 1").fetchone():
            raise ValueError('A pending approval must be cancelled or expired before starting another sandbox scenario')
        db.execute("INSERT OR REPLACE INTO workflow_locks VALUES('demo',?)", ((datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),))
    try:
        store.record_deploy(SERVICE, secrets.token_hex(4), faulty=False, note='Sandbox baseline; real local HTTP requests')
        baseline = await probes(8)
        if baseline['health'] != 'healthy':
            raise ValueError('Local target baseline did not pass; check target connectivity and database configuration')
        store.record_deploy(SERVICE, secrets.token_hex(4), faulty=True, note='Isolated sandbox timeout scenario')
        open_incident('Payments checkout timeout — sandbox')
        measured = await probes(8)
        if measured['health'] != 'degraded' or measured['statuses'] != [500] * 8:
            audit('Sandbox fault not observed', 'Target responses do not match this scenario; check target URL and shared database.', 'error', session)
            raise ValueError('Sandbox fault was not observed; check target URL and database configuration')
        audit('Sandbox scenario started', f"Real checkout HTTP responses: {measured['statuses']}; transport errors: {measured['transport_errors']}", 'scenario', session)
        return await state(session)
    finally:
        with store.connect() as db:
            db.execute("DELETE FROM workflow_locks WHERE name='demo'")


async def reset_demo(session='local-cli'):
    """Return to a healthy baseline without deleting history.

    Open incidents close as resolved with a 'Demo reset' audit note (excluded from
    similar-incident memory), pending approvals and voice codes are cancelled, and the
    known-good release is restored. Live mode requires ONCALL_DEMO_CONTROLS=true and
    rolls forward through the release controller's authenticated /deploy.
    """
    init()
    outcome = None
    if settings().mode == 'live':
        _live_demo_allowed()
        outcome = await _controller_deploy(settings().demo_good_commit, 'Demo reset: known-good release')
    else:
        ensure_local_sandbox()
    stamp = now()
    key = session_key(session)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        open_rows = db.execute("SELECT id FROM incidents WHERE status!='resolved'").fetchall()
        for row in open_rows:
            db.execute("UPDATE incidents SET status='resolved',resolved_at=? WHERE id=?", (stamp, row['id']))
            db.execute('INSERT INTO audit(at,title,detail,kind,incident_id,session) VALUES(?,?,?,?,?,?)',
                       (stamp, 'Demo reset', 'Closed by demo controls; recovery was not verified.', 'scenario', row['id'], key))
        cancelled = db.execute("UPDATE approvals SET status='cancelled' WHERE status='pending'").rowcount
        db.execute("UPDATE voice_challenges SET status='cancelled' WHERE status='pending'")
        db.execute("DELETE FROM workflow_locks WHERE name='demo'")
    if settings().mode == 'sandbox' and store.fault_active(SERVICE):
        store.record_deploy(SERVICE, secrets.token_hex(4), faulty=False, note='Demo reset: known-good baseline')
    return {'status': 'reset', 'closed_incidents': len(open_rows), 'cancelled_approvals': cancelled,
            'demo': outcome, 'state': await state(session)}


WATCH_INTERVAL = 10


async def _sleep(seconds):
    import asyncio
    await asyncio.sleep(seconds)


async def _watch(watch_seconds):
    """Sandbox watch window: fresh probe batches every 10s; every batch must be healthy.

    Stops at the first non-healthy batch. One healthy sample is not recovery.
    """
    batches = []
    for index in range(watch_seconds // WATCH_INTERVAL + 1):
        if index:
            await _sleep(WATCH_INTERVAL)
        batch = await probes(8)
        batches.append({'at': batch.get('observed_at'), 'health': batch['health'],
                        'sample_count': batch.get('sample_count', 0), 'current_error_rate': batch.get('current_error_rate')})
        if batch['health'] != 'healthy':
            break
    health = batches[-1]['health'] if batches[-1]['health'] != 'healthy' else 'healthy'
    return {'health': health, 'watch': batches, 'watch_seconds': watch_seconds,
            'sample_count': sum(b['sample_count'] for b in batches),
            'current_error_rate': batches[-1]['current_error_rate'], 'observed_at': batches[-1]['at']}


async def verify(session='local-cli', watch_seconds=None):
    from config import WATCH_WINDOWS
    watch_seconds = getattr(settings(), 'verify_watch_seconds', 30) if watch_seconds is None else watch_seconds
    if watch_seconds not in WATCH_WINDOWS:
        raise ValueError('watch_seconds must be one of ' + ', '.join(map(str, WATCH_WINDOWS)))
    if settings().mode == 'sandbox':
        result = await _watch(watch_seconds)
    else:
        # Live probes are read-only metrics queries, never synthetic payment POSTs.
        metrics = await rates(settings().service, 5)
        result = dict(metrics, health=assess(metrics))
        latest = incident()
        observed = metrics.get('last_sample_at') or metrics.get('observed_at')
        if latest and latest['status'] == 'verifying':
            with store.connect() as db:
                action = db.execute("SELECT e.completed_at FROM executions e JOIN approvals a ON a.id=e.action_id WHERE a.status='rolled_back' ORDER BY e.completed_at DESC LIMIT 1").fetchone()
            # Require a whole two-minute metrics bucket after execution, not a
            # cached pre-change scrape or a bucket dominated by historical traffic.
            try:
                complete = datetime.fromisoformat(action['completed_at']) if action else None
                observed_time = datetime.fromisoformat(observed.replace('Z', '+00:00')) if observed else None
                if not complete or not observed_time or observed_time < complete + timedelta(seconds=120):
                    result['health'] = 'unknown'
            except (ValueError, TypeError):
                result['health'] = 'unknown'
            if result['health'] == 'unknown':
                # Pre-rollback traffic is not fresh evidence; don't report it as such.
                result['sample_count'] = 0
    if result['health'] == 'healthy':
        with store.connect() as db:
            db.execute("UPDATE incidents SET status='resolved',resolved_at=? WHERE status!='resolved'", (now(),))
        held = f" held healthy across {watch_seconds} seconds" if settings().mode == 'sandbox' and watch_seconds else ''
        # Prometheus increase() yields estimated (fractional) counts; speak a whole number.
        count = round(result.get('sample_count') or 0)
        message = f"Recovery verified with {count} fresh observed requests{held}. Continue monitoring."
    else:
        message = 'Recovery is not verified. ' + ('Health is unknown; probes or fresh telemetry are unavailable.' if result['health'] == 'unknown' else 'Fresh requests still show failures.')
    audit('Recovery verified' if result['health'] == 'healthy' else 'Recovery not verified', message, 'verification', session)
    result['speech'] = message
    report()
    return result


# One background verification at a time, so voice clients (Alexa+ needs <500 ms per
# call) can start the watch window and poll it instead of holding a request open.
_background = {'task': None, 'started': None, 'watch_seconds': None, 'session': None}


async def check_recovery(session='local-cli'):
    """Non-blocking verify: start the watch window on first call, report on later calls.

    Same evidence and resolution rules as verify(); only the waiting moves off the request.
    """
    import asyncio
    task = _background['task']
    if task and task.done():
        _background['task'] = None
        try:
            result = task.result()
        except Exception:
            return {'status': 'unknown', 'health': 'unknown',
                    'speech': 'Recovery check failed to complete. Health is unknown; ask me to check recovery again.'}
        return dict(result, status='complete')
    if task:
        elapsed = int((datetime.now(timezone.utc) - _background['started']).total_seconds())
        remaining = max(0, _background['watch_seconds'] - elapsed)
        return {'status': 'watching', 'health': 'unknown', 'elapsed_seconds': elapsed, 'remaining_seconds': remaining,
                'speech': f'Still watching fresh traffic, about {remaining} seconds left. Nothing is resolved until it finishes.'}
    current = incident()
    if not current or current['status'] in ('resolved', 'closed'):
        return {'status': 'idle', 'health': 'unknown', 'speech': 'There is no incident waiting on recovery verification.'}
    watch = getattr(settings(), 'verify_watch_seconds', 30) if settings().mode == 'sandbox' else 0
    _background.update(task=asyncio.create_task(verify(session, watch)), started=datetime.now(timezone.utc),
                       watch_seconds=watch, session=session)
    await asyncio.sleep(0)
    if watch == 0:
        # Live verification is one fast metrics read; give it a moment to answer inline.
        try:
            await asyncio.wait_for(asyncio.shield(_background['task']), 0.4)
        except asyncio.TimeoutError:
            pass
        return await check_recovery(session)
    return {'status': 'watching', 'health': 'unknown', 'elapsed_seconds': 0, 'remaining_seconds': watch,
            'speech': f'Watching fresh traffic for {watch} seconds. Ask me to check recovery again after that.'}


def _duration(start, end):
    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return 'not recorded'
    return f'{int(seconds // 60)}m {int(seconds % 60)}s'


KEY_EVENTS = ('alert', 'ack', 'diagnosis', 'investigation', 'approval', 'cancel', 'mutation', 'verification', 'scenario')


def report(persist=True):
    init()
    current = incident()
    entries = audit_entries(current['id']) if current else []
    lines = ['# OnCall incident handoff', '', f"Mode: {settings().mode}",
             f"Incident: {current['title'] if current else 'No incident recorded'}",
             f"Status: {current['status'] if current else 'unknown'}"]
    if current:
        lines += [f"Source: {current.get('source') or 'manual'}", f"Started: {current['started_at']}",
                  f"Time to acknowledge: {_duration(current['started_at'], current.get('acknowledged_at'))}",
                  f"Time to resolve: {_duration(current['started_at'], current.get('resolved_at'))}"]
        with store.connect() as db:
            alerts = db.execute('SELECT * FROM alerts WHERE incident_id=? ORDER BY first_seen', (current['id'],)).fetchall()
        if alerts:
            lines += ['', '## Alerts']
            lines += [f"- {a['name']} ({a['severity']}, {a['status']}, seen {a['count']}x): {a['summary']}" for a in alerts]
    lines += ['', '## Chronology']
    key = [e for e in entries if e['kind'] in KEY_EVENTS]
    lines += [f"- {e['at']} — {e['title']}" for e in key] or ['- No key events recorded.']
    lines += ['', '## Evidence and actions']
    for entry in entries:
        lines.append(f"- {entry['at']} — {entry['title']}: {entry['detail']}")
    lines.extend(['', '## Follow-up', 'Confirm sustained recovery, investigate root cause, and review deployment safeguards.',
                  'Timing correlation between a deployment and errors is not proof of cause; confirm with code review.',
                  'Single-operator shared-key authorization; no claim of multi-tenant identity.'])
    markdown = '\n'.join(lines)
    if persist:
        with store.connect() as db:
            db.execute('INSERT INTO reports(at,markdown) VALUES(?,?)', (now(), markdown))
    return {'markdown': markdown}
