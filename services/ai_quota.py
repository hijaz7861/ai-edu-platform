"""Atomic per-institution AI generation quotas with bounded rolling state."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


MAX_DAILY_LIMIT = 100_000
MAX_PER_MINUTE_LIMIT = 10_000


def _validate_limit(value, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")
    return value


def consume_ai_generation_quota(
    conn,
    institution_id: str,
    *,
    daily_limit: int,
    per_minute_limit: int,
    now: datetime | None = None,
) -> dict:
    """Atomically consume one tenant request from both UTC daily and minute budgets.

    Provider failures still consume the slot: the budget protects spend, not only
    successful responses. BEGIN IMMEDIATE serializes concurrent workers so they
    cannot race past a tenant's configured limit.
    """
    if not isinstance(institution_id, str) or not institution_id.strip():
        raise ValueError("institution_id_required")
    daily_limit = _validate_limit(daily_limit, "daily_limit", MAX_DAILY_LIMIT)
    per_minute_limit = _validate_limit(
        per_minute_limit, "per_minute_limit", MAX_PER_MINUTE_LIMIT
    )

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    moment = moment.astimezone(timezone.utc)
    daily_window = moment.date().isoformat()
    minute_window = moment.strftime("%Y-%m-%dT%H:%M")
    next_day = datetime.combine(
        moment.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
    next_minute = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)

    conn.execute("BEGIN IMMEDIATE")
    try:
        institution = conn.execute(
            "SELECT status FROM institutions WHERE id = ?",
            (institution_id.strip(),),
        ).fetchone()
        if not institution or institution["status"] != "active":
            conn.rollback()
            return {
                "allowed": False,
                "reason": "institution_inactive",
                "retry_after": 0,
                "daily_limit": daily_limit,
                "per_minute_limit": per_minute_limit,
            }

        daily_row = conn.execute(
            """SELECT request_count FROM ai_generation_usage
               WHERE institution_id = ? AND window_kind = 'day' AND window_start = ?""",
            (institution_id.strip(), daily_window),
        ).fetchone()
        minute_row = conn.execute(
            """SELECT request_count FROM ai_generation_usage
               WHERE institution_id = ? AND window_kind = 'minute' AND window_start = ?""",
            (institution_id.strip(), minute_window),
        ).fetchone()
        daily_used = int(daily_row["request_count"]) if daily_row else 0
        minute_used = int(minute_row["request_count"]) if minute_row else 0

        if daily_used >= daily_limit:
            conn.rollback()
            return {
                "allowed": False,
                "reason": "daily_limit",
                "retry_after": max(1, math.ceil((next_day - moment).total_seconds())),
                "daily_used": daily_used,
                "minute_used": minute_used,
                "daily_limit": daily_limit,
                "per_minute_limit": per_minute_limit,
            }
        if minute_used >= per_minute_limit:
            conn.rollback()
            return {
                "allowed": False,
                "reason": "minute_limit",
                "retry_after": max(1, math.ceil((next_minute - moment).total_seconds())),
                "daily_used": daily_used,
                "minute_used": minute_used,
                "daily_limit": daily_limit,
                "per_minute_limit": per_minute_limit,
            }

        updated_at = moment.isoformat()
        conn.execute(
            """INSERT INTO ai_generation_usage
               (institution_id, window_kind, window_start, request_count, updated_at)
               VALUES (?, 'day', ?, 1, ?)
               ON CONFLICT(institution_id, window_kind, window_start)
               DO UPDATE SET request_count = request_count + 1, updated_at = excluded.updated_at""",
            (institution_id.strip(), daily_window, updated_at),
        )
        conn.execute(
            """INSERT INTO ai_generation_usage
               (institution_id, window_kind, window_start, request_count, updated_at)
               VALUES (?, 'minute', ?, 1, ?)
               ON CONFLICT(institution_id, window_kind, window_start)
               DO UPDATE SET request_count = request_count + 1, updated_at = excluded.updated_at""",
            (institution_id.strip(), minute_window, updated_at),
        )

        # Keep at most 30 days of daily counters and 48 hours of minute counters.
        daily_cutoff = (moment - timedelta(days=30)).date().isoformat()
        minute_cutoff = (moment - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M")
        conn.execute(
            """DELETE FROM ai_generation_usage
               WHERE (window_kind = 'day' AND window_start < ?)
                  OR (window_kind = 'minute' AND window_start < ?)""",
            (daily_cutoff, minute_cutoff),
        )
        conn.commit()
        daily_used += 1
        minute_used += 1
        return {
            "allowed": True,
            "daily_used": daily_used,
            "minute_used": minute_used,
            "daily_limit": daily_limit,
            "per_minute_limit": per_minute_limit,
            "daily_remaining": max(0, daily_limit - daily_used),
            "minute_remaining": max(0, per_minute_limit - minute_used),
            "retry_after": 0,
        }
    except Exception:
        conn.rollback()
        raise
