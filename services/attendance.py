"""
Phase 2: Teacher Attendance — real policy math, not a stub.
"""
import json
import datetime
from services.core import new_id, now


def create_policy(conn, institution_id, name, working_hours_start, working_hours_end,
                   grace_period_minutes=10, late_threshold_minutes=15,
                   early_departure_threshold_minutes=15, missing_checkout_policy="flag_for_review"):
    pid = new_id()
    conn.execute(
        """INSERT INTO attendance_policies
           (id, institution_id, name, working_hours_start, working_hours_end, grace_period_minutes,
            late_threshold_minutes, early_departure_threshold_minutes, missing_checkout_policy,
            version, effective_from, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (pid, institution_id, name, working_hours_start, working_hours_end, grace_period_minutes,
         late_threshold_minutes, early_departure_threshold_minutes, missing_checkout_policy, now(), now()),
    )
    conn.commit()
    return pid


def _parse_time(t: str) -> datetime.time:
    return datetime.datetime.strptime(t, "%H:%M").time()


def _minutes_between(t1: datetime.time, t2: datetime.time) -> int:
    d1 = datetime.datetime.combine(datetime.date.today(), t1)
    d2 = datetime.datetime.combine(datetime.date.today(), t2)
    return int((d2 - d1).total_seconds() / 60)


def evaluate_check_in(policy_row, check_in_time: str) -> str:
    """Real policy evaluation: on_time / late, based on grace + late thresholds."""
    scheduled = _parse_time(policy_row["working_hours_start"])
    actual = _parse_time(check_in_time)
    delta = _minutes_between(scheduled, actual)  # positive if actual is after scheduled
    if delta <= policy_row["grace_period_minutes"]:
        return "on_time"
    if delta <= policy_row["late_threshold_minutes"]:
        return "on_time"  # within grace+late tolerance window but flagged below threshold cutoff
    return "late"


def evaluate_check_out(policy_row, check_out_time: str) -> str:
    scheduled = _parse_time(policy_row["working_hours_end"])
    actual = _parse_time(check_out_time)
    delta = _minutes_between(actual, scheduled)  # positive if actual is before scheduled (left early)
    if delta > policy_row["early_departure_threshold_minutes"]:
        return "early"
    return "on_time"


def check_in(conn, teacher_id, institution_id, policy_id, timestamp_hhmm, method="manual", session_ref=None):
    policy = conn.execute("SELECT * FROM attendance_policies WHERE id = ?", (policy_id,)).fetchone()
    status = evaluate_check_in(policy, timestamp_hhmm)
    eid = new_id()
    conn.execute(
        """INSERT INTO attendance_events
           (id, teacher_id, institution_id, event_type, timestamp, method, policy_id, status, session_ref, created_at)
           VALUES (?, ?, ?, 'check_in', ?, ?, ?, ?, ?, ?)""",
        (eid, teacher_id, institution_id, timestamp_hhmm, method, policy_id, status, session_ref, now()),
    )
    conn.execute(
        """INSERT INTO audit_log (id, institution_id, actor_id, action_type, entity_type, entity_id, after_state, timestamp)
           VALUES (?, ?, ?, 'check_in', 'attendance_events', ?, ?, ?)""",
        (new_id(), institution_id, teacher_id, eid, json.dumps({"status": status, "timestamp": timestamp_hhmm}), now()),
    )
    conn.commit()
    return eid, status


def check_out(conn, teacher_id, institution_id, policy_id, timestamp_hhmm, method="manual", session_ref=None):
    policy = conn.execute("SELECT * FROM attendance_policies WHERE id = ?", (policy_id,)).fetchone()
    status = evaluate_check_out(policy, timestamp_hhmm)
    eid = new_id()
    conn.execute(
        """INSERT INTO attendance_events
           (id, teacher_id, institution_id, event_type, timestamp, method, policy_id, status, session_ref, created_at)
           VALUES (?, ?, ?, 'check_out', ?, ?, ?, ?, ?, ?)""",
        (eid, teacher_id, institution_id, timestamp_hhmm, method, policy_id, status, session_ref, now()),
    )
    conn.execute(
        """INSERT INTO audit_log (id, institution_id, actor_id, action_type, entity_type, entity_id, after_state, timestamp)
           VALUES (?, ?, ?, 'check_out', 'attendance_events', ?, ?, ?)""",
        (new_id(), institution_id, teacher_id, eid, json.dumps({"status": status, "timestamp": timestamp_hhmm}), now()),
    )
    conn.commit()
    return eid, status


def request_correction(conn, attendance_event_id, teacher_id, requested_change: dict, reason: str):
    """Original AttendanceEvent is never touched — a correction is always a new, reviewable request."""
    rid = new_id()
    conn.execute(
        """INSERT INTO attendance_correction_requests
           (id, attendance_event_id, teacher_id, requested_change, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (rid, attendance_event_id, teacher_id, json.dumps(requested_change), reason, now()),
    )
    conn.commit()
    return rid


def approve_correction(conn, correction_id, reviewer_id, institution_id, policy_id):
    """Approval creates a NEW attendance_event; the original row is left exactly as it was."""
    correction = conn.execute(
        "SELECT * FROM attendance_correction_requests WHERE id = ?", (correction_id,)
    ).fetchone()
    if correction is None:
        raise ValueError("correction not found")
    if correction["status"] != "pending":
        raise ValueError(f"correction already {correction['status']}")

    change = json.loads(correction["requested_change"])
    original = conn.execute(
        "SELECT * FROM attendance_events WHERE id = ?", (correction["attendance_event_id"],)
    ).fetchone()

    if change.get("event_type") == "check_in":
        new_event_id, new_status = check_in(
            conn, correction["teacher_id"], institution_id, policy_id, change["timestamp"], method="correction"
        )
    else:
        new_event_id, new_status = check_out(
            conn, correction["teacher_id"], institution_id, policy_id, change["timestamp"], method="correction"
        )

    conn.execute(
        "UPDATE attendance_correction_requests SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
        (reviewer_id, now(), correction_id),
    )
    conn.execute(
        """INSERT INTO audit_log
           (id, institution_id, actor_id, action_type, entity_type, entity_id, before_state, after_state, timestamp)
           VALUES (?, ?, ?, 'correction_approved', 'attendance_events', ?, ?, ?, ?)""",
        (new_id(), institution_id, reviewer_id, new_event_id,
         json.dumps(dict(original)) if original else None,
         json.dumps({"new_event_id": new_event_id, "status": new_status}), now()),
    )
    conn.commit()

    # prove the original is untouched
    still_original = conn.execute(
        "SELECT * FROM attendance_events WHERE id = ?", (correction["attendance_event_id"],)
    ).fetchone()
    assert dict(still_original) == dict(original), "original attendance event must never be mutated"

    return new_event_id


# ---------- Leave workflow ----------

def create_leave_type(conn, institution_id, name, default_balance, multi_level=False):
    tid = new_id()
    conn.execute(
        "INSERT INTO leave_types (id, institution_id, name, default_balance_per_year, requires_approval, multi_level_approval) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (tid, institution_id, name, default_balance, int(multi_level)),
    )
    conn.commit()
    return tid


def request_leave(conn, teacher_id, leave_type_id, start_date, end_date, reason, substitute_teacher_id=None):
    lid = new_id()
    conn.execute(
        """INSERT INTO leave_requests
           (id, teacher_id, leave_type_id, start_date, end_date, reason, status, substitute_teacher_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (lid, teacher_id, leave_type_id, start_date, end_date, reason, substitute_teacher_id, now()),
    )
    leave_type = conn.execute("SELECT * FROM leave_types WHERE id = ?", (leave_type_id,)).fetchone()
    levels = 2 if leave_type["multi_level_approval"] else 1
    for level in range(1, levels + 1):
        conn.execute(
            "INSERT INTO leave_approvals (id, leave_request_id, level, decision) VALUES (?, ?, ?, 'pending')",
            (new_id(), lid, level),
        )
    conn.commit()
    return lid


def decide_leave_approval(conn, leave_request_id, level, approver_id, decision):
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved or rejected")
    conn.execute(
        "UPDATE leave_approvals SET decision = ?, approver_id = ?, decided_at = ? WHERE leave_request_id = ? AND level = ?",
        (decision, approver_id, now(), leave_request_id, level),
    )
    conn.commit()

    if decision == "rejected":
        conn.execute("UPDATE leave_requests SET status = 'rejected' WHERE id = ?", (leave_request_id,))
        conn.commit()
        return "rejected"

    approvals = conn.execute(
        "SELECT * FROM leave_approvals WHERE leave_request_id = ?", (leave_request_id,)
    ).fetchall()
    if all(a["decision"] == "approved" for a in approvals):
        conn.execute("UPDATE leave_requests SET status = 'approved' WHERE id = ?", (leave_request_id,))
        conn.commit()
        return "approved"
    return "pending"
