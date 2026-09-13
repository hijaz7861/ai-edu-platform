import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db  # noqa: E402
from services import core, attendance  # noqa: E402


class TestAttendance(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        core.seed_default_roles(self.conn)
        self.inst = core.create_institution(self.conn, "Springfield Academy", "SPR")
        self.campus = core.create_campus(self.conn, self.inst, "Main", "SPR-MAIN")
        user = core.create_user(self.conn, self.inst, "t1@springfield.edu")
        self.teacher = core.create_teacher(self.conn, user, self.inst, "EMP-001")
        self.policy = attendance.create_policy(
            self.conn, self.inst, "Standard",
            working_hours_start="08:00", working_hours_end="15:00",
            grace_period_minutes=10, late_threshold_minutes=20,
            early_departure_threshold_minutes=15,
        )

    def tearDown(self):
        self.conn.close()

    def test_on_time_check_in_within_grace(self):
        _, status = attendance.check_in(self.conn, self.teacher, self.inst, self.policy, "08:07")
        self.assertEqual(status, "on_time")

    def test_late_check_in_past_threshold(self):
        _, status = attendance.check_in(self.conn, self.teacher, self.inst, self.policy, "08:35")
        self.assertEqual(status, "late")

    def test_early_check_out_past_threshold(self):
        _, status = attendance.check_out(self.conn, self.teacher, self.inst, self.policy, "14:30")
        self.assertEqual(status, "early")

    def test_on_time_check_out(self):
        _, status = attendance.check_out(self.conn, self.teacher, self.inst, self.policy, "15:05")
        self.assertEqual(status, "on_time")

    def test_correction_never_mutates_original_event(self):
        event_id, orig_status = attendance.check_in(self.conn, self.teacher, self.inst, self.policy, "09:15")
        self.assertEqual(orig_status, "late")

        before = dict(self.conn.execute("SELECT * FROM attendance_events WHERE id = ?", (event_id,)).fetchone())

        correction_id = attendance.request_correction(
            self.conn, event_id, self.teacher,
            {"event_type": "check_in", "timestamp": "08:05"},
            reason="Device clock was wrong",
        )
        new_event_id = attendance.approve_correction(self.conn, correction_id, reviewer_id="principal-1",
                                                       institution_id=self.inst, policy_id=self.policy)

        after = dict(self.conn.execute("SELECT * FROM attendance_events WHERE id = ?", (event_id,)).fetchone())
        self.assertEqual(before, after, "original event must be byte-for-byte unchanged")

        new_event = self.conn.execute("SELECT * FROM attendance_events WHERE id = ?", (new_event_id,)).fetchone()
        self.assertEqual(new_event["status"], "on_time")
        self.assertNotEqual(new_event_id, event_id)

        # both rows still exist — nothing was deleted
        all_events = self.conn.execute(
            "SELECT * FROM attendance_events WHERE teacher_id = ?", (self.teacher,)
        ).fetchall()
        self.assertEqual(len(all_events), 2)

    def test_multi_level_leave_requires_both_approvals(self):
        leave_type = attendance.create_leave_type(self.conn, self.inst, "Sick Leave", 10, multi_level=True)
        leave_id = attendance.request_leave(
            self.conn, self.teacher, leave_type, "2026-10-01", "2026-10-02", "Flu"
        )

        result = attendance.decide_leave_approval(self.conn, leave_id, level=1, approver_id="dept-head-1", decision="approved")
        self.assertEqual(result, "pending", "must stay pending until level 2 also approves")

        result = attendance.decide_leave_approval(self.conn, leave_id, level=2, approver_id="principal-1", decision="approved")
        self.assertEqual(result, "approved")

        row = self.conn.execute("SELECT status FROM leave_requests WHERE id = ?", (leave_id,)).fetchone()
        self.assertEqual(row["status"], "approved")

    def test_single_level_leave_rejected_stops_immediately(self):
        leave_type = attendance.create_leave_type(self.conn, self.inst, "Casual Leave", 5, multi_level=False)
        leave_id = attendance.request_leave(
            self.conn, self.teacher, leave_type, "2026-11-01", "2026-11-01", "Personal"
        )
        result = attendance.decide_leave_approval(self.conn, leave_id, level=1, approver_id="principal-1", decision="rejected")
        self.assertEqual(result, "rejected")


if __name__ == "__main__":
    unittest.main()
