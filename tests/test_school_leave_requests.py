"""Tenant-isolation and least-privilege tests for school leave-request reads."""
import re
import uuid

import pytest

from db import init_db
from services import attendance, core, identity
from services.licensing import create_plan, grant_subscription


@pytest.fixture
def leave_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    path = str(tmp_path / "school-leave.sqlite")
    conn = init_db(path)
    core.seed_default_roles(conn)
    suffix = uuid.uuid4().hex[:10]
    tenant_a = core.create_institution(conn, "Leave Tenant A", "LA-" + suffix)
    tenant_b = core.create_institution(conn, "Leave Tenant B", "LB-" + suffix)
    teacher_a = core.create_teacher(conn, None, tenant_a, "TA-" + suffix)
    teacher_b = core.create_teacher(conn, None, tenant_b, "TB-" + suffix)
    leave_type_a = attendance.create_leave_type(conn, tenant_a, "Annual Leave", 20)
    leave_type_b = attendance.create_leave_type(conn, tenant_b, "Medical Leave", 10)

    request_a = attendance.request_leave(
        conn, teacher_a, leave_type_a, "2026-11-10", "2026-11-11",
        "private reason that must not be returned", substitute_teacher_id=teacher_b,
    )
    request_a_approved = attendance.request_leave(
        conn, teacher_a, leave_type_a, "2026-12-01", "2026-12-02",
        "approved private reason",
    )
    request_b = attendance.request_leave(
        conn, teacher_b, leave_type_b, "2026-11-12", "2026-11-13",
        "tenant B private reason",
    )
    # These inconsistent legacy rows must not cross the teacher/type tenant join.
    request_wrong_type = attendance.request_leave(
        conn, teacher_a, leave_type_b, "2026-11-14", "2026-11-15",
        "wrong-tenant leave type",
    )
    request_wrong_teacher = attendance.request_leave(
        conn, teacher_b, leave_type_a, "2026-11-16", "2026-11-17",
        "wrong-tenant teacher",
    )
    conn.execute(
        "UPDATE leave_requests SET status='approved' WHERE id=?",
        (request_a_approved,),
    )
    conn.execute(
        "UPDATE leave_approvals SET comments='private approval comment' "
        "WHERE leave_request_id=?",
        (request_a,),
    )
    conn.commit()

    users = {}
    roles = [
        "Institution Admin",
        "Principal",
        "Academic Coordinator",
        "Examiner",
        "HR/Attendance Officer",
    ]
    for index, role_name in enumerate(roles):
        users[role_name] = identity.create_school_user(
            conn,
            email=f"leave-{index}-{suffix}@school.example",
            password="correct horse battery staple",
            institution_id=tenant_a,
            role_name=role_name,
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
        "request_a": request_a,
        "request_a_approved": request_a_approved,
        "request_b": request_b,
        "request_wrong_type": request_wrong_type,
        "request_wrong_teacher": request_wrong_teacher,
        "users": users,
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


def test_leave_requests_require_both_teacher_and_leave_type_tenant_ownership(leave_env):
    e = leave_env
    client = e["app"].app.test_client()
    login(client, e["users"]["Institution Admin"])
    response = client.get(
        "/api/school/leave-requests",
        query_string={"institution_id": e["tenant_b"]},
    )
    assert response.status_code == 200
    rows = response.get_json()["leave_requests"]
    assert {row["id"] for row in rows} == {e["request_a"], e["request_a_approved"]}
    assert all(row["teacher_id"] == e["teacher_a"] for row in rows)
    assert e["request_b"] not in {row["id"] for row in rows}
    assert e["request_wrong_type"] not in {row["id"] for row in rows}
    assert e["request_wrong_teacher"] not in {row["id"] for row in rows}


def test_leave_request_response_excludes_reasons_substitutes_and_approval_comments(leave_env):
    e = leave_env
    client = e["app"].app.test_client()
    login(client, e["users"]["Institution Admin"])
    response = client.get("/api/school/leave-requests")
    assert response.status_code == 200
    rows = response.get_json()["leave_requests"]
    assert rows
    assert all(set(row) == {
        "id", "teacher_id", "leave_type_name", "start_date", "end_date",
        "status", "created_at",
    } for row in rows)
    encoded = response.get_data(as_text=True)
    assert "private reason" not in encoded
    assert "private approval comment" not in encoded
    assert e["teacher_b"] not in encoded


def test_leave_request_status_filter_is_bounded_and_strict(leave_env):
    e = leave_env
    client = e["app"].app.test_client()
    login(client, e["users"]["Institution Admin"])
    pending = client.get("/api/school/leave-requests?status=pending")
    approved = client.get("/api/school/leave-requests?status=approved")
    assert pending.status_code == 200
    assert {row["id"] for row in pending.get_json()["leave_requests"]} == {e["request_a"]}
    assert approved.status_code == 200
    assert {row["id"] for row in approved.get_json()["leave_requests"]} == {e["request_a_approved"]}

    duplicate = client.get(
        "/api/school/leave-requests?status=pending&status=approved"
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json() == {"error": "status_must_be_unique"}
    invalid = client.get("/api/school/leave-requests?status=processing")
    assert invalid.status_code == 400
    assert invalid.get_json() == {"error": "invalid_status"}
    unsupported = client.get("/api/school/leave-requests?limit=5000")
    assert unsupported.status_code == 400
    assert unsupported.get_json() == {"error": "unsupported_query_parameter"}


def test_leave_request_access_matches_institution_role_permissions(leave_env):
    e = leave_env
    expected = {
        "Institution Admin": 200,
        "Principal": 200,
        "Academic Coordinator": 403,
        "Examiner": 403,
        "HR/Attendance Officer": 200,
    }
    for role_name, status in expected.items():
        client = e["app"].app.test_client()
        login(client, e["users"][role_name])
        response = client.get("/api/school/leave-requests")
        assert response.status_code == status, role_name
        if status == 403:
            assert response.get_json() == {"error": "permission_denied"}


def test_leave_requests_require_active_leave_management_entitlement_in_production(
    leave_env, monkeypatch
):
    e = leave_env
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(e["app"], "allow_request", lambda *args, **kwargs: True)
    client = e["app"].app.test_client()
    login(client, e["users"]["Institution Admin"])
    denied = client.get("/api/school/leave-requests")
    assert denied.status_code == 403
    assert denied.get_json() == {"error": "subscription_required"}

    conn = init_db(e["path"])
    plan_id = create_plan(
        conn,
        code="leave-" + uuid.uuid4().hex[:8],
        name="Leave Management Test",
        entitlements={"leave_management": True},
    )
    grant_subscription(
        conn,
        institution_id=e["tenant_a"],
        plan_id=plan_id,
        granted_by="platform-admin",
        reason="automated entitlement regression",
    )
    conn.close()
    allowed = client.get("/api/school/leave-requests")
    assert allowed.status_code == 200
