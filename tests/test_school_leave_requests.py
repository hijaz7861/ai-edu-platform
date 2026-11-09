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
        "leave_type_a": leave_type_a,
        "leave_type_b": leave_type_b,
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
    with client.session_transaction() as session:
        return session["csrf_token"]


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



def _submit_leave(client, csrf, key, payload):
    return client.post(
        "/api/school/leave-requests",
        json=payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": key},
    )


def test_school_leave_submission_is_tenant_bound_and_idempotent(leave_env):
    e = leave_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    payload = {
        "teacher_id": e["teacher_a"],
        "leave_type_id": e["leave_type_a"],
        "start_date": "2026-11-20",
        "end_date": "2026-11-21",
        "reason": "Family commitment - confidential",
        "substitute_teacher_id": None,
    }
    first = _submit_leave(client, csrf, "leave-submit-0001", payload)
    replay = _submit_leave(client, csrf, "leave-submit-0001", payload)
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.get_json() == first.get_json()
    assert first.get_json()["status"] == "pending"

    conn = init_db(e["path"])
    row = conn.execute(
        "SELECT teacher_id, leave_type_id, status, reason FROM leave_requests WHERE id=?",
        (first.get_json()["id"],),
    ).fetchone()
    assert row["teacher_id"] == e["teacher_a"]
    assert row["leave_type_id"] == e["leave_type_a"]
    assert row["status"] == "pending"
    assert row["reason"] == payload["reason"]
    assert conn.execute(
        "SELECT COUNT(*) FROM leave_approvals WHERE leave_request_id=?",
        (first.get_json()["id"],),
    ).fetchone()[0] == 1
    key_row = conn.execute(
        "SELECT idempotency_key_hash, created_at, expires_at FROM tenant_leave_request_keys WHERE leave_request_id=?",
        (first.get_json()["id"],),
    ).fetchone()
    assert key_row is not None
    assert key_row["idempotency_key_hash"] != "leave-submit-0001"
    assert len(key_row["idempotency_key_hash"]) == 64
    assert key_row["expires_at"] > key_row["created_at"]
    conn.close()

    changed = dict(payload, reason="Different request")
    conflict = _submit_leave(client, csrf, "leave-submit-0001", changed)
    assert conflict.status_code == 409
    assert conflict.get_json() == {"error": "idempotency_key_conflict"}

    other_client = e["app"].app.test_client()
    other_csrf = login(other_client, e["users"]["Principal"])
    actor_conflict = _submit_leave(other_client, other_csrf, "leave-submit-0001", payload)
    assert actor_conflict.status_code == 409
    assert actor_conflict.get_json() == {"error": "idempotency_key_conflict"}

    conn = init_db(e["path"])
    conn.execute(
        "UPDATE tenant_leave_request_keys SET expires_at='2000-01-01T00:00:00+00:00' WHERE idempotency_key_hash=?",
        (key_row["idempotency_key_hash"],),
    )
    conn.commit()
    conn.close()
    expired_replay = _submit_leave(client, csrf, "leave-submit-0001", payload)
    assert expired_replay.status_code == 201
    assert expired_replay.get_json()["id"] != first.get_json()["id"]

    listed = client.get("/api/school/leave-requests")
    assert listed.status_code == 200
    encoded = listed.get_data(as_text=True)
    assert payload["reason"] not in encoded


@pytest.mark.parametrize(
    "field",
    ["teacher_id", "leave_type_id", "substitute_teacher_id"],
)
def test_school_leave_submission_rejects_foreign_tenant_references(leave_env, field):
    e = leave_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    payload = {
        "teacher_id": e["teacher_a"],
        "leave_type_id": e["leave_type_a"],
        "start_date": "2026-11-22",
        "end_date": "2026-11-23",
        "reason": "Tenant-bound request",
        "substitute_teacher_id": None,
    }
    payload[field] = (
        e["teacher_b"] if field in {"teacher_id", "substitute_teacher_id"}
        else e["leave_type_b"]
    )
    response = _submit_leave(client, csrf, "foreign-leave-" + field, payload)
    assert response.status_code == 404
    assert response.get_json() == {"error": "leave_resource_not_found"}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("Institution Admin", 201),
        ("Principal", 201),
        ("HR/Attendance Officer", 201),
        ("Academic Coordinator", 403),
        ("Examiner", 403),
    ],
)
def test_school_leave_submission_enforces_institution_role_permissions(leave_env, role, expected):
    e = leave_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"][role])
    payload = {
        "teacher_id": e["teacher_a"],
        "leave_type_id": e["leave_type_a"],
        "start_date": "2026-11-24",
        "end_date": "2026-11-25",
        "reason": "Role-bound request",
    }
    response = _submit_leave(client, csrf, "role-leave-" + role.lower().replace("/", "-").replace(" ", "-"), payload)
    assert response.status_code == expected, role
    if expected == 403:
        assert response.get_json() == {"error": "permission_denied"}


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("start_date", "2026-2-01", "invalid_start_date"),
        ("end_date", "2026-11-25T00:00:00", "invalid_end_date"),
        ("end_date", "2026-11-23", "end_date_before_start_date"),
        ("reason", "x" * 2001, "reason_must_be_between_1_and_2000_characters"),
    ],
)
def test_school_leave_submission_rejects_invalid_payload_values(leave_env, field, value, error):
    e = leave_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    payload = {
        "teacher_id": e["teacher_a"],
        "leave_type_id": e["leave_type_a"],
        "start_date": "2026-11-24",
        "end_date": "2026-11-25",
        "reason": "Valid reason",
    }
    payload[field] = value
    response = _submit_leave(client, csrf, "invalid-leave-" + field, payload)
    assert response.status_code == 400
    assert response.get_json() == {"error": error}


def test_school_leave_submission_requires_idempotency_and_csrf(leave_env):
    e = leave_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    payload = {
        "teacher_id": e["teacher_a"],
        "leave_type_id": e["leave_type_a"],
        "start_date": "2026-11-26",
        "end_date": "2026-11-27",
        "reason": "Must be protected",
    }
    missing_key = client.post(
        "/api/school/leave-requests", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert missing_key.status_code == 400
    assert missing_key.get_json() == {"error": "valid_idempotency_key_required"}

    missing_csrf = client.post(
        "/api/school/leave-requests", json=payload,
        headers={"Idempotency-Key": "leave-submit-csrf-01"},
    )
    assert missing_csrf.status_code == 400


def test_school_leave_submission_requires_active_entitlement_in_production(leave_env, monkeypatch):
    e = leave_env
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(e["app"], "allow_request", lambda *args, **kwargs: True)
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    payload = {
        "teacher_id": e["teacher_a"],
        "leave_type_id": e["leave_type_a"],
        "start_date": "2026-11-28",
        "end_date": "2026-11-29",
        "reason": "Entitlement-gated request",
    }
    denied = _submit_leave(client, csrf, "leave-entitlement-0001", payload)
    assert denied.status_code == 403
    assert denied.get_json() == {"error": "subscription_required"}

    conn = init_db(e["path"])
    plan_id = create_plan(
        conn,
        code="leave-write-" + uuid.uuid4().hex[:8],
        name="Leave Write Test",
        entitlements={"leave_management": True},
    )
    grant_subscription(
        conn, institution_id=e["tenant_a"], plan_id=plan_id,
        granted_by="platform-admin", reason="automated leave submission test",
    )
    conn.close()
    allowed = _submit_leave(client, csrf, "leave-entitlement-0002", payload)
    assert allowed.status_code == 201


def test_school_leave_submission_honors_configured_approval_levels(leave_env):
    e = leave_env
    conn = init_db(e["path"])
    multi = attendance.create_leave_type(
        conn, e["tenant_a"], "Multi-level Leave", 15, multi_level=True
    )
    automatic = attendance.create_leave_type(
        conn, e["tenant_a"], "No Approval Leave", 5
    )
    malformed = attendance.create_leave_type(
        conn, e["tenant_a"], "Malformed Approval Leave", 5
    )
    conn.execute("UPDATE leave_types SET requires_approval=0 WHERE id=?", (automatic,))
    conn.execute("UPDATE leave_types SET requires_approval=NULL WHERE id=?", (malformed,))
    conn.commit()
    conn.close()

    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    multi_response = _submit_leave(
        client, csrf, "leave-multilevel-0001",
        {
            "teacher_id": e["teacher_a"], "leave_type_id": multi,
            "start_date": "2026-12-01", "end_date": "2026-12-02",
            "reason": "Two-step approval",
        },
    )
    auto_response = _submit_leave(
        client, csrf, "leave-auto-approve-0001",
        {
            "teacher_id": e["teacher_a"], "leave_type_id": automatic,
            "start_date": "2026-12-03", "end_date": "2026-12-03",
            "reason": "Configured no-approval leave",
        },
    )
    malformed_response = _submit_leave(
        client, csrf, "leave-malformed-approval-0001",
        {
            "teacher_id": e["teacher_a"], "leave_type_id": malformed,
            "start_date": "2026-12-04", "end_date": "2026-12-04",
            "reason": "Malformed policy must fail closed",
        },
    )
    assert multi_response.status_code == 201
    assert multi_response.get_json()["status"] == "pending"
    assert auto_response.status_code == 201
    assert auto_response.get_json()["status"] == "approved"
    assert malformed_response.status_code == 201
    assert malformed_response.get_json()["status"] == "pending"

    conn = init_db(e["path"])
    assert conn.execute(
        "SELECT COUNT(*) FROM leave_approvals WHERE leave_request_id=?",
        (multi_response.get_json()["id"],),
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM leave_approvals WHERE leave_request_id=?",
        (auto_response.get_json()["id"],),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM leave_approvals WHERE leave_request_id=?",
        (malformed_response.get_json()["id"],),
    ).fetchone()[0] == 1
    conn.close()


def test_school_leave_submission_rejects_overlapping_active_leave_but_allows_adjacent_dates(leave_env):
    e = leave_env
    client = e["app"].app.test_client()
    csrf = login(client, e["users"]["Institution Admin"])
    base_payload = {
        "teacher_id": e["teacher_a"],
        "leave_type_id": e["leave_type_a"],
        "start_date": "2026-12-10",
        "end_date": "2026-12-12",
        "reason": "Initial approved-period reservation",
    }
    first = _submit_leave(client, csrf, "leave-overlap-base-0001", base_payload)
    assert first.status_code == 201

    overlapping = dict(base_payload, start_date="2026-12-12", end_date="2026-12-14", reason="Overlapping request must be rejected")
    denied = _submit_leave(client, csrf, "leave-overlap-conflict-01", overlapping)
    assert denied.status_code == 409
    assert denied.get_json() == {"error": "leave_dates_overlap"}

    adjacent = dict(base_payload, start_date="2026-12-13", end_date="2026-12-14", reason="Immediately following non-overlapping request")
    accepted = _submit_leave(client, csrf, "leave-overlap-adjacent-01", adjacent)
    assert accepted.status_code == 201

    conn = init_db(e["path"])
    rejected_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO leave_requests
           (id, teacher_id, leave_type_id, start_date, end_date, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'rejected', ?)""",
        (rejected_id, e["teacher_a"], e["leave_type_a"], "2026-12-20", "2026-12-22", "Rejected historical request", "2026-11-09T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    after_rejection = dict(base_payload, start_date="2026-12-20", end_date="2026-12-22", reason="Rejected requests do not reserve leave dates")
    allowed = _submit_leave(client, csrf, "leave-overlap-rejected-01", after_rejection)
    assert allowed.status_code == 201
