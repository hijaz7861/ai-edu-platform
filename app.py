"""
Runnable entry point. Start with:  python3 app.py
Then, e.g.:  curl -X POST http://localhost:8080/institutions -d '{"name":"Springfield Academy","code":"SPR"}' -H "Content-Type: application/json"

This wires the real services from Phases 1-2, 5-7, 9-10 into actual HTTP routes.
Phases 3-4 (OCR/knowledge extraction) and 8 (notifications/voice) are demonstrated
in their own test suites — this app exposes a representative slice of each, not
every endpoint from the Phase 8/9 API design docs, to keep this file readable.
"""
import hmac
import hashlib
import os
import secrets
from dotenv import load_dotenv
from flask import Flask, request, jsonify, g, abort, redirect, render_template, session, url_for
from db import init_db, get_connection, DB_PATH
from services import core, attendance, questions, exams, performance, governance
from services.licensing import feature_allowed
from services.login_throttle import clear_failures, is_locked, record_failure
from providers.base import get_default_registry
from dashboard_api import dashboard_api

load_dotenv()

app = Flask(__name__)
app.register_blueprint(dashboard_api)

from services.security_config import configure_security

configure_security(app)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.path == "/" or request.path.startswith(("/auth/", "/admin/", "/api/admin/")):
        response.headers["Cache-Control"] = "no-store"
    return response


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": _csrf_token}


def _valid_csrf():
    supplied = (
        request.headers.get("X-CSRF-Token", "")
        or request.form.get("csrf_token", "")
    )
    expected = session.get("csrf_token", "")
    return bool(
        supplied and expected and hmac.compare_digest(supplied, expected)
    )


def _login_rate_key(username):
    # Store only a keyed digest, not a raw username or client IP.
    identity = str(username or "").strip().casefold()
    address = request.remote_addr or ""
    message = (identity + "|" + address).encode("utf-8")
    return hmac.new(str(app.secret_key).encode("utf-8"), message, hashlib.sha256).hexdigest()


_PROTECTED_API_PREFIXES = (
    "/api/questions/",
    "/institutions",
    "/teachers/",
    "/question-banks/",
    "/blueprints/",
    "/approval-gates",
    "/providers/",

    "/api/dashboard/",
    "/api/institutions",
    "/api/classes",
    "/api/question-banks",
    "/api/exams",
    "/api/approval-gates",
    "/api/admin/",)


@app.before_request
def enforce_api_security():
    # Admin APIs are always protected. Legacy mode only relaxes non-admin APIs
    # for isolated demos; it must never expose plan/subscription administration.
    is_admin_api = request.path.startswith("/api/admin/")
    legacy_mode = os.environ.get("REQUIRE_API_AUTH", "1") != "1"
    if legacy_mode and not is_admin_api:
        return None

    if not request.path.startswith(_PROTECTED_API_PREFIXES):
        return None

    if not session.get("user"):
        return jsonify({"error": "authentication_required"}), 401

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not _valid_csrf():
            return jsonify({"error": "csrf_invalid"}), 400

    return None


@app.get("/admin/subscriptions")
def admin_subscriptions_page():
    if not session.get("user"):
        return redirect(url_for("home"))
    return render_template("subscriptions.html", username=session["user"])


@app.route("/", methods=["GET"])
def home():
    if session.get("user"):
        return render_template("dashboard.html", username=session["user"])
    return render_template("login.html")


@app.route("/auth/login", methods=["POST"])
def login():
    if not _valid_csrf():
        abort(400, description="Invalid or missing CSRF token")

    configured_user = os.environ.get("ADMIN_USERNAME")
    configured_password = os.environ.get("ADMIN_PASSWORD")
    if not configured_user or not configured_password:
        return "Login is not configured on the server.", 503

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    rate_key = _login_rate_key(username)
    conn = get_db()

    if is_locked(conn, rate_key):
        return "Too many failed login attempts. Try again later.", 429

    if (
        hmac.compare_digest(username, configured_user)
        and hmac.compare_digest(password, configured_password)
    ):
        clear_failures(conn, rate_key)
        session.clear()
        session["user"] = username
        session["role"] = "platform_admin"
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(url_for("home"))

    locked = record_failure(conn, rate_key)
    if locked:
        return "Too many failed login attempts. Try again later.", 429
    return "Invalid credentials. Please try again.", 401


@app.route("/auth/logout", methods=["POST"])
def logout():
    if not _valid_csrf():
        abort(400, description="Invalid or missing CSRF token")
    session.clear()
    return redirect(url_for("home"))



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


def _require_feature_access(institution_id, feature):
    """Enforce commercial feature access in production or explicit test/staging mode.

    Development stays usable without seeded plans. Production fails closed and
    requires an institution-scoped active subscription with the feature enabled.
    """
    enforcement_enabled = (
        os.environ.get("APP_ENV", "").strip().lower() == "production"
        or os.environ.get("ENFORCE_ENTITLEMENTS", "0") == "1"
    )
    if not enforcement_enabled:
        return None
    if not isinstance(institution_id, str) or not institution_id.strip():
        return err("institution_id_required", 400)
    if not feature_allowed(get_db(), institution_id.strip(), feature):
        return err("subscription_required", 403)
    return None


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/status")
def api_status():
    return jsonify({"status": "ok", "api": "available"})


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
    access_error = _require_feature_access(institution_id, "class_management")
    if access_error:
        return access_error
    campus_id = body.get("campus_id") or core.create_campus(conn, institution_id, "Main Campus", f"{institution_id}-MAIN")
    cid = core.create_class(conn, institution_id, campus_id, body["name"], body.get("academic_year", "2026-2027"))
    return jsonify({"id": cid}), 201


@app.get("/institutions/<institution_id>/classes")
def list_classes(institution_id):
    conn = get_db()
    access_error = _require_feature_access(institution_id, "class_management")
    if access_error:
        return access_error
    rows = core.scoped_query(conn, "classes", institution_id)
    return jsonify([dict(r) for r in rows])


# ---------- Phase 2 ----------

@app.post("/teachers/<teacher_id>/check-in")
def teacher_check_in(teacher_id):
    conn = get_db()
    body = request.get_json(silent=True) or {}
    access_error = _require_feature_access(body.get("institution_id"), "attendance")
    if access_error:
        return access_error
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
    access_error = _require_feature_access(body.get("institution_id"), "attendance")
    if access_error:
        return access_error
    try:
        event_id, status = attendance.check_out(
            conn, teacher_id, body["institution_id"], body["policy_id"], body["time"], method=body.get("method", "manual")
        )
    except Exception as e:
        return err(str(e))
    return jsonify({"event_id": event_id, "status": status})


# ---------- AI question generation ----------

@app.post("/api/questions/generate")
def generate_question():
    body = request.get_json(silent=True) or {}
    concept = body.get("concept")
    question_type = body.get("question_type", "short_answer")
    difficulty = body.get("difficulty", "medium")

    if isinstance(concept, str):
        concept = {"name": concept.strip()}

    if not isinstance(concept, dict) or not str(concept.get("name", "")).strip():
        return err("concept with a non-empty name is required", 400)
    if question_type not in {"mcq", "definition", "short_answer", "long_answer"}:
        return err("unsupported question_type", 400)
    if difficulty not in {"easy", "medium", "hard"}:
        return err("unsupported difficulty", 400)

    access_error = _require_feature_access(body.get("institution_id"), "ai_generation")
    if access_error:
        return access_error

    registry = get_default_registry()
    preferred_provider = os.environ.get("AI_PROVIDER", "").strip()
    if preferred_provider:
        provider = registry.get(preferred_provider)
        if provider is None:
            return err("configured AI_PROVIDER is unavailable or not configured", 503)
    else:
        # Prefer explicitly configured real providers. The offline stub is used
        # only when AI_PROVIDER=stub-offline-v1 is selected intentionally.
        provider = next(
            (
                registry[name]
                for name in ("mistral-agent", "gemini-native", "openai-compatible")
                if name in registry
            ),
            None,
        )

    if provider is None:
        return err("AI provider is not configured", 503)

    try:
        result = provider.generate_question(concept, question_type, difficulty)
    except RuntimeError:
        app.logger.warning("AI question generation failed; provider error details withheld.")
        return err("AI generation failed; check model availability, API quota, and configuration", 502)

    return jsonify({"question": result, "provider": provider.name}), 200


# ---------- Phase 5 ----------

@app.post("/question-banks/<bank_id>/duplicates")
def find_duplicates(bank_id):
    conn = get_db()
    bank = conn.execute(
        "SELECT institution_id FROM question_banks WHERE id = ?", (bank_id,)
    ).fetchone()
    if not bank:
        return err("question_bank_not_found", 404)
    access_error = _require_feature_access(bank["institution_id"], "question_bank_analysis")
    if access_error:
        return access_error
    flags = questions.find_duplicates(conn, bank_id)
    return jsonify({"flags": flags})


@app.post("/question-banks/<bank_id>/coverage-gaps")
def coverage_gaps(bank_id):
    conn = get_db()
    bank = conn.execute(
        "SELECT institution_id FROM question_banks WHERE id = ?", (bank_id,)
    ).fetchone()
    if not bank:
        return err("question_bank_not_found", 404)
    access_error = _require_feature_access(bank["institution_id"], "question_bank_analysis")
    if access_error:
        return access_error
    result = questions.analyze_coverage_gaps(conn, bank_id)
    return jsonify(result)


# ---------- Phase 6 ----------

@app.post("/blueprints/<blueprint_id>/generate")
def generate_exam(blueprint_id):
    conn = get_db()
    body = request.get_json(silent=True) or {}
    blueprint = conn.execute(
        "SELECT institution_id FROM exam_blueprints WHERE id = ?", (blueprint_id,)
    ).fetchone()
    if not blueprint:
        return err("exam_blueprint_not_found", 404)
    access_error = _require_feature_access(blueprint["institution_id"], "exam_generation")
    if access_error:
        return access_error
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
    decision = body.get("decision")
    if decision not in ("approved", "rejected"):
        return err("decision must be approved or rejected")
    approver_id = session.get("user")
    if not approver_id:
        return err("authentication_required", 401)
    try:
        result = governance.decide_approval(conn, gate_id, approver_id, decision)
    except ValueError as exc:
        message = str(exc)
        return err(message, 404 if message == "approval gate not found" else 409)
    return jsonify({"gate_id": gate_id, "decision": result})

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
