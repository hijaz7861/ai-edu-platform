from datetime import datetime, timedelta, timezone
import uuid

import pytest

from db import init_db
from services.api_rate_limit import allow_request


@pytest.fixture
def conn():
    connection = init_db(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def test_api_rate_limit_blocks_excess_and_resets_after_window(conn):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = "hashed-api-key"
    assert allow_request(conn, key, limit=2, now=start) is True
    assert allow_request(conn, key, limit=2, now=start + timedelta(seconds=1)) is True
    assert allow_request(conn, key, limit=2, now=start + timedelta(seconds=2)) is False
    assert allow_request(conn, key, limit=2, now=start + timedelta(seconds=59)) is False
    assert allow_request(conn, key, limit=2, now=start + timedelta(seconds=60)) is True


def test_api_rate_limit_rejects_invalid_limit(conn):
    with pytest.raises(ValueError, match="positive integer"):
        allow_request(conn, "key", limit=0)


def test_api_rate_limit_middleware_returns_429(monkeypatch):
    import app as app_module

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("ADMIN_USERNAME", "api-limit-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password-9284")
    monkeypatch.setitem(app_module.app.config, "API_RATE_LIMIT_PER_MINUTE", 2)
    app_module.app.config.update(TESTING=True)
    remote_addr = "test-" + uuid.uuid4().hex

    with app_module.app.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        login = client.post(
            "/auth/login",
            data={
                "username": "api-limit-admin",
                "password": "test-only-password-9284",
                "csrf_token": csrf,
            },
        )
        assert login.status_code == 302

        first = client.get("/providers/select", environ_overrides={"REMOTE_ADDR": remote_addr})
        second = client.get("/providers/select", environ_overrides={"REMOTE_ADDR": remote_addr})
        third = client.get("/providers/select", environ_overrides={"REMOTE_ADDR": remote_addr})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.get_json()["error"] == "rate_limit_exceeded"
    assert third.headers["Retry-After"] == "60"


def test_api_rate_limit_state_is_bounded_and_expired_keys_are_reclaimed(conn, monkeypatch):
    import services.api_rate_limit as limiter

    monkeypatch.setattr(limiter, "MAX_TRACKED_KEYS", 1)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert allow_request(conn, "first-key", limit=10, now=start) is True
    assert allow_request(conn, "second-key", limit=10, now=start + timedelta(seconds=1)) is False
    assert allow_request(conn, "second-key", limit=10, now=start + timedelta(seconds=121)) is True
