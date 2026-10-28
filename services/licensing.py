"""Subscription and feature-entitlement services for the SaaS platform.

This module records commercial access decisions; it does not process payments.
Payment providers must update subscriptions only through a verified server-side webhook.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone


VALID_INTERVALS = {"monthly", "yearly", "none"}
VALID_STATUSES = {"trialing", "active", "past_due", "suspended", "cancelled", "expired"}
VALID_ACCESS_TYPES = {"paid", "free_grant", "trial"}

# Deliberately explicit lifecycle: terminal states cannot silently regain access.
# A new subscription should be created after cancellation/expiry.
ALLOWED_STATUS_TRANSITIONS = {
    "trialing": {"active", "past_due", "suspended", "cancelled", "expired"},
    "active": {"past_due", "suspended", "cancelled", "expired"},
    "past_due": {"active", "suspended", "cancelled", "expired"},
    "suspended": {"active", "cancelled", "expired"},
    "cancelled": {"expired"},
    "expired": set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return str(uuid.uuid4())


def create_plan(
    conn,
    *,
    code: str,
    name: str,
    description: str = "",
    price_minor: int = 0,
    currency: str = "USD",
    billing_interval: str = "monthly",
    entitlements: dict | None = None,
    is_public: bool = True,
) -> str:
    """Create a plan. Prices use minor currency units (e.g. cents), never floats."""
    code = str(code or "").strip().lower()
    name = str(name or "").strip()
    currency = str(currency or "").strip().upper()
    if not code or len(code) > 60 or not all(c.isalnum() or c in "-_" for c in code):
        raise ValueError("code must be 1-60 letters, digits, '-' or '_'")
    if not name or len(name) > 120:
        raise ValueError("name must be 1-120 characters")
    if isinstance(price_minor, bool) or not isinstance(price_minor, int) or price_minor < 0:
        raise ValueError("price_minor must be a non-negative integer")
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("currency must be a 3-letter code")
    if billing_interval not in VALID_INTERVALS:
        raise ValueError("unsupported billing_interval")
    if entitlements is not None and not isinstance(entitlements, dict):
        raise ValueError("entitlements must be an object")
    plan_id = _id()
    now = _now()
    conn.execute(
        """INSERT INTO platform_plans
           (id, code, name, description, price_minor, currency, billing_interval,
            entitlements_json, is_public, is_active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (plan_id, code, name, str(description or "").strip(), price_minor, currency,
         billing_interval, json.dumps(entitlements or {}, sort_keys=True),
         int(bool(is_public)), now, now),
    )
    conn.commit()
    return plan_id


def grant_subscription(
    conn,
    *,
    institution_id: str,
    plan_id: str,
    access_type: str = "free_grant",
    granted_by: str,
    reason: str,
    starts_at: str | None = None,
    ends_at: str | None = None,
) -> str:
    """Grant a plan to an institution and record an auditable event."""
    if access_type not in VALID_ACCESS_TYPES:
        raise ValueError("unsupported access_type")
    if not str(granted_by or "").strip():
        raise ValueError("granted_by is required")
    if not str(reason or "").strip():
        raise ValueError("reason is required for an auditable subscription grant")
    start = starts_at or _now()
    end = ends_at
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None
    except (TypeError, ValueError):
        raise ValueError("starts_at and ends_at must be ISO-8601 timestamps") from None
    if end_dt and end_dt <= start_dt:
        raise ValueError("ends_at must be later than starts_at")

    institution = conn.execute("SELECT id FROM institutions WHERE id = ?", (institution_id,)).fetchone()
    if not institution:
        raise ValueError("institution not found")
    plan = conn.execute(
        "SELECT id, is_active FROM platform_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if not plan or not plan["is_active"]:
        raise ValueError("plan not found or inactive")

    if get_current_subscription(conn, institution_id):
        raise ValueError("institution already has an active subscription; change its status before granting another")

    now = _now()
    subscription_id = _id()
    conn.execute(
        """INSERT INTO institution_subscriptions
           (id, institution_id, plan_id, status, access_type, starts_at, ends_at,
            granted_by, grant_reason, created_at, updated_at)
           VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
        (subscription_id, institution_id, plan_id, access_type, start, end,
         str(granted_by).strip(), str(reason).strip(), now, now),
    )
    conn.execute(
        """INSERT INTO subscription_events
           (id, subscription_id, institution_id, event_type, actor_id, reason, metadata_json)
           VALUES (?, ?, ?, 'subscription_granted', ?, ?, ?)""",
        (_id(), subscription_id, institution_id, str(granted_by).strip(),
         str(reason).strip(), json.dumps({"access_type": access_type, "plan_id": plan_id})),
    )
    conn.commit()
    return subscription_id


def get_current_subscription(conn, institution_id: str, *, at: str | None = None):
    """Return the current eligible subscription, or None; expired grants never confer access."""
    moment = at or _now()
    return conn.execute(
        """SELECT s.*, p.code AS plan_code, p.name AS plan_name,
                  p.entitlements_json, p.price_minor, p.currency, p.billing_interval
           FROM institution_subscriptions s
           JOIN platform_plans p ON p.id = s.plan_id
           WHERE s.institution_id = ?
             AND s.status IN ('active', 'trialing')
             AND p.is_active = 1
             AND s.starts_at <= ?
             AND (s.ends_at IS NULL OR s.ends_at > ?)
           ORDER BY s.starts_at DESC, s.created_at DESC
           LIMIT 1""",
        (institution_id, moment, moment),
    ).fetchone()


def feature_allowed(conn, institution_id: str, feature: str, *, at: str | None = None) -> bool:
    """Fail closed: absent subscription, malformed entitlements, or missing feature means denied."""
    if not isinstance(feature, str) or not feature.strip():
        return False
    subscription = get_current_subscription(conn, institution_id, at=at)
    if subscription is None:
        return False
    try:
        entitlements = json.loads(subscription["entitlements_json"])
    except (TypeError, ValueError):
        return False
    if not isinstance(entitlements, dict):
        return False
    return entitlements.get(feature.strip()) is True


def change_subscription_status(
    conn, subscription_id: str, *, status: str, actor_id: str, reason: str
) -> None:
    """Change access status with an audit event; payment status must come from verified billing events."""
    if status not in VALID_STATUSES:
        raise ValueError("unsupported subscription status")
    if not str(actor_id or "").strip() or not str(reason or "").strip():
        raise ValueError("actor_id and reason are required")
    row = conn.execute(
        "SELECT id, institution_id, status FROM institution_subscriptions WHERE id = ?",
        (subscription_id,),
    ).fetchone()
    if not row:
        raise ValueError("subscription not found")

    current_status = row["status"]
    if status == current_status:
        # Idempotent retries must not append duplicate state-change events.
        return
    if status not in ALLOWED_STATUS_TRANSITIONS.get(current_status, set()):
        raise ValueError(
            f"invalid subscription status transition: {current_status} -> {status}"
        )

    now = _now()
    conn.execute(
        "UPDATE institution_subscriptions SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, subscription_id),
    )
    conn.execute(
        """INSERT INTO subscription_events
           (id, subscription_id, institution_id, event_type, actor_id, reason, metadata_json)
           VALUES (?, ?, ?, 'subscription_status_changed', ?, ?, ?)""",
        (_id(), subscription_id, row["institution_id"], actor_id.strip(),
         reason.strip(), json.dumps({"status": status})),
    )
    conn.commit()
