"""Run one tenant-bound job or poll as a dedicated worker process."""
from __future__ import annotations
import argparse
import json
import os
import signal
import threading
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
    stop_requested = threading.Event()
    previous_handlers = {}

    def request_shutdown(_signum, _frame):
        stop_requested.set()

    # A supervisor's SIGTERM (or an operator's SIGINT) must stop the worker
    # from claiming another job, not kill it between a claim and a commit.
    # Signal registration is only supported from the main thread; this CLI
    # always runs there, while imported/test use remains safe if not.
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
        except ValueError:
            continue

    try:
        init_db(args.database).close()
        while not stop_requested.is_set():
            conn = get_connection(args.database)
            try:
                result = run_once(conn, args.worker_id, HANDLERS)
            finally:
                conn.close()
            if result is not None:
                print(json.dumps(result, sort_keys=True))

            # run_once is synchronous: an in-flight handler is allowed to
            # reach its normal completion/lease check before shutdown.
            if stop_requested.is_set():
                print(json.dumps({"status": "shutdown"}))
                return 0
            if not args.loop:
                if result is None:
                    print(json.dumps({"status": "idle"}))
                return 0
            if result is None:
                # Event.wait wakes immediately when SIGTERM/SIGINT sets the
                # event, unlike time.sleep which may be restarted by Python.
                stop_requested.wait(args.poll_interval)
        print(json.dumps({"status": "shutdown"}))
        return 0
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


if __name__ == "__main__":
    raise SystemExit(main())
