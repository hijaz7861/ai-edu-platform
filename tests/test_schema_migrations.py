"""Versioned SQLite migration and concurrent-startup regressions."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

from db import init_db


def _versions(conn):
    return [
        (row["version"], row["name"])
        for row in conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]


def test_migrations_are_versioned_and_idempotent(tmp_path):
    path = str(tmp_path / "migrations.sqlite")
    first = init_db(path)
    expected = [
        (1, "school_identity_password_hash"),
        (2, "tenant_background_jobs"),
        (3, "tenant_leave_request_idempotency"),
        (4, "tenant_leave_key_retention_window"),
        (5, "tenant_attendance_event_idempotency"),
        (6, "tenant_teacher_identity_links"),
    ]
    assert _versions(first) == expected
    assert "password_hash" in {
        row["name"] for row in first.execute("PRAGMA table_info(users)").fetchall()
    }
    assert first.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tenant_jobs'"
    ).fetchone()[0] == 1
    assert first.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tenant_attendance_event_keys'"
    ).fetchone()[0] == 1
    first.close()

    second = init_db(path)
    assert _versions(second) == expected
    second.close()


def test_legacy_users_table_receives_password_hash_migration(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE institutions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            type TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            institution_id TEXT REFERENCES institutions(id),
            email TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO institutions(id, name, code) VALUES ('tenant-a', 'Tenant A', 'TA');
        INSERT INTO users(id, institution_id, email)
        VALUES ('user-a', 'tenant-a', 'a@example.test');
        """
    )
    legacy.commit()
    legacy.close()

    upgraded = init_db(path)
    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(users)")}
    assert "password_hash" in columns
    assert _versions(upgraded)[0] == (1, "school_identity_password_hash")
    upgraded.close()


def test_legacy_duplicate_casefolded_emails_are_not_destroyed(tmp_path):
    path = str(tmp_path / "duplicate-emails.sqlite")
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE institutions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            type TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            institution_id TEXT REFERENCES institutions(id),
            email TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO institutions(id, name, code) VALUES ('tenant-a', 'Tenant A', 'TA');
        INSERT INTO users(id, institution_id, email)
        VALUES ('user-a', 'tenant-a', 'Admin@example.test'),
               ('user-b', 'tenant-a', 'admin@example.test');
        """
    )
    legacy.commit()
    legacy.close()

    conn = init_db(path)
    index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_users_email_ci'"
    ).fetchone()
    assert index is None
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    conn.execute("DELETE FROM users WHERE id='user-b'")
    conn.commit()
    conn.close()

    repaired = init_db(path)
    assert repaired.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_users_email_ci'"
    ).fetchone() is not None
    repaired.close()


def test_concurrent_database_initialization_applies_each_migration_once(tmp_path):
    path = str(tmp_path / "concurrent-init.sqlite")

    def initialize(_):
        conn = init_db(path)
        versions = _versions(conn)
        job_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tenant_jobs'"
        ).fetchone()
        conn.close()
        return versions, job_table is not None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(initialize, range(4)))

    expected = [
        (1, "school_identity_password_hash"),
        (2, "tenant_background_jobs"),
        (3, "tenant_leave_request_idempotency"),
        (4, "tenant_leave_key_retention_window"),
        (5, "tenant_attendance_event_idempotency"),
        (6, "tenant_teacher_identity_links"),
    ]
    assert all(versions == expected and has_jobs for versions, has_jobs in results)



def test_tenant_leave_request_key_migration_has_expiry_column(tmp_path):
    conn = init_db(str(tmp_path / "leave-keys.sqlite"))
    columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(tenant_leave_request_keys)"
        ).fetchall()
    }
    assert {"idempotency_key_hash", "request_hash", "actor_id", "expires_at"} <= columns
    assert _versions(conn)[-1] == (6, "tenant_teacher_identity_links")
    conn.close()



def test_legacy_v3_leave_key_table_is_upgraded_for_expiry(tmp_path):
    path = str(tmp_path / "legacy-leave-keys.sqlite")
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO schema_migrations(version, name) VALUES
            (1, 'school_identity_password_hash'),
            (2, 'tenant_background_jobs'),
            (3, 'tenant_leave_request_idempotency');
        CREATE TABLE tenant_leave_request_keys (
            id TEXT PRIMARY KEY,
            institution_id TEXT NOT NULL REFERENCES institutions(id),
            idempotency_key_hash TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            leave_request_id TEXT NOT NULL REFERENCES leave_requests(id),
            actor_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(institution_id, idempotency_key_hash)
        );
        CREATE TRIGGER tenant_leave_request_keys_no_update
        BEFORE UPDATE ON tenant_leave_request_keys BEGIN
            SELECT RAISE(ABORT, 'tenant leave request keys are append-only');
        END;
        CREATE TRIGGER tenant_leave_request_keys_no_delete
        BEFORE DELETE ON tenant_leave_request_keys BEGIN
            SELECT RAISE(ABORT, 'tenant leave request keys are append-only');
        END;
        """
    )
    legacy.commit()
    legacy.close()

    upgraded = init_db(path)
    columns = {
        row["name"] for row in upgraded.execute(
            "PRAGMA table_info(tenant_leave_request_keys)"
        ).fetchall()
    }
    assert "expires_at" in columns
    triggers = {
        row["name"] for row in upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert "tenant_leave_request_keys_no_update" not in triggers
    assert "tenant_leave_request_keys_no_delete" not in triggers
    assert _versions(upgraded)[-1] == (6, "tenant_teacher_identity_links")
    upgraded.close()



def test_teacher_identity_link_migration_enforces_tenant_scope_and_role(tmp_path):
    conn = init_db(str(tmp_path / "teacher-identity-links.sqlite"))
    role = conn.execute(
        "SELECT id, scope_level, permissions FROM roles WHERE name='Teacher Portal'"
    ).fetchone()
    assert role is not None
    assert role["scope_level"] == "institution"
    assert {"self_attendance", "self_leave_request", "view_own_records"} <= set(
        __import__("json").loads(role["permissions"])
    )
    assert _versions(conn)[-1] == (6, "tenant_teacher_identity_links")

    conn.execute(
        "INSERT INTO institutions(id, name, code, status) VALUES "
        "('link-tenant-a', 'Link A', 'LINK-A', 'active'), "
        "('link-tenant-b', 'Link B', 'LINK-B', 'active')"
    )
    conn.execute(
        "INSERT INTO users(id, institution_id, email, status) "
        "VALUES ('link-user-a', 'link-tenant-a', 'link-a@example.test', 'active')"
    )
    conn.execute(
        "INSERT INTO teachers(id, user_id, institution_id, employee_code, status) "
        "VALUES ('link-teacher-b', 'link-user-a', 'link-tenant-b', 'TB', 'active')"
    )
    conn.execute(
        "INSERT INTO role_assignments(id, user_id, role_id, scope_type, scope_id) "
        "VALUES ('link-role-a', 'link-user-a', ?, 'institution', 'link-tenant-a')",
        (role["id"],),
    )
    import pytest
    with pytest.raises(sqlite3.IntegrityError, match="teacher_identity_scope_mismatch"):
        conn.execute(
            "INSERT INTO teacher_identity_links(user_id, teacher_id, institution_id) "
            "VALUES ('link-user-a', 'link-teacher-b', 'link-tenant-a')"
        )
    conn.rollback()
    conn.close()


def test_teacher_identity_link_prevents_later_tenant_reassignment(tmp_path):
    conn = init_db(str(tmp_path / "teacher-identity-reassignment.sqlite"))
    role = conn.execute(
        "SELECT id FROM roles WHERE name='Teacher Portal' AND scope_level='institution'"
    ).fetchone()
    conn.execute(
        "INSERT INTO institutions(id, name, code, status) VALUES "
        "('link-tenant-a', 'Link A', 'LINK-A', 'active'), "
        "('link-tenant-b', 'Link B', 'LINK-B', 'active')"
    )
    conn.execute(
        "INSERT INTO users(id, institution_id, email, status) "
        "VALUES ('link-user-a', 'link-tenant-a', 'link-a@example.test', 'active')"
    )
    conn.execute(
        "INSERT INTO teachers(id, user_id, institution_id, employee_code, status) "
        "VALUES ('link-teacher-a', 'link-user-a', 'link-tenant-a', 'TA', 'active')"
    )
    conn.execute(
        "INSERT INTO role_assignments(id, user_id, role_id, scope_type, scope_id) "
        "VALUES ('link-role-a', 'link-user-a', ?, 'institution', 'link-tenant-a')",
        (role["id"],),
    )
    conn.execute(
        "INSERT INTO teacher_identity_links(user_id, teacher_id, institution_id) "
        "VALUES ('link-user-a', 'link-teacher-a', 'link-tenant-a')"
    )
    conn.commit()
    import pytest
    with pytest.raises(sqlite3.IntegrityError, match="teacher_identity_scope_mismatch"):
        conn.execute("UPDATE users SET institution_id='link-tenant-b' WHERE id='link-user-a'")
    with pytest.raises(sqlite3.IntegrityError, match="teacher_identity_scope_mismatch"):
        conn.execute("UPDATE teachers SET institution_id='link-tenant-b' WHERE id='link-teacher-a'")
    with pytest.raises(sqlite3.IntegrityError, match="audited revocation"):
        conn.execute("DELETE FROM teacher_identity_links WHERE user_id='link-user-a'")
    conn.rollback()
    conn.close()
