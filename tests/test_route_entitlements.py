import uuid

import pytest

import app as app_module


@pytest.fixture
def authenticated_client(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("ADMIN_USERNAME", "entitlement-test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password-9284")
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "0")
    app_module.app.config.update(TESTING=True)

    with app_module.app.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        response = client.post(
            "/auth/login",
            data={
                "username": "entitlement-test-admin",
                "password": "test-only-password-9284",
                "csrf_token": csrf,
            },
        )
        assert response.status_code == 302
        with client.session_transaction() as session:
            client.csrf = session["csrf_token"]
        yield client


def test_production_ai_generation_requires_institution_id(authenticated_client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "0")
    monkeypatch.setattr(
        app_module,
        "get_default_registry",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not run before entitlement check")),
    )

    response = authenticated_client.post(
        "/api/questions/generate",
        json={
            "concept": {"name": "gravity"},
            "question_type": "short_answer",
            "difficulty": "easy",
        },
        headers={"X-CSRF-Token": authenticated_client.csrf},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "institution_id_required"


def test_production_ai_generation_denies_missing_subscription(authenticated_client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "0")
    institution_id = str(uuid.uuid4())
    monkeypatch.setattr(
        app_module,
        "get_default_registry",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not run without entitlement")),
    )

    response = authenticated_client.post(
        "/api/questions/generate",
        json={
            "institution_id": institution_id,
            "concept": {"name": "gravity"},
            "question_type": "short_answer",
            "difficulty": "easy",
        },
        headers={"X-CSRF-Token": authenticated_client.csrf},
    )
    assert response.status_code == 403
    assert response.get_json()["error"] == "subscription_required"


def test_production_class_access_denies_missing_subscription(authenticated_client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "0")
    institution_id = str(uuid.uuid4())

    response = authenticated_client.get(f"/institutions/{institution_id}/classes")
    assert response.status_code == 403
    assert response.get_json()["error"] == "subscription_required"


def test_entitlement_gate_can_be_enabled_in_staging_without_production_mode(authenticated_client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "1")
    institution_id = str(uuid.uuid4())

    response = authenticated_client.get(f"/institutions/{institution_id}/classes")
    assert response.status_code == 403
    assert response.get_json()["error"] == "subscription_required"
