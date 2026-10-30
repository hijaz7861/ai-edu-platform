import pytest
from pathlib import Path

from db import get_connection, init_db
from tools.backup_sqlite import backup_database, restore_backup, verify_backup


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



def test_backup_verification_rejects_empty_checksum_sidecar(tmp_path):
    source = tmp_path / "source.db"
    init_db(str(source)).close()
    target = tmp_path / "backup.sqlite3"
    backup_database(source, target)
    (tmp_path / "backup.sqlite3.sha256").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum sidecar is empty"):
        verify_backup(target)



def test_restore_creates_verified_database_at_new_destination(tmp_path):
    source = tmp_path / "source.db"
    conn = init_db(str(source))
    conn.execute(
        "INSERT INTO institutions (id, name, code, type, status) VALUES (?, ?, ?, ?, ?)",
        ("restore-school", "Restore School", "RESTORE", "school", "active"),
    )
    conn.commit()
    conn.close()

    backup = tmp_path / "backups" / "source-backup.sqlite3"
    backup_database(source, backup)
    destination = tmp_path / "restore-drill" / "restored-platform.db"

    result = restore_backup(backup, destination)

    assert result["integrity"] == "ok"
    assert result["foreign_key_violations"] == 0
    assert result["restored_to"] == str(destination.resolve())
    assert (tmp_path / "restore-drill" / "restored-platform.db.sha256").is_file()
    sidecar_text = (tmp_path / "restore-drill" / "restored-platform.db.sha256").read_text(encoding="utf-8")
    assert sidecar_text.endswith("\n")
    assert sidecar_text.split()[0] == result["sha256"]
    verified = verify_backup(destination)
    assert verified["sha256"] == result["sha256"]

    restored = get_connection(str(destination))
    try:
        row = restored.execute(
            "SELECT name FROM institutions WHERE id = ?",
            ("restore-school",),
        ).fetchone()
        assert row["name"] == "Restore School"
    finally:
        restored.close()


def test_restore_never_overwrites_an_existing_destination(tmp_path):
    source = tmp_path / "source.db"
    init_db(str(source)).close()
    backup = tmp_path / "backup.sqlite3"
    backup_database(source, backup)
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"preserve-existing-file")

    with pytest.raises(ValueError, match="restore destination already exists"):
        restore_backup(backup, destination)

    assert destination.read_bytes() == b"preserve-existing-file"


def test_restore_rejects_tampered_backup_before_publishing_destination(tmp_path):
    source = tmp_path / "source.db"
    init_db(str(source)).close()
    backup = tmp_path / "backup.sqlite3"
    backup_database(source, backup)
    with backup.open("ab") as stream:
        stream.write(b"tampered")

    destination = tmp_path / "restored.db"
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore_backup(backup, destination)

    assert not destination.exists()
    assert not Path(str(destination) + ".sha256").exists()
