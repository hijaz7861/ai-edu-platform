"""Tenant isolation, authorization, idempotency and sequencing for attendance writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
import uuid

import pytest

from db import get_connection, init_db
from services import attendance, core, identity
from services.licensing import create_plan, grant_subscription
from services.tenant_attendance import (
    AttendanceSequenceConflict,
    record_school_attendance_event,
)


@pytest.fixture
def attendance_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    monkeypatch.delenv("ENFORCE_ENTITLEMENTS", raising=False)
    path = str(tmp_path / "school-attendance.sqlite")
    conn = init_db(path)
    core.seed_default_roles(conn)
    suffix = uuid.uuid4().hex[:10]
    tenant_a = core.create_institution(conn, "Attendance Tenant A", "AA-" + suffix)
    tenant_b = core.create_institution(conn, "Attendance Tenant B", "AB-" + suffix)
    teacher_a = core.create_teacher(conn, None, tenant_a, "TA-" + suffix)
    teacher_b = core.create_teacher(conn, None, tenant_b, "TB-" + suffix)
    policy_a = attendance.create_policy(
        conn, tenant_a, "Tenant A policy", "09:00", "17:00",
        grace_period_minutes=10, late_threshold_minutes=15,
        early_departure_threshold_minutes=15,
    )
    policy_b = attendance.create_policy(
        conn, tenant_b, "Tenant B policy", "08:00", "16:00",
        grace_period_minutes=5, late_threshold_minutes=10,
        early_departure_threshold_minutes=10,
    )

    roles = [
        "Institution Admin",
        "Principal",
        "Academic Coordinator",
        "Examiner",
        "HR/Attendance Officer",
    ]
    users = {}
    role_teachers = {}
    for index, role_name in enumerate(roles):
        users[role_name] = identity.create_school_user(
            conn,
            email=f"attendance-{index}-{suffix}@school.example",
            password="correct horse battery staple",
            institution_id=tenant_a,
            role_name=role_name,
            granted_by="platform-admin",
        )
        role_teachers[role_name] = core.create_teacher(
            conn, None, tenant_a, f"ROLE-{index}-{suffix}"
        )
    user_b = identity.create_school_user(
        conn,
        email=f"attendance-b-{suffix}@school.example",
        password="correct horse battery staple",
        institution_id=tenant_b,
        role_name="Institution Admin",
        granted_by="platform-admin",
    )
    conn.close()

    import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", path)
    app_module.app.config.update(TESTING=True)
    return {
        "path": path,
        "app": app_module,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "teacher_a": teacher_a,
        "teacher_b": teacher_b,
        "policy_a": policy_a,
        "policy_b": policy_b,
        "users": users,
        "user_b": user_b,
        "role_teachers": role_teachers,
    }


def login(client, user):
    page = client.get("/")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
    assert match
    response = client.post("/auth/school-login", data={
        "email": user["email"],
        "password": "correct horse battery staple",
        "csrf_token": match.group(1),
    })
    assert response.status_code == 302
    with client.session_transaction() as session:
        return session["csrf_token"]


def post_event(client, csrf, key, *, teacher_id, policy_id, event_type="check_in", time="09:00"):
    return client.post(
        "/api/school/attendance/events",
        json={
            "teacher_id": teacher_id,
            "policy_id": policy_id,
            "event_type": event_type,
            "time": time,
        },
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": key},
    )


def test_attendance_write_requires_csrf_and_idempotency_key(attendance_env):
    e = attendance_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    payload = {
        "teacher_id": e["teacher_a"],
        "policy_id": e["policy_a"],
        "event_type": "check_in",
        "time": "09:00",
    }
    response = client.post(
        "/api/school/attendance/events",
        json=payload,
        headers={"Idempotency-Key": "attendance-csrf-0001"},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "csrf_invalid"}

    response = client.post(
        "/api/school/attendance/events",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "valid_idempotency_key_required"}


def test_attendance_write_is_tenant_bound_and_audits_the_school_actor(attendance_env):
    e = attendance_env
    client = e["app"].app.test_client()
    actor = e["users"]["Institution Admin"]
    csrf = login(client, actor)
    response = post_event(
        client, csrf, "attendance-write-0001",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"], time="09:05",
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["created"] is True
    assert body["event_type"] == "check_in"
    assert body["status"] == "on_time"

    conn = init_db(e["path"])
    event = conn.execute(
        "SELECT * FROM attendance_events WHERE id = ?", (body["id"],)
    ).fetchone()
    assert event["institution_id"] == e["tenant_a"]
    assert event["teacher_id"] == e["teacher_a"]
    assert event["method"] == "school_api"
    audit = conn.execute(
        "SELECT * FROM audit_log WHERE entity_type='attendance_events' AND entity_id=?",
        (body["id"],),
    ).fetchone()
    assert audit["actor_id"] == actor["id"]
    assert audit["institution_id"] == e["tenant_a"]
    key = conn.execute(
        "SELECT * FROM tenant_attendance_event_keys WHERE attendance_event_id=?",
        (body["id"],),
    ).fetchone()
    assert key["actor_id"] == actor["id"]
    assert len(key["idempotency_key_hash"]) == 64
    assert "attendance-write-0001" not in key["idempotency_key_hash"]
    conn.close()


def test_attendance_write_rejects_foreign_teacher_and_policy_with_uniform_404(attendance_env):
    e = attendance_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    foreign_teacher = post_event(
        client, csrf, "attendance-foreign-0001",
        teacher_id=e["teacher_b"], policy_id=e["policy_a"],
    )
    foreign_policy = post_event(
        client, csrf, "attendance-foreign-0002",
        teacher_id=e["teacher_a"], policy_id=e["policy_b"],
    )
    unknown_teacher = post_event(
        client, csrf, "attendance-foreign-0003",
        teacher_id="missing-teacher", policy_id=e["policy_a"],
    )
    assert foreign_teacher.status_code == foreign_policy.status_code == unknown_teacher.status_code == 404
    assert foreign_teacher.get_json() == foreign_policy.get_json() == unknown_teacher.get_json() == {
        "error": "attendance_resource_not_found"
    }


@pytest.mark.parametrize(
    ("role", "expected_status"),
    [
        ("Institution Admin", 201),
        ("Principal", 403),
        ("Academic Coordinator", 403),
        ("Examiner", 403),
        ("HR/Attendance Officer", 201),
    ],
)
def test_attendance_write_uses_least_privilege_role_matrix(attendance_env, role, expected_status):
    e = attendance_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"][role])
    response = post_event(
        client, csrf, "attendance-role-" + role.replace("/", "-").replace(" ", "-"),
        teacher_id=e["role_teachers"][role], policy_id=e["policy_a"],
    )
    assert response.status_code == expected_status


def test_attendance_write_rejects_client_tenant_actor_and_extra_fields(attendance_env):
    e = attendance_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    response = client.post(
        "/api/school/attendance/events",
        json={
            "teacher_id": e["teacher_a"],
            "policy_id": e["policy_a"],
            "event_type": "check_in",
            "time": "09:00",
            "institution_id": e["tenant_b"],
            "actor_id": e["user_b"]["id"],
        },
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "attendance-extra-0001"},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "unsupported_payload"}


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("event_type", ["check_in"], "unsupported_attendance_event_type"),
        ("event_type", {"type": "check_in"}, "unsupported_attendance_event_type"),
        ("time", ["09:00"], "invalid_attendance_time"),
        ("time", "9:00", "invalid_attendance_time"),
    ],
)
def test_attendance_write_rejects_malformed_event_values(
    attendance_env, field, value, expected_error
):
    e = attendance_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    payload = {
        "teacher_id": e["teacher_a"],
        "policy_id": e["policy_a"],
        "event_type": "check_in",
        "time": "09:00",
    }
    payload[field] = value
    response = client.post(
        "/api/school/attendance/events",
        json=payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "attendance-invalid-0001"},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": expected_error}


def test_attendance_write_idempotency_replay_and_actor_payload_conflicts(attendance_env):
    e = attendance_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    key = "attendance-idempotent-0001"
    first = post_event(
        client, csrf, key, teacher_id=e["teacher_a"],
        policy_id=e["policy_a"], time="09:00",
    )
    replay = post_event(
        client, csrf, key, teacher_id=e["teacher_a"],
        policy_id=e["policy_a"], time="09:00",
    )
    changed = post_event(
        client, csrf, key, teacher_id=e["teacher_a"],
        policy_id=e["policy_a"], time="09:01",
    )
    other_client = e["app"].app.test_client()
    other_csrf = login(other_client, e["users"]["HR/Attendance Officer"])
    other_actor = post_event(
        other_client, other_csrf, key, teacher_id=e["teacher_a"],
        policy_id=e["policy_a"], time="09:00",
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.get_json()["id"] == first.get_json()["id"]
    assert replay.get_json()["created"] is False
    assert changed.status_code == 409
    assert changed.get_json() == {"error": "idempotency_key_conflict"}
    assert other_actor.status_code == 409
    assert other_actor.get_json() == {"error": "idempotency_key_conflict"}

    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT COUNT(*) FROM attendance_events WHERE teacher_id=?", (e["teacher_a"],)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity_type='attendance_events' AND entity_id=?",
        (first.get_json()["id"],),
    ).fetchone()[0] == 1
    conn.close()


def test_attendance_write_enforces_check_in_checkout_and_time_order(attendance_env):
    e = attendance_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])

    checkout_first = post_event(
        client, csrf, "attendance-sequence-0001",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
        event_type="check_out", time="17:00",
    )
    assert checkout_first.status_code == 409
    assert checkout_first.get_json() == {"error": "attendance_check_in_required"}

    checkin = post_event(
        client, csrf, "attendance-sequence-0002",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
        event_type="check_in", time="09:00",
    )
    duplicate_checkin = post_event(
        client, csrf, "attendance-sequence-0003",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
        event_type="check_in", time="09:10",
    )
    early_checkout = post_event(
        client, csrf, "attendance-sequence-0004",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
        event_type="check_out", time="08:59",
    )
    checkout = post_event(
        client, csrf, "attendance-sequence-0005",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
        event_type="check_out", time="17:00",
    )
    duplicate_checkout = post_event(
        client, csrf, "attendance-sequence-0006",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
        event_type="check_out", time="17:05",
    )
    next_checkin = post_event(
        client, csrf, "attendance-sequence-0007",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
        event_type="check_in", time="18:00",
    )

    assert checkin.status_code == 201
    assert duplicate_checkin.status_code == 409
    assert duplicate_checkin.get_json() == {"error": "attendance_already_checked_in"}
    assert early_checkout.status_code == 409
    assert early_checkout.get_json() == {"error": "attendance_time_order_invalid"}
    assert checkout.status_code == 201
    assert duplicate_checkout.status_code == 409
    assert duplicate_checkout.get_json() == {"error": "attendance_check_in_required"}
    assert next_checkin.status_code == 201

    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT COUNT(*) FROM attendance_events WHERE teacher_id=?", (e["teacher_a"],)
    ).fetchone()[0] == 3
    conn.close()


def test_attendance_write_fails_closed_on_inconsistent_legacy_history(attendance_env):
    e = attendance_env
    conn = init_db(e["path"])
    conn.execute(
        """INSERT INTO attendance_events
           (id, teacher_id, institution_id, event_type, timestamp, method,
            policy_id, status, created_at)
           VALUES (?, ?, ?, 'check_in', '09:00', 'manual', ?, 'on_time', ?)""",
        (
            uuid.uuid4().hex, e["teacher_a"], e["tenant_b"], e["policy_a"],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    response = post_event(
        client, csrf, "attendance-history-0001",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
    )
    assert response.status_code == 409
    assert response.get_json() == {"error": "attendance_history_inconsistent"}


def test_attendance_write_requires_subscription_entitlement_when_enforced(attendance_env, monkeypatch):
    e = attendance_env
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "1")
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    denied = post_event(
        client, csrf, "attendance-entitlement-0001",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
    )
    assert denied.status_code == 403
    assert denied.get_json() == {"error": "subscription_required"}

    conn = init_db(e["path"])
    plan_id = create_plan(
        conn,
        code="attendance-" + uuid.uuid4().hex[:8],
        name="Attendance Test",
        entitlements={"attendance": True},
    )
    grant_subscription(
        conn,
        institution_id=e["tenant_a"],
        plan_id=plan_id,
        access_type="free_grant",
        granted_by="ci-test-admin",
        reason="automated entitlement regression",
    )
    conn.close()

    allowed = post_event(
        client, csrf, "attendance-entitlement-0002",
        teacher_id=e["teacher_a"], policy_id=e["policy_a"],
    )
    assert allowed.status_code == 201


def test_concurrent_attendance_checkins_serialize_to_one_event(attendance_env):
    e = attendance_env
    actor_id = e["users"]["Institution Admin"]["id"]

    def submit(key):
        conn = get_connection(e["path"])
        try:
            return record_school_attendance_event(
                conn,
                institution_id=e["tenant_a"],
                actor_id=actor_id,
                teacher_id=e["teacher_a"],
                policy_id=e["policy_a"],
                event_type="check_in",
                timestamp_hhmm="09:00",
                idempotency_key=key,
            )
        except AttendanceSequenceConflict as exc:
            return str(exc)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [
            "attendance-concurrent-0001",
            "attendance-concurrent-0002",
        ]))

    assert sum(isinstance(result, dict) and result["created"] for result in results) == 1
    assert results.count("attendance_already_checked_in") == 1
    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT COUNT(*) FROM attendance_events WHERE teacher_id=?", (e["teacher_a"],)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM tenant_attendance_event_keys WHERE institution_id=?",
        (e["tenant_a"],),
    ).fetchone()[0] == 1
    conn.close()
