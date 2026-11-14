"""Tenant-bound teacher identity provisioning and self-service regressions."""
import re
import uuid

import pytest

from db import init_db
from services import attendance, core, identity


@pytest.fixture
def teacher_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    monkeypatch.delenv("ENFORCE_ENTITLEMENTS", raising=False)
    path = str(tmp_path / "teacher-portal.sqlite")
    conn = init_db(path)
    core.seed_default_roles(conn)
    suffix = uuid.uuid4().hex[:10]
    tenant_a = core.create_institution(conn, "Teacher Tenant A", "TTA-" + suffix)
    tenant_b = core.create_institution(conn, "Teacher Tenant B", "TTB-" + suffix)
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
    leave_type_a = attendance.create_leave_type(conn, tenant_a, "Annual Leave", 20)
    leave_type_b = attendance.create_leave_type(conn, tenant_b, "Medical Leave", 10)
    user_a = identity.create_school_user(
        conn, email=f"teacher-a-{suffix}@school.example",
        password="correct horse battery staple", institution_id=tenant_a,
        role_name="Teacher Portal", teacher_id=teacher_a, granted_by="platform-admin",
    )
    user_b = identity.create_school_user(
        conn, email=f"teacher-b-{suffix}@school.example",
        password="correct horse battery staple", institution_id=tenant_b,
        role_name="Teacher Portal", teacher_id=teacher_b, granted_by="platform-admin",
    )
    conn.close()

    import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", path)
    app_module.app.config.update(TESTING=True)
    return {
        "path": path, "app": app_module,
        "tenant_a": tenant_a, "tenant_b": tenant_b,
        "teacher_a": teacher_a, "teacher_b": teacher_b,
        "employee_code_a": "TA-" + suffix,
        "policy_a": policy_a, "policy_b": policy_b,
        "leave_type_a": leave_type_a, "leave_type_b": leave_type_b,
        "user_a": user_a, "user_b": user_b,
    }


def login(client, user):
    page = client.get("/")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
    assert match
    response = client.post(
        "/auth/school-login",
        data={
            "email": user["email"],
            "password": "correct horse battery staple",
            "csrf_token": match.group(1),
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    with client.session_transaction() as session:
        return session["csrf_token"]


def headers(csrf, key=None):
    result = {"X-CSRF-Token": csrf}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def test_teacher_portal_provisioning_requires_one_active_same_tenant_teacher(teacher_env):
    e = teacher_env
    conn = init_db(e["path"])
    with pytest.raises(ValueError, match="teacher_id_required"):
        identity.create_school_user(
            conn, email="missing-teacher@example.test",
            password="correct horse battery staple", institution_id=e["tenant_a"],
            role_name="Teacher Portal", granted_by="platform-admin",
        )
    with pytest.raises(ValueError, match="teacher_identity_unavailable"):
        identity.create_school_user(
            conn, email="foreign-teacher@example.test",
            password="correct horse battery staple", institution_id=e["tenant_a"],
            role_name="Teacher Portal", teacher_id=e["teacher_b"],
            granted_by="platform-admin",
        )
    with pytest.raises(ValueError, match="teacher_link_only_for_teacher_portal"):
        identity.create_school_user(
            conn, email="staff-with-link@example.test",
            password="correct horse battery staple", institution_id=e["tenant_a"],
            role_name="Institution Admin", teacher_id=e["teacher_a"],
            granted_by="platform-admin",
        )
    with pytest.raises(ValueError, match="teacher_identity_unavailable"):
        identity.create_school_user(
            conn, email="duplicate-link@example.test",
            password="correct horse battery staple", institution_id=e["tenant_a"],
            role_name="Teacher Portal", teacher_id=e["teacher_a"],
            granted_by="platform-admin",
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_links WHERE institution_id=?",
        (e["tenant_a"],),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM users WHERE email IN "
        "('missing-teacher@example.test','foreign-teacher@example.test',"
        "'staff-with-link@example.test','duplicate-link@example.test')"
    ).fetchone()[0] == 0
    conn.close()


def test_teacher_portal_profile_and_attendance_are_bound_to_linked_teacher(teacher_env):
    e = teacher_env
    client = e["app"].app.test_client()
    csrf = login(client, e["user_a"])

    profile = client.get(
        "/api/school/teacher/me",
        query_string={"institution_id": e["tenant_b"]},
    )
    assert profile.status_code == 200
    profile_teacher = profile.get_json()["teacher"]
    assert profile_teacher["id"] == e["teacher_a"]
    assert profile_teacher["employee_code"] == e["employee_code_a"]
    assert profile_teacher["department_id"] is None

    payload = {"policy_id": e["policy_a"], "event_type": "check_in", "time": "09:00"}
    first = client.post(
        "/api/school/teacher/attendance/events", json=payload,
        headers=headers(csrf, "teacher-attendance-self-0001"),
    )
    replay = client.post(
        "/api/school/teacher/attendance/events", json=payload,
        headers=headers(csrf, "teacher-attendance-self-0001"),
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.get_json()["id"] == first.get_json()["id"]

    own = client.get(
        "/api/school/teacher/attendance",
        query_string={"institution_id": e["tenant_b"]},
    )
    assert own.status_code == 200
    assert len(own.get_json()["attendance_events"]) == 1
    assert set(own.get_json()["attendance_events"][0]) == {
        "id", "event_type", "timestamp", "status", "created_at",
    }

    foreign_policy = client.post(
        "/api/school/teacher/attendance/events",
        json={**payload, "policy_id": e["policy_b"]},
        headers=headers(csrf, "teacher-attendance-self-0002"),
    )
    forged_teacher = client.post(
        "/api/school/teacher/attendance/events",
        json={**payload, "teacher_id": e["teacher_b"]},
        headers=headers(csrf, "teacher-attendance-self-0003"),
    )
    staff_write = client.post(
        "/api/school/attendance/events",
        json={
            "teacher_id": e["teacher_a"], "policy_id": e["policy_a"],
            "event_type": "check_in", "time": "09:15",
        },
        headers=headers(csrf, "teacher-staff-write-0001"),
    )
    staff_read = client.get("/api/school/attendance")
    assert foreign_policy.status_code == 404
    assert forged_teacher.status_code == 400
    assert staff_write.status_code == 403
    assert staff_read.status_code == 403


def test_teacher_portal_leave_requests_are_self_only_and_cannot_be_approved(teacher_env):
    e = teacher_env
    client = e["app"].app.test_client()
    csrf = login(client, e["user_a"])
    payload = {
        "leave_type_id": e["leave_type_a"],
        "start_date": "2026-12-15",
        "end_date": "2026-12-16",
        "reason": "Personal leave request",
    }
    first = client.post(
        "/api/school/teacher/leave-requests", json=payload,
        headers=headers(csrf, "teacher-leave-self-0001"),
    )
    replay = client.post(
        "/api/school/teacher/leave-requests", json=payload,
        headers=headers(csrf, "teacher-leave-self-0001"),
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.get_json()["id"] == first.get_json()["id"]

    own = client.get(
        "/api/school/teacher/leave-requests",
        query_string={"institution_id": e["tenant_b"]},
    )
    assert own.status_code == 200
    rows = own.get_json()["leave_requests"]
    assert len(rows) == 1
    assert rows[0]["id"] == first.get_json()["id"]
    assert set(rows[0]) == {
        "id", "leave_type_name", "start_date", "end_date", "status", "created_at",
    }

    foreign_type = client.post(
        "/api/school/teacher/leave-requests",
        json={**payload, "leave_type_id": e["leave_type_b"]},
        headers=headers(csrf, "teacher-leave-self-0002"),
    )
    forged_teacher = client.post(
        "/api/school/teacher/leave-requests",
        json={**payload, "teacher_id": e["teacher_b"]},
        headers=headers(csrf, "teacher-leave-self-0003"),
    )
    staff_read = client.get("/api/school/leave-requests")
    approval = client.post(
        f"/api/school/leave-requests/{first.get_json()['id']}/decision",
        json={"decision": "approved"},
        headers=headers(csrf),
    )
    assert foreign_type.status_code == 404
    assert forged_teacher.status_code == 400
    assert staff_read.status_code == 403
    assert approval.status_code == 403


@pytest.mark.parametrize("revocation", ["teacher_suspended", "extra_institution_role"])
def test_teacher_portal_denies_access_after_link_or_role_authority_changes(teacher_env, revocation):
    e = teacher_env
    client = e["app"].app.test_client()
    login(client, e["user_a"])
    conn = init_db(e["path"])
    if revocation == "teacher_suspended":
        conn.execute(
            "UPDATE teachers SET status='suspended' WHERE id=?",
            (e["teacher_a"],),
        )
    else:
        role = conn.execute(
            "SELECT id FROM roles WHERE name='HR/Attendance Officer'"
        ).fetchone()
        conn.execute(
            "INSERT INTO role_assignments(id,user_id,role_id,scope_type,scope_id) "
            "VALUES(?,?,?,?,?)",
            ("extra-role-" + uuid.uuid4().hex, e["user_a"]["id"], role["id"],
             "institution", e["tenant_a"]),
        )
    conn.commit()
    conn.close()
    response = client.get("/api/school/teacher/me")
    assert response.status_code == 403
    assert response.get_json()["error"] == "teacher_identity_required"


def test_teacher_portal_mutations_require_csrf_and_idempotency(teacher_env):
    e = teacher_env
    client = e["app"].app.test_client()
    csrf = login(client, e["user_a"])
    payload = {"policy_id": e["policy_a"], "event_type": "check_in", "time": "09:00"}
    missing_csrf = client.post(
        "/api/school/teacher/attendance/events", json=payload,
        headers={"Idempotency-Key": "teacher-attendance-csrf-0001"},
    )
    missing_key = client.post(
        "/api/school/teacher/attendance/events", json=payload,
        headers=headers(csrf),
    )
    assert missing_csrf.status_code == 400
    assert missing_key.status_code == 400
