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
    args = parser.parse_args()

    result = (
        backup_database(args.database, args.output)
        if args.command == "backup"
        else verify_backup(args.backup)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
