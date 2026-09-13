import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db  # noqa: E402
from services import core, questions  # noqa: E402
from providers.base import StubProvider  # noqa: E402


class TestQuestionBank(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        self.inst = core.create_institution(self.conn, "Springfield Academy", "SPR")
        bid = self.conn.execute(
            "INSERT INTO question_banks (id, institution_id, name, status) VALUES ('bank-1', ?, 'Grade 8 Science', 'active')",
            (self.inst,),
        )
        self.conn.commit()
        self.bank_id = "bank-1"

    def tearDown(self):
        self.conn.close()

    def _add_to_bank(self, question_id):
        self.conn.execute(
            "INSERT INTO question_bank_items (id, question_bank_id, question_id, added_at) VALUES (?, ?, ?, datetime('now'))",
            (f"item-{question_id}", self.bank_id, question_id),
        )
        self.conn.commit()

    def test_real_similarity_flags_near_duplicates_not_unrelated_questions(self):
        q1 = questions.create_question(self.conn, "short_answer",
                                        "Explain how plants convert sunlight into chemical energy through photosynthesis.",
                                        class_id="c8", subject_id="science")
        q2 = questions.create_question(self.conn, "short_answer",
                                        "Describe how plants convert sunlight into chemical energy via photosynthesis.",
                                        class_id="c8", subject_id="science")
        q3 = questions.create_question(self.conn, "short_answer",
                                        "What is the capital city of France and why is it significant historically?",
                                        class_id="c8", subject_id="geography")
        for q in (q1, q2, q3):
            self._add_to_bank(q)

        flags = questions.find_duplicates(self.conn, self.bank_id)
        flagged_pairs = {(f["question_id"], f["duplicate_of"]) for f in flags}

        # q1/q2 are genuinely near-duplicate wording — must be flagged
        self.assertTrue(
            (q2, q1) in flagged_pairs or (q1, q2) in flagged_pairs,
            f"expected q1/q2 flagged as similar, got flags: {flags}",
        )
        # q3 is unrelated — must NOT be flagged against q1 or q2
        for f in flags:
            self.assertNotIn(q3, (f["question_id"], f["duplicate_of"]))

    def test_unmapped_source_is_explicit_not_guessed(self):
        qid = questions.create_question(self.conn, "mcq", "What is mitosis?", class_id="c8", subject_id="science")
        _, status = questions.set_question_source(self.conn, qid)  # no book/chapter/page given
        self.assertEqual(status, "unavailable")

    def test_mapped_source_when_page_given(self):
        qid = questions.create_question(self.conn, "mcq", "What is mitosis?", class_id="c8", subject_id="science")
        _, status = questions.set_question_source(self.conn, qid, book_id="book-1", page_ref=42, confidence=0.9)
        self.assertEqual(status, "mapped")

    def test_question_only_verified_when_all_checks_pass(self):
        provider = StubProvider()
        qid = questions.create_question(self.conn, "definition", "Define mitosis.", class_id="c8", subject_id="science")
        results, all_pass = questions.run_verification(self.conn, qid, provider)
        self.assertTrue(all_pass)
        row = self.conn.execute("SELECT verification_status FROM questions WHERE id = ?", (qid,)).fetchone()
        self.assertEqual(row["verification_status"], "verified")

    def test_short_content_fails_ambiguity_check_and_blocks_publish(self):
        provider = StubProvider()
        qid = questions.create_question(self.conn, "mcq", "X?", class_id="c8", subject_id="science")
        results, all_pass = questions.run_verification(self.conn, qid, provider)
        self.assertFalse(all_pass)
        row = self.conn.execute("SELECT verification_status FROM questions WHERE id = ?", (qid,)).fetchone()
        self.assertEqual(row["verification_status"], "flagged")


if __name__ == "__main__":
    unittest.main()
