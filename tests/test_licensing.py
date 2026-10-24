import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from db import SCHEMA
from services.licensing import (
    change_subscription_status,
    create_plan,
    feature_allowed,
    get_current_subscription,
    grant_subscription,
)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "nested" / "platform.db"
    from db import init_db
    connection = init_db(str(path))
    connection.execute(
        "INSERT INTO institutions (id, name, code, type) VALUES ('school-1', 'Example School', 'EX1', 'school')"
    )
    connection.commit()
    yield connection
    connection.close()


def test_database_initializer_creates_parent_directory(tmp_path):
    from db import init_db
    path = tmp_path / "missing" / "nested" / "platform.db"
    conn = init_db(str(path))
    assert path.exists()
    conn.close()


def test_free_grant_entitlements_and_audit(conn):
    plan_id = create_plan(
        conn, code="community-free", name="Community Free",
        billing_interval="none", entitlements={"attendance": True, "ai_generation": False},
    )
    subscription_id = grant_subscription(
        conn, institution_id="school-1", plan_id=plan_id,
        access_type="free_grant", granted_by="platform-admin",
        reason="Approved community access",
    )
    current = get_current_subscription(conn, "school-1")
    assert current["id"] == subscription_id
    assert current["access_type"] == "free_grant"
    assert feature_allowed(conn, "school-1", "attendance") is True
    assert feature_allowed(conn, "school-1", "ai_generation") is False
    assert feature_allowed(conn, "school-1", "unknown_feature") is False
    event = conn.execute(
        "SELECT event_type, actor_id, reason FROM subscription_events WHERE subscription_id = ?",
        (subscription_id,),
    ).fetchone()
    assert tuple(event) == ("subscription_granted", "platform-admin", "Approved community access")


def test_no_subscription_or_expired_grant_denies_features(conn):
    plan_id = create_plan(conn, code="trial", name="Trial", entitlements={"reports": True})
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    before = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    grant_subscription(
        conn, institution_id="school-1", plan_id=plan_id, access_type="trial",
        granted_by="admin", reason="Expired trial", starts_at=past, ends_at=before,
    )
    assert feature_allowed(conn, "school-1", "reports") is False
    assert get_current_subscription(conn, "school-1") is None


def test_invalid_plan_and_subscription_grants_are_rejected(conn):
    with pytest.raises(ValueError, match="reason is required"):
        grant_subscription(
            conn, institution_id="school-1", plan_id="missing", granted_by="admin", reason=""
        )
    with pytest.raises(ValueError, match="price_minor"):
        create_plan(conn, code="bad", name="Bad", price_minor=-1)


def test_status_change_is_audited_and_revokes_access(conn):
    plan_id = create_plan(conn, code="standard", name="Standard", entitlements={"reports": True})
    sub_id = grant_subscription(
        conn, institution_id="school-1", plan_id=plan_id,
        access_type="paid", granted_by="billing-webhook",
        reason="Verified subscription activation",
    )
    assert feature_allowed(conn, "school-1", "reports") is True
    change_subscription_status(
        conn, sub_id, status="suspended", actor_id="platform-admin", reason="Manual review",
    )
    assert feature_allowed(conn, "school-1", "reports") is False
    events = conn.execute(
        "SELECT event_type FROM subscription_events WHERE subscription_id = ? ORDER BY created_at, rowid",
        (sub_id,),
    ).fetchall()
    assert [row["event_type"] for row in events] == [
        "subscription_granted", "subscription_status_changed"
    ]


def test_invalid_status_change_does_not_mutate(conn):
    plan_id = create_plan(conn, code="plus", name="Plus")
    sub_id = grant_subscription(
        conn, institution_id="school-1", plan_id=plan_id,
        granted_by="admin", reason="Manual grant",
    )
    with pytest.raises(ValueError, match="unsupported subscription status"):
        change_subscription_status(
            conn, sub_id, status="magic", actor_id="admin", reason="bad status",
        )
    assert get_current_subscription(conn, "school-1") is not None
