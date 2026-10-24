"""Persistent, bounded login throttling helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60


def _utcnow():
    return datetime.now(timezone.utc)


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_locked(conn, key_hash: str, *, now=None) -> bool:
    moment = now or _utcnow()
    row = conn.execute(
        "SELECT window_started_at, locked_until FROM login_rate_limits WHERE key_hash = ?",
        (key_hash,),
    ).fetchone()
    if row is None:
        return False
    locked_until = row["locked_until"]
    # An active lock must outlive the rolling failure window when the fifth
    # attempt occurs near the end of that window.
    if locked_until and _parse(locked_until) > moment:
        return True
    window_start = _parse(row["window_started_at"])
    if moment >= window_start + timedelta(seconds=WINDOW_SECONDS):
        conn.execute("DELETE FROM login_rate_limits WHERE key_hash = ?", (key_hash,))
        conn.commit()
    return False


def record_failure(conn, key_hash: str, *, now=None) -> bool:
    """Record a failed attempt and return whether the key is now locked."""
    moment = now or _utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT failures, window_started_at FROM login_rate_limits WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        if row is None or moment >= _parse(row["window_started_at"]) + timedelta(seconds=WINDOW_SECONDS):
            failures = 1
            window_start = moment
        else:
            failures = int(row["failures"]) + 1
            window_start = _parse(row["window_started_at"])
        locked_until = (
            (moment + timedelta(seconds=LOCK_SECONDS)).isoformat()
            if failures >= MAX_FAILURES
            else None
        )
        conn.execute(
            """INSERT INTO login_rate_limits
               (key_hash, failures, window_started_at, locked_until, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key_hash) DO UPDATE SET
                 failures = excluded.failures,
                 window_started_at = excluded.window_started_at,
                 locked_until = excluded.locked_until,
                 updated_at = excluded.updated_at""",
            (key_hash, failures, window_start.isoformat(), locked_until, moment.isoformat()),
        )
        conn.commit()
        return failures >= MAX_FAILURES
    except Exception:
        conn.rollback()
        raise


def clear_failures(conn, key_hash: str) -> None:
    conn.execute("DELETE FROM login_rate_limits WHERE key_hash = ?", (key_hash,))
    conn.commit()
