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
from datetime import date as date_type
from dotenv import load_dotenv
from flask import Flask, request, jsonify, g, abort, redirect, render_template, session, url_for
from db import init_db, get_connection, DB_PATH
from services import core, attendance, questions, exams, performance, governance, connected_systems
from services.licensing import feature_allowed
from services.login_throttle import clear_failures, is_locked, record_failure
from services.api_rate_limit import allow_request
from services.security_audit import append_security_event
from services import identity
from services.tenant_jobs import enqueue_job, get_tenant_job, JobAuthorizationError, IdempotencyConflict
from services.tenant_leave import (
    create_school_leave_request, decide_school_leave_approval,
    LeaveAuthorizationError, LeaveIdempotencyConflict,
    LeaveResourceNotFound, LeaveDateOverlapError, LeaveApprovalConflict,
)
from services.tenant_attendance import (
    record_school_attendance_event, AttendanceAuthorizationError,
    AttendanceEntitlementError, AttendanceResourceNotFound,
    AttendanceIdempotencyConflict, AttendanceSequenceConflict,
)
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
    # Dynamic application responses can contain tenant, identity, assessment,
    # billing, or provider data. Do not let browsers or intermediary caches
    # retain them, including non-admin legacy routes and school APIs.
    # Static assets remain cacheable so normal asset caching is unaffected.
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def _security_audit_source_hash():
    # Store a keyed digest rather than a raw client IP address.
    identity = request.remote_addr or ""
    return hmac.new(
        str(app.secret_key).encode("utf-8"),
        ("security-audit|" + identity).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@app.after_request
def audit_sensitive_request(response):
    path = request.path
    is_auth_route = path in {"/auth/login", "/auth/school-login", "/auth/logout"}
    is_admin_route = path.startswith(("/admin/", "/api/admin/")) or path == "/api/admin"
    is_protected_mutation = (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and path.startswith(_PROTECTED_API_PREFIXES)
    )
    if not (is_auth_route or is_admin_route or is_protected_mutation):
        return response

    if path in {"/auth/login", "/auth/school-login"}:
        event_type = (
            "login_succeeded" if response.status_code in {200, 302}
            else "login_throttled" if response.status_code == 429
            else "login_failed" if response.status_code == 401
            else "login_rejected"
        )
    elif path == "/auth/logout":
        event_type = "logout_succeeded" if response.status_code == 302 else "logout_rejected"
    elif is_admin_route:
        event_type = "admin_request_rejected" if response.status_code >= 400 else "admin_request_completed"
    else:
        event_type = (
            "protected_mutation_rejected"
            if response.status_code >= 400
            else "protected_mutation_completed"
        )

    try:
        append_security_event(
            get_db(),
            event_type=event_type,
            source_hash=_security_audit_source_hash(),
            http_method=request.method,
            route=path,
            response_status=response.status_code,
            actor_id=session.get("user"),
            actor_role=session.get("role"),
            details={"category": "authentication" if is_auth_route else "admin" if is_admin_route else "protected_mutation"},
        )
    except Exception:
        # Audit failure must be visible to operators without leaking internals
        # or changing an already-produced business response.
        app.logger.exception("Failed to persist security audit event")
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


def _api_rate_key():
    # Use a keyed digest of the client address and Flask endpoint, not raw IP data.
    identity = (request.remote_addr or "") + "|" + (request.endpoint or request.path)
    return hmac.new(
        str(app.secret_key).encode("utf-8"),
        ("api|" + identity).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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
    "/api/admin/",
    "/api/school/",)


# School APIs are opt-in by exact Flask endpoint and HTTP method. A broad
# /api/school/ prefix is not an authorization policy: new routes must be
# reviewed and added here only after tenant and role checks are implemented.
_SCHOOL_API_ROUTE_METHODS = frozenset({
    ("school_me", "GET"),
    ("school_classes", "GET"),
    ("school_create_class", "POST"),
    ("school_question_banks", "GET"),
    ("school_exam_blueprints", "GET"),
    ("school_attendance", "GET"),
    ("school_create_attendance_event", "POST"),
    ("school_leave_requests", "GET"),
    ("school_create_leave_request", "POST"),
    ("school_decide_leave_approval", "POST"),
    ("school_create_job", "POST"),
    ("school_get_job", "GET"),
})


# Every registered route is classified by endpoint and HTTP method. Legacy
# business APIs intentionally remain platform-admin-only until they receive
# an institution-bound identity and a reviewed per-role data-access policy.
# This is a fail-closed inventory: adding a route or method requires an
# explicit classification here and a corresponding regression update.
_ROUTE_ACCESS_POLICIES = {
    "static": ("public", frozenset({"GET"})),
    "home": ("public", frozenset({"GET"})),
    "login": ("public", frozenset({"POST"})),
    "school_login": ("public", frozenset({"POST"})),
    "school_dashboard": ("public", frozenset({"GET"})),
    "health": ("public", frozenset({"GET"})),
    "api_status": ("public", frozenset({"GET"})),
    "logout": ("authenticated", frozenset({"POST"})),
    "school_me": ("school_user", frozenset({"GET"})),
    "school_classes": ("school_user", frozenset({"GET"})),
    "school_create_class": ("school_user", frozenset({"POST"})),
    "school_question_banks": ("school_user", frozenset({"GET"})),
    "school_exam_blueprints": ("school_user", frozenset({"GET"})),
    "school_attendance": ("school_user", frozenset({"GET"})),
    "school_create_attendance_event": ("school_user", frozenset({"POST"})),
    "school_leave_requests": ("school_user", frozenset({"GET"})),
    "school_create_leave_request": ("school_user", frozenset({"POST"})),
    "school_decide_leave_approval": ("school_user", frozenset({"POST"})),
    "school_create_job": ("school_user", frozenset({"POST"})),
    "school_get_job": ("school_user", frozenset({"GET"})),
    "admin_subscriptions_page": ("platform_admin", frozenset({"GET"})),
    "connected_systems_admin_page": ("platform_admin", frozenset({"GET"})),
    "admin_create_school_user": ("platform_admin", frozenset({"POST"})),
    "create_institution": ("platform_admin", frozenset({"POST"})),
    "create_class": ("platform_admin", frozenset({"POST"})),
    "list_classes": ("platform_admin", frozenset({"GET"})),
    "teacher_check_in": ("platform_admin", frozenset({"POST"})),
    "teacher_check_out": ("platform_admin", frozenset({"POST"})),
    "generate_question": ("platform_admin", frozenset({"POST"})),
    "find_duplicates": ("platform_admin", frozenset({"POST"})),
    "coverage_gaps": ("platform_admin", frozenset({"POST"})),
    "generate_exam": ("platform_admin", frozenset({"POST"})),
    "create_approval_gate": ("platform_admin", frozenset({"POST"})),
    "decide_approval_gate": ("platform_admin", frozenset({"POST"})),
    "select_provider": ("platform_admin", frozenset({"GET"})),
    "list_connected_systems": ("platform_admin", frozenset({"GET"})),
    "register_connected_system": ("platform_admin", frozenset({"POST"})),
    "test_connected_system": ("platform_admin", frozenset({"POST"})),
    "change_connected_system_status": ("platform_admin", frozenset({"POST"})),
    "dashboard_api.dashboard_stats": ("platform_admin", frozenset({"GET"})),
    "dashboard_api.list_institutions": ("platform_admin", frozenset({"GET"})),
    "dashboard_api.list_classes": ("platform_admin", frozenset({"GET"})),
    "dashboard_api.list_question_banks": ("platform_admin", frozenset({"GET"})),
    "dashboard_api.list_exams": ("platform_admin", frozenset({"GET"})),
    "dashboard_api.list_approval_gates": ("platform_admin", frozenset({"GET"})),
    "dashboard_api.admin_list_plans": ("platform_admin", frozenset({"GET"})),
    "dashboard_api.admin_create_plan": ("platform_admin", frozenset({"POST"})),
    "dashboard_api.admin_list_subscriptions": ("platform_admin", frozenset({"GET"})),
    "dashboard_api.admin_grant_subscription": ("platform_admin", frozenset({"POST"})),
    "dashboard_api.admin_update_subscription_status": ("platform_admin", frozenset({"POST"})),
}


def _route_access_class(endpoint, method):
    """Return a reviewed route class, or 'unclassified' for unknown methods/routes."""
    policy = _ROUTE_ACCESS_POLICIES.get(endpoint)
    if policy is None:
        return "unclassified"
    access_class, methods = policy
    if method == "HEAD" and "GET" in methods:
        return access_class
    if method == "OPTIONS":
        return access_class
    return access_class if method in methods else "unclassified"



@app.before_request
def enforce_api_security():
    # Admin APIs are always protected. Legacy mode only relaxes anonymous
    # access for isolated demos; it must never relax authenticated role bounds.
    is_admin_api = request.path.startswith("/api/admin/")
    production_mode = os.environ.get("APP_ENV", "").strip().lower() == "production"
    legacy_mode = (not production_mode) and os.environ.get("REQUIRE_API_AUTH", "1").strip() != "1"
    is_api_route = request.path.startswith("/api/")
    public_routes = {"/", "/auth/login", "/auth/school-login", "/school", "/health", "/api/status"}
    is_public_route = (
        request.path in public_routes
        or request.path.startswith("/static/")
    )
    # In production, default-deny every route not explicitly public. This
    # prevents newly added business endpoints from bypassing the protected
    # prefix list. Development keeps the existing explicit-prefix behavior.
    must_protect = request.path.startswith(_PROTECTED_API_PREFIXES) or (
        production_mode and not is_public_route
    )

    rate_limit_enabled = (
        production_mode
        or os.environ.get("API_RATE_LIMIT_ENABLED", "0") == "1"
    )
    rate_limit_candidate = (
        is_api_route or request.path.startswith(_PROTECTED_API_PREFIXES)
    )
    if must_protect and rate_limit_enabled and rate_limit_candidate and not allow_request(
        get_db(),
        _api_rate_key(),
        limit=int(app.config.get("API_RATE_LIMIT_PER_MINUTE", 120)),
    ):
        response = jsonify({"error": "rate_limit_exceeded"})
        response.headers["Retry-After"] = "60"
        return response, 429

    # Authenticated users must always be able to end their own session. Keep
    # logout POST-only and CSRF-protected, even in development/legacy mode.
    if request.path == "/auth/logout" and session.get("user"):
        if request.method != "POST":
            return jsonify({"error": "method_not_allowed"}), 405
        if not _valid_csrf():
            return jsonify({"error": "csrf_invalid"}), 400
        return None

    # Enforce role boundaries before the development legacy-mode early return.
    # Otherwise a future route outside the current prefix list could become
    # reachable by a school or unknown-role session without route-level guards.
    if session.get("user") and session.get("role") == "school_user":
        if request.path.startswith("/api/school/"):
            # Fail closed for unregistered routes and methods inside the school
            # namespace. Otherwise a future endpoint could inherit access merely
            # by sharing the prefix, before its tenant/RBAC policy is reviewed.
            if (request.endpoint, request.method) not in _SCHOOL_API_ROUTE_METHODS:
                return jsonify({"error": "school_route_not_authorized"}), 403
            # Re-check active user, institution and role assignment on every
            # school API request; never trust tenant claims from the cookie.
            if not identity.active_tenant_identity(
                get_db(),
                user_id=session.get("user_id"),
                institution_id=session.get("institution_id"),
            ):
                session.clear()
                return jsonify({"error": "school_identity_inactive"}), 401
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _valid_csrf():
                return jsonify({"error": "csrf_invalid"}), 400
            return None
        if is_public_route:
            return None
        return jsonify({"error": "platform_admin_required"}), 403

    if session.get("user") and session.get("role") != "platform_admin":
        # Unknown/retired roles may reach only authentication and health
        # endpoints. In particular, do not render the platform dashboard.
        safe_session_routes = {"/auth/login", "/auth/school-login", "/health", "/api/status"}
        if request.path in safe_session_routes or request.path.startswith("/static/"):
            return None
        return jsonify({"error": "platform_admin_required"}), 403

    # Route inventory is independent of URL-prefix protection. A future
    # endpoint or newly added method cannot inherit the development legacy
    # bypass merely because it sits outside a known prefix. School APIs also
    # require an actual school identity; a platform-admin cookie is not a
    # substitute for an institution-bound user/session.
    if request.endpoint is not None:
        route_access = _route_access_class(request.endpoint, request.method)
        if route_access == "unclassified":
            return jsonify({"error": "route_access_policy_missing"}), 403
        if route_access == "school_user" and session.get("role") != "school_user":
            if not session.get("user"):
                return jsonify({"error": "authentication_required"}), 401
            return jsonify({"error": "school_identity_required"}), 403
        if route_access == "authenticated" and not session.get("user"):
            return jsonify({"error": "authentication_required"}), 401
        if (
            route_access == "platform_admin"
            and not session.get("user")
            and (must_protect or production_mode)
            and not (legacy_mode and not is_admin_api and must_protect)
        ):
            return jsonify({"error": "authentication_required"}), 401

    if not must_protect:
        return None

    # Legacy mode is for anonymous local demos only; authenticated role checks
    # above always run before this compatibility escape hatch.
    if legacy_mode and not is_admin_api:
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
    if session.get("role") != "platform_admin":
        return jsonify({"error": "platform_admin_required"}), 403
    return render_template("subscriptions.html", username=session["user"])


@app.get("/admin/connected-systems")
def connected_systems_admin_page():
    if not session.get("user"):
        return redirect(url_for("home"))
    if session.get("role") != "platform_admin":
        return jsonify({"error": "platform_admin_required"}), 403
    return render_template("connected_systems.html", username=session["user"])


@app.route("/", methods=["GET"])
def home():
    if session.get("user") and session.get("role") == "school_user":
        return redirect(url_for("school_dashboard"))
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


@app.route("/auth/school-login", methods=["POST"])
def school_login():
    if not _valid_csrf():
        return err("csrf_invalid", 400)
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    rate_key = _login_rate_key(email)
    conn = get_db()
    if is_locked(conn, rate_key):
        return err("login_throttled", 429)
    user = identity.authenticate_school_user(conn, email=email, password=password)
    if user is None:
        record_failure(conn, rate_key)
        return err("invalid_credentials", 401)
    clear_failures(conn, rate_key)
    session.clear()
    session["user"] = user["email"]
    session["user_id"] = user["id"]
    session["institution_id"] = user["institution_id"]
    session["school_role_name"] = user["role_name"]
    session["role"] = "school_user"
    session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("school_dashboard"))


@app.get("/school")
def school_dashboard():
    if not session.get("user") or session.get("role") != "school_user":
        return redirect(url_for("home"))
    if not identity.active_tenant_identity(
        get_db(), user_id=session.get("user_id"),
        institution_id=session.get("institution_id"),
    ):
        session.clear()
        return redirect(url_for("home"))
    institution = get_db().execute(
        "SELECT name FROM institutions WHERE id = ?",
        (session["institution_id"],),
    ).fetchone()
    return render_template(
        "school_dashboard.html",
        username=session["user"],
        institution_name=institution["name"] if institution else "Institution",
        role_name=session.get("school_role_name", "School user"),
    )


@app.get("/api/school/me")
def school_me():
    institution = get_db().execute(
        "SELECT id, name, code FROM institutions WHERE id = ? AND status = 'active'",
        (session["institution_id"],),
    ).fetchone()
    if institution is None:
        return err("active_institution_required", 403)
    return jsonify({
        "user": session["user"],
        "role": session.get("school_role_name"),
        "institution": dict(institution),
    })


@app.post("/api/school/classes")
def school_create_class():
    institution_id = session["institution_id"]
    access_error = _require_feature_access(institution_id, "class_management")
    if access_error:
        return access_error
    permissions = identity.institution_permissions(
        get_db(), user_id=session["user_id"], institution_id=institution_id
    )
    if not ({"*", "manage_institution"} & permissions):
        return err("permission_denied", 403)

    body = request.get_json(silent=True) or {}
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        return err("class_name_required", 400)
    academic_year = body.get("academic_year", "2026-2027")
    if not isinstance(academic_year, str) or not academic_year.strip() or len(academic_year) > 40:
        return err("invalid_academic_year", 400)

    conn = get_db()
    campus_id = body.get("campus_id")
    if campus_id is not None:
        campus = conn.execute(
            "SELECT id FROM campuses WHERE id = ? AND institution_id = ? AND status = 'active'",
            (campus_id, institution_id),
        ).fetchone()
        if campus is None:
            return err("campus_not_in_institution", 400)
    else:
        campus = conn.execute(
            "SELECT id FROM campuses WHERE institution_id = ? AND status = 'active' ORDER BY rowid LIMIT 1",
            (institution_id,),
        ).fetchone()
        if campus is None:
            campus_id = core.create_campus(
                conn, institution_id, "Main Campus", "MAIN-" + institution_id[:8]
            )
        else:
            campus_id = campus["id"]

    class_id = core.create_class(
        conn, institution_id, campus_id, name.strip(), academic_year.strip()
    )
    return jsonify({"id": class_id, "institution_id": institution_id, "name": name.strip()}), 201


@app.get("/api/school/classes")
def school_classes():
    access_error = _require_feature_access(session["institution_id"], "class_management")
    if access_error:
        return access_error
    permissions = identity.institution_permissions(
        get_db(), user_id=session["user_id"],
        institution_id=session["institution_id"],
    )
    # Academic Coordinators need read access to classes to perform their
    # institution-scoped curriculum-management role.
    if not ({"*", "manage_institution", "view_all_reports", "manage_curriculum"} & permissions):
        return err("permission_denied", 403)
    rows = core.scoped_query(get_db(), "classes", session["institution_id"])
    return jsonify({"classes": [dict(row) for row in rows]})



@app.get("/api/school/question-banks")
def school_question_banks():
    """List only the authenticated institution's question-bank metadata."""
    institution_id = session["institution_id"]
    permissions = identity.institution_permissions(
        get_db(), user_id=session["user_id"], institution_id=institution_id
    )
    if not ({"*", "manage_institution", "manage_curriculum", "verify_questions"} & permissions):
        return err("permission_denied", 403)
    access_error = _require_feature_access(institution_id, "question_bank_analysis")
    if access_error:
        return access_error

    rows = get_db().execute(
        """SELECT id, name, status
           FROM question_banks
           WHERE institution_id = ?
           ORDER BY name, id
           LIMIT 200""",
        (institution_id,),
    ).fetchall()
    return jsonify({"question_banks": [dict(row) for row in rows]})


@app.get("/api/school/exam-blueprints")
def school_exam_blueprints():
    """List only the authenticated institution's exam-blueprint metadata."""
    institution_id = session["institution_id"]
    permissions = identity.institution_permissions(
        get_db(), user_id=session["user_id"], institution_id=institution_id
    )
    if not ({"*", "manage_institution", "manage_curriculum", "publish_exam"} & permissions):
        return err("permission_denied", 403)
    access_error = _require_feature_access(institution_id, "exam_generation")
    if access_error:
        return access_error

    rows = get_db().execute(
        """SELECT id, total_marks, duration_minutes, language, status, created_at
           FROM exam_blueprints
           WHERE institution_id = ?
           ORDER BY created_at DESC, id
           LIMIT 200""",
        (institution_id,),
    ).fetchall()
    return jsonify({"exam_blueprints": [dict(row) for row in rows]})


@app.get("/api/school/attendance")
def school_attendance():
    """List bounded teacher-attendance metadata for the authenticated tenant."""
    institution_id = session["institution_id"]
    permissions = identity.institution_permissions(
        get_db(), user_id=session["user_id"], institution_id=institution_id
    )
    if not ({"*", "manage_institution", "view_all_reports", "manage_attendance_policy"} & permissions):
        return err("permission_denied", 403)

    access_error = _require_feature_access(institution_id, "attendance")
    if access_error:
        return access_error

    # Tenant selection is never accepted from the client. The legacy
    # institution_id selector is ignored for compatibility; other filters are
    # explicit and validated so callers cannot smuggle SQL fragments/options.
    unsupported = set(request.args.keys()) - {"date", "institution_id"}
    if unsupported:
        return err("unsupported_query_parameter", 400)
    date_values = request.args.getlist("date")
    if len(date_values) > 1:
        return err("date_must_be_unique", 400)

    date_filter = None
    if date_values:
        try:
            parsed_date = date_type.fromisoformat(date_values[0])
        except (TypeError, ValueError):
            return err("invalid_date", 400)
        if parsed_date.isoformat() != date_values[0]:
            return err("invalid_date", 400)
        date_filter = parsed_date.isoformat()

    sql = """
        SELECT ae.id, ae.teacher_id, ae.event_type, ae.timestamp,
               ae.method, ae.status, ae.created_at
        FROM attendance_events ae
        JOIN teachers t ON t.id = ae.teacher_id
        WHERE ae.institution_id = ? AND t.institution_id = ?
    """
    params = [institution_id, institution_id]
    if date_filter is not None:
        sql += " AND substr(ae.created_at, 1, 10) = ?"
        params.append(date_filter)
    sql += " ORDER BY ae.created_at DESC, ae.id DESC LIMIT 200"

    rows = get_db().execute(sql, params).fetchall()
    return jsonify({"attendance_events": [dict(row) for row in rows]})


@app.post("/api/school/attendance/events")
def school_create_attendance_event():
    """Record a staff-assisted check-in or check-out for this tenant."""
    body = request.get_json(silent=True)
    if body is None:
        if request.data:
            return err("invalid_json", 400)
        body = {}
    if not isinstance(body, dict) or set(body) - {"teacher_id", "policy_id", "event_type", "time"}:
        return err("unsupported_payload", 400)
    if not {"teacher_id", "policy_id", "event_type", "time"}.issubset(body):
        return err("required_fields_missing", 400)

    institution_id = session["institution_id"]
    access_error = _require_feature_access(institution_id, "attendance")
    if access_error:
        return access_error

    try:
        result = record_school_attendance_event(
            get_db(),
            institution_id=institution_id,
            actor_id=session["user_id"],
            teacher_id=body.get("teacher_id"),
            policy_id=body.get("policy_id"),
            event_type=body.get("event_type"),
            timestamp_hhmm=body.get("time"),
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except AttendanceAuthorizationError:
        return err("permission_denied", 403)
    except AttendanceEntitlementError:
        return err("subscription_required", 403)
    except AttendanceResourceNotFound:
        return err("attendance_resource_not_found", 404)
    except AttendanceIdempotencyConflict:
        return err("idempotency_key_conflict", 409)
    except AttendanceSequenceConflict as exc:
        return err(str(exc), 409)
    except ValueError as exc:
        return err(str(exc), 400)

    return jsonify(result), 201 if result["created"] else 200


@app.get("/api/school/leave-requests")
def school_leave_requests():
    """List bounded leave-request metadata after checking both tenant relationships."""
    institution_id = session["institution_id"]
    permissions = identity.institution_permissions(
        get_db(), user_id=session["user_id"], institution_id=institution_id
    )
    if not ({"*", "manage_institution", "approve_leave", "view_all_reports"} & permissions):
        return err("permission_denied", 403)

    access_error = _require_feature_access(institution_id, "leave_management")
    if access_error:
        return access_error

    unsupported = set(request.args.keys()) - {"status", "institution_id"}
    if unsupported:
        return err("unsupported_query_parameter", 400)
    status_values = request.args.getlist("status")
    if len(status_values) > 1:
        return err("status_must_be_unique", 400)
    status_filter = None
    if status_values:
        status_filter = status_values[0]
        if status_filter not in {"pending", "approved", "rejected"}:
            return err("invalid_status", 400)

    # A leave request has no direct institution_id column. Require both its
    # teacher and leave type to belong to the authenticated institution so
    # inconsistent legacy rows cannot bridge tenants.
    sql = """
        SELECT lr.id, lr.teacher_id, lt.name AS leave_type_name,
               lr.start_date, lr.end_date, lr.status, lr.created_at
        FROM leave_requests lr
        JOIN teachers t ON t.id = lr.teacher_id
        JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE t.institution_id = ? AND lt.institution_id = ?
    """
    params = [institution_id, institution_id]
    if status_filter is not None:
        sql += " AND lr.status = ?"
        params.append(status_filter)
    sql += " ORDER BY lr.created_at DESC, lr.id DESC LIMIT 200"

    rows = get_db().execute(sql, params).fetchall()
    return jsonify({"leave_requests": [dict(row) for row in rows]})


@app.post("/api/school/leave-requests")
def school_create_leave_request():
    """Create an idempotent, tenant-bound leave request for an active teacher."""
    body = request.get_json(silent=True)
    if body is None:
        if request.data:
            return err("invalid_json", 400)
        body = {}
    allowed = {
        "teacher_id", "leave_type_id", "start_date", "end_date",
        "reason", "substitute_teacher_id",
    }
    if not isinstance(body, dict) or set(body) - allowed:
        return err("unsupported_payload", 400)
    required = {"teacher_id", "leave_type_id", "start_date", "end_date", "reason"}
    if not required.issubset(body):
        return err("required_fields_missing", 400)

    institution_id = session["institution_id"]
    access_error = _require_feature_access(institution_id, "leave_management")
    if access_error:
        return access_error

    try:
        leave_request_id, created, status = create_school_leave_request(
            get_db(),
            institution_id=institution_id,
            actor_id=session["user_id"],
            teacher_id=body.get("teacher_id"),
            leave_type_id=body.get("leave_type_id"),
            start_date=body.get("start_date"),
            end_date=body.get("end_date"),
            reason=body.get("reason"),
            substitute_teacher_id=body.get("substitute_teacher_id"),
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except LeaveAuthorizationError:
        return err("permission_denied", 403)
    except LeaveResourceNotFound:
        return err("leave_resource_not_found", 404)
    except LeaveIdempotencyConflict:
        return err("idempotency_key_conflict", 409)
    except LeaveDateOverlapError:
        return err("leave_dates_overlap", 409)
    except ValueError as exc:
        return err(str(exc), 400)

    return jsonify({"id": leave_request_id, "status": status}), 201 if created else 200


@app.post("/api/school/leave-requests/<request_id>/decision")
def school_decide_leave_approval(request_id):
    """Record the next ordered leave approval/rejection for this tenant."""
    body = request.get_json(silent=True)
    if body is None:
        if request.data:
            return err("invalid_json", 400)
        body = {}
    if not isinstance(body, dict) or set(body) - {"decision", "comments"}:
        return err("unsupported_payload", 400)
    if "decision" not in body:
        return err("required_fields_missing", 400)

    institution_id = session["institution_id"]
    access_error = _require_feature_access(institution_id, "leave_management")
    if access_error:
        return access_error

    try:
        result = decide_school_leave_approval(
            get_db(),
            institution_id=institution_id,
            actor_id=session["user_id"],
            leave_request_id=request_id,
            decision=body.get("decision"),
            comments=body.get("comments"),
        )
    except LeaveAuthorizationError:
        return err("permission_denied", 403)
    except LeaveResourceNotFound:
        return err("leave_resource_not_found", 404)
    except LeaveApprovalConflict:
        return err("leave_approval_conflict", 409)
    except ValueError as exc:
        return err(str(exc), 400)

    return jsonify(result)


@app.post("/api/school/jobs")
def school_create_job():
    """Queue a tenant health check using only authenticated session identity."""
    body = request.get_json(silent=True)
    if body is None:
        if request.data:
            return err("invalid_json", 400)
        body = {}
    if not isinstance(body, dict) or set(body) - {"resources"}:
        return err("unsupported_job_payload", 400)
    try:
        job_id, created = enqueue_job(
            get_db(),
            institution_id=session.get("institution_id"),
            actor_id=session.get("user_id"),
            job_type="tenant_health_check",
            payload=body,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except JobAuthorizationError:
        return err("permission_denied", 403)
    except IdempotencyConflict:
        return err("idempotency_key_conflict", 409)
    except ValueError as exc:
        return err(str(exc), 400)
    job = get_tenant_job(
        get_db(), job_id=job_id, institution_id=session["institution_id"]
    )
    return jsonify({"id": job_id, "status": job["status"]}), 202 if created else 200


@app.get("/api/school/jobs/<job_id>")
def school_get_job(job_id):
    """Return only a job belonging to the authenticated institution."""
    permissions = identity.institution_permissions(
        get_db(), user_id=session["user_id"],
        institution_id=session["institution_id"],
    )
    if not ({"*", "manage_institution", "view_all_reports"} & permissions):
        return err("permission_denied", 403)
    job = get_tenant_job(
        get_db(), job_id=job_id, institution_id=session["institution_id"]
    )
    if job is None:
        return err("job_not_found", 404)
    return jsonify(job)


@app.post("/api/admin/school-users")
def admin_create_school_user():
    body = request.get_json(silent=True) or {}
    try:
        created = identity.create_school_user(
            get_db(),
            email=body.get("email"),
            password=body.get("password"),
            institution_id=body.get("institution_id"),
            role_name=body.get("role_name"),
            granted_by=session.get("user", ""),
        )
    except ValueError as exc:
        return err(str(exc), 400)
    except Exception:
        app.logger.exception("Failed to create school identity")
        return err("school_user_creation_failed", 500)
    return jsonify(created), 201


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


def _connected_system_host_allowlist():
    return [
        host.strip().lower()
        for host in os.environ.get("CONNECTED_SYSTEM_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    ]


def _connected_system_allow_private():
    return os.environ.get("CONNECTED_SYSTEM_ALLOW_PRIVATE", "0") == "1"


@app.get("/api/admin/connected-systems")
def list_connected_systems():
    return jsonify({"systems": connected_systems.list_systems(get_db())})


@app.post("/api/admin/connected-systems")
def register_connected_system():
    body = request.get_json(silent=True) or {}
    try:
        system_id = connected_systems.create_system(
            get_db(),
            name=body.get("name"),
            base_url=body.get("base_url"),
            auth_env_var=body.get("auth_env_var"),
            institution_id=body.get("institution_id"),
            scopes=body.get("scopes", []),
            created_by=session.get("user", ""),
            allowed_hosts=_connected_system_host_allowlist(),
            allow_private=_connected_system_allow_private(),
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "institution_not_found" else 400
        return err(message, status)
    return jsonify({"id": system_id, "status": "registered"}), 201


@app.post("/api/admin/connected-systems/<system_id>/test")
def test_connected_system(system_id):
    try:
        result = connected_systems.test_connection(
            get_db(),
            system_id,
            actor_id=session.get("user", ""),
            allowed_hosts=_connected_system_host_allowlist(),
            allow_private=_connected_system_allow_private(),
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if message == "connected_system_not_found" else 409 if "not_testable" in message else 400
        return err(message, status)
    except RuntimeError as exc:
        return err(str(exc), 502)
    return jsonify(result)


@app.post("/api/admin/connected-systems/<system_id>/status")
def change_connected_system_status(system_id):
    body = request.get_json(silent=True) or {}
    try:
        status = connected_systems.change_status(
            get_db(),
            system_id,
            status=body.get("status"),
            actor_id=session.get("user", ""),
            reason=body.get("reason", ""),
        )
    except ValueError as exc:
        message = str(exc)
        code = 404 if message == "connected_system_not_found" else 409 if "health_check_required" in message or "terminal" in message or "unchanged" in message else 400
        return err(message, code)
    return jsonify({"id": system_id, "status": status})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
