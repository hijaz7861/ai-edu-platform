"""Production regression tests for school-session route boundaries.

School identities must remain confined to tenant-scoped school APIs. They must
not reach legacy platform-wide endpoints, but they must be able to log out with
a CSRF-protected POST.
"""
import pytest
from flask import session


# Every school-facing route and method must be explicitly reviewed. Adding a
# new /api/school/* endpoint without adding it here must fail the route audit.
SCHOOL_API_ROUTE_METHODS = frozenset({
    ("school_me", "GET"),
    ("school_classes", "GET"),
    ("school_create_class", "POST"),
})


LEGACY_PLATFORM_ROUTES = [
    ("POST", "/institutions"),
    ("POST", "/institutions/tenant-a/classes"),
    ("GET", "/institutions/tenant-a/classes"),
    ("POST", "/teachers/teacher-a/check-in"),
    ("POST", "/teachers/teacher-a/check-out"),
    ("POST", "/api/questions/generate"),
    ("POST", "/question-banks/bank-a/duplicates"),
    ("POST", "/question-banks/bank-a/coverage-gaps"),
    ("POST", "/blueprints/blueprint-a/generate"),
    ("POST", "/approval-gates"),
    ("POST", "/approval-gates/gate-a/decide"),
    ("GET", "/providers/select"),
    ("GET", "/api/dashboard/stats"),
    ("GET", "/api/institutions"),
    ("GET", "/api/classes"),
    ("GET", "/api/question-banks"),
    ("GET", "/api/exams"),
    ("GET", "/api/approval-gates"),
    ("GET", "/api/admin/plans"),
    ("POST", "/api/admin/plans"),
    ("GET", "/api/admin/subscriptions"),
    ("POST", "/api/admin/subscriptions/grant"),
    ("POST", "/api/admin/subscriptions/sub-a/status"),
    ("POST", "/api/admin/school-users"),
    ("GET", "/api/admin/connected-systems"),
    ("POST", "/api/admin/connected-systems"),
    ("POST", "/api/admin/connected-systems/system-a/test"),
    ("POST", "/api/admin/connected-systems/system-a/status"),
    ("GET", "/admin/subscriptions"),
    ("GET", "/admin/connected-systems"),
]


@pytest.fixture
def school_session_client(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")

    import app as app_module

    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "school-admin@example.test"
            session["user_id"] = "school-user-id"
            session["institution_id"] = "tenant-a"
            session["school_role_name"] = "Institution Admin"
            session["role"] = "school_user"
            session["csrf_token"] = "csrf-test-token"
        yield client


@pytest.mark.parametrize(("method", "path"), LEGACY_PLATFORM_ROUTES)
def test_school_session_cannot_access_legacy_platform_routes(
    school_session_client, method, path
):
    response = school_session_client.open(path, method=method)
    assert response.status_code == 403, (method, path, response.get_data(as_text=True))
    assert response.get_json() == {"error": "platform_admin_required"}


def test_school_user_can_logout_with_csrf_in_production(school_session_client):
    response = school_session_client.post(
        "/auth/logout", headers={"X-CSRF-Token": "csrf-test-token"}
    )
    assert response.status_code == 302
    with school_session_client.session_transaction() as session:
        assert "user" not in session
        assert "institution_id" not in session


def test_school_user_cannot_logout_without_csrf_in_production(school_session_client):
    response = school_session_client.post("/auth/logout")
    assert response.status_code == 400
    assert response.get_json() == {"error": "csrf_invalid"}
    with school_session_client.session_transaction() as session:
        assert session["user"] == "school-admin@example.test"



@pytest.mark.parametrize("role", ["school_user", "support_agent"])
def test_non_platform_session_cannot_reach_unclassified_route_in_legacy_mode(
    monkeypatch, role
):
    """Legacy demo mode must never bypass authenticated role boundaries."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "0")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")

    import app as app_module

    with app_module.app.test_request_context("/future/private-export", method="GET"):
        session["user"] = "principal@example.test"
        session["role"] = role
        if role == "school_user":
            session["user_id"] = "school-user-id"
            session["institution_id"] = "tenant-a"
        result = app_module.enforce_api_security()

    assert isinstance(result, tuple), "unclassified routes must not bypass role authorization"
    response, status = result
    assert status == 403
    assert response.get_json() == {"error": "platform_admin_required"}


def test_platform_admin_keeps_legacy_access_to_unclassified_demo_route(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "0")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")

    import app as app_module

    with app_module.app.test_request_context("/future/demo-route", method="GET"):
        session["user"] = "platform-admin"
        session["role"] = "platform_admin"
        result = app_module.enforce_api_security()

    assert result is None



def test_school_session_is_denied_from_every_registered_platform_route_and_blueprint_method(
    school_session_client, monkeypatch
):
    """Enumerate Flask and dashboard blueprint routes to catch future auth gaps."""
    import re
    import app as app_module

    # Keep this a focused authorization test; the request middleware is still
    # exercised, while the limiter's persistent state is outside this assertion.
    monkeypatch.setattr(app_module, "allow_request", lambda *args, **kwargs: True)

    allowed_paths = {
        "/", "/auth/login", "/auth/school-login", "/auth/logout",
        "/school", "/health", "/api/status",
    }
    checked = []
    blueprint_routes = set()

    for rule in app_module.app.url_map.iter_rules():
        path = re.sub(
            r"<(?:(int|float|path|uuid|string):)?([^>]+)>",
            lambda match: (
                "1" if match.group(1) == "int"
                else "1.0" if match.group(1) == "float"
                else "00000000-0000-0000-0000-000000000001"
                if match.group(1) == "uuid"
                else "sample"
            ),
            rule.rule,
        )
        if path in allowed_paths or path.startswith("/static/"):
            continue

        methods = set(rule.methods or ()) - {"HEAD", "OPTIONS"}
        if path.startswith("/api/school/"):
            for method in methods:
                assert (rule.endpoint, method) in SCHOOL_API_ROUTE_METHODS, (
                    f"{method} {path} ({rule.endpoint}) needs explicit school authorization"
                )
            continue

        for method in sorted(methods):
            response = school_session_client.open(
                path,
                method=method,
                json={} if method in {"POST", "PUT", "PATCH", "DELETE"} else None,
            )
            assert response.status_code == 403, (
                f"{method} {path} ({rule.endpoint}) must be denied to school sessions; "
                f"got {response.status_code}: {response.get_data(as_text=True)}"
            )
            assert response.get_json() == {"error": "platform_admin_required"}
            checked.append((method, path, rule.endpoint))
            if rule.endpoint.startswith("dashboard_api."):
                blueprint_routes.add(rule.endpoint)

    assert checked, "expected registered platform routes to be covered"
    assert blueprint_routes, "dashboard_api blueprint routes must be included in coverage"

    registered_school_methods = {
        (rule.endpoint, method)
        for rule in app_module.app.url_map.iter_rules()
        if rule.rule.startswith("/api/school/")
        for method in (set(rule.methods or ()) - {"HEAD", "OPTIONS"})
    }
    assert registered_school_methods == SCHOOL_API_ROUTE_METHODS



def test_school_session_cannot_access_unregistered_school_api_path(school_session_client):
    response = school_session_client.get("/api/school/exports")
    assert response.status_code == 403
    assert response.get_json() == {"error": "school_route_not_authorized"}


def test_school_session_cannot_use_unapproved_method_on_school_api(school_session_client):
    response = school_session_client.post("/api/school/me", json={})
    assert response.status_code == 403
    assert response.get_json() == {"error": "school_route_not_authorized"}
