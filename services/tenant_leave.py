"""Transactional, tenant-bound school leave-request submission."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from services import identity


class LeaveAuthorizationError(PermissionError):
    """The current school identity is not allowed to submit leave requests."""


class LeaveIdempotencyConflict(ValueError):
    """An idempotency key was reused for a different command or actor."""


class LeaveResourceNotFound(LookupError):
    """A teacher, leave type, or substitute is not owned by the authenticated tenant."""

class LeaveDateOverlapError(ValueError):
    """The teacher already has a pending or approved leave for overlapping dates."""


def _required_identifier(value, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(error_code)
    return value.strip()


def _iso_date(value, error_code: str) -> str:
    if not isinstance(value, str):
        raise ValueError(error_code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(error_code) from exc
    if parsed.isoformat() != value:
        raise ValueError(error_code)
    return value


def _idempotency_hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 128
        or re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None
    ):
        raise ValueError("valid_idempotency_key_required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_school_leave_request(
    conn,
    *,
    institution_id,
    actor_id,
    teacher_id,
    leave_type_id,
    start_date,
    end_date,
    reason,
    substitute_teacher_id=None,
    idempotency_key,
    self_service=False,
):
    """Create one staff-assisted or self-service leave request atomically; tenant and actor come from the session.

    The caller must pass the institution and actor from the revalidated school
    session. This function revalidates both identity and institution-scoped
    permissions inside the write transaction, checks every referenced teacher
    and leave type against that same tenant, and stores only a hash of the
    caller-provided idempotency key.
    """
    if conn.in_transaction:
        raise ValueError("connection_has_open_transaction")

    institution_id = _required_identifier(institution_id, "active_tenant_identity_required")
    actor_id = _required_identifier(actor_id, "active_tenant_identity_required")
    teacher_id = _required_identifier(teacher_id, "teacher_id_required")
    leave_type_id = _required_identifier(leave_type_id, "leave_type_id_required")
    start_date = _iso_date(start_date, "invalid_start_date")
    end_date = _iso_date(end_date, "invalid_end_date")
    if end_date < start_date:
        raise ValueError("end_date_before_start_date")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 2000:
        raise ValueError("reason_must_be_between_1_and_2000_characters")
    reason = reason.strip()
    if "\x00" in reason:
        raise ValueError("invalid_reason")
    if substitute_teacher_id is not None:
        substitute_teacher_id = _required_identifier(
            substitute_teacher_id, "invalid_substitute_teacher_id"
        )

    key_hash = _idempotency_hash(idempotency_key)
    canonical = {
        "teacher_id": teacher_id,
        "leave_type_id": leave_type_id,
        "start_date": start_date,
        "end_date": end_date,
        "reason": reason,
        "substitute_teacher_id": substitute_teacher_id,
    }
    request_hash = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not identity.active_tenant_identity(
            conn, user_id=actor_id, institution_id=institution_id
        ):
            raise LeaveAuthorizationError("active_tenant_identity_required")
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
                or "self_leave_request" not in permissions
            ):
                raise LeaveAuthorizationError("teacher_identity_required")
        elif not ({"*", "manage_institution", "approve_leave", "manage_attendance_policy"} & permissions):
            raise LeaveAuthorizationError("leave_request_submission_denied")

        now_dt = datetime.now(timezone.utc)
        created_at = now_dt.isoformat()
        expires_at = (now_dt + timedelta(hours=24)).isoformat()
        # Expire only this tenant's idempotency metadata; the leave request and
        # its audit record remain intact. This bounds retry-key retention.
        conn.execute(
            "DELETE FROM tenant_leave_request_keys WHERE institution_id = ? AND expires_at <= ?",
            (institution_id, created_at),
        )
        replay = conn.execute(
            """SELECT k.actor_id, k.request_hash, lr.id, lr.status
               FROM tenant_leave_request_keys k
               JOIN leave_requests lr ON lr.id = k.leave_request_id
               WHERE k.institution_id = ? AND k.idempotency_key_hash = ?""",
            (institution_id, key_hash),
        ).fetchone()
        if replay is not None:
            if replay["actor_id"] != actor_id or replay["request_hash"] != request_hash:
                raise LeaveIdempotencyConflict("idempotency_key_conflict")
            conn.commit()
            return replay["id"], False, replay["status"]

        teacher = conn.execute(
            """SELECT id FROM teachers
               WHERE id = ? AND institution_id = ? AND status = 'active'""",
            (teacher_id, institution_id),
        ).fetchone()
        leave_type = conn.execute(
            """SELECT id, requires_approval, multi_level_approval
               FROM leave_types WHERE id = ? AND institution_id = ?""",
            (leave_type_id, institution_id),
        ).fetchone()
        if teacher is None or leave_type is None:
            raise LeaveResourceNotFound("leave_resource_not_found")

        if substitute_teacher_id is not None:
            substitute = conn.execute(
                """SELECT id FROM teachers
                   WHERE id = ? AND institution_id = ? AND status = 'active'""",
                (substitute_teacher_id, institution_id),
            ).fetchone()
            if substitute is None:
                raise LeaveResourceNotFound("leave_resource_not_found")

        # Serialize overlap checks with the write transaction. Pending and approved
        # requests reserve dates; rejected requests do not. Date ranges are inclusive.
        overlap = conn.execute(
            """SELECT 1 FROM leave_requests
               WHERE teacher_id = ?
                 AND status IN ('pending', 'approved')
                 AND start_date <= ? AND end_date >= ?
               LIMIT 1""",
            (teacher_id, end_date, start_date),
        ).fetchone()
        if overlap is not None:
            raise LeaveDateOverlapError("leave_dates_overlap")

        # Only an explicit zero disables approval; NULL/corrupt legacy values fail closed.
        requires_approval = leave_type["requires_approval"] != 0
        approval_levels = (
            2 if requires_approval and bool(leave_type["multi_level_approval"])
            else 1 if requires_approval else 0
        )
        status = "pending" if requires_approval else "approved"
        request_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO leave_requests
               (id, teacher_id, leave_type_id, start_date, end_date, reason,
                status, substitute_teacher_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id, teacher_id, leave_type_id, start_date, end_date,
                reason, status, substitute_teacher_id, created_at,
            ),
        )
        for level in range(1, approval_levels + 1):
            conn.execute(
                """INSERT INTO leave_approvals
                   (id, leave_request_id, level, decision)
                   VALUES (?, ?, ?, 'pending')""",
                (uuid.uuid4().hex, request_id, level),
            )

        conn.execute(
            """INSERT INTO tenant_leave_request_keys
               (id, institution_id, idempotency_key_hash, request_hash,
                leave_request_id, actor_id, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex, institution_id, key_hash, request_hash,
                request_id, actor_id, created_at, expires_at,
            ),
        )
        conn.execute(
            """INSERT INTO audit_log
               (id, institution_id, actor_id, action_type, entity_type,
                entity_id, after_state, timestamp)
               VALUES (?, ?, ?, 'school_leave_request_created',
                       'leave_requests', ?, ?, ?)""",
            (
                uuid.uuid4().hex, institution_id, actor_id, request_id,
                json.dumps(
                    {"status": status, "approval_levels": approval_levels},
                    sort_keys=True,
                ),
                created_at,
            ),
        )
        conn.commit()
        return request_id, True, status
    except Exception:
        conn.rollback()
        raise


class LeaveApprovalConflict(ValueError):
    """The leave request or ordered approval state cannot accept this decision."""


def decide_school_leave_approval(
    conn,
    *,
    institution_id,
    actor_id,
    leave_request_id,
    decision,
    comments=None,
):
    """Record one ordered, tenant-bound leave approval decision atomically.

    A retry by the same approver with the same decision/comments is idempotent.
    Each approval level must be handled by a distinct approver, and the request
    creator cannot approve their own submission.
    """
    if conn.in_transaction:
        raise ValueError("connection_has_open_transaction")

    institution_id = _required_identifier(
        institution_id, "active_tenant_identity_required"
    )
    actor_id = _required_identifier(actor_id, "active_tenant_identity_required")
    leave_request_id = _required_identifier(
        leave_request_id, "leave_request_id_required"
    )
    if not isinstance(decision, str) or decision not in {"approved", "rejected"}:
        raise ValueError("invalid_leave_decision")
    if comments is None:
        comments = ""
    if not isinstance(comments, str) or len(comments.strip()) > 2000:
        raise ValueError("comments_must_be_at_most_2000_characters")
    comments = comments.strip()
    if "\x00" in comments:
        raise ValueError("invalid_comments")

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not identity.active_tenant_identity(
            conn, user_id=actor_id, institution_id=institution_id
        ):
            raise LeaveAuthorizationError("active_tenant_identity_required")
        permissions = identity.institution_permissions(
            conn, user_id=actor_id, institution_id=institution_id
        )
        if not ({"*", "approve_leave"} & permissions):
            raise LeaveAuthorizationError("leave_approval_denied")

        leave_request = conn.execute(
            """SELECT lr.id, lr.status
               FROM leave_requests lr
               JOIN teachers t ON t.id = lr.teacher_id
               JOIN leave_types lt ON lt.id = lr.leave_type_id
               WHERE lr.id = ?
                 AND t.institution_id = ?
                 AND lt.institution_id = ?""",
            (leave_request_id, institution_id, institution_id),
        ).fetchone()
        if leave_request is None:
            raise LeaveResourceNotFound("leave_resource_not_found")

        approvals = conn.execute(
            """SELECT id, level, approver_id, decision, comments
               FROM leave_approvals
               WHERE leave_request_id = ?
               ORDER BY level ASC, id ASC""",
            (leave_request_id,),
        ).fetchall()
        if not approvals:
            raise LeaveApprovalConflict("leave_request_has_no_approval_steps")

        levels = [row["level"] for row in approvals]
        if levels != list(range(1, len(approvals) + 1)):
            raise LeaveApprovalConflict("leave_approval_sequence_invalid")
        if any(row["decision"] not in {"pending", "approved", "rejected"} for row in approvals):
            raise LeaveApprovalConflict("leave_approval_state_invalid")

        # A retry of a previously committed decision returns the current
        # aggregate state without writing another audit event.
        replay = next(
            (
                row for row in approvals
                if row["approver_id"] == actor_id
                and row["decision"] == decision
                and (row["comments"] or "") == comments
            ),
            None,
        )
        if replay is not None:
            conn.commit()
            return {
                "id": leave_request_id,
                "decision": decision,
                "level": replay["level"],
                "status": leave_request["status"],
            }

        if leave_request["status"] != "pending":
            raise LeaveApprovalConflict("leave_request_not_pending")

        creator = conn.execute(
            """SELECT actor_id
               FROM audit_log
               WHERE institution_id = ?
                 AND action_type = 'school_leave_request_created'
                 AND entity_type = 'leave_requests'
                 AND entity_id = ?
               ORDER BY timestamp ASC, id ASC
               LIMIT 1""",
            (institution_id, leave_request_id),
        ).fetchone()
        if creator is None:
            # Legacy requests without a recorded creator cannot prove separation
            # of duties, so do not allow a school-session decision on them.
            raise LeaveApprovalConflict("leave_request_creator_unknown")
        if creator["actor_id"] == actor_id:
            raise LeaveAuthorizationError("self_approval_not_allowed")

        if any(row["approver_id"] == actor_id for row in approvals):
            raise LeaveApprovalConflict("approver_already_used_for_request")

        first_pending = next(
            (index for index, row in enumerate(approvals) if row["decision"] == "pending"),
            None,
        )
        if first_pending is None:
            raise LeaveApprovalConflict("leave_approval_already_complete")
        if any(row["decision"] != "approved" for row in approvals[:first_pending]):
            raise LeaveApprovalConflict("prior_approval_level_incomplete")
        if any(row["decision"] != "pending" for row in approvals[first_pending + 1:]):
            raise LeaveApprovalConflict("future_approval_level_already_decided")

        current = approvals[first_pending]
        decided_at = datetime.now(timezone.utc).isoformat()
        changed = conn.execute(
            """UPDATE leave_approvals
               SET decision = ?, approver_id = ?, decided_at = ?, comments = ?
               WHERE id = ? AND decision = 'pending'""",
            (decision, actor_id, decided_at, comments, current["id"]),
        )
        if changed.rowcount != 1:
            raise LeaveApprovalConflict("leave_approval_changed_concurrently")

        if decision == "rejected":
            status = "rejected"
        else:
            remaining = conn.execute(
                """SELECT COUNT(*) FROM leave_approvals
                   WHERE leave_request_id = ? AND decision != 'approved'""",
                (leave_request_id,),
            ).fetchone()[0]
            status = "approved" if remaining == 0 else "pending"

        updated = conn.execute(
            """UPDATE leave_requests SET status = ?
               WHERE id = ? AND status = 'pending'""",
            (status, leave_request_id),
        )
        if updated.rowcount != 1:
            raise LeaveApprovalConflict("leave_request_changed_concurrently")

        conn.execute(
            """INSERT INTO audit_log
               (id, institution_id, actor_id, action_type, entity_type,
                entity_id, before_state, after_state, timestamp)
               VALUES (?, ?, ?, 'school_leave_approval_decided',
                       'leave_requests', ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex,
                institution_id,
                actor_id,
                leave_request_id,
                json.dumps({"status": "pending"}, sort_keys=True),
                json.dumps(
                    {
                        "decision": decision,
                        "level": current["level"],
                        "status": status,
                    },
                    sort_keys=True,
                ),
                decided_at,
            ),
        )
        conn.commit()
        return {
            "id": leave_request_id,
            "decision": decision,
            "level": current["level"],
            "status": status,
        }
    except Exception:
        conn.rollback()
        raise
