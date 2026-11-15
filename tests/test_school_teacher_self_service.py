"""Tenant-bound teacher identity provisioning and self-service regressions."""
import re
import sqlite3
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
        conn, tenant_a, "Tenant A policy", "00:00", "23:59",
        grace_period_minutes=10, late_threshold_minutes=15,
        early_departure_threshold_minutes=15,
    )
    policy_b = attendance.create_policy(
        conn, tenant_b, "Tenant B policy", "00:00", "23:59",
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
    admin_a = identity.create_school_user(
        conn, email=f"admin-a-{suffix}@school.example",
        password="correct horse battery staple", institution_id=tenant_a,
        role_name="Institution Admin", granted_by="platform-admin",
    )
    principal_a = identity.create_school_user(
        conn, email=f"principal-a-{suffix}@school.example",
        password="correct horse battery staple", institution_id=tenant_a,
        role_name="Principal", granted_by="platform-admin",
    )
    hr_a = identity.create_school_user(
        conn, email=f"hr-a-{suffix}@school.example",
        password="correct horse battery staple", institution_id=tenant_a,
        role_name="HR/Attendance Officer", granted_by="platform-admin",
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
        "admin_a": admin_a, "principal_a": principal_a, "hr_a": hr_a,
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

    payload = {"policy_id": e["policy_a"], "event_type": "check_in"}
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
    forged_time = client.post(
        "/api/school/teacher/attendance/events",
        json={**payload, "time": "09:00"},
        headers=headers(csrf, "teacher-attendance-self-0004"),
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
    assert forged_time.status_code == 400
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
def test_teacher_portal_session_is_invalidated_after_link_or_role_authority_changes(teacher_env, revocation):
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
    assert response.status_code == 401
    assert response.get_json()["error"] == "school_identity_inactive"
    with client.session_transaction() as session:
        assert "user_id" not in session


@pytest.mark.parametrize(
    "path",
    ["/api/school/attendance", "/api/school/leave-requests", "/api/school/classes"],
)
def test_teacher_portal_cannot_inherit_staff_permissions_after_role_escalation(teacher_env, path):
    e = teacher_env
    client = e["app"].app.test_client()
    login(client, e["user_a"])
    conn = init_db(e["path"])
    role = conn.execute(
        "SELECT id FROM roles WHERE name='HR/Attendance Officer'"
    ).fetchone()
    conn.execute(
        "INSERT INTO role_assignments(id,user_id,role_id,scope_type,scope_id) "
        "VALUES(?,?,?,?,?)",
        ("staff-escalation-" + uuid.uuid4().hex, e["user_a"]["id"], role["id"],
         "institution", e["tenant_a"]),
    )
    conn.commit()
    conn.close()

    response = client.get(path)
    assert response.status_code == 401
    assert response.get_json()["error"] == "school_identity_inactive"
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_regular_school_identity_can_retain_multiple_institution_roles(teacher_env):
    e = teacher_env
    conn = init_db(e["path"])
    staff = identity.create_school_user(
        conn,
        email="multi-role-staff@example.test",
        password="correct horse battery staple",
        institution_id=e["tenant_a"],
        role_name="Institution Admin",
        granted_by="platform-admin",
    )
    role = conn.execute(
        "SELECT id FROM roles WHERE name='HR/Attendance Officer'"
    ).fetchone()
    conn.execute(
        "INSERT INTO role_assignments(id,user_id,role_id,scope_type,scope_id) "
        "VALUES(?,?,?,?,?)",
        ("staff-extra-role-" + uuid.uuid4().hex, staff["id"], role["id"],
         "institution", e["tenant_a"]),
    )
    conn.commit()
    assert identity.active_tenant_identity(
        conn, user_id=staff["id"], institution_id=e["tenant_a"]
    ) is True
    conn.close()


def test_teacher_portal_mutations_require_csrf_and_idempotency(teacher_env):
    e = teacher_env
    client = e["app"].app.test_client()
    csrf = login(client, e["user_a"])
    payload = {"policy_id": e["policy_a"], "event_type": "check_in"}
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



def test_teacher_identity_provisioning_rolls_back_when_audit_insert_fails(teacher_env):
    e = teacher_env
    conn = init_db(e["path"])
    suffix = uuid.uuid4().hex[:8]
    teacher_id = core.create_teacher(
        conn, None, e["tenant_a"], "AUDIT-" + suffix
    )
    conn.execute(
        """
        CREATE TRIGGER fail_school_user_audit
        BEFORE INSERT ON audit_log
        WHEN NEW.action_type = 'school_user_created'
        BEGIN
            SELECT RAISE(ABORT, 'school_user_audit_unavailable');
        END
        """
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="school_user_audit_failed"):
        identity.create_school_user(
            conn, email=f"audit-failure-{suffix}@school.example",
            password="correct horse battery staple", institution_id=e["tenant_a"],
            role_name="Teacher Portal", teacher_id=teacher_id,
            granted_by="platform-admin",
        )
    teacher = conn.execute(
        "SELECT user_id FROM teachers WHERE id=?", (teacher_id,)
    ).fetchone()
    assert teacher["user_id"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_links WHERE teacher_id=?",
        (teacher_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM users WHERE email=?",
        (f"audit-failure-{suffix}@school.example",),
    ).fetchone()[0] == 0
    conn.close()

def test_teacher_identity_revocation_is_audited_idempotent_and_allows_reassignment(teacher_env):
    e = teacher_env
    client = e["app"].app.test_client()
    csrf = login(client, e["admin_a"])
    path = f"/api/school/teachers/{e['teacher_a']}/identity/revoke"
    payload = {"reason_code": "employment_ended"}
    request_headers = headers(csrf, "teacher-revoke-idempotency-0001")

    revoked = client.post(path, json=payload, headers=request_headers)
    assert revoked.status_code == 200
    result = revoked.get_json()
    assert result["revoked"] is True
    assert result["created"] is True
    assert result["teacher_id"] == e["teacher_a"]
    assert result["user_id"] == e["user_a"]["id"]

    conn = init_db(e["path"])
    link_count = conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_links WHERE teacher_id=?",
        (e["teacher_a"],),
    ).fetchone()[0]
    teacher = conn.execute(
        "SELECT user_id FROM teachers WHERE id=?", (e["teacher_a"],)
    ).fetchone()
    old_user = conn.execute(
        "SELECT status FROM users WHERE id=?", (e["user_a"]["id"],)
    ).fetchone()
    event = conn.execute(
        "SELECT * FROM teacher_identity_events WHERE id=?",
        (result["revocation_id"],),
    ).fetchone()
    audit = conn.execute(
        """SELECT action_type, actor_id, entity_type, entity_id
           FROM audit_log WHERE entity_id=?""",
        (result["revocation_id"],),
    ).fetchone()
    assert link_count == 0
    assert teacher["user_id"] is None
    assert old_user["status"] == "suspended"
    assert conn.execute(
        "SELECT COUNT(*) FROM role_assignments WHERE user_id=?",
        (e["user_a"]["id"],),
    ).fetchone()[0] == 0
    assert event["event_type"] == "revoked"
    assert event["reason_code"] == "employment_ended"
    assert event["actor_id"] == e["admin_a"]["id"]
    assert audit["action_type"] == "teacher_identity_link_revoked"
    assert audit["entity_type"] == "teacher_identity_events"
    assert audit["entity_id"] == result["revocation_id"]

    replay = client.post(path, json=payload, headers=request_headers)
    assert replay.status_code == 200
    assert replay.get_json()["created"] is False
    assert replay.get_json()["revocation_id"] == result["revocation_id"]
    conflict = client.post(
        path,
        json={"reason_code": "account_compromised"},
        headers=request_headers,
    )
    assert conflict.status_code == 409

    replacement_user = identity.create_school_user(
        conn,
        email="replacement-teacher@example.test",
        password="correct horse battery staple",
        institution_id=e["tenant_a"],
        role_name="Teacher Portal",
        teacher_id=e["teacher_a"],
        granted_by=e["admin_a"]["id"],
    )
    assert identity.linked_teacher_for_school_user(
        conn, user_id=replacement_user["id"], institution_id=e["tenant_a"]
    )["id"] == e["teacher_a"]

    # Replaying the old idempotency key must not revoke a newly-created link.
    replay_after_reassignment = client.post(path, json=payload, headers=request_headers)
    assert replay_after_reassignment.status_code == 200
    assert replay_after_reassignment.get_json()["created"] is False
    current_teacher = conn.execute(
        "SELECT user_id FROM teachers WHERE id=?", (e["teacher_a"],)
    ).fetchone()
    assert current_teacher["user_id"] == replacement_user["id"]

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE teacher_identity_events SET reason_code='identity_correction' WHERE id=?",
            (result["revocation_id"],),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM teacher_identity_events WHERE id=?",
            (result["revocation_id"],),
        )
    conn.rollback()
    conn.close()


@pytest.mark.parametrize("actor_key", ["principal_a", "hr_a", "user_a"])
def test_teacher_identity_revocation_requires_manage_users_permission(teacher_env, actor_key):
    e = teacher_env
    client = e["app"].app.test_client()
    csrf = login(client, e[actor_key])
    response = client.post(
        f"/api/school/teachers/{e['teacher_a']}/identity/revoke",
        json={"reason_code": "identity_correction"},
        headers=headers(csrf, "teacher-revoke-denied-" + actor_key),
    )
    assert response.status_code == 403
    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_links WHERE teacher_id=?",
        (e["teacher_a"],),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_events"
    ).fetchone()[0] == 0
    conn.close()


def test_teacher_identity_revocation_cannot_cross_tenant(teacher_env):
    e = teacher_env
    client = e["app"].app.test_client()
    csrf = login(client, e["admin_a"])
    response = client.post(
        f"/api/school/teachers/{e['teacher_b']}/identity/revoke",
        json={"reason_code": "teacher_reassignment"},
        headers=headers(csrf, "teacher-revoke-cross-tenant-0001"),
    )
    assert response.status_code == 404
    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT status FROM users WHERE id=?", (e["user_b"]["id"],)
    ).fetchone()["status"] == "active"
    assert conn.execute(
        "SELECT user_id FROM teachers WHERE id=?", (e["teacher_b"],)
    ).fetchone()["user_id"] == e["user_b"]["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_events"
    ).fetchone()[0] == 0
    conn.close()


def test_teacher_identity_revocation_requires_csrf_key_and_valid_reason(teacher_env):
    e = teacher_env
    client = e["app"].app.test_client()
    csrf = login(client, e["admin_a"])
    path = f"/api/school/teachers/{e['teacher_a']}/identity/revoke"
    payload = {"reason_code": "identity_correction"}
    no_csrf = client.post(
        path, json=payload, headers={"Idempotency-Key": "teacher-revoke-no-csrf-0001"}
    )
    no_key = client.post(path, json=payload, headers={"X-CSRF-Token": csrf})
    invalid_reason = client.post(
        path,
        json={"reason_code": ["identity_correction"]},
        headers=headers(csrf, "teacher-revoke-invalid-reason-0001"),
    )
    extra_field = client.post(
        path,
        json={"reason_code": "identity_correction", "institution_id": e["tenant_b"]},
        headers=headers(csrf, "teacher-revoke-extra-field-0001"),
    )
    assert no_csrf.status_code == 400
    assert no_key.status_code == 400
    assert invalid_reason.status_code == 400
    assert extra_field.status_code == 400
    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_events"
    ).fetchone()[0] == 0
    conn.close()


def test_teacher_identity_revocation_rolls_back_if_audit_write_fails(teacher_env):
    e = teacher_env
    conn = init_db(e["path"])
    conn.execute(
        """
        CREATE TRIGGER fail_teacher_identity_revocation_audit
        BEFORE INSERT ON audit_log
        WHEN NEW.action_type = 'teacher_identity_link_revoked'
        BEGIN
            SELECT RAISE(ABORT, 'teacher_identity_audit_unavailable');
        END
        """
    )
    conn.commit()
    conn.close()

    client = e["app"].app.test_client()
    csrf = login(client, e["admin_a"])
    response = client.post(
        f"/api/school/teachers/{e['teacher_a']}/identity/revoke",
        json={"reason_code": "account_compromised"},
        headers=headers(csrf, "teacher-revoke-audit-failure-0001"),
    )
    assert response.status_code == 500
    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_links WHERE teacher_id=?",
        (e["teacher_a"],),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT user_id FROM teachers WHERE id=?", (e["teacher_a"],)
    ).fetchone()["user_id"] == e["user_a"]["id"]
    assert conn.execute(
        "SELECT status FROM users WHERE id=?", (e["user_a"]["id"],)
    ).fetchone()["status"] == "active"
    assert conn.execute(
        "SELECT COUNT(*) FROM teacher_identity_events"
    ).fetchone()[0] == 0
    conn.close()


def test_teacher_link_without_teacher_portal_role_cannot_become_staff_session(teacher_env):
    e = teacher_env
    client = e["app"].app.test_client()
    login(client, e["user_a"])
    conn = init_db(e["path"])
    conn.execute(
        "DELETE FROM role_assignments WHERE user_id=?",
        (e["user_a"]["id"],),
    )
    institution_admin_role = conn.execute(
        "SELECT id FROM roles WHERE name='Institution Admin'"
    ).fetchone()
    conn.execute(
        """INSERT INTO role_assignments
           (id, user_id, role_id, scope_type, scope_id)
           VALUES (?, ?, ?, 'institution', ?)""",
        ("role-swap-" + uuid.uuid4().hex, e["user_a"]["id"],
         institution_admin_role["id"], e["tenant_a"]),
    )
    conn.commit()
    assert identity.active_tenant_identity(
        conn, user_id=e["user_a"]["id"], institution_id=e["tenant_a"]
    ) is False
    assert identity.authenticate_school_user(
        conn, email=e["user_a"]["email"], password="correct horse battery staple"
    ) is None
    conn.close()

    response = client.get("/api/school/attendance")
    assert response.status_code == 401
    assert response.get_json()["error"] == "school_identity_inactive"
    with client.session_transaction() as session:
        assert "user_id" not in session

def test_teacher_identity_revocation_cleans_stale_role_escalation_and_allows_reassignment(teacher_env):
    e = teacher_env
    conn = init_db(e["path"])
    elevated_role = conn.execute(
        "SELECT id FROM roles WHERE name='Institution Admin'"
    ).fetchone()
    conn.execute(
        """INSERT INTO role_assignments(id, user_id, role_id, scope_type, scope_id)
           VALUES (?, ?, ?, 'institution', ?)""",
        (
            "unexpected-role-" + uuid.uuid4().hex,
            e["user_a"]["id"],
            elevated_role["id"],
            e["tenant_a"],
        ),
    )
    conn.commit()
    assert identity.active_tenant_identity(
        conn, user_id=e["user_a"]["id"], institution_id=e["tenant_a"]
    ) is False
    conn.close()

    client = e["app"].app.test_client()
    csrf = login(client, e["admin_a"])
    response = client.post(
        f"/api/school/teachers/{e['teacher_a']}/identity/revoke",
        json={"reason_code": "identity_correction"},
        headers=headers(csrf, "teacher-revoke-stale-role-0001"),
    )
    assert response.status_code == 200

    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT COUNT(*) FROM role_assignments WHERE user_id=?",
        (e["user_a"]["id"],),
    ).fetchone()[0] == 0
    audit = conn.execute(
        """SELECT before_state FROM audit_log
           WHERE entity_id=? AND action_type='teacher_identity_link_revoked'""",
        (response.get_json()["revocation_id"],),
    ).fetchone()
    assert "Institution Admin" in audit["before_state"]
    assert "Teacher Portal" in audit["before_state"]

    replacement = identity.create_school_user(
        conn,
        email="stale-role-replacement@example.test",
        password="correct horse battery staple",
        institution_id=e["tenant_a"],
        role_name="Teacher Portal",
        teacher_id=e["teacher_a"],
        granted_by=e["admin_a"]["id"],
    )
    assert identity.linked_teacher_for_school_user(
        conn, user_id=replacement["id"], institution_id=e["tenant_a"]
    )["id"] == e["teacher_a"]
    conn.close()

