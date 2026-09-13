import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db  # noqa: E402
from services import core, governance  # noqa: E402


class TestGovernance(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        self.inst = core.create_institution(self.conn, "Springfield Academy", "SPR")

    def tearDown(self):
        self.conn.close()

    def test_gated_action_blocked_until_approved(self):
        executed = {"ran": False}

        def publish_exam():
            executed["ran"] = True
            return "published"

        gate_id = governance.request_approval(
            self.conn, "publish_exam", payload={"exam_id": "exam-1"},
            explanation="Publishing Grade 8 Science midterm to all sections.",
            requested_by="teacher-1",
        )

        with self.assertRaises(governance.NotApproved):
            governance.execute_gated_action(self.conn, gate_id, publish_exam)
        self.assertFalse(executed["ran"], "action must not run before approval")

        governance.decide_approval(self.conn, gate_id, approver_id="principal-1", decision="approved")
        result = governance.execute_gated_action(self.conn, gate_id, publish_exam)
        self.assertEqual(result, "published")
        self.assertTrue(executed["ran"])

    def test_approval_requires_real_explanation(self):
        with self.assertRaises(ValueError):
            governance.request_approval(self.conn, "delete_academic_record", {}, explanation="", requested_by="admin-1")

    def test_rejected_gate_stays_blocked_forever(self):
        gate_id = governance.request_approval(
            self.conn, "delete_academic_record", {"student_id": "s1"},
            explanation="Requesting deletion of duplicate student record.", requested_by="admin-1",
        )
        governance.decide_approval(self.conn, gate_id, approver_id="principal-1", decision="rejected")
        with self.assertRaises(governance.NotApproved):
            governance.execute_gated_action(self.conn, gate_id, lambda: "should not run")

    def test_audit_log_redacts_credential_like_keys(self):
        governance.write_audit(
            self.conn, self.inst, actor_id="user-1", action_type="admin_action",
            entity_type="users", entity_id="user-2",
            after_state={"email": "a@b.com", "api_key": "sk-real-secret-value", "password": "hunter2"},
        )
        row = self.conn.execute("SELECT after_state FROM audit_log ORDER BY timestamp DESC LIMIT 1").fetchone()
        self.assertNotIn("sk-real-secret-value", row["after_state"])
        self.assertNotIn("hunter2", row["after_state"])
        self.assertIn("a@b.com", row["after_state"])  # non-sensitive field preserved

    def test_provider_selection_skips_degraded_provider(self):
        p1 = governance.register_provider(self.conn, "primary-cloud-llm", "cloud", ["question_generation"], priority=10)
        p2 = governance.register_provider(self.conn, "stub-offline-v1", "local", ["question_generation"], priority=100)

        governance.set_provider_status(self.conn, p1, "unavailable")

        chosen = governance.select_provider(self.conn, "question_generation")
        self.assertEqual(chosen["provider_name"], "stub-offline-v1")

        governance.set_provider_status(self.conn, p1, "available")
        chosen_again = governance.select_provider(self.conn, "question_generation")
        self.assertEqual(chosen_again["provider_name"], "primary-cloud-llm")

    def test_retention_deletes_only_rows_past_cutoff(self):
        import datetime
        old_id = "notif-old"
        new_id_ = "notif-new"
        old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)).isoformat()
        new_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO notifications (id, event_type, status, created_at) VALUES (?, 'exam_published', 'sent', ?)",
            (old_id, old_ts),
        )
        self.conn.execute(
            "INSERT INTO notifications (id, event_type, status, created_at) VALUES (?, 'exam_published', 'sent', ?)",
            (new_id_, new_ts),
        )
        self.conn.commit()

        deleted = governance.enforce_notification_retention(self.conn, self.inst, retention_days=365)
        self.assertIn(old_id, deleted)
        self.assertNotIn(new_id_, deleted)

        remaining = {r["id"] for r in self.conn.execute("SELECT id FROM notifications").fetchall()}
        self.assertEqual(remaining, {new_id_})


if __name__ == "__main__":
    unittest.main()
