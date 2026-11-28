"""Dry-run-first cleanup for expired tenant idempotency metadata."""
from __future__ import annotations

from datetime import datetime, timezone


_IDEMPOTENCY_TABLES = (
    "tenant_leave_request_keys",
    "tenant_attendance_event_keys",
)


def _as_of_iso(as_of=None) -> str:
    if as_of is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(as_of, datetime):
        moment = as_of
    elif isinstance(as_of, str):
        try:
            moment = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("as_of_must_be_timezone_aware_iso8601") from None
    else:
        raise ValueError("as_of_must_be_timezone_aware_iso8601")

    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("as_of_must_be_timezone_aware_iso8601")
    return moment.astimezone(timezone.utc).isoformat()


def purge_expired_idempotency(conn, *, apply: bool = False, as_of=None) -> dict:
    """Count expired retry keys; delete them only when apply=True is explicit."""
    if conn.in_transaction:
        raise ValueError("retention_requires_clean_connection")
    if not isinstance(apply, bool):
        raise ValueError("apply_must_be_boolean")
    cutoff = _as_of_iso(as_of)

    conn.execute("BEGIN IMMEDIATE")
    try:
        expired = {}
        deleted = {}
        for table in _IDEMPOTENCY_TABLES:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE expires_at <= ?",
                (cutoff,),
            ).fetchone()[0]
            expired[table] = count
            if apply:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE expires_at <= ?",
                    (cutoff,),
                )
                deleted[table] = cursor.rowcount
            else:
                deleted[table] = 0

        conn.commit()
        return {
            "as_of": cutoff,
            "dry_run": not apply,
            "expired_rows": expired,
            "deleted_rows": deleted,
        }
    except Exception:
        conn.rollback()
        raise
