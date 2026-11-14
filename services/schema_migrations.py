"""Serialized, additive SQLite schema migrations.

The base schema remains an idempotent bootstrap. Changes that must upgrade
existing databases are recorded here and applied under one SQLite write lock.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from services.tenant_jobs import ensure_schema


def _add_school_password_hash(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "password_hash" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")


def _add_tenant_job_schema(conn: sqlite3.Connection) -> None:
    # ensure_schema uses individual execute calls and does not commit when
    # requested, so queue tables, indexes and append-only triggers are
    # committed atomically with the migration record.
    ensure_schema(conn, commit=False)


def _add_tenant_leave_request_keys(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_leave_request_keys (
            id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL REFERENCES institutions(id) ON DELETE RESTRICT,
            idempotency_key_hash TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            leave_request_id TEXT NOT NULL REFERENCES leave_requests(id) ON DELETE RESTRICT,
            actor_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE (institution_id, idempotency_key_hash)
        )
        """
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_tenant_leave_keys_request
           ON tenant_leave_request_keys(leave_request_id)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_tenant_leave_keys_expiry
           ON tenant_leave_request_keys(institution_id, expires_at)"""
    )


def _upgrade_tenant_leave_key_retention(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(tenant_leave_request_keys)").fetchall()
    }
    if "expires_at" not in columns:
        conn.execute(
            "ALTER TABLE tenant_leave_request_keys ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''"
        )

    # Backfill older v3 rows to a 24-hour retry window. Invalid legacy
    # timestamps expire immediately instead of extending their retention.
    rows = conn.execute(
        "SELECT id, created_at FROM tenant_leave_request_keys WHERE expires_at = ''"
    ).fetchall()
    for row in rows:
        try:
            created = datetime.fromisoformat(row["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            expires = (created + timedelta(hours=24)).isoformat()
        except (TypeError, ValueError):
            expires = "1970-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE tenant_leave_request_keys SET expires_at = ? WHERE id = ?",
            (expires, row["id"]),
        )

    # Idempotency records are operational retry metadata, not immutable audit
    # events. They must be deletable after expiry to honor retention policy.
    conn.execute("DROP TRIGGER IF EXISTS tenant_leave_request_keys_no_update")
    conn.execute("DROP TRIGGER IF EXISTS tenant_leave_request_keys_no_delete")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_tenant_leave_keys_expiry
           ON tenant_leave_request_keys(institution_id, expires_at)"""
    )


def _add_tenant_attendance_event_keys(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_attendance_event_keys (
            id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL REFERENCES institutions(id) ON DELETE RESTRICT,
            idempotency_key_hash TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            attendance_event_id TEXT NOT NULL REFERENCES attendance_events(id) ON DELETE RESTRICT,
            actor_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE (institution_id, idempotency_key_hash)
        )
        """
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_tenant_attendance_event_keys_expiry
           ON tenant_attendance_event_keys(institution_id, expires_at)"""
    )


def _add_teacher_identity_links(conn: sqlite3.Connection) -> None:
    expected_permissions = {"self_attendance", "self_leave_request", "view_own_records"}
    roles = conn.execute(
        "SELECT id, scope_level, permissions FROM roles WHERE name = 'Teacher Portal'"
    ).fetchall()
    if len(roles) > 1:
        raise RuntimeError("teacher_portal_role_conflict")
    if not roles:
        conn.execute(
            "INSERT INTO roles (id, name, scope_level, permissions) VALUES (?, 'Teacher Portal', 'institution', ?)",
            (uuid.uuid4().hex, json.dumps(sorted(expected_permissions))),
        )
    else:
        try:
            permissions = set(json.loads(roles[0]["permissions"]))
        except (TypeError, ValueError):
            permissions = set()
        if roles[0]["scope_level"] != "institution" or not expected_permissions.issubset(permissions):
            raise RuntimeError("teacher_portal_role_conflict")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS teacher_identity_links (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
            teacher_id TEXT NOT NULL UNIQUE REFERENCES teachers(id) ON DELETE RESTRICT,
            institution_id TEXT NOT NULL REFERENCES institutions(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_teacher_identity_links_institution
           ON teacher_identity_links(institution_id, teacher_id)"""
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS teacher_identity_links_scope_insert
        BEFORE INSERT ON teacher_identity_links
        WHEN NOT EXISTS (
            SELECT 1
            FROM users u
            JOIN institutions i ON i.id = NEW.institution_id
            JOIN teachers t ON t.id = NEW.teacher_id
            JOIN role_assignments ra
              ON ra.user_id = u.id AND ra.scope_type = 'institution'
             AND ra.scope_id = NEW.institution_id
            JOIN roles r ON r.id = ra.role_id
            WHERE u.id = NEW.user_id
              AND u.institution_id = NEW.institution_id AND u.status = 'active'
              AND i.status = 'active'
              AND t.institution_id = NEW.institution_id AND t.status = 'active'
              AND t.user_id = NEW.user_id
              AND r.name = 'Teacher Portal' AND r.scope_level = 'institution'
              AND (SELECT COUNT(*) FROM role_assignments extra
                   WHERE extra.user_id = u.id
                     AND extra.scope_type = 'institution'
                     AND extra.scope_id = NEW.institution_id) = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'teacher_identity_scope_mismatch');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS teacher_identity_links_scope_update
        BEFORE UPDATE ON teacher_identity_links
        WHEN NOT EXISTS (
            SELECT 1
            FROM users u
            JOIN institutions i ON i.id = NEW.institution_id
            JOIN teachers t ON t.id = NEW.teacher_id
            JOIN role_assignments ra
              ON ra.user_id = u.id AND ra.scope_type = 'institution'
             AND ra.scope_id = NEW.institution_id
            JOIN roles r ON r.id = ra.role_id
            WHERE u.id = NEW.user_id
              AND u.institution_id = NEW.institution_id AND u.status = 'active'
              AND i.status = 'active'
              AND t.institution_id = NEW.institution_id AND t.status = 'active'
              AND t.user_id = NEW.user_id
              AND r.name = 'Teacher Portal' AND r.scope_level = 'institution'
              AND (SELECT COUNT(*) FROM role_assignments extra
                   WHERE extra.user_id = u.id
                     AND extra.scope_type = 'institution'
                     AND extra.scope_id = NEW.institution_id) = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'teacher_identity_scope_mismatch');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS teacher_identity_links_no_delete
        BEFORE DELETE ON teacher_identity_links
        BEGIN
            SELECT RAISE(ABORT, 'teacher identity links require an audited revocation workflow');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS users_preserve_teacher_link_scope
        BEFORE UPDATE OF institution_id ON users
        WHEN EXISTS (
            SELECT 1 FROM teacher_identity_links l
            WHERE l.user_id = OLD.id AND l.institution_id <> NEW.institution_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'teacher_identity_scope_mismatch');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS teachers_preserve_teacher_link_scope
        BEFORE UPDATE OF institution_id ON teachers
        WHEN EXISTS (
            SELECT 1 FROM teacher_identity_links l
            WHERE l.teacher_id = OLD.id AND l.institution_id <> NEW.institution_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'teacher_identity_scope_mismatch');
        END
        """
    )


_MIGRATIONS = (
    (1, "school_identity_password_hash", _add_school_password_hash),
    (2, "tenant_background_jobs", _add_tenant_job_schema),
    (3, "tenant_leave_request_idempotency", _add_tenant_leave_request_keys),
    (4, "tenant_leave_key_retention_window", _upgrade_tenant_leave_key_retention),
    (5, "tenant_attendance_event_idempotency", _add_tenant_attendance_event_keys),
    (6, "tenant_teacher_identity_links", _add_teacher_identity_links),
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending additive migrations exactly once under BEGIN IMMEDIATE."""
    if conn.in_transaction:
        raise RuntimeError("schema_migration_requires_clean_connection")

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS schema_migrations_no_update
            BEFORE UPDATE ON schema_migrations BEGIN
                SELECT RAISE(ABORT, 'schema migrations are append-only');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS schema_migrations_no_delete
            BEFORE DELETE ON schema_migrations BEGIN
                SELECT RAISE(ABORT, 'schema migrations are append-only');
            END
            """
        )

        applied_rows = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = {row["version"]: row["name"] for row in applied_rows}
        latest_known = _MIGRATIONS[-1][0]
        if applied and max(applied) > latest_known:
            raise RuntimeError("database_schema_version_newer_than_application")

        for version, name, migrate in _MIGRATIONS:
            if version in applied:
                if applied[version] != name:
                    raise RuntimeError("schema_migration_version_conflict")
                continue
            migrate(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (version, name),
            )

        # Preserve fail-closed behavior for legacy case-variant duplicate
        # emails. Never delete or rewrite identities. Reconcile the unique
        # index each startup so it is installed after an operator resolves
        # duplicates, without making that repair a one-time migration.
        duplicates = conn.execute(
            "SELECT lower(email) FROM users "
            "GROUP BY lower(email) HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicates is None:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_ci "
                "ON users(lower(email))"
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
