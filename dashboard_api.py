import json
import sqlite3
from flask import Blueprint, jsonify, g, request, session
from db import get_connection, DB_PATH
from services.licensing import create_plan, grant_subscription, change_subscription_status

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


@dashboard_api.get("/api/admin/plans")
def admin_list_plans():
    rows = _connection().execute(
        """SELECT id, code, name, description, price_minor, currency,
                  billing_interval, entitlements_json, is_public, is_active
           FROM platform_plans ORDER BY price_minor, name"""
    ).fetchall()
    return jsonify({"plans": [dict(r) for r in rows]})


@dashboard_api.post("/api/admin/plans")
def admin_create_plan():
    body = request.get_json(silent=True) or {}
    try:
        plan_id = create_plan(
            _connection(),
            code=body.get("code", ""),
            name=body.get("name", ""),
            description=body.get("description", ""),
            price_minor=body.get("price_minor", 0),
            currency=body.get("currency", "USD"),
            billing_interval=body.get("billing_interval", "monthly"),
            entitlements=body.get("entitlements", {}),
            is_public=body.get("is_public", True),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "plan code already exists"}), 409
    return jsonify({"plan_id": plan_id}), 201


@dashboard_api.get("/api/admin/subscriptions")
def admin_list_subscriptions():
    rows = _connection().execute(
        """SELECT s.id, s.institution_id, i.name AS institution_name, i.code AS institution_code,
                  s.plan_id, p.code AS plan_code, p.name AS plan_name, s.status, s.access_type,
                  s.starts_at, s.ends_at, s.granted_by, s.grant_reason, s.created_at
           FROM institution_subscriptions s
           JOIN institutions i ON i.id = s.institution_id
           JOIN platform_plans p ON p.id = s.plan_id
           ORDER BY s.created_at DESC LIMIT 500"""
    ).fetchall()
    return jsonify({"subscriptions": [dict(r) for r in rows]})


@dashboard_api.post("/api/admin/subscriptions/grant")
def admin_grant_subscription():
    body = request.get_json(silent=True) or {}
    try:
        subscription_id = grant_subscription(
            _connection(),
            institution_id=body.get("institution_id", ""),
            plan_id=body.get("plan_id", ""),
            access_type=body.get("access_type", "free_grant"),
            granted_by=session.get("user", ""),
            reason=body.get("reason", ""),
            starts_at=body.get("starts_at"),
            ends_at=body.get("ends_at"),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"subscription_id": subscription_id}), 201


@dashboard_api.post("/api/admin/subscriptions/<subscription_id>/status")
def admin_update_subscription_status(subscription_id):
    body = request.get_json(silent=True) or {}
    try:
        change_subscription_status(
            _connection(), subscription_id,
            status=body.get("status", ""),
            actor_id=session.get("user", ""),
            reason=body.get("reason", ""),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"status": "updated"}), 200
