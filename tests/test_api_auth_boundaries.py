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


def test_every_api_route_is_middleware_protected_or_explicitly_public():
    # Prevent a newly registered API route from silently bypassing the
    # centralized authentication middleware. Health status is intentionally
    # public so platform probes can check process liveness.
    import app as app_module

    public_api_routes = {"/api/status"}
    unprotected = []
    for rule in app_module.app.url_map.iter_rules():
        if not rule.rule.startswith("/api/") or rule.rule in public_api_routes:
            continue
        if not rule.rule.startswith(app_module._PROTECTED_API_PREFIXES):
            unprotected.append(rule.rule)

    assert unprotected == []



def test_production_default_denies_non_api_routes_not_explicitly_public(client, monkeypatch):
    # /auth/logout is intentionally outside the API-prefix allowlist. In
    # production, all non-public routes must still require a session.
    monkeypatch.setenv("APP_ENV", "production")
    response = client.post("/auth/logout")
    assert response.status_code == 401
    assert response.get_json() == {"error": "authentication_required"}


@pytest.mark.parametrize("path", ["/health", "/api/status"])
def test_production_keeps_only_explicit_health_routes_public(client, monkeypatch, path):
    monkeypatch.setenv("APP_ENV", "production")
    response = client.get(path)
    assert response.status_code == 200
