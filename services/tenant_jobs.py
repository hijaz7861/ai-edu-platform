"""Persistent tenant-bound job queue with leased worker claims."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from services import identity

JOB_PERMISSIONS = {"tenant_health_check": "manage_institution"}
RESOURCE_TABLES = {
    "books": "books",
    "classes": "classes",
    "exam_blueprints": "exam_blueprints",
    "question_banks": "question_banks",
}
MAX_PAYLOAD_BYTES = 8192
MAX_RESULT_BYTES = 32768
DEFAULT_MAX_ATTEMPTS = 3


class JobAuthorizationError(PermissionError):
    pass


class IdempotencyConflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


class RetryableJobError(Exception):
    pass


class PermanentJobError(Exception):
    pass


@dataclass(frozen=True)
class TenantJobContext:
    job_id: str
    institution_id: str
    actor_id: str
    job_type: str
    payload: dict


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS tenant_jobs (
        id TEXT PRIMARY KEY,
        institution_id TEXT NOT NULL REFERENCES institutions(id) ON DELETE RESTRICT,
        actor_id TEXT NOT NULL,
        job_type TEXT NOT NULL CHECK (job_type IN ('tenant_health_check')),
        payload_json TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        idempotency_key_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 5),
        available_at TEXT NOT NULL,
        locked_by TEXT,
        lease_expires_at TEXT,
        result_json TEXT,
        last_error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        UNIQUE (institution_id, idempotency_key_hash),
        CHECK (
            (status = 'running' AND locked_by IS NOT NULL AND lease_expires_at IS NOT NULL)
            OR
            (status <> 'running' AND locked_by IS NULL AND lease_expires_at IS NULL)
        )
    );
    CREATE INDEX IF NOT EXISTS idx_tenant_jobs_claim
        ON tenant_jobs(status, available_at, created_at);
    CREATE INDEX IF NOT EXISTS idx_tenant_jobs_tenant_created
        ON tenant_jobs(institution_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_tenant_jobs_lease
        ON tenant_jobs(status, lease_expires_at);
    CREATE TABLE IF NOT EXISTS tenant_job_events (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES tenant_jobs(id) ON DELETE RESTRICT,
        institution_id TEXT NOT NULL REFERENCES institutions(id) ON DELETE RESTRICT,
        actor_id TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN (
            'job_queued', 'job_started', 'job_retry_scheduled',
            'job_succeeded', 'job_failed'
        )),
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tenant_job_events_job
        ON tenant_job_events(job_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_tenant_job_events_tenant
        ON tenant_job_events(institution_id, created_at);
    CREATE TRIGGER IF NOT EXISTS tenant_job_events_no_update
    BEFORE UPDATE ON tenant_job_events BEGIN
        SELECT RAISE(ABORT, 'tenant job events are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS tenant_job_events_no_delete
    BEFORE DELETE ON tenant_job_events BEGIN
        SELECT RAISE(ABORT, 'tenant job events are append-only');
    END;
    """)
    conn.commit()


def _normalise_payload(payload):
    if not isinstance(payload, dict) or set(payload) - {"resources"}:
        raise ValueError("unsupported_job_payload")
    resources = payload.get("resources", sorted(RESOURCE_TABLES))
    if (not isinstance(resources, list) or not resources
            or len(resources) > len(RESOURCE_TABLES)
            or any(not isinstance(v, str) or v not in RESOURCE_TABLES for v in resources)
            or len(set(resources)) != len(resources)):
        raise ValueError("invalid_job_resources")
    normalized = {"resources": sorted(resources)}
    if len(_json(normalized).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("job_payload_too_large")
    return normalized


def _key_hash(value):
    if (not isinstance(value, str) or not 8 <= len(value) <= 128
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", value)):
        raise ValueError("valid_idempotency_key_required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_job_authority(conn, institution_id, actor_id, job_type):
    if not identity.active_tenant_identity(
        conn, user_id=actor_id, institution_id=institution_id
    ):
        raise JobAuthorizationError("active_tenant_identity_required")
    permissions = identity.institution_permissions(
        conn, user_id=actor_id, institution_id=institution_id
    )
    required = JOB_PERMISSIONS[job_type]
    if "*" not in permissions and required not in permissions:
        raise JobAuthorizationError("job_permission_denied")


def _event(conn, job_id, institution_id, actor_id, event_type, details=None):
    conn.execute(
        """INSERT INTO tenant_job_events
           (id, job_id, institution_id, actor_id, event_type, detail_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (uuid.uuid4().hex, job_id, institution_id, actor_id, event_type,
         _json(details or {}), _utcnow()),
    )


def enqueue_job(conn, *, institution_id, actor_id, job_type, payload, idempotency_key):
    if conn.in_transaction:
        raise ValueError("connection_has_open_transaction")
    if not isinstance(institution_id, str) or not institution_id.strip():
        raise JobAuthorizationError("active_tenant_identity_required")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise JobAuthorizationError("active_tenant_identity_required")
    if job_type not in JOB_PERMISSIONS:
        raise ValueError("unsupported_job_type")
    normalized = _normalise_payload(payload)
    payload_json = _json(normalized)
    request_hash = hashlib.sha256(
        (job_type + "\n" + payload_json).encode("utf-8")
    ).hexdigest()
    key_hash = _key_hash(idempotency_key)

    conn.execute("BEGIN IMMEDIATE")
    try:
        _require_job_authority(conn, institution_id, actor_id, job_type)
        existing = conn.execute(
            """SELECT id, request_hash FROM tenant_jobs
               WHERE institution_id = ? AND idempotency_key_hash = ?""",
            (institution_id, key_hash),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict("idempotency_key_conflict")
            conn.commit()
            return existing["id"], False
        now = _utcnow()
        job_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO tenant_jobs
               (id, institution_id, actor_id, job_type, payload_json, request_hash,
                idempotency_key_hash, status, attempt_count, max_attempts,
                available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)""",
            (job_id, institution_id, actor_id, job_type, payload_json, request_hash,
             key_hash, DEFAULT_MAX_ATTEMPTS, now, now, now),
        )
        _event(conn, job_id, institution_id, actor_id, "job_queued",
               {"job_type": job_type})
        conn.commit()
        return job_id, True
    except Exception:
        conn.rollback()
        raise


def get_tenant_job(conn, *, job_id, institution_id):
    if not isinstance(job_id, str) or not isinstance(institution_id, str):
        return None
    row = conn.execute(
        """SELECT id, job_type, status, attempt_count, max_attempts,
                  last_error_code, result_json, created_at, updated_at, completed_at
           FROM tenant_jobs WHERE id = ? AND institution_id = ?""",
        (job_id, institution_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"], "job_type": row["job_type"], "status": row["status"],
        "attempt_count": row["attempt_count"], "max_attempts": row["max_attempts"],
        "last_error_code": row["last_error_code"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def claim_next_job(conn, worker_id, *, lease_seconds=120):
    if conn.in_transaction:
        raise ValueError("connection_has_open_transaction")
    if (not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", worker_id)):
        raise ValueError("invalid_worker_id")
    if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int)
            or not 30 <= lease_seconds <= 300):
        raise ValueError("lease_seconds_must_be_between_30_and_300")

    conn.execute("BEGIN IMMEDIATE")
    try:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        exhausted = conn.execute(
            """SELECT id, institution_id, actor_id, attempt_count
               FROM tenant_jobs
               WHERE status = 'running' AND lease_expires_at <= ?
                 AND attempt_count >= max_attempts""", (now,)
        ).fetchall()
        for item in exhausted:
            conn.execute(
                """UPDATE tenant_jobs
                   SET status='failed', locked_by=NULL, lease_expires_at=NULL,
                       last_error_code='lease_expired_max_attempts',
                       completed_at=?, updated_at=? WHERE id=? AND status='running'""",
                (now, now, item["id"]),
            )
            _event(conn, item["id"], item["institution_id"], item["actor_id"],
                   "job_failed", {"error_code": "lease_expired_max_attempts",
                                  "attempt": item["attempt_count"]})
        row = conn.execute(
            """SELECT * FROM tenant_jobs
               WHERE ((status='queued' AND available_at <= ?)
                   OR (status='running' AND lease_expires_at <= ?))
                 AND attempt_count < max_attempts
               ORDER BY available_at, created_at LIMIT 1""", (now, now)
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        attempt = row["attempt_count"] + 1
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        conn.execute(
            """UPDATE tenant_jobs SET status='running', attempt_count=?,
               locked_by=?, lease_expires_at=?, updated_at=? WHERE id=?""",
            (attempt, worker_id, lease_until, now, row["id"]),
        )
        _event(conn, row["id"], row["institution_id"], row["actor_id"],
               "job_started", {"attempt": attempt})
        claimed = dict(conn.execute(
            "SELECT * FROM tenant_jobs WHERE id=?", (row["id"],)
        ).fetchone())
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise


def _owned_job(conn, job_id, worker_id):
    return conn.execute(
        """SELECT * FROM tenant_jobs
           WHERE id=? AND status='running' AND locked_by=?
             AND lease_expires_at > ?""",
        (job_id, worker_id, _utcnow()),
    ).fetchone()


def _finish_success(conn, *, job_id, worker_id, result_json):
    if conn.in_transaction:
        conn.rollback()
        raise ValueError("handler_transaction_not_allowed")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _owned_job(conn, job_id, worker_id)
        if row is None:
            raise LeaseLost("worker_lease_lost")
        now = _utcnow()
        conn.execute(
            """UPDATE tenant_jobs SET status='succeeded', result_json=?,
               last_error_code=NULL, locked_by=NULL, lease_expires_at=NULL,
               completed_at=?, updated_at=? WHERE id=?""",
            (result_json, now, now, job_id),
        )
        _event(conn, job_id, row["institution_id"], row["actor_id"],
               "job_succeeded", {"attempt": row["attempt_count"]})
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _finish_failure(conn, *, job_id, worker_id, error_code, retryable=False):
    if conn.in_transaction:
        conn.rollback()
        raise ValueError("handler_transaction_not_allowed")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _owned_job(conn, job_id, worker_id)
        if row is None:
            raise LeaseLost("worker_lease_lost")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        retry = retryable and row["attempt_count"] < row["max_attempts"]
        if retry:
            delay = min(2 ** max(row["attempt_count"] - 1, 0), 60)
            available = (now_dt + timedelta(seconds=delay)).isoformat()
            conn.execute(
                """UPDATE tenant_jobs SET status='queued', available_at=?,
                   last_error_code=?, locked_by=NULL, lease_expires_at=NULL,
                   updated_at=? WHERE id=?""",
                (available, error_code, now, job_id),
            )
            event_type = "job_retry_scheduled"
            details = {"error_code": error_code, "attempt": row["attempt_count"],
                       "delay_seconds": delay}
            status = "queued"
        else:
            conn.execute(
                """UPDATE tenant_jobs SET status='failed', last_error_code=?,
                   locked_by=NULL, lease_expires_at=NULL, completed_at=?,
                   updated_at=? WHERE id=?""",
                (error_code, now, now, job_id),
            )
            event_type = "job_failed"
            details = {"error_code": error_code, "attempt": row["attempt_count"]}
            status = "failed"
        _event(conn, job_id, row["institution_id"], row["actor_id"],
               event_type, details)
        conn.commit()
        return status
    except Exception:
        conn.rollback()
        raise


def run_once(conn, worker_id, handlers, *, lease_seconds=120):
    """Claim and execute at most one job; handler exception text is never persisted."""
    job = claim_next_job(conn, worker_id, lease_seconds=lease_seconds)
    if job is None:
        return None
    job_id = job["id"]
    try:
        _require_job_authority(conn, job["institution_id"], job["actor_id"],
                               job["job_type"])
    except JobAuthorizationError:
        try:
            status = _finish_failure(conn, job_id=job_id, worker_id=worker_id,
                                      error_code="authorization_revoked")
        except LeaseLost:
            status = "lease_lost"
        return {"job_id": job_id, "status": status}

    handler = handlers.get(job["job_type"]) if isinstance(handlers, dict) else None
    if handler is None:
        try:
            status = _finish_failure(conn, job_id=job_id, worker_id=worker_id,
                                      error_code="handler_unavailable")
        except LeaseLost:
            status = "lease_lost"
        return {"job_id": job_id, "status": status}

    try:
        context = TenantJobContext(
            job_id=job_id, institution_id=job["institution_id"],
            actor_id=job["actor_id"], job_type=job["job_type"],
            payload=json.loads(job["payload_json"]),
        )
        result = handler(conn, context)
        if conn.in_transaction:
            conn.rollback()
            status = _finish_failure(conn, job_id=job_id, worker_id=worker_id,
                                      error_code="handler_transaction_not_allowed")
            return {"job_id": job_id, "status": status}
        if not isinstance(result, dict):
            raise PermanentJobError()
        result_json = _json(result)
        if len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise PermanentJobError()
        _finish_success(conn, job_id=job_id, worker_id=worker_id,
                        result_json=result_json)
        return {"job_id": job_id, "status": "succeeded"}
    except RetryableJobError:
        if conn.in_transaction:
            conn.rollback()
        try:
            status = _finish_failure(conn, job_id=job_id, worker_id=worker_id,
                                      error_code="retryable_handler_failure",
                                      retryable=True)
        except LeaseLost:
            status = "lease_lost"
        return {"job_id": job_id, "status": status}
    except PermanentJobError:
        if conn.in_transaction:
            conn.rollback()
        try:
            status = _finish_failure(conn, job_id=job_id, worker_id=worker_id,
                                      error_code="handler_rejected")
        except LeaseLost:
            status = "lease_lost"
        return {"job_id": job_id, "status": status}
    except LeaseLost:
        return {"job_id": job_id, "status": "lease_lost"}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        try:
            status = _finish_failure(conn, job_id=job_id, worker_id=worker_id,
                                      error_code="handler_error")
        except LeaseLost:
            status = "lease_lost"
        return {"job_id": job_id, "status": status}
