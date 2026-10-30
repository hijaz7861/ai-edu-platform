"""Production regression tests for school-session route boundaries.

School identities must remain confined to tenant-scoped school APIs. They must
not reach legacy platform-wide endpoints, but they must be able to log out with
a CSRF-protected POST.
"""
import pytest
from flask import session


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
