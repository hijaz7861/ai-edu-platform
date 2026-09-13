import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db  # noqa: E402
from services import core, questions, exams  # noqa: E402


class TestExamGenerator(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        self.inst = core.create_institution(self.conn, "Springfield Academy", "SPR")

        # seed a real, verified question pool: 5 MCQ + 3 short_answer
        self.mcq_ids = []
        for i in range(5):
            qid = questions.create_question(
                self.conn, "mcq", f"Real MCQ question number {i} about photosynthesis?",
                class_id="c8", subject_id="science", verification_status="verified",
            )
            self.mcq_ids.append(qid)
        self.short_ids = []
        for i in range(3):
            qid = questions.create_question(
                self.conn, "short_answer", f"Real short-answer question number {i} about respiration.",
                class_id="c8", subject_id="science", verification_status="verified",
            )
            self.short_ids.append(qid)

        self.blueprint_ok = exams.create_blueprint(
            self.conn, self.inst, "c8", "science", book_id=None,
            total_marks=10, duration_minutes=60,
            question_type_distribution={"mcq": 3, "short_answer": 2},
        )
        self.blueprint_impossible = exams.create_blueprint(
            self.conn, self.inst, "c8", "science", book_id=None,
            total_marks=100, duration_minutes=60,
            question_type_distribution={"mcq": 50, "short_answer": 50},  # way more than exists
        )

    def tearDown(self):
        self.conn.close()

    def test_satisfiable_blueprint_generates_real_exam_with_answer_key(self):
        exam_id, shortfalls = exams.generate_exam(self.conn, self.blueprint_ok, version_label="A")
        self.assertEqual(shortfalls, [])

        eq_rows = self.conn.execute("SELECT * FROM exam_questions WHERE exam_id = ?", (exam_id,)).fetchall()
        self.assertEqual(len(eq_rows), 5)  # 3 mcq + 2 short_answer, exactly what the blueprint asked for

        counts = {}
        for row in eq_rows:
            q = self.conn.execute("SELECT question_type FROM questions WHERE id = ?", (row["question_id"],)).fetchone()
            counts[q["question_type"]] = counts.get(q["question_type"], 0) + 1
        self.assertEqual(counts, {"mcq": 3, "short_answer": 2})

        answer_key = self.conn.execute("SELECT * FROM answer_keys WHERE exam_id = ?", (exam_id,)).fetchone()
        self.assertIsNotNone(answer_key)

    def test_impossible_blueprint_raises_instead_of_fabricating(self):
        with self.assertRaises(exams.InsufficientQuestionsError) as ctx:
            exams.generate_exam(self.conn, self.blueprint_impossible, version_label="A")
        shortfalls = ctx.exception.shortfalls
        mcq_shortfall = next(s for s in shortfalls if s["question_type"] == "mcq")
        self.assertEqual(mcq_shortfall["available"], 5)
        self.assertEqual(mcq_shortfall["needed"], 50)

        # and no exam should have been left behind from the failed attempt
        count = self.conn.execute("SELECT COUNT(*) as c FROM exams").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_multi_version_exams_use_different_question_sets(self):
        group_id, exam_results = exams.generate_versions(self.conn, self.blueprint_ok, labels=("A", "B"))
        self.assertEqual(len(exam_results), 2)

        (exam_a, _), (exam_b, _) = exam_results
        qs_a = {r["question_id"] for r in self.conn.execute(
            "SELECT question_id FROM exam_questions WHERE exam_id = ?", (exam_a,)
        ).fetchall()}
        qs_b = {r["question_id"] for r in self.conn.execute(
            "SELECT question_id FROM exam_questions WHERE exam_id = ?", (exam_b,)
        ).fetchall()}

        # pool is exactly big enough (5 mcq, 3 short_answer) for two non-overlapping
        # 3-mcq/2-short_answer versions — so real content should differ, not just order
        self.assertNotEqual(qs_a, qs_b, "expected genuinely different questions between versions given pool size")

        both_exams = self.conn.execute(
            "SELECT version_group_id FROM exams WHERE id IN (?, ?)", (exam_a, exam_b)
        ).fetchall()
        self.assertEqual(both_exams[0]["version_group_id"], both_exams[1]["version_group_id"])


if __name__ == "__main__":
    unittest.main()
