from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from db import get_connection, init_db
from services.ai_quota import consume_ai_generation_quota


def _seed_institutions(db_path, *institution_ids):
    conn = init_db(str(db_path))
    for institution_id in institution_ids:
        conn.execute(
            "INSERT INTO institutions (id, name, code, type, status) VALUES (?, ?, ?, ?, 'active')",
            (institution_id, f"School {institution_id}", institution_id.upper(), "school"),
        )
    conn.commit()
    conn.close()


def test_daily_and_minute_quotas_are_tenant_scoped_and_reset_by_utc_window(tmp_path):
    db_path = tmp_path / "quota.db"
    _seed_institutions(db_path, "tenant-a", "tenant-b")
    conn = get_connection(str(db_path))
    now = datetime(2026, 10, 30, 12, 34, 10, tzinfo=timezone.utc)
    try:
        first = consume_ai_generation_quota(
            conn, "tenant-a", daily_limit=2, per_minute_limit=1, now=now
        )
        assert first["allowed"] is True
        assert first["daily_remaining"] == 1

        minute_limited = consume_ai_generation_quota(
            conn, "tenant-a", daily_limit=2, per_minute_limit=1, now=now
        )
        assert minute_limited["allowed"] is False
        assert minute_limited["reason"] == "minute_limit"
        assert minute_limited["retry_after"] > 0

        other_tenant = consume_ai_generation_quota(
            conn, "tenant-b", daily_limit=2, per_minute_limit=1, now=now
        )
        assert other_tenant["allowed"] is True

        next_minute = consume_ai_generation_quota(
            conn,
            "tenant-a",
            daily_limit=2,
            per_minute_limit=1,
            now=datetime(2026, 10, 30, 12, 35, 1, tzinfo=timezone.utc),
        )
        assert next_minute["allowed"] is True
        daily_limited = consume_ai_generation_quota(
            conn,
            "tenant-a",
            daily_limit=2,
            per_minute_limit=5,
            now=datetime(2026, 10, 30, 12, 36, 1, tzinfo=timezone.utc),
        )
        assert daily_limited["allowed"] is False
        assert daily_limited["reason"] == "daily_limit"

        next_day = consume_ai_generation_quota(
            conn,
            "tenant-a",
            daily_limit=2,
            per_minute_limit=1,
            now=datetime(2026, 10, 31, 0, 0, 1, tzinfo=timezone.utc),
        )
        assert next_day["allowed"] is True
    finally:
        conn.close()


def test_concurrent_requests_cannot_exceed_tenant_quota(tmp_path):
    db_path = tmp_path / "concurrent-quota.db"
    _seed_institutions(db_path, "tenant-a")

    def consume():
        conn = get_connection(str(db_path))
        try:
            return consume_ai_generation_quota(
                conn,
                "tenant-a",
                daily_limit=3,
                per_minute_limit=20,
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: consume(), range(8)))

    assert sum(result["allowed"] for result in results) == 3
    assert sum(result.get("reason") == "daily_limit" for result in results) == 5


@pytest.mark.parametrize("bad_limit", [True, -1, "not-an-integer", 100_001])
def test_quota_rejects_invalid_daily_limit(tmp_path, bad_limit):
    db_path = tmp_path / "invalid-quota.db"
    _seed_institutions(db_path, "tenant-a")
    conn = get_connection(str(db_path))
    try:
        with pytest.raises(ValueError):
            consume_ai_generation_quota(
                conn, "tenant-a", daily_limit=bad_limit, per_minute_limit=10
            )
    finally:
        conn.close()


def test_quota_fails_closed_for_inactive_or_unknown_tenant(tmp_path):
    db_path = tmp_path / "inactive-quota.db"
    _seed_institutions(db_path, "tenant-a")
    conn = get_connection(str(db_path))
    try:
        conn.execute("UPDATE institutions SET status = 'suspended' WHERE id = 'tenant-a'")
        conn.commit()
        result = consume_ai_generation_quota(
            conn, "tenant-a", daily_limit=10, per_minute_limit=2
        )
        assert result["allowed"] is False
        assert result["reason"] == "institution_inactive"

        unknown = consume_ai_generation_quota(
            conn, "missing-tenant", daily_limit=10, per_minute_limit=2
        )
        assert unknown["allowed"] is False
        assert unknown["reason"] == "institution_inactive"
    finally:
        conn.close()


def test_generation_route_enforces_plan_quota_before_provider_call(monkeypatch, tmp_path):
    import app as app_module
    from services.licensing import create_plan, grant_subscription

    db_path = tmp_path / "route-quota.db"
    conn = init_db(str(db_path))
    institution_id = "quota-school"
    conn.execute(
        "INSERT INTO institutions (id, name, code, type, status) VALUES (?, ?, ?, ?, 'active')",
        (institution_id, "Quota School", "QUOTA", "school"),
    )
    plan_id = create_plan(
        conn,
        code="quota-test",
        name="Quota Test",
        entitlements={
            "ai_generation": True,
            "ai_generation_daily_limit": 1,
            "ai_generation_per_minute_limit": 5,
        },
    )
    grant_subscription(
        conn,
        institution_id=institution_id,
        plan_id=plan_id,
        granted_by="test-admin",
        reason="automated quota regression",
    )
    conn.close()

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "1")
    monkeypatch.setenv("ADMIN_USERNAME", "quota-test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password-9284")
    monkeypatch.setenv("AI_PROVIDER", "quota-fake-provider")
    monkeypatch.setattr(app_module, "DB_PATH", str(db_path))
    monkeypatch.setattr(app_module, "allow_request", lambda *args, **kwargs: True)
    app_module.app.config.update(TESTING=True, SECRET_KEY="quota-test-secret-key-32-bytes-minimum")

    class FakeProvider:
        name = "quota-fake-provider"

        def __init__(self):
            self.calls = 0

        def generate_question(self, concept, question_type, difficulty):
            self.calls += 1
            return {
                "content": "What is gravity?",
                "difficulty": difficulty,
                "provider": self.name,
                "verification_status": "ai_generated_unverified",
            }

    provider = FakeProvider()
    monkeypatch.setattr(app_module, "get_default_registry", lambda: {provider.name: provider})

    with app_module.app.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        login = client.post(
            "/auth/login",
            data={
                "username": "quota-test-admin",
                "password": "test-only-password-9284",
                "csrf_token": csrf,
            },
        )
        assert login.status_code == 302
        with client.session_transaction() as session:
            csrf = session["csrf_token"]

        payload = {
            "institution_id": institution_id,
            "concept": {"name": "gravity"},
            "question_type": "short_answer",
            "difficulty": "easy",
        }
        first = client.post(
            "/api/questions/generate",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        second = client.post(
            "/api/questions/generate",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )

    assert first.status_code == 200
    assert first.headers["X-AI-Daily-Remaining"] == "0"
    assert second.status_code == 429
    assert second.get_json()["error"] == "ai_generation_quota_exceeded"
    assert int(second.headers["Retry-After"]) > 0
    assert provider.calls == 1
