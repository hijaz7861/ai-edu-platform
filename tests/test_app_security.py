import pytest

from app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("ADMIN_USERNAME", "security-test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password-9284")
    app.config.update(TESTING=True)

    with app.test_client() as test_client:
        yield test_client


def login(client):
    # Rendering the login page initializes the anonymous session CSRF token.
    response = client.get("/")
    assert response.status_code == 200

    with client.session_transaction() as sess:
        csrf = sess["csrf_token"]

    response = client.post(
        "/auth/login",
        data={
            "username": "security-test-admin",
            "password": "test-only-password-9284",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 302

    with client.session_transaction() as sess:
        assert sess.get("user") == "security-test-admin"
        return sess["csrf_token"]


def test_public_health_endpoints(client):
    for path in ("/health", "/api/status"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.get_json()["status"] == "ok"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/providers/select"),
        ("post", "/institutions"),
        ("post", "/approval-gates"),
    ],
)
def test_business_api_rejects_anonymous_requests(client, method, path):
    response = getattr(client, method)(path, json={})
    assert response.status_code == 401
    assert response.get_json()["error"] == "authentication_required"


def test_authenticated_session_can_access_protected_read_api(client):
    login(client)
    response = client.get("/providers/select")
    assert response.status_code == 200


def test_authenticated_post_rejects_missing_csrf(client):
    login(client)
    response = client.post("/approval-gates", json={})
    assert response.status_code == 400
    assert response.get_json()["error"] == "csrf_invalid"


def test_authenticated_post_accepts_csrf_header(client):
    csrf = login(client)
    response = client.post(
        "/approval-gates",
        json={},
        headers={"X-CSRF-Token": csrf},
    )

    # The CSRF check succeeds, then normal endpoint validation rejects
    # the deliberately incomplete payload without creating a gate.
    assert response.status_code == 400
    assert response.get_json()["error"] == "action_type is required"


def test_legacy_api_mode_is_explicitly_configurable(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_API_AUTH", "0")
    response = client.get("/providers/select")
    assert response.status_code == 200



def test_admin_subscription_api_requires_login(client):
    for path in ("/api/admin/plans", "/api/admin/subscriptions"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.get_json()["error"] == "authentication_required"


def test_admin_subscription_mutations_require_csrf(client):
    login(client)
    response = client.post(
        "/api/admin/plans",
        json={"code": "unauthorized-write", "name": "Should Not Exist"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "csrf_invalid"
