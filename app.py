"""
Runnable entry point. Start with:  python3 app.py
Then, e.g.:  curl -X POST http://localhost:8080/institutions -d '{"name":"Springfield Academy","code":"SPR"}' -H "Content-Type: application/json"

This wires the real services from Phases 1-2, 5-7, 9-10 into actual HTTP routes.
Phases 3-4 (OCR/knowledge extraction) and 8 (notifications/voice) are demonstrated
in their own test suites — this app exposes a representative slice of each, not
every endpoint from the Phase 8/9 API design docs, to keep this file readable.
"""
from flask import Flask, request, jsonify, g
from db import init_db, get_connection, DB_PATH
from services import core, attendance, questions, exams, performance, governance

app = Flask(__name__)

# Flask's dev server handles requests on multiple threads, and a raw sqlite3
# connection can't cross threads — real bug, caught by actually running this
# and hitting it with curl. Fix: one connection per request via flask.g.
init_db()  # ensures schema + storage/platform.db exist before any request


def get_db():
    if "db" not in g:
        g.db = get_connection(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _bootstrap_once():
    conn = get_connection(DB_PATH)
    core.seed_default_roles(conn)
    existing = conn.execute(
        "SELECT id FROM ai_provider_configs WHERE provider_name = 'stub-offline-v1'"
    ).fetchone()
    if not existing:
        governance.register_provider(conn, "stub-offline-v1", "local",
                                      ["question_generation", "question_verification", "nlu"], priority=100)
    conn.close()


_bootstrap_once()


def err(message, code=400):
    return jsonify({"error": message}), code


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ---------- Phase 1 ----------

@app.post("/institutions")
def create_institution():
    conn = get_db()
    body = request.get_json(silent=True) or {}
    try:
        iid = core.create_institution(conn, body["name"], body["code"], body.get("type", "school"))
    except Exception as e:
        return err(str(e))
    return jsonify({"id": iid}), 201


@app.post("/institutions/<institution_id>/classes")
def create_class(institution_id):
    conn = get_db()
    body = request.get_json(silent=True) or {}
    campus_id = body.get("campus_id") or core.create_campus(conn, institution_id, "Main Campus", f"{institution_id}-MAIN")
    cid = core.create_class(conn, institution_id, campus_id, body["name"], body.get("academic_year", "2026-2027"))
    return jsonify({"id": cid}), 201


@app.get("/institutions/<institution_id>/classes")
def list_classes(institution_id):
    conn = get_db()
    rows = core.scoped_query(conn, "classes", institution_id)
    return jsonify([dict(r) for r in rows])


# ---------- Phase 2 ----------

@app.post("/teachers/<teacher_id>/check-in")
def teacher_check_in(teacher_id):
    conn = get_db()
    body = request.get_json(silent=True) or {}
    try:
        event_id, status = attendance.check_in(
            conn, teacher_id, body["institution_id"], body["policy_id"], body["time"], method=body.get("method", "manual")
        )
    except Exception as e:
        return err(str(e))
    return jsonify({"event_id": event_id, "status": status})


@app.post("/teachers/<teacher_id>/check-out")
def teacher_check_out(teacher_id):
    conn = get_db()
    body = request.get_json(silent=True) or {}
    try:
        event_id, status = attendance.check_out(
            conn, teacher_id, body["institution_id"], body["policy_id"], body["time"], method=body.get("method", "manual")
        )
    except Exception as e:
        return err(str(e))
    return jsonify({"event_id": event_id, "status": status})


# ---------- Phase 5 ----------

@app.post("/question-banks/<bank_id>/duplicates")
def find_duplicates(bank_id):
    conn = get_db()
    flags = questions.find_duplicates(conn, bank_id)
    return jsonify({"flags": flags})


@app.post("/question-banks/<bank_id>/coverage-gaps")
def coverage_gaps(bank_id):
    conn = get_db()
    result = questions.analyze_coverage_gaps(conn, bank_id)
    return jsonify(result)


# ---------- Phase 6 ----------

@app.post("/blueprints/<blueprint_id>/generate")
def generate_exam(blueprint_id):
    conn = get_db()
    body = request.get_json(silent=True) or {}
    try:
        exam_id, shortfalls = exams.generate_exam(
            conn, blueprint_id, version_label=body.get("version_label", "A"),
            allow_partial=body.get("allow_partial", False),
        )
    except exams.InsufficientQuestionsError as e:
        return jsonify({"error": "insufficient_questions", "shortfalls": e.shortfalls}), 422
    return jsonify({"exam_id": exam_id, "shortfalls": shortfalls})


# ---------- Phase 9 ----------

@app.post("/approval-gates")
def create_approval_gate():
    conn = get_db()
    body = request.get_json(silent=True) or {}
    action_type = body.get("action_type")
    explanation = body.get("explanation")

    if not action_type:
        return err("action_type is required")

    if not explanation or not str(explanation).strip():
        return err("explanation is required")

    try:
        gate_id = governance.request_approval(
            conn, action_type, body.get("payload", {}), explanation,
            requested_by=body.get("requested_by"),
        )
    except ValueError as e:
        return err(str(e))
    return jsonify({"gate_id": gate_id, "status": "pending"}), 201


@app.post("/approval-gates/<gate_id>/decide")
def decide_approval_gate(gate_id):
    conn = get_db()
    body = request.get_json(silent=True) or {}
    decision = governance.decide_approval(conn, gate_id, body["approver_id"], body["decision"])
    return jsonify({"gate_id": gate_id, "decision": decision})


# ---------- Phase 10 ----------

@app.get("/providers/select")
def select_provider():
    conn = get_db()
    capability = request.args.get("capability", "question_generation")
    provider = governance.select_provider(conn, capability)
    if not provider:
        return err("no available provider for this capability", 503)
    return jsonify(dict(provider))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
