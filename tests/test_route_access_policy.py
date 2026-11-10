"""Fail-closed route and blueprint authorization inventory regressions."""
import pytest

import app as app_module


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
