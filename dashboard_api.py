from flask import Blueprint, jsonify, g
from db import get_connection, DB_PATH

dashboard_api = Blueprint("dashboard_api", __name__)


def _connection():
    conn = g.get("db")
    if conn is None:
        conn = get_connection(DB_PATH)
        g.db = conn
    return conn


@dashboard_api.get("/api/dashboard/stats")
def dashboard_stats():
    conn = _connection()
    tables = (
        "institutions", "students", "teachers", "classes",
        "questions", "question_banks", "exam_blueprints",
        "exams", "books"
    )
    stats = {}

    for table in tables:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM " + table
        ).fetchone()
        stats[table] = row["total"]

    row = conn.execute(
        "SELECT COUNT(*) AS total FROM approval_gates WHERE status = ?",
        ("pending",)
    ).fetchone()
    stats["pending_approvals"] = row["total"]

    return jsonify({"stats": stats})


@dashboard_api.get("/api/institutions")
def list_institutions():
    rows = _connection().execute(
        "SELECT id, name, code, type, status "
        "FROM institutions ORDER BY name LIMIT 200"
    ).fetchall()
    return jsonify({"institutions": [dict(r) for r in rows]})


@dashboard_api.get("/api/classes")
def list_classes():
    rows = _connection().execute(
        "SELECT id, name, academic_year, status "
        "FROM classes ORDER BY name LIMIT 300"
    ).fetchall()
    return jsonify({"classes": [dict(r) for r in rows]})


@dashboard_api.get("/api/question-banks")
def list_question_banks():
    rows = _connection().execute(
        "SELECT id, name, status "
        "FROM question_banks ORDER BY name LIMIT 200"
    ).fetchall()
    return jsonify({"question_banks": [dict(r) for r in rows]})


@dashboard_api.get("/api/exams")
def list_exams():
    rows = _connection().execute(
        "SELECT id, title, version_label, total_marks, "
        "duration_minutes, status, language "
        "FROM exams ORDER BY created_at DESC LIMIT 200"
    ).fetchall()
    return jsonify({"exams": [dict(r) for r in rows]})


@dashboard_api.get("/api/approval-gates")
def list_approval_gates():
    rows = _connection().execute(
        "SELECT id, action_type, explanation, status, created_at "
        "FROM approval_gates ORDER BY created_at DESC LIMIT 200"
    ).fetchall()
    return jsonify({"approval_gates": [dict(r) for r in rows]})
