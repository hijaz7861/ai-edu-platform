"""Dry-run-first CLI for tenant idempotency-key retention cleanup."""
from __future__ import annotations

import argparse
import json

from db import get_connection
from services.idempotency_retention import purge_expired_idempotency


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report or explicitly delete expired tenant idempotency keys."
    )
    parser.add_argument(
        "--db",
        help="SQLite database path (defaults to AI_EDU_DB / application configuration).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete expired retry-key rows. Without this flag the command is read-only.",
    )
    args = parser.parse_args(argv)

    conn = get_connection(args.db)
    try:
        result = purge_expired_idempotency(conn, apply=args.apply)
    finally:
        conn.close()

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
