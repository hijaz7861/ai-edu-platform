import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db  # noqa: E402
from services import core, questions, exams, performance  # noqa: E402


class TestPerformance(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        self.inst = core.create_institution(self.conn, "Springfield Academy", "SPR")

        # two topics: photosynthesis (student will do WELL) and respiration (student will do POORLY)
        self.q_photo = [
            questions.create_question(self.conn, "mcq", f"Photosynthesis Q{i}",
                                       class_id="c8", subject_id="science", verification_status="verified")
            for i in range(3)
        ]
        self.q_resp = [
            questions.create_question(self.conn, "mcq", f"Respiration Q{i}",
                                       class_id="c8", subject_id="science", verification_status="verified")
            for i in range(3)
        ]
        for qid in self.q_photo:
            self.conn.execute("UPDATE questions SET topic_id = 'topic-photosynthesis' WHERE id = ?", (qid,))
        for qid in self.q_resp:
            self.conn.execute("UPDATE questions SET topic_id = 'topic-respiration' WHERE id = ?", (qid,))
        self.conn.commit()

        bp = exams.create_blueprint(
            self.conn, self.inst, "c8", "science", book_id=None, total_marks=6, duration_minutes=30,
            question_type_distribution={"mcq": 6},
        )
        self.exam_id, shortfalls = exams.generate_exam(self.conn, bp, version_label="A")
        assert shortfalls == []

    def tearDown(self):
        self.conn.close()

    def test_weak_and_strong_areas_traced_from_real_answers(self):
        student_id = "student-1"
        submission_id = performance.submit_exam(self.conn, student_id, self.exam_id)

        eq_rows = self.conn.execute(
            "SELECT eq.id as eq_id, q.topic_id as topic_id FROM exam_questions eq "
            "JOIN questions q ON q.id = eq.question_id WHERE eq.exam_id = ?",
            (self.exam_id,),
        ).fetchall()

        # student gets ALL photosynthesis questions right, ALL respiration questions wrong
        for row in eq_rows:
            is_correct = row["topic_id"] == "topic-photosynthesis"
            performance.record_answer(
                self.conn, submission_id, row["eq_id"], {"selected": "A"},
                marks_obtained=1 if is_correct else 0, is_correct=is_correct,
            )
        performance.finalize_submission(self.conn, submission_id)

        result = performance.compute_topic_mastery(self.conn, student_id)

        self.assertEqual(result["topic_mastery"]["topic-photosynthesis"], 1.0)
        self.assertEqual(result["topic_mastery"]["topic-respiration"], 0.0)
        self.assertIn("topic-respiration", result["weak_areas"])
        self.assertIn("topic-photosynthesis", result["strong_areas"])
        self.assertNotIn("topic-photosynthesis", result["weak_areas"])

    def test_performance_record_persists_real_average(self):
        student_id = "student-2"
        submission_id = performance.submit_exam(self.conn, student_id, self.exam_id)
        eq_rows = self.conn.execute(
            "SELECT id FROM exam_questions WHERE exam_id = ?", (self.exam_id,)
        ).fetchall()
        for row in eq_rows:
            performance.record_answer(self.conn, submission_id, row["id"], {"selected": "A"}, marks_obtained=1, is_correct=True)
        total = performance.finalize_submission(self.conn, submission_id)
        self.assertEqual(total, 6)

        record_id, _ = performance.generate_performance_record(self.conn, student_id, "science", "2026-T1")
        row = self.conn.execute("SELECT * FROM student_performance_records WHERE id = ?", (record_id,)).fetchone()
        self.assertEqual(row["avg_marks"], 6.0)


if __name__ == "__main__":
    unittest.main()
