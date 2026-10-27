"""Regression tests for API authentication boundaries.

These tests exercise the Flask request middleware, not just individual services.
They deliberately verify that representative dashboard and admin endpoints cannot
be used anonymously or by a session that is not the platform administrator.
"""
import pytest


@pytest.fixture
def client(monkeypatch):
    import app as app_module

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard/stats",
        "/api/institutions",
        "/api/classes",
        "/api/question-banks",
        "/api/exams",
        "/api/approval-gates",
        "/api/admin/plans",
        "/api/admin/subscriptions",
    ],
)
def test_dashboard_and_admin_apis_require_authentication(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.get_json() == {"error": "authentication_required"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard/stats",
        "/api/institutions",
        "/api/classes",
        "/api/question-banks",
        "/api/exams",
        "/api/approval-gates",
        "/api/admin/plans",
        "/api/admin/subscriptions",
    ],
)
def test_non_admin_session_cannot_access_dashboard_or_admin_apis(client, path):
    with client.session_transaction() as session:
        session["user"] = "school-user"
        session["role"] = "teacher"

    response = client.get(path)
    assert response.status_code == 403
    assert response.get_json() == {"error": "platform_admin_required"}


def test_admin_api_mutation_requires_csrf_even_in_legacy_demo_mode(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_API_AUTH", "0")
    with client.session_transaction() as session:
        session["user"] = "platform-admin"
        session["role"] = "platform_admin"

    response = client.post(
        "/api/admin/plans",
        json={
            "code": "csrf-test-plan",
            "name": "CSRF Test",
            "price_minor": 0,
            "currency": "USD",
            "billing_interval": "monthly",
            "entitlements": {},
        },
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "csrf_invalid"}
