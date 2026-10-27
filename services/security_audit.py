"""Append-only audit events for authentication and administrative requests."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone


def append_security_event(
    conn,
    *,
    event_type: str,
    source_hash: str,
    http_method: str,
    route: str,
    response_status: int,
    actor_id: str | None = None,
    actor_role: str | None = None,
    details: dict | None = None,
    created_at: str | None = None,
) -> str:
    """Persist one security event without storing raw client addresses or bodies."""
    event_type = str(event_type or "").strip()
    source_hash = str(source_hash or "").strip()
    method = str(http_method or "").strip().upper()
    route = str(route or "").strip().split("?", 1)[0]
    if not event_type or len(event_type) > 80:
        raise ValueError("event_type must be 1-80 characters")
    if not source_hash or len(source_hash) > 128:
        raise ValueError("source_hash is required")
    if not method or len(method) > 16:
        raise ValueError("http_method must be 1-16 characters")
    if not route.startswith("/") or len(route) > 500:
        raise ValueError("route must be an absolute path of at most 500 characters")
    if isinstance(response_status, bool) or not isinstance(response_status, int) or not 100 <= response_status <= 599:
        raise ValueError("response_status must be an HTTP status code")
    if details is not None and not isinstance(details, dict):
        raise ValueError("details must be an object")

    event_id = str(uuid.uuid4())
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO security_audit_events
           (id, event_type, actor_id, actor_role, source_hash, http_method,
            route, response_status, detail_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            event_type,
            str(actor_id)[:120] if actor_id else None,
            str(actor_role)[:80] if actor_role else None,
            source_hash,
            method,
            route,
            response_status,
            json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
            timestamp,
        ),
    )
    conn.commit()
    return event_id
