"""Transactional tenant-bound school class creation."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from services import identity
from services.licensing import feature_allowed


class SchoolClassAuthorizationError(PermissionError):
    """The current school identity lacks authority to create a class."""


class SchoolClassEntitlementError(PermissionError):
    """The institution lacks the active class-management entitlement."""


class SchoolClassCampusNotFound(LookupError):
    """The requested campus is not active and owned by this institution."""


class SchoolClassPersistenceError(RuntimeError):
    """The class and its audit event could not be committed atomically."""


def create_school_class(
    conn, *, institution_id, actor_id, name, academic_year, campus_id=None
):
    """Create a class, optional default campus and audit event in one transaction.

    Tenant, actor, permission and feature entitlement are revalidated after
    BEGIN IMMEDIATE. This prevents a role revocation between middleware and the
    write from authorizing a stale class-creation request.
    """
    if conn.in_transaction:
        raise ValueError("connection_has_open_transaction")
    if not isinstance(institution_id, str) or not institution_id.strip():
        raise SchoolClassAuthorizationError("active_tenant_identity_required")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise SchoolClassAuthorizationError("active_tenant_identity_required")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        raise ValueError("class_name_required")
    if "\x00" in name:
        raise ValueError("invalid_class_name")
    if (
        not isinstance(academic_year, str)
        or not academic_year.strip()
        or len(academic_year.strip()) > 40
        or "\x00" in academic_year
    ):
        raise ValueError("invalid_academic_year")
    if campus_id is not None:
        if not isinstance(campus_id, str) or not campus_id.strip() or len(campus_id) > 128:
            raise ValueError("invalid_campus_id")
        campus_id = campus_id.strip()

    institution_id = institution_id.strip()
    actor_id = actor_id.strip()
    name = name.strip()
    academic_year = academic_year.strip()

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not identity.active_tenant_identity(
            conn, user_id=actor_id, institution_id=institution_id
        ):
            raise SchoolClassAuthorizationError("active_tenant_identity_required")
        permissions = identity.institution_permissions(
            conn, user_id=actor_id, institution_id=institution_id
        )
        if "*" not in permissions and "manage_institution" not in permissions:
            raise SchoolClassAuthorizationError("permission_denied")

        enforcement_enabled = (
            os.environ.get("APP_ENV", "").strip().lower() == "production"
            or os.environ.get("ENFORCE_ENTITLEMENTS", "0") == "1"
        )
        if enforcement_enabled and not feature_allowed(
            conn, institution_id, "class_management"
        ):
            raise SchoolClassEntitlementError("subscription_required")

        active_institution = conn.execute(
            "SELECT id FROM institutions WHERE id = ? AND status = 'active'",
            (institution_id,),
        ).fetchone()
        if active_institution is None:
            raise SchoolClassAuthorizationError("active_institution_required")

        if campus_id is None:
            campus = conn.execute(
                """SELECT id FROM campuses
                   WHERE institution_id = ? AND status = 'active'
                   ORDER BY rowid LIMIT 1""",
                (institution_id,),
            ).fetchone()
            if campus is None:
                campus_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO campuses
                       (id, institution_id, name, code, timezone, status)
                       VALUES (?, ?, 'Main Campus', ?, 'UTC', 'active')""",
                    (campus_id, institution_id, "MAIN-" + institution_id[:8]),
                )
            else:
                campus_id = campus["id"]
        else:
            campus = conn.execute(
                """SELECT id FROM campuses
                   WHERE id = ? AND institution_id = ? AND status = 'active'""",
                (campus_id, institution_id),
            ).fetchone()
            if campus is None:
                raise SchoolClassCampusNotFound("campus_not_in_institution")

        class_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO classes
               (id, institution_id, campus_id, department_id, name,
                academic_year, status)
               VALUES (?, ?, ?, NULL, ?, ?, 'active')""",
            (class_id, institution_id, campus_id, name, academic_year),
        )
        conn.execute(
            """INSERT INTO audit_log
               (id, institution_id, actor_id, action_type, entity_type,
                entity_id, after_state, timestamp)
               VALUES (?, ?, ?, 'school_class_created', 'classes', ?, ?, ?)""",
            (
                uuid.uuid4().hex,
                institution_id,
                actor_id,
                class_id,
                json.dumps(
                    {
                        "campus_id": campus_id,
                        "name": name,
                        "academic_year": academic_year,
                    },
                    sort_keys=True,
                ),
                created_at,
            ),
        )
        conn.commit()
        return class_id, campus_id
    except (
        SchoolClassAuthorizationError,
        SchoolClassEntitlementError,
        SchoolClassCampusNotFound,
        ValueError,
    ):
        conn.rollback()
        raise
    except sqlite3.IntegrityError:
        conn.rollback()
        raise SchoolClassPersistenceError("school_class_persistence_failed") from None
    except Exception:
        conn.rollback()
        raise
