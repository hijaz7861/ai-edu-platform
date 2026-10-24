"""Persistent fixed-window API rate limiter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


WINDOW_SECONDS = 60


def allow_request(conn, key_hash: str, *, limit: int = 120, now=None) -> bool:
    """Atomically count requests for a keyed identity/endpoint within one minute."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    moment = now or datetime.now(timezone.utc)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT request_count, window_started_at FROM api_rate_limits WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        if row is None or moment >= datetime.fromisoformat(
            row["window_started_at"].replace("Z", "+00:00")
        ) + timedelta(seconds=WINDOW_SECONDS):
            conn.execute(
                """INSERT INTO api_rate_limits (key_hash, request_count, window_started_at, updated_at)
                   VALUES (?, 1, ?, ?)
                   ON CONFLICT(key_hash) DO UPDATE SET
                     request_count = 1,
                     window_started_at = excluded.window_started_at,
                     updated_at = excluded.updated_at""",
                (key_hash, moment.isoformat(), moment.isoformat()),
            )
            conn.commit()
            return True

        if int(row["request_count"]) >= limit:
            conn.commit()
            return False

        conn.execute(
            "UPDATE api_rate_limits SET request_count = request_count + 1, updated_at = ? WHERE key_hash = ?",
            (moment.isoformat(), key_hash),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
