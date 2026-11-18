"""Fail-closed route, blueprint and institution-role authorization regressions."""
import uuid

import pytest

import app as app_module
from db import init_db
from services import core, identity


def test_every_registered_route_method_has_an_explicit_access_policy():
    registered = {}
    for rule in app_module.app.url_map.iter_rules():
        methods = set(rule.methods or ()) - {"HEAD", "OPTIONS"}
        if not methods:
            continue
        registered.setdefault(rule.endpoint, set()).update(methods)

    policies = app_module._ROUTE_ACCESS_POLICIES
    assert set(registered) == set(policies)
    for endpoint, methods in registered.items():
        access_class, approved_methods = policies[endpoint]
        assert access_class in {"public", "authenticated", "school_user", "platform_admin"}
        assert methods == set(approved_methods), (
            f"{endpoint} changed methods without an authorization review"
        )


@pytest.mark.parametrize(
    ("endpoint", "method", "expected"),
    [
        ("dashboard_api.dashboard_stats", "GET", "platform_admin"),
        ("dashboard_api.admin_grant_subscription", "POST", "platform_admin"),
        ("school_create_job", "POST", "school_user"),
        ("school_decide_leave_approval", "POST", "school_user"),
        ("school_teacher_me", "GET", "school_user"),
        ("school_teacher_attendance", "GET", "school_user"),
        ("school_teacher_create_attendance_event", "POST", "school_user"),
        ("school_teacher_leave_requests", "GET", "school_user"),
        ("school_teacher_create_leave_request", "POST", "school_user"),
        ("school_revoke_teacher_identity", "POST", "school_user"),
        ("api_status", "GET", "public"),
        ("logout", "POST", "authenticated"),
        ("future_blueprint.export_all", "GET", "unclassified"),
        ("dashboard_api.admin_grant_subscription", "GET", "unclassified"),
    ],
)
def test_route_policy_is_exact_and_unknown_methods_fail_closed(endpoint, method, expected):
    assert app_module._route_access_class(endpoint, method) == expected


def test_platform_admin_session_cannot_call_tenant_identity_api(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    app_module.app.config.update(TESTING=True)

    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "platform-admin"
            session["role"] = "platform_admin"
        response = client.get("/api/school/me")

    assert response.status_code == 403
    assert response.get_json() == {"error": "school_identity_required"}


def test_every_school_route_method_has_an_explicit_permission_rule():
    assert set(app_module._SCHOOL_ROUTE_PERMISSION_REQUIREMENTS) == set(
        app_module._SCHOOL_API_ROUTE_METHODS
    )


def test_seeded_institution_roles_match_the_explicit_school_route_matrix():
    allowed_roles = {
        ("school_me", "GET"): {
            "Institution Admin", "Principal", "Academic Coordinator",
            "Examiner", "HR/Attendance Officer", "Teacher Portal",
        },
        ("school_teacher_me", "GET"): {"Teacher Portal"},
        ("school_teacher_attendance", "GET"): {"Teacher Portal"},
        ("school_teacher_create_attendance_event", "POST"): {"Teacher Portal"},
        ("school_teacher_leave_requests", "GET"): {"Teacher Portal"},
        ("school_teacher_create_leave_request", "POST"): {"Teacher Portal"},
        ("school_revoke_teacher_identity", "POST"): {"Institution Admin"},
        ("school_classes", "GET"): {
            "Institution Admin", "Principal", "Academic Coordinator",
        },
        ("school_create_class", "POST"): {"Institution Admin"},
        ("school_question_banks", "GET"): {
            "Institution Admin", "Academic Coordinator", "Examiner",
        },
        ("school_exam_blueprints", "GET"): {
            "Institution Admin", "Principal", "Academic Coordinator", "Examiner",
        },
        ("school_attendance", "GET"): {
            "Institution Admin", "Principal", "HR/Attendance Officer",
        },
        ("school_create_attendance_event", "POST"): {
            "Institution Admin", "HR/Attendance Officer",
        },
        ("school_leave_requests", "GET"): {
            "Institution Admin", "Principal", "HR/Attendance Officer",
        },
        ("school_create_leave_request", "POST"): {
            "Institution Admin", "Principal", "HR/Attendance Officer",
        },
        ("school_decide_leave_approval", "POST"): {
            "Principal", "HR/Attendance Officer",
        },
        ("school_create_job", "POST"): {"Institution Admin"},
        ("school_get_job", "GET"): {"Institution Admin", "Principal"},
    }
    role_permissions = {
        name: set(permissions)
        for name, scope, permissions in core.DEFAULT_ROLES
        if scope == "institution"
    }
    assert set(allowed_roles) == set(app_module._SCHOOL_ROUTE_PERMISSION_REQUIREMENTS)
    for route, required in app_module._SCHOOL_ROUTE_PERMISSION_REQUIREMENTS.items():
        for role_name, permissions in role_permissions.items():
            actual = app_module._school_route_permission_allowed(
                route[0], route[1], permissions
            )
            assert actual is (role_name in allowed_roles[route]), (
                f"{role_name} permission mismatch for {route}: {sorted(required)}"
            )


@pytest.fixture
def school_route_rbac_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    monkeypatch.delenv("ENFORCE_ENTITLEMENTS", raising=False)
    path = str(tmp_path / "school-route-rbac.sqlite")
    conn = init_db(path)
    core.seed_default_roles(conn)
    suffix = uuid.uuid4().hex[:10]
    institution_id = core.create_institution(
        conn, "Route RBAC Tenant", "RBAC-" + suffix
    )
    teacher_id = core.create_teacher(
        conn, None, institution_id, "T-" + suffix
    )
    users = {}
    roles = (
        "Institution Admin",
        "Principal",
        "Academic Coordinator",
        "Examiner",
        "HR/Attendance Officer",
        "Teacher Portal",
    )
    for index, role_name in enumerate(roles):
        users[role_name] = identity.create_school_user(
            conn,
            email=f"route-rbac-{index}-{suffix}@school.example",
            password="correct horse battery staple",
            institution_id=institution_id,
            role_name=role_name,
            teacher_id=teacher_id if role_name == "Teacher Portal" else None,
            granted_by="platform-admin",
        )
    conn.close()

    monkeypatch.setattr(app_module, "DB_PATH", path)
    app_module.app.config.update(TESTING=True)
    return {"institution_id": institution_id, "teacher_id": teacher_id, "users": users}


def test_school_route_permission_matrix_denies_each_unassigned_role(
    school_route_rbac_env,
):
    env = school_route_rbac_env
    all_roles = set(env["users"])
    route_cases = [
        ("school_me", "GET", "/api/school/me", all_roles),
        ("school_teacher_me", "GET", "/api/school/teacher/me", {"Teacher Portal"}),
        ("school_teacher_attendance", "GET", "/api/school/teacher/attendance", {"Teacher Portal"}),
        ("school_teacher_create_attendance_event", "POST", "/api/school/teacher/attendance/events", {"Teacher Portal"}),
        ("school_teacher_leave_requests", "GET", "/api/school/teacher/leave-requests", {"Teacher Portal"}),
        ("school_teacher_create_leave_request", "POST", "/api/school/teacher/leave-requests", {"Teacher Portal"}),
        ("school_revoke_teacher_identity", "POST", f"/api/school/teachers/{env['teacher_id']}/identity/revoke", {"Institution Admin"}),
        ("school_classes", "GET", "/api/school/classes", {"Institution Admin", "Principal", "Academic Coordinator"}),
        ("school_create_class", "POST", "/api/school/classes", {"Institution Admin"}),
        ("school_question_banks", "GET", "/api/school/question-banks", {"Institution Admin", "Academic Coordinator", "Examiner"}),
        ("school_exam_blueprints", "GET", "/api/school/exam-blueprints", {"Institution Admin", "Principal", "Academic Coordinator", "Examiner"}),
        ("school_attendance", "GET", "/api/school/attendance", {"Institution Admin", "Principal", "HR/Attendance Officer"}),
        ("school_create_attendance_event", "POST", "/api/school/attendance/events", {"Institution Admin", "HR/Attendance Officer"}),
        ("school_leave_requests", "GET", "/api/school/leave-requests", {"Institution Admin", "Principal", "HR/Attendance Officer"}),
        ("school_create_leave_request", "POST", "/api/school/leave-requests", {"Institution Admin", "Principal", "HR/Attendance Officer"}),
        ("school_decide_leave_approval", "POST", "/api/school/leave-requests/missing/decision", {"Principal", "HR/Attendance Officer"}),
        ("school_create_job", "POST", "/api/school/jobs", {"Institution Admin"}),
        ("school_get_job", "GET", "/api/school/jobs/missing-job", {"Institution Admin", "Principal"}),
    ]

    for role_name, user in env["users"].items():
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session["user"] = user["email"]
            session["user_id"] = user["id"]
            session["institution_id"] = env["institution_id"]
            session["school_role_name"] = role_name
            session["role"] = "school_user"
            session["csrf_token"] = "route-rbac-csrf-token"

        for endpoint, method, path, permitted_roles in route_cases:
            kwargs = {
                "method": method,
                "headers": {"X-CSRF-Token": "route-rbac-csrf-token"},
            }
            if method == "POST":
                # Empty objects fail request validation before any mutation;
                # this test isolates the middleware permission decision.
                kwargs["json"] = {}
            response = client.open(path, **kwargs)
            if role_name in permitted_roles:
                assert response.status_code not in {401, 403}, (
                    f"{role_name} was denied {method} {path}: "
                    f"{response.status_code} {response.get_json(silent=True)}"
                )
            else:
                assert response.status_code == 403, (
                    f"{role_name} unexpectedly reached {method} {path}: "
                    f"{response.status_code} {response.get_json(silent=True)}"
                )
                assert response.get_json() == {"error": "permission_denied"}
