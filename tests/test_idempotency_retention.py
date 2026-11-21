"""Retention regressions for expired leave and attendance idempotency keys."""
from datetime import datetime, timezone
import uuid

import pytest

from db import init_db
from services import attendance, core
from services.idempotency_retention import purge_expired_idempotency


@pytest.fixture
def retention_env(tmp_path):
    path = str(tmp_path / "idempotency-retention.sqlite")
    conn = init_db(path)
    tenant = core.create_institution(conn, "Retention Tenant", "RT-" + uuid.uuid4().hex[:8])
    teacher = core.create_teacher(conn, None, tenant, "RET-" + uuid.uuid4().hex[:8])
    leave_type = attendance.create_leave_type(conn, tenant, "Annual", 20)
    leave_request = attendance.request_leave(
        conn, teacher, leave_type, "2026-12-01", "2026-12-02", "retention test"
    )
    policy = attendance.create_policy(conn, tenant, "Retention policy", "09:00", "17:00")
    event_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO attendance_events
           (id, teacher_id, institution_id, event_type, timestamp, method,
            policy_id, status, created_at)
           VALUES (?, ?, ?, 'check_in', '09:00', 'manual', ?, 'on_time', ?)""",
        (event_id, teacher, tenant, policy, datetime.now(timezone.utc).isoformat()),
    )

    conn.execute(
        """INSERT INTO tenant_leave_request_keys
           (id, institution_id, idempotency_key_hash, request_hash,
            leave_request_id, actor_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid.uuid4().hex, tenant, "a" * 64, "b" * 64, leave_request,
            "actor-a", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO tenant_leave_request_keys
           (id, institution_id, idempotency_key_hash, request_hash,
            leave_request_id, actor_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid.uuid4().hex, tenant, "c" * 64, "d" * 64, leave_request,
            "actor-a", "2026-01-01T00:00:00+00:00", "2999-01-02T00:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO tenant_attendance_event_keys
           (id, institution_id, idempotency_key_hash, request_hash,
            attendance_event_id, actor_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid.uuid4().hex, tenant, "e" * 64, "f" * 64, event_id,
            "actor-a", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO tenant_attendance_event_keys
           (id, institution_id, idempotency_key_hash, request_hash,
            attendance_event_id, actor_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid.uuid4().hex, tenant, "1" * 64, "2" * 64, event_id,
            "actor-a", "2026-01-01T00:00:00+00:00", "2999-01-02T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    return {"path": path, "tenant": tenant, "leave_request": leave_request, "event_id": event_id}


def test_retention_dry_run_reports_without_deleting_business_or_retry_rows(retention_env):
    e = retention_env
    conn = init_db(e["path"])
    result = purge_expired_idempotency(
        conn, as_of="2026-11-11T00:00:00+00:00"
    )
    assert result["dry_run"] is True
    assert result["expired_rows"] == {
        "tenant_leave_request_keys": 1,
        "tenant_attendance_event_keys": 1,
    }
    assert result["deleted_rows"] == {
        "tenant_leave_request_keys": 0,
        "tenant_attendance_event_keys": 0,
    }
    assert conn.execute("SELECT COUNT(*) FROM tenant_leave_request_keys").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM tenant_attendance_event_keys").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM leave_requests WHERE id=?", (e["leave_request"],)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM attendance_events WHERE id=?", (e["event_id"],)).fetchone()[0] == 1
    conn.close()


def test_retention_apply_deletes_only_expired_retry_metadata(retention_env):
    e = retention_env
    conn = init_db(e["path"])
    result = purge_expired_idempotency(
        conn, apply=True, as_of="2026-11-11T00:00:00+00:00"
    )
    assert result["dry_run"] is False
    assert result["deleted_rows"] == {
        "tenant_leave_request_keys": 1,
        "tenant_attendance_event_keys": 1,
    }
    assert conn.execute("SELECT COUNT(*) FROM tenant_leave_request_keys").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tenant_attendance_event_keys").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM leave_requests WHERE id=?", (e["leave_request"],)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM attendance_events WHERE id=?", (e["event_id"],)).fetchone()[0] == 1
    conn.close()


def test_retention_requires_clean_transaction_and_timezone_aware_cutoff(retention_env):
    conn = init_db(retention_env["path"])
    conn.execute("BEGIN")
    with pytest.raises(ValueError, match="retention_requires_clean_connection"):
        purge_expired_idempotency(conn)
    conn.rollback()
    with pytest.raises(ValueError, match="as_of_must_be_timezone_aware_iso8601"):
        purge_expired_idempotency(conn, as_of="2026-11-11T00:00:00")
    conn.close()
