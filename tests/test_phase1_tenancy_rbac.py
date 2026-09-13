import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db  # noqa: E402
from services import core  # noqa: E402


class TestTenancyAndRBAC(unittest.TestCase):
    def setUp(self):
        # fresh in-memory DB per test — no shared state, no test-order dependence
        self.conn = init_db(":memory:")
        self.roles = core.seed_default_roles(self.conn)

        self.inst_a = core.create_institution(self.conn, "Springfield Academy", "SPR-A")
        self.inst_b = core.create_institution(self.conn, "Shelbyville Institute", "SHL-B")

        self.campus_a = core.create_campus(self.conn, self.inst_a, "Main Campus", "SPR-A-MAIN")
        self.campus_b = core.create_campus(self.conn, self.inst_b, "Main Campus", "SHL-B-MAIN")

        self.class_a = core.create_class(self.conn, self.inst_a, self.campus_a, "Class 8", "2026-2027")
        self.class_b = core.create_class(self.conn, self.inst_b, self.campus_b, "Class 8", "2026-2027")

    def tearDown(self):
        self.conn.close()

    def test_tenant_isolation_across_institutions(self):
        """Institution A must never see Institution B's classes through scoped_query."""
        classes_a = core.scoped_query(self.conn, "classes", self.inst_a)
        classes_b = core.scoped_query(self.conn, "classes", self.inst_b)

        self.assertEqual(len(classes_a), 1)
        self.assertEqual(len(classes_b), 1)
        self.assertNotEqual(classes_a[0]["id"], classes_b[0]["id"])
        # explicit cross-check: A's class id must not appear anywhere in B's result set
        b_ids = {row["id"] for row in classes_b}
        self.assertNotIn(classes_a[0]["id"], b_ids)

    def test_teacher_can_mark_attendance_in_own_class_scope(self):
        user_id = core.create_user(self.conn, self.inst_a, "teacher1@springfield.edu")
        core.assign_role(self.conn, user_id, self.roles["Teacher"], "class", self.class_a)

        # should succeed — real permission, real scope match
        self.assertTrue(
            core.require_permission(self.conn, user_id, "mark_attendance", "class", self.class_a)
        )

    def test_teacher_cannot_act_outside_assigned_scope(self):
        user_id = core.create_user(self.conn, self.inst_a, "teacher2@springfield.edu")
        core.assign_role(self.conn, user_id, self.roles["Teacher"], "class", self.class_a)

        # same user, different class (even within the same institution) — must be denied
        other_class = core.create_class(self.conn, self.inst_a, self.campus_a, "Class 9", "2026-2027")
        with self.assertRaises(core.PermissionDenied):
            core.require_permission(self.conn, user_id, "mark_attendance", "class", other_class)

    def test_teacher_cannot_use_admin_permission(self):
        user_id = core.create_user(self.conn, self.inst_a, "teacher3@springfield.edu")
        core.assign_role(self.conn, user_id, self.roles["Teacher"], "class", self.class_a)

        with self.assertRaises(core.PermissionDenied):
            core.require_permission(self.conn, user_id, "manage_institution", "class", self.class_a)

    def test_platform_super_admin_bypasses_scope(self):
        user_id = core.create_user(self.conn, None, "root@platform.internal")
        core.assign_role(self.conn, user_id, self.roles["Super Admin"], "platform", "GLOBAL")

        # Super Admin should pass a check for a scope it was never explicitly assigned to
        self.assertTrue(
            core.require_permission(self.conn, user_id, "manage_institution", "class", self.class_b)
        )


if __name__ == "__main__":
    unittest.main()
