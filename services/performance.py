"""
Phase 7: Student Performance — topic mastery computed from real StudentExamAnswer
rows traced through ExamQuestion -> Question -> topic_id, not an AI guess.
"""
import json
from services.core import new_id, now


def submit_exam(conn, student_id, exam_id):
    sid = new_id()
    conn.execute(
        "INSERT INTO student_exam_submissions (id, student_id, exam_id, submitted_at, status) "
        "VALUES (?, ?, ?, ?, 'submitted')",
        (sid, student_id, exam_id, now()),
    )
    conn.commit()
    return sid


def record_answer(conn, submission_id, exam_question_id, answer_content, marks_obtained, is_correct, graded_by=None):
    aid = new_id()
    conn.execute(
        """INSERT INTO student_exam_answers
           (id, submission_id, exam_question_id, answer_content, marks_obtained, is_correct, graded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (aid, submission_id, exam_question_id, json.dumps(answer_content), marks_obtained, int(is_correct), graded_by),
    )
    conn.commit()
    return aid


def finalize_submission(conn, submission_id):
    total = conn.execute(
        "SELECT COALESCE(SUM(marks_obtained), 0) as total FROM student_exam_answers WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()["total"]
    conn.execute(
        "UPDATE student_exam_submissions SET total_marks_obtained = ?, status = 'graded' WHERE id = ?",
        (total, submission_id),
    )
    conn.commit()
    return total


def compute_topic_mastery(conn, student_id, subject_id=None):
    """Real traversal: StudentExamAnswer -> ExamQuestion -> Question.topic_id.
    mastery[topic] = correct_answers / total_answers for that topic, from actual rows."""
    rows = conn.execute(
        """SELECT q.topic_id as topic_id, sea.is_correct as is_correct
           FROM student_exam_answers sea
           JOIN exam_questions eq ON eq.id = sea.exam_question_id
           JOIN questions q ON q.id = eq.question_id
           JOIN student_exam_submissions ses ON ses.id = sea.submission_id
           WHERE ses.student_id = ? AND q.topic_id IS NOT NULL""",
        (student_id,),
    ).fetchall()

    tally = {}
    for r in rows:
        t = tally.setdefault(r["topic_id"], {"correct": 0, "total": 0})
        t["total"] += 1
        if r["is_correct"]:
            t["correct"] += 1

    mastery = {topic: (v["correct"] / v["total"]) for topic, v in tally.items()}
    weak = [t for t, score in mastery.items() if score < 0.5]
    strong = [t for t, score in mastery.items() if score >= 0.8]
    return {"topic_mastery": mastery, "weak_areas": weak, "strong_areas": strong, "raw_counts": tally}


def generate_performance_record(conn, student_id, subject_id, period):
    mastery_result = compute_topic_mastery(conn, student_id, subject_id)

    submissions = conn.execute(
        "SELECT total_marks_obtained FROM student_exam_submissions WHERE student_id = ? AND status = 'graded'",
        (student_id,),
    ).fetchall()
    marks = [s["total_marks_obtained"] for s in submissions if s["total_marks_obtained"] is not None]
    avg_marks = sum(marks) / len(marks) if marks else None

    rid = new_id()
    conn.execute(
        """INSERT INTO student_performance_records
           (id, student_id, subject_id, period, avg_marks, topic_mastery, weak_areas, strong_areas, generated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rid, student_id, subject_id, period, avg_marks,
         json.dumps(mastery_result["topic_mastery"]), json.dumps(mastery_result["weak_areas"]),
         json.dumps(mastery_result["strong_areas"]), now()),
    )
    conn.commit()
    return rid, mastery_result
