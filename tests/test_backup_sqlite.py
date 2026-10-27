import pytest

from db import get_connection, init_db
from tools.backup_sqlite import backup_database, verify_backup


def test_backup_is_consistent_checksummed_and_readable(tmp_path):
    source = tmp_path / "platform.db"
    conn = init_db(str(source))
    conn.execute(
        "INSERT INTO institutions (id, name, code, type, status) VALUES (?, ?, ?, ?, ?)",
        ("school-1", "Backup School", "BACKUP", "school", "active"),
    )
    conn.commit()
    conn.close()

    target = tmp_path / "backups" / "platform-backup.sqlite3"
    result = backup_database(source, target)

    assert result["integrity"] == "ok"
    assert result["foreign_key_violations"] == 0
    assert result["table_count"] > 0
    assert result["bytes"] > 0
    assert len(result["sha256"]) == 64
    assert (tmp_path / "backups" / "platform-backup.sqlite3.sha256").is_file()

    verified = verify_backup(target)
    assert verified["sha256"] == result["sha256"]
    restored = get_connection(str(target))
    try:
        row = restored.execute(
            "SELECT name FROM institutions WHERE id = ?", ("school-1",)
        ).fetchone()
        assert row["name"] == "Backup School"
    finally:
        restored.close()


def test_backup_verification_detects_checksum_mismatch(tmp_path):
    source = tmp_path / "source.db"
    init_db(str(source)).close()
    target = tmp_path / "backup.sqlite3"
    backup_database(source, target)

    with target.open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_backup(target)


def test_backup_never_overwrites_source_database(tmp_path):
    source = tmp_path / "source.db"
    init_db(str(source)).close()

    with pytest.raises(ValueError, match="must not overwrite"):
        backup_database(source, source)


def test_backup_rejects_missing_source(tmp_path):
    with pytest.raises(ValueError, match="existing on-disk"):
        backup_database(tmp_path / "missing.db", tmp_path / "backup.sqlite3")


def test_backup_verification_requires_checksum_sidecar(tmp_path):
    source = tmp_path / "source.db"
    init_db(str(source)).close()
    target = tmp_path / "backup.sqlite3"
    backup_database(source, target)
    (tmp_path / "backup.sqlite3.sha256").unlink()

    with pytest.raises(ValueError, match="checksum sidecar is missing"):
        verify_backup(target)
