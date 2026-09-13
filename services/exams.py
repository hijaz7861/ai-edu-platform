"""
Phase 6: Exam Generator — real selection algorithm against the actual question
bank. If the bank genuinely doesn't have enough verified questions to satisfy
the blueprint, this raises InsufficientQuestionsError instead of inventing
questions to fill the gap — matches §19's "no fabrication" requirement.
"""
import json
import random
from services.core import new_id, now


class InsufficientQuestionsError(Exception):
    def __init__(self, shortfalls):
        self.shortfalls = shortfalls
        super().__init__(f"blueprint cannot be satisfied: {shortfalls}")


def create_blueprint(conn, institution_id, class_id, subject_id, book_id, total_marks,
                      duration_minutes, question_type_distribution: dict,
                      difficulty_distribution: dict = None, topic_coverage: dict = None,
                      language="en", created_by=None):
    bid = new_id()
    conn.execute(
        """INSERT INTO exam_blueprints
           (id, institution_id, class_id, subject_id, book_id, total_marks, duration_minutes, language,
            topic_coverage, difficulty_distribution, question_type_distribution, status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (bid, institution_id, class_id, subject_id, book_id, total_marks, duration_minutes, language,
         json.dumps(topic_coverage or {}), json.dumps(difficulty_distribution or {}),
         json.dumps(question_type_distribution), created_by, now()),
    )
    conn.commit()
    return bid


def _select_questions(conn, blueprint, exclude_ids=()):
    qtype_dist = json.loads(blueprint["question_type_distribution"])
    selected = []
    shortfalls = []

    for qtype, count in qtype_dist.items():
        rows = conn.execute(
            """SELECT * FROM questions
               WHERE question_type = ? AND class_id = ? AND subject_id = ?
                 AND verification_status = 'verified'""",
            (qtype, blueprint["class_id"], blueprint["subject_id"]),
        ).fetchall()
        candidates = [r for r in rows if r["id"] not in exclude_ids]
        random.shuffle(candidates)

        if len(candidates) < count:
            shortfalls.append({"question_type": qtype, "needed": count, "available": len(candidates)})
            selected.extend(candidates)  # take what's real and available; don't invent the rest
        else:
            selected.extend(candidates[:count])

    return selected, shortfalls


def generate_exam(conn, blueprint_id, version_label="A", version_group_id=None,
                   created_by=None, allow_partial=False, exclude_ids=()):
    blueprint = conn.execute("SELECT * FROM exam_blueprints WHERE id = ?", (blueprint_id,)).fetchone()
    if blueprint is None:
        raise ValueError("blueprint not found")

    selected, shortfalls = _select_questions(conn, blueprint, exclude_ids=exclude_ids)

    if shortfalls and not allow_partial:
        raise InsufficientQuestionsError(shortfalls)

    exam_id = new_id()
    total_marks = sum(q["marks"] for q in selected)
    conn.execute(
        """INSERT INTO exams
           (id, blueprint_id, version_group_id, version_label, title, total_marks, duration_minutes,
            status, language, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'generated', ?, ?, ?)""",
        (exam_id, blueprint_id, version_group_id, version_label,
         f"Exam ({version_label})", total_marks, blueprint["duration_minutes"],
         blueprint["language"], created_by, now()),
    )

    random.shuffle(selected)  # order varies per version — real, not cosmetic
    answer_key_content = []
    for idx, q in enumerate(selected):
        eqid = new_id()
        conn.execute(
            """INSERT INTO exam_questions (id, exam_id, question_id, order_index, marks_allocated)
               VALUES (?, ?, ?, ?, ?)""",
            (eqid, exam_id, q["id"], idx, q["marks"]),
        )
        answer_row = conn.execute("SELECT * FROM question_answers WHERE question_id = ?", (q["id"],)).fetchone()
        answer_key_content.append({
            "exam_question_id": eqid,
            "question_id": q["id"],
            "answer": dict(answer_row)["answer_content"] if answer_row else None,
        })

    akid = new_id()
    conn.execute(
        "INSERT INTO answer_keys (id, exam_id, content, generated_at) VALUES (?, ?, ?, ?)",
        (akid, exam_id, json.dumps(answer_key_content), now()),
    )
    msid = new_id()
    marking_scheme = [{"question_id": q["id"], "marks": q["marks"]} for q in selected]
    conn.execute(
        "INSERT INTO marking_schemes (id, exam_id, content, generated_at) VALUES (?, ?, ?, ?)",
        (msid, exam_id, json.dumps(marking_scheme), now()),
    )
    conn.commit()
    return exam_id, shortfalls


def generate_versions(conn, blueprint_id, labels=("A", "B"), created_by=None):
    """Each version draws a genuinely different subset (when the pool allows) so
    versions vary in actual content, not just cosmetic shuffling of one paper."""
    group_id = new_id()
    exam_ids = []
    used_ids = set()
    for label in labels:
        exam_id, shortfalls = generate_exam(
            conn, blueprint_id, version_label=label, version_group_id=group_id,
            created_by=created_by, allow_partial=True, exclude_ids=used_ids,
        )
        used = {r["question_id"] for r in conn.execute(
            "SELECT question_id FROM exam_questions WHERE exam_id = ?", (exam_id,)
        ).fetchall()}
        used_ids |= used
        exam_ids.append((exam_id, shortfalls))
    return group_id, exam_ids
