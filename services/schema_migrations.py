"""Serialized, additive SQLite schema migrations.

The base schema remains an idempotent bootstrap. Changes that must upgrade
existing databases are recorded here and applied under one SQLite write lock.
"""
from __future__ import annotations

import sqlite3

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
            UNIQUE (institution_id, idempotency_key_hash)
        )
        """
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_tenant_leave_keys_request
           ON tenant_leave_request_keys(leave_request_id)"""
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS tenant_leave_request_keys_no_update
        BEFORE UPDATE ON tenant_leave_request_keys BEGIN
            SELECT RAISE(ABORT, 'tenant leave request keys are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS tenant_leave_request_keys_no_delete
        BEFORE DELETE ON tenant_leave_request_keys BEGIN
            SELECT RAISE(ABORT, 'tenant leave request keys are append-only');
        END
        """
    )


_MIGRATIONS = (
    (1, "school_identity_password_hash", _add_school_password_hash),
    (2, "tenant_background_jobs", _add_tenant_job_schema),
    (3, "tenant_leave_request_idempotency", _add_tenant_leave_request_keys),
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
