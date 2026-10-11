"""Shared persistence for OnCall.

One SQLite database (WAL mode) records every real operational event: deploys,
individual request outcomes, and error details. The target service writes
events as they happen; the MCP server reads the same truth. Nothing here is
seeded or simulated — every row exists because something actually occurred.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings

DB_PATH = Path(settings.db_path)
if not DB_PATH.is_absolute():
    DB_PATH = Path(__file__).resolve().parent.parent / DB_PATH


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS deploys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                faulty INTEGER NOT NULL DEFAULT 0,
                rolled_back INTEGER NOT NULL DEFAULT 0,
                deployed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                route TEXT NOT NULL,
                status INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                route TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            """
        )


# ── deploys ──────────────────────────────────────────────────────────────

def record_deploy(service: str, commit_sha: str, *, faulty: bool, note: str = "") -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO deploys (service, commit_sha, note, faulty, deployed_at) VALUES (?, ?, ?, ?, ?)",
            (service, commit_sha, note, int(faulty), _now()),
        )
    return {"deploy_id": cur.lastrowid, "service": service, "commit": commit_sha, "faulty": faulty}


def rollback_deploy(service: str, commit_sha: str) -> bool:
    """Mark a deploy rolled back. Returns True when exactly one row matched."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE deploys SET rolled_back = 1 WHERE id = (SELECT id FROM deploys WHERE service = ? ORDER BY id DESC LIMIT 1) AND commit_sha = ? AND rolled_back = 0",
            (service, commit_sha),
        )
        return cur.rowcount == 1


def latest_deploy(service: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM deploys WHERE service = ? ORDER BY id DESC LIMIT 1", (service,)
        ).fetchone()
    return dict(row) if row else None


def deploys_since(hours: float, service: str | None = None) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    query = "SELECT * FROM deploys WHERE deployed_at >= ?"
    params: list[object] = [cutoff]
    if service:
        query += " AND service = ?"
        params.append(service)
    query += " ORDER BY deployed_at"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# ── requests / errors ────────────────────────────────────────────────────

def log_request(route: str, status: int) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO requests (ts, route, status) VALUES (?, ?, ?)", (_now(), route, status))


def log_error(route: str, detail: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO errors (ts, route, detail) VALUES (?, ?, ?)", (_now(), route, detail))


def fault_active(service: str) -> bool:
    """A fault is live while the service's latest deploy is faulty and not rolled back."""
    latest = latest_deploy(service)
    return bool(latest and latest["faulty"] and not latest["rolled_back"])


def error_series(minutes: float, bucket_seconds: int = 120) -> list[dict]:
    """Real error-rate series: per-bucket 5xx share over all recorded requests."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, status FROM requests WHERE ts >= ? AND route='/checkout' ORDER BY ts", (cutoff,)
        ).fetchall()
    if not rows:
        return []
    buckets: dict[str, dict] = {}
    for row in rows:
        dt = _parse(row["ts"])
        bucket_ts = datetime.fromtimestamp(int(dt.timestamp()) // bucket_seconds * bucket_seconds, timezone.utc)
        key = bucket_ts.isoformat(timespec="seconds")
        b = buckets.setdefault(key, {"ts": key, "total": 0, "errors": 0})
        b["total"] += 1
        if row["status"] >= 500:
            b["errors"] += 1
    return [
        {
            "ts": b["ts"],
            "total_requests": b["total"],
            "error_count": b["errors"],
            "error_rate": round(b["errors"] / b["total"], 4) if b["total"] else 0.0,
        }
        for b in sorted(buckets.values(), key=lambda x: x["ts"])
    ]


def recent_errors(minutes: float, query: str | None = None) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    sql = "SELECT ts, route, detail FROM errors WHERE ts >= ?"
    params: list[object] = [cutoff]
    if query:
        sql += " AND detail LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY ts DESC LIMIT 50"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]
