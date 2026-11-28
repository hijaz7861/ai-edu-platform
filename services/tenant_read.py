"""Consistent tenant authorization snapshots for paid read endpoints."""
from __future__ import annotations

import os
from contextlib import contextmanager

from services import identity
from services.licensing import feature_allowed


class TenantReadIdentityError(PermissionError):
    """The actor or institution is no longer an active tenant identity."""


class TenantReadPermissionError(PermissionError):
    """The active tenant identity lacks the required institution permission."""


class TenantReadEntitlementError(PermissionError):
    """The tenant does not currently hold the requested paid feature."""


def _entitlements_enforced() -> bool:
    return (
        os.environ.get("APP_ENV", "").strip().lower() == "production"
        or os.environ.get("ENFORCE_ENTITLEMENTS", "0") == "1"
    )


@contextmanager
def tenant_read_transaction(
    conn,
    *,
    user_id: str,
    institution_id: str,
    required_permissions=(),
    feature: str | None = None,
):
    """Keep identity, role, entitlement and the protected SELECT in one snapshot.

    The first identity query establishes SQLite's read snapshot. A revocation
    committed before this transaction is observed and denied; later queries
    in the same request cannot silently read from a different database state.
    Callers must perform only reads inside the context and must not commit it.
    """
    if conn.in_transaction:
        raise ValueError("connection_has_open_transaction")
    if not isinstance(user_id, str) or not user_id.strip():
        raise TenantReadIdentityError("active_tenant_identity_required")
    if not isinstance(institution_id, str) or not institution_id.strip():
        raise TenantReadIdentityError("active_tenant_identity_required")
    if isinstance(required_permissions, (str, bytes)):
        raise ValueError("required_permissions_must_be_a_collection")
    try:
        required = frozenset(required_permissions)
    except TypeError:
        raise ValueError("required_permissions_must_be_a_collection") from None
    if any(not isinstance(value, str) or not value.strip() for value in required):
        raise ValueError("required_permission_invalid")
    if feature is not None and (
        not isinstance(feature, str) or not feature.strip()
    ):
        raise ValueError("feature_invalid")

    conn.execute("BEGIN")
    try:
        if not identity.active_tenant_identity(
            conn, user_id=user_id, institution_id=institution_id
        ):
            raise TenantReadIdentityError("active_tenant_identity_required")

        permissions = identity.institution_permissions(
            conn, user_id=user_id, institution_id=institution_id
        )
        if not isinstance(permissions, (set, frozenset)):
            raise TenantReadPermissionError("permission_denied")
        if required and "*" not in permissions and required.isdisjoint(permissions):
            raise TenantReadPermissionError("permission_denied")

        if (
            feature is not None
            and _entitlements_enforced()
            and not feature_allowed(conn, institution_id, feature)
        ):
            raise TenantReadEntitlementError("subscription_required")

        yield permissions
        if not conn.in_transaction:
            raise RuntimeError("tenant_read_transaction_ended_early")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
