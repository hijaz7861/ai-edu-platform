"""Tenant-bound, transactional attendance event writes for school sessions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from services import attendance, identity
from services.licensing import feature_allowed


class AttendanceAuthorizationError(PermissionError):
    """The authenticated school actor is not authorized to record attendance."""


class AttendanceEntitlementError(PermissionError):
    """The tenant does not have the required attendance entitlement."""


class AttendanceResourceNotFound(LookupError):
    """The teacher or attendance policy is not owned by the authenticated tenant."""


class AttendanceIdempotencyConflict(ValueError):
    """An idempotency key was reused by another actor or for another command."""


class AttendanceSequenceConflict(ValueError):
    """The requested event violates the teacher's same-day attendance sequence."""


def _identifier(value, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(error_code)
    return value.strip()


def _idempotency_hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 128
        or re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None
    ):
        raise ValueError("valid_idempotency_key_required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_hhmm(value: str):
    if not isinstance(value, str):
        raise ValueError("invalid_attendance_time")
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise ValueError("invalid_attendance_time") from None
    if parsed.strftime("%H:%M") != value:
        raise ValueError("invalid_attendance_time")
    return parsed


def _validate_policy(policy, today: date) -> None:
    try:
        _parse_hhmm(policy["working_hours_start"])
        _parse_hhmm(policy["working_hours_end"])
        for field in (
            "grace_period_minutes",
            "late_threshold_minutes",
            "early_departure_threshold_minutes",
        ):
            value = policy[field]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1440:
                raise ValueError
        for field, is_start in (("effective_from", True), ("effective_to", False)):
            raw = policy[field]
            if raw is None:
                continue
            if not isinstance(raw, str) or len(raw) < 10:
                raise ValueError
            parsed = date.fromisoformat(raw[:10])
            if parsed.isoformat() != raw[:10]:
                raise ValueError
            if is_start and parsed > today:
                raise AttendanceResourceNotFound("attendance_resource_not_found")
            if not is_start and parsed < today:
                raise AttendanceResourceNotFound("attendance_resource_not_found")
    except AttendanceResourceNotFound:
        raise
    except (KeyError, TypeError, ValueError):
        raise ValueError("attendance_policy_invalid") from None


def record_school_attendance_event(
    conn,
    *,
    institution_id,
    actor_id,
    teacher_id,
    policy_id,
    event_type,
    timestamp_hhmm,
    idempotency_key,
    self_service=False,
):
    """Record a staff-assisted or self-service attendance event with tenant and actor revalidation.

    The server assigns the UTC event day. The caller supplies only the HH:MM
    time; tenant, actor, event method and date cannot be selected by the client.
    """
    if conn.in_transaction:
        raise ValueError("connection_has_open_transaction")

    institution_id = _identifier(institution_id, "active_tenant_identity_required")
    actor_id = _identifier(actor_id, "active_tenant_identity_required")
    teacher_id = _identifier(teacher_id, "teacher_id_required")
    policy_id = _identifier(policy_id, "policy_id_required")
    if not isinstance(event_type, str) or event_type not in {"check_in", "check_out"}:
        raise ValueError("unsupported_attendance_event_type")
    if self_service:
        if timestamp_hhmm is not None:
            raise ValueError("client_attendance_time_not_allowed")
        event_time = None
    else:
        event_time = _parse_hhmm(timestamp_hhmm)
    key_hash = _idempotency_hash(idempotency_key)
    canonical = {
        "teacher_id": teacher_id,
        "policy_id": policy_id,
        "event_type": event_type,
    }
    if not self_service:
        canonical["time"] = timestamp_hhmm
    request_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not identity.active_tenant_identity(
            conn, user_id=actor_id, institution_id=institution_id
        ):
            raise AttendanceAuthorizationError("active_tenant_identity_required")

        permissions = identity.institution_permissions(
            conn, user_id=actor_id, institution_id=institution_id
        )
        if self_service:
            linked_teacher = identity.linked_teacher_for_school_user(
                conn, user_id=actor_id, institution_id=institution_id
            )
            if (
                linked_teacher is None
                or linked_teacher["id"] != teacher_id
                or "self_attendance" not in permissions
            ):
                raise AttendanceAuthorizationError("teacher_identity_required")
        elif not ({"*", "manage_institution", "manage_attendance_policy"} & permissions):
            raise AttendanceAuthorizationError("attendance_write_denied")

        enforcement_enabled = (
            os.environ.get("APP_ENV", "").strip().lower() == "production"
            or os.environ.get("ENFORCE_ENTITLEMENTS", "0") == "1"
        )
        if enforcement_enabled and not feature_allowed(conn, institution_id, "attendance"):
            raise AttendanceEntitlementError("subscription_required")

        now_dt = datetime.now(timezone.utc)
        created_at = now_dt.isoformat()
        event_day = now_dt.date().isoformat()
        if self_service:
            # The teacher cannot backdate or choose an attendance time. The
            # same idempotency payload replays before this timestamp is used.
            timestamp_hhmm = now_dt.strftime("%H:%M")
            event_time = _parse_hhmm(timestamp_hhmm)
        expires_at = (now_dt + timedelta(hours=24)).isoformat()

        # Idempotency rows are retry metadata and are pruned only after expiry.
        conn.execute(
            "DELETE FROM tenant_attendance_event_keys WHERE institution_id = ? AND expires_at <= ?",
            (institution_id, created_at),
        )
        replay = conn.execute(
            """SELECT actor_id, request_hash, attendance_event_id
               FROM tenant_attendance_event_keys
               WHERE institution_id = ? AND idempotency_key_hash = ?""",
            (institution_id, key_hash),
        ).fetchone()
        if replay is not None:
            if replay["actor_id"] != actor_id or replay["request_hash"] != request_hash:
                raise AttendanceIdempotencyConflict("idempotency_key_conflict")
            event = conn.execute(
                """SELECT id, event_type, status FROM attendance_events
                   WHERE id = ? AND institution_id = ? AND teacher_id = ?""",
                (replay["attendance_event_id"], institution_id, teacher_id),
            ).fetchone()
            if event is None:
                raise AttendanceIdempotencyConflict("idempotency_key_conflict")
            conn.commit()
            return {
                "id": event["id"],
                "event_type": event["event_type"],
                "status": event["status"],
                "created": False,
            }

        teacher = conn.execute(
            """SELECT t.id, t.department_id,
                      d.id AS scoped_department_id, d.campus_id
               FROM teachers t
               LEFT JOIN departments d
                 ON d.id = t.department_id AND d.institution_id = t.institution_id
               WHERE t.id = ? AND t.institution_id = ? AND t.status = 'active'""",
            (teacher_id, institution_id),
        ).fetchone()
        if teacher is None or (
            teacher["department_id"] is not None and teacher["scoped_department_id"] is None
        ):
            raise AttendanceResourceNotFound("attendance_resource_not_found")

        policy = conn.execute(
            "SELECT * FROM attendance_policies WHERE id = ? AND institution_id = ?",
            (policy_id, institution_id),
        ).fetchone()
        if policy is None:
            raise AttendanceResourceNotFound("attendance_resource_not_found")
        if (
            policy["department_id"] is not None
            and policy["department_id"] != teacher["department_id"]
        ):
            raise AttendanceResourceNotFound("attendance_resource_not_found")
        if policy["campus_id"] is not None and policy["campus_id"] != teacher["campus_id"]:
            raise AttendanceResourceNotFound("attendance_resource_not_found")
        _validate_policy(policy, now_dt.date())

        # Corrupt legacy rows must not be silently ignored when they could alter
        # the same-day sequence for a tenant-owned teacher.
        inconsistent = conn.execute(
            """SELECT 1 FROM attendance_events
               WHERE teacher_id = ? AND substr(COALESCE(created_at, ''), 1, 10) = ?
                 AND (
                     institution_id IS NULL OR institution_id <> ?
                     OR julianday(created_at) IS NULL
                 )
               LIMIT 1""",
            (teacher_id, event_day, institution_id),
        ).fetchone()
        if inconsistent is not None:
            raise AttendanceSequenceConflict("attendance_history_inconsistent")

        latest = conn.execute(
            """SELECT ae.event_type, ae.timestamp, ae.policy_id,
                      p.institution_id AS policy_institution_id
               FROM attendance_events ae
               LEFT JOIN attendance_policies p ON p.id = ae.policy_id
               WHERE ae.teacher_id = ? AND ae.institution_id = ?
                 AND date(ae.created_at) = ?
               ORDER BY julianday(ae.created_at) DESC, ae.id DESC LIMIT 1""",
            (teacher_id, institution_id, event_day),
        ).fetchone()

        if latest is None:
            if event_type == "check_out":
                raise AttendanceSequenceConflict("attendance_check_in_required")
        else:
            if (
                latest["event_type"] not in {"check_in", "check_out"}
                or latest["policy_id"] is None
                or latest["policy_institution_id"] != institution_id
            ):
                raise AttendanceSequenceConflict("attendance_history_inconsistent")
            try:
                previous_time = _parse_hhmm(latest["timestamp"])
            except ValueError:
                raise AttendanceSequenceConflict("attendance_history_inconsistent") from None

            if event_type == "check_in":
                if latest["event_type"] == "check_in":
                    raise AttendanceSequenceConflict("attendance_already_checked_in")
                if event_time <= previous_time:
                    raise AttendanceSequenceConflict("attendance_time_order_invalid")
            else:
                if latest["event_type"] != "check_in":
                    raise AttendanceSequenceConflict("attendance_check_in_required")
                if event_time <= previous_time:
                    raise AttendanceSequenceConflict("attendance_time_order_invalid")

        try:
            status = (
                attendance.evaluate_check_in(policy, timestamp_hhmm)
                if event_type == "check_in"
                else attendance.evaluate_check_out(policy, timestamp_hhmm)
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("attendance_policy_invalid") from None

        event_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO attendance_events
               (id, teacher_id, institution_id, event_type, timestamp, method,
                policy_id, status, session_ref, created_at)
               VALUES (?, ?, ?, ?, ?, 'school_api', ?, ?, NULL, ?)""",
            (event_id, teacher_id, institution_id, event_type, timestamp_hhmm,
             policy_id, status, created_at),
        )
        conn.execute(
            """INSERT INTO audit_log
               (id, institution_id, actor_id, action_type, entity_type,
                entity_id, after_state, timestamp)
               VALUES (?, ?, ?, ?, 'attendance_events', ?, ?, ?)""",
            (
                uuid.uuid4().hex,
                institution_id,
                actor_id,
                "attendance_" + event_type,
                event_id,
                json.dumps(
                    {
                        "teacher_id": teacher_id,
                        "event_type": event_type,
                        "time": timestamp_hhmm,
                        "status": status,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at,
            ),
        )
        conn.execute(
            """INSERT INTO tenant_attendance_event_keys
               (id, institution_id, idempotency_key_hash, request_hash,
                attendance_event_id, actor_id, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex, institution_id, key_hash, request_hash,
                event_id, actor_id, created_at, expires_at,
            ),
        )
        conn.commit()
        return {
            "id": event_id,
            "event_type": event_type,
            "status": status,
            "created": True,
        }
    except Exception:
        conn.rollback()
        raise
