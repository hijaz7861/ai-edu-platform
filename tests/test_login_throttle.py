from datetime import datetime, timedelta, timezone

import pytest

from db import init_db
from services.login_throttle import clear_failures, is_locked, record_failure


@pytest.fixture
def conn():
    connection = init_db(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def test_fifth_failed_attempt_locks_key_for_cooldown(conn):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = "hashed-login-key"
    for attempt in range(1, 5):
        assert record_failure(conn, key, now=start + timedelta(seconds=attempt)) is False
        assert is_locked(conn, key, now=start + timedelta(seconds=attempt)) is False

    fifth = start + timedelta(seconds=5)
    assert record_failure(conn, key, now=fifth) is True
    assert is_locked(conn, key, now=fifth) is True
    assert is_locked(conn, key, now=fifth + timedelta(minutes=14)) is True


def test_window_expiry_clears_old_failures(conn):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = "another-hashed-key"
    for attempt in range(5):
        record_failure(conn, key, now=start + timedelta(seconds=attempt))
    assert is_locked(conn, key, now=start + timedelta(minutes=16)) is False

    assert record_failure(conn, key, now=start + timedelta(minutes=16, seconds=1)) is False
    row = conn.execute(
        "SELECT failures FROM login_rate_limits WHERE key_hash = ?", (key,)
    ).fetchone()
    assert row["failures"] == 1


def test_successful_login_can_clear_failure_state(conn):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key = "cleared-key"
    for attempt in range(3):
        record_failure(conn, key, now=start + timedelta(seconds=attempt))
    clear_failures(conn, key)
    assert is_locked(conn, key, now=start + timedelta(seconds=4)) is False
    assert conn.execute(
        "SELECT 1 FROM login_rate_limits WHERE key_hash = ?", (key,)
    ).fetchone() is None


def test_login_route_returns_429_after_five_bad_passwords(monkeypatch):
    import app as app_module

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ADMIN_USERNAME", "configured-throttle-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-correct-password")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    app_module.app.config.update(TESTING=True)
    username = "unknown-" + __import__("uuid").uuid4().hex

    with app_module.app.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        statuses = []
        for _ in range(5):
            response = client.post(
                "/auth/login",
                data={
                    "username": username,
                    "password": "incorrect-password",
                    "csrf_token": csrf,
                },
            )
            statuses.append(response.status_code)

    assert statuses == [401, 401, 401, 401, 429]
