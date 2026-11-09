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
    ]
    assert _versions(first) == expected
    assert "password_hash" in {
        row["name"] for row in first.execute("PRAGMA table_info(users)").fetchall()
    }
    assert first.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tenant_jobs'"
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
    ]
    assert all(versions == expected and has_jobs for versions, has_jobs in results)



def test_tenant_leave_request_key_migration_is_append_only(tmp_path):
    conn = init_db(str(tmp_path / "leave-keys.sqlite"))
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tenant_leave_request_keys'"
    ).fetchone()[0] == 1
    triggers = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert "tenant_leave_request_keys_no_update" in triggers
    assert "tenant_leave_request_keys_no_delete" in triggers
    assert _versions(conn)[-1] == (3, "tenant_leave_request_idempotency")
    conn.close()
