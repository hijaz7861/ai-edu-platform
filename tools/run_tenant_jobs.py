"""Run one tenant-bound job or poll as a dedicated worker process."""
from __future__ import annotations
import argparse
import json
import os
import time
import uuid
from db import DB_PATH, get_connection, init_db
from services.tenant_job_handlers import HANDLERS
from services.tenant_jobs import run_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=os.environ.get("AI_EDU_DB", DB_PATH))
    parser.add_argument("--worker-id", default="worker-" + uuid.uuid4().hex)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()
    if not 0.25 <= args.poll_interval <= 60:
        parser.error("--poll-interval must be between 0.25 and 60 seconds")
    init_db(args.database).close()
    while True:
        conn = get_connection(args.database)
        try:
            result = run_once(conn, args.worker_id, HANDLERS)
        finally:
            conn.close()
        if result is not None:
            print(json.dumps(result, sort_keys=True))
        elif not args.loop:
            print(json.dumps({"status": "idle"}))
            return 0
        if not args.loop:
            return 0
        if result is None:
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
