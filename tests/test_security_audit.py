import sqlite3

import pytest

from db import init_db
from services.security_audit import append_security_event


@pytest.fixture
def conn():
    connection = init_db(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def test_security_audit_event_is_persisted_without_query_string(conn):
    event_id = append_security_event(
        conn,
        event_type="login_failed",
        source_hash="hmac-digest-not-raw-ip",
        http_method="POST",
        route="/auth/login?password=must-not-be-recorded",
        response_status=401,
        details={"reason": "invalid_credentials"},
        created_at="2026-10-27T00:00:00+00:00",
    )

    row = conn.execute(
        "SELECT * FROM security_audit_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "login_failed"
    assert row["source_hash"] == "hmac-digest-not-raw-ip"
    assert row["route"] == "/auth/login"
    assert "password" not in row["route"]
    assert row["response_status"] == 401
    assert row["detail_json"] == '{"reason":"invalid_credentials"}'


def test_security_audit_events_cannot_be_updated_or_deleted(conn):
    event_id = append_security_event(
        conn,
        event_type="admin_request",
        source_hash="source-digest",
        http_method="POST",
        route="/api/admin/plans",
        response_status=201,
        actor_id="platform-admin",
        actor_role="platform_admin",
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE security_audit_events SET response_status = 200 WHERE id = ?",
            (event_id,),
        )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM security_audit_events WHERE id = ?", (event_id,))


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": ""},
        {"source_hash": ""},
        {"http_method": ""},
        {"route": "not-an-absolute-path"},
        {"response_status": 99},
        {"details": []},
    ],
)
def test_security_audit_rejects_invalid_event_fields(conn, overrides):
    args = {
        "event_type": "admin_request",
        "source_hash": "source-digest",
        "http_method": "POST",
        "route": "/api/admin/plans",
        "response_status": 201,
        "details": {},
    }
    args.update(overrides)
    with pytest.raises(ValueError):
        append_security_event(conn, **args)
