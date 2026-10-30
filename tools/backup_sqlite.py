"""Create and verify consistent SQLite backups without overwriting the live database.

This tool creates a local backup only. Production deployments must copy verified
backups to separately managed/off-site storage and periodically rehearse restore.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _check_database(path: Path) -> dict:
    conn = _read_only_connection(path)
    try:
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        integrity = [row[0] for row in integrity_rows]
        if integrity != ["ok"]:
            raise RuntimeError("SQLite integrity check failed: " + "; ".join(integrity))
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"SQLite foreign-key check failed with {len(foreign_key_errors)} violation(s)"
            )
        table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        return {
            "integrity": "ok",
            "foreign_key_violations": 0,
            "table_count": table_count,
        }
    finally:
        conn.close()


def verify_backup(backup_path: str | Path, *, checksum_path: str | Path | None = None) -> dict:
    path = Path(backup_path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("backup file is missing or empty")
    sidecar = Path(checksum_path).expanduser().resolve() if checksum_path else Path(str(path) + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"checksum sidecar is missing: {sidecar}")
    checksum_fields = sidecar.read_text(encoding="utf-8").strip().split()
    if not checksum_fields:
        raise ValueError("checksum sidecar is empty")
    expected = checksum_fields[0]
    actual = _sha256(path)
    if len(expected) != 64 or expected.lower() != actual:
        raise ValueError("backup SHA-256 checksum mismatch")
    result = _check_database(path)
    result.update({"backup": str(path), "sha256": actual, "bytes": path.stat().st_size})
    return result


def backup_database(database_path: str | Path, output_path: str | Path) -> dict:
    source = Path(database_path).expanduser()
    target = Path(output_path).expanduser().resolve()
    if str(database_path) == ":memory:" or not source.is_file():
        raise ValueError("source database must be an existing on-disk SQLite file")
    source = source.resolve()
    if source == target:
        raise ValueError("backup output must not overwrite the source database")
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(prefix=".sqlite-backup-", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        source_conn = sqlite3.connect(str(source), timeout=30)
        target_conn = sqlite3.connect(str(temporary), timeout=30)
        try:
            source_conn.backup(target_conn)
            target_conn.commit()
        finally:
            target_conn.close()
            source_conn.close()

        # Validate before publishing the target path.
        _check_database(temporary)
        os.replace(temporary, target)
        checksum = _sha256(target)
        sidecar = Path(str(target) + ".sha256")
        sidecar_tmp = Path(str(sidecar) + ".tmp")
        sidecar_tmp.write_text(f"{checksum}  {target.name}\n", encoding="utf-8")
        os.replace(sidecar_tmp, sidecar)
        return verify_backup(target)
    finally:
        if temporary.exists():
            temporary.unlink()



def restore_backup(backup_path: str | Path, destination_path: str | Path) -> dict:
    """Restore a verified backup to a new path without overwriting any file.

    The destination and its checksum sidecar are published with no-clobber
    hard links from temporary files in the destination directory. A failed
    sidecar publish rolls back the database link created by this operation.
    """
    source = Path(backup_path).expanduser().resolve()
    target = Path(destination_path).expanduser().resolve()
    if source == target:
        raise ValueError("restore destination must differ from the backup source")

    verified = verify_backup(source)
    sidecar_target = Path(str(target) + ".sha256")
    if target.exists() or sidecar_target.exists():
        raise ValueError("restore destination already exists")

    target.parent.mkdir(parents=True, exist_ok=True)
    db_fd, db_temp_name = tempfile.mkstemp(
        prefix=".sqlite-restore-", suffix=".tmp", dir=target.parent
    )
    os.close(db_fd)
    db_temp = Path(db_temp_name)
    sidecar_fd, sidecar_temp_name = tempfile.mkstemp(
        prefix=".sqlite-restore-checksum-", suffix=".tmp", dir=target.parent
    )
    os.close(sidecar_fd)
    sidecar_temp = Path(sidecar_temp_name)
    published_database = False
    published_sidecar = False

    try:
        with source.open("rb") as source_stream, db_temp.open("wb") as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())

        if _sha256(db_temp) != verified["sha256"]:
            raise ValueError("restored backup checksum mismatch")
        _check_database(db_temp)

        sidecar_temp.write_text(
            f"{verified['sha256']}  {target.name}\\n", encoding="utf-8"
        )
        with sidecar_temp.open("rb") as stream:
            os.fsync(stream.fileno())

        # os.link is atomic and refuses to replace a destination created by
        # another process after the preflight existence check.
        try:
            os.link(db_temp, target)
        except FileExistsError:
            raise ValueError("restore destination already exists") from None
        published_database = True

        try:
            os.link(sidecar_temp, sidecar_target)
        except FileExistsError:
            raise ValueError("restore checksum destination already exists") from None
        published_sidecar = True

        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is not supported on every filesystem. The file
            # data and checksum have already been fsynced before publication.
            pass

        result = verify_backup(target)
        result["restored_to"] = str(target)
        return result
    except Exception:
        if published_sidecar:
            sidecar_target.unlink(missing_ok=True)
        if published_database:
            target.unlink(missing_ok=True)
        raise
    finally:
        db_temp.unlink(missing_ok=True)
        sidecar_temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="create and verify a consistent SQLite backup")
    default_database = os.environ.get(
        "AI_EDU_DB", str(Path(__file__).resolve().parents[1] / "storage" / "platform.db")
    )
    backup_parser.add_argument("--database", default=default_database)
    backup_parser.add_argument("--output", required=True, help="output .sqlite3 backup path")
    verify_parser = subparsers.add_parser("verify", help="verify checksum, SQLite integrity and foreign keys")
    verify_parser.add_argument("--backup", required=True, help="backup file created by this tool")
    restore_parser = subparsers.add_parser("restore", help="restore a verified backup to a new destination")
    restore_parser.add_argument("--backup", required=True, help="verified backup file")
    restore_parser.add_argument("--destination", required=True, help="new database path; existing files are never overwritten")
    args = parser.parse_args()

    if args.command == "backup":
        result = backup_database(args.database, args.output)
    elif args.command == "verify":
        result = verify_backup(args.backup)
    else:
        result = restore_backup(args.backup, args.destination)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
