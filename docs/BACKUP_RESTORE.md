# SQLite backup and restore

The CLI creates a consistent local SQLite backup, verifies its checksum and database integrity, and restores only to a new destination path.

## Commands

```bash
python tools/backup_sqlite.py backup --database storage/platform.db --output backups/platform-2026-10-30.sqlite3
python tools/backup_sqlite.py verify --backup backups/platform-2026-10-30.sqlite3
python tools/backup_sqlite.py restore --backup backups/platform-2026-10-30.sqlite3 --destination restore-drill/platform-restored.sqlite3
```

A restore validates the source backup checksum, SQLite integrity and foreign keys before publishing the destination. It uses temporary files and no-clobber publication; an existing destination database or checksum sidecar is never overwritten. The restored database is verified again before the command reports success.

## Release limitations

This is a local restore primitive and an automated restore drill, not a complete disaster-recovery service. Off-site replication, encryption at rest, key management, retention/rotation, access separation, backup monitoring, point-in-time recovery, and a rehearsed restore against an independently deployed staging/production environment remain required before launch. Do not treat a local test as proof of off-site recoverability.
