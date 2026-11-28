"""Route-level regressions for transactional paid-read authorization."""
import uuid

import pytest

from db import init_db
from services import core, identity, tenant_jobs, tenant_read
from services.tenant_jobs import enqueue_job, ensure_schema


READ_ROUTES = (
    ("admin", "/api/school/classes"),
    ("admin", "/api/school/question-banks"),
    ("admin", "/api/school/exam-blueprints"),
    ("admin", "/api/school/attendance"),
    ("admin", "/api/school/leave-requests"),
    ("teacher", "/api/school/teacher/attendance"),
    ("teacher", "/api/school/teacher/leave-requests"),
    ("admin", "/api/school/jobs/{job_id}"),
)


@pytest.fixture
def tenant_read_env(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "0")

    db_path = str(tmp_path / "tenant-read.sqlite")
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    conn = init_db(db_path)
    core.seed_default_roles(conn)
    suffix = uuid.uuid4().hex[:10]
    institution_id = core.create_institution(
        conn, "Read Snapshot Tenant", "RS-" + suffix
    )
    campus_id = core.create_campus(conn, institution_id, "Main", "RS-" + suffix)
    class_id = core.create_class(
        conn, institution_id, campus_id, "Read Snapshot Class", "2026"
    )
    tenant_b_id = core.create_institution(
        conn, "Read Snapshot Foreign Tenant", "RFT-" + suffix
    )
    campus_b_id = core.create_campus(
        conn, tenant_b_id, "Foreign Campus", "RFT-" + suffix
    )
    class_b_id = core.create_class(
        conn, tenant_b_id, campus_b_id, "Foreign Class", "2026"
    )
    question_bank_id = "read-bank-a-" + suffix
    question_bank_b_id = "read-bank-b-" + suffix
    blueprint_id = "read-blueprint-a-" + suffix
    blueprint_b_id = "read-blueprint-b-" + suffix
    conn.executemany(
        """INSERT INTO question_banks (id, institution_id, name, status)
           VALUES (?, ?, ?, 'active')""",
        [
            (question_bank_id, institution_id, "Tenant A Bank"),
            (question_bank_b_id, tenant_b_id, "Tenant B Bank"),
        ],
    )
    conn.executemany(
        """INSERT INTO exam_blueprints
           (id, institution_id, total_marks, duration_minutes, language, status)
           VALUES (?, ?, 100, 60, 'en', 'draft')""",
        [
            (blueprint_id, institution_id),
            (blueprint_b_id, tenant_b_id),
        ],
    )
    teacher_id = core.create_teacher(
        conn, None, institution_id, "RS-T-" + suffix
    )
    admin = identity.create_school_user(
        conn,
        email=f"read-admin-{suffix}@school.example",
        password="correct horse battery staple",
        institution_id=institution_id,
        role_name="Institution Admin",
        granted_by="platform-admin",
    )
    teacher = identity.create_school_user(
        conn,
        email=f"read-teacher-{suffix}@school.example",
        password="correct horse battery staple",
        institution_id=institution_id,
        role_name="Teacher Portal",
        teacher_id=teacher_id,
        granted_by="platform-admin",
    )
    ensure_schema(conn)
    job_id, created = enqueue_job(
        conn,
        institution_id=institution_id,
        actor_id=admin["id"],
        job_type="tenant_health_check",
        payload={"resources": ["classes"]},
        idempotency_key="tenant-read-job-" + suffix,
    )
    assert created is True
    conn.close()
    app_module.app.config.update(TESTING=True)
    return {
        "app": app_module,
        "institution_id": institution_id,
        "tenant_b_id": tenant_b_id,
        "class_id": class_id,
        "class_b_id": class_b_id,
        "question_bank_id": question_bank_id,
        "question_bank_b_id": question_bank_b_id,
        "blueprint_id": blueprint_id,
        "blueprint_b_id": blueprint_b_id,
        "admin": admin,
        "teacher": teacher,
        "job_id": job_id,
    }


def _session_client(env, actor):
    client = env["app"].app.test_client()
    with client.session_transaction() as session:
        session["user"] = actor["email"]
        session["user_id"] = actor["id"]
        session["institution_id"] = env["institution_id"]
        session["school_role_name"] = (
            "Institution Admin" if actor is env["admin"] else "Teacher Portal"
        )
        session["role"] = "school_user"
        session["csrf_token"] = "tenant-read-csrf"
    return client


def test_paid_academic_reads_ignore_forged_tenant_selector_and_exclude_foreign_rows(
    tenant_read_env,
):
    """Tenant selectors supplied by a client must never switch a school read."""
    env = tenant_read_env
    client = _session_client(env, env["admin"])
    cases = (
        (
            "/api/school/classes",
            "classes",
            env["class_id"],
            env["class_b_id"],
        ),
        (
            "/api/school/question-banks",
            "question_banks",
            env["question_bank_id"],
            env["question_bank_b_id"],
        ),
        (
            "/api/school/exam-blueprints",
            "exam_blueprints",
            env["blueprint_id"],
            env["blueprint_b_id"],
        ),
    )

    for path, collection, own_id, foreign_id in cases:
        response = client.get(
            f"{path}?institution_id={env['tenant_b_id']}"
        )
        assert response.status_code == 200, response.get_json()
        rows = response.get_json()[collection]
        returned_ids = {row["id"] for row in rows}
        assert own_id in returned_ids
        assert foreign_id not in returned_ids


@pytest.mark.parametrize(("actor_key", "path_template"), READ_ROUTES)
def test_paid_school_reads_run_identity_permission_entitlement_and_query_in_one_snapshot(
    tenant_read_env, monkeypatch, actor_key, path_template
):
    env = tenant_read_env
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "1")
    actor = env[actor_key]
    client = _session_client(env, actor)
    path = path_template.format(job_id=env["job_id"])

    observed = {"identity": [], "permissions": [], "features": []}
    original_identity = identity.active_tenant_identity
    original_permissions = identity.institution_permissions

    def track_identity(conn, **kwargs):
        observed["identity"].append(conn.in_transaction)
        return original_identity(conn, **kwargs)

    def track_permissions(conn, **kwargs):
        observed["permissions"].append(conn.in_transaction)
        return original_permissions(conn, **kwargs)

    def allow_feature(conn, institution_id, feature):
        observed["features"].append((conn.in_transaction, feature))
        return True

    monkeypatch.setattr(identity, "active_tenant_identity", track_identity)
    monkeypatch.setattr(identity, "institution_permissions", track_permissions)
    monkeypatch.setattr(tenant_read, "feature_allowed", allow_feature)
    monkeypatch.setattr(tenant_jobs, "feature_allowed", allow_feature)

    response = client.get(path)

    assert response.status_code == 200, response.get_json()
    assert observed["identity"] and True in observed["identity"]
    assert observed["permissions"] and True in observed["permissions"]
    assert observed["features"]
    assert all(in_transaction for in_transaction, _ in observed["features"])


@pytest.mark.parametrize(("actor_key", "path_template"), READ_ROUTES)
def test_paid_school_reads_fail_closed_when_entitlement_is_absent_in_snapshot(
    tenant_read_env, monkeypatch, actor_key, path_template
):
    env = tenant_read_env
    monkeypatch.setenv("ENFORCE_ENTITLEMENTS", "1")
    actor = env[actor_key]
    client = _session_client(env, actor)
    path = path_template.format(job_id=env["job_id"])
    observed = []

    def deny_feature(conn, institution_id, feature):
        observed.append((conn.in_transaction, institution_id, feature))
        return False

    monkeypatch.setattr(tenant_read, "feature_allowed", deny_feature)
    monkeypatch.setattr(tenant_jobs, "feature_allowed", deny_feature)

    response = client.get(path)

    assert response.status_code == 403
    assert response.get_json() == {"error": "subscription_required"}
    assert observed
    assert all(in_transaction for in_transaction, _, _ in observed)


def test_read_snapshot_rejects_permission_revoked_after_middleware(
    tenant_read_env, monkeypatch
):
    env = tenant_read_env
    client = _session_client(env, env["admin"])
    original = identity.institution_permissions

    def revoke_in_snapshot(conn, **kwargs):
        if conn.in_transaction:
            return set()
        return original(conn, **kwargs)

    monkeypatch.setattr(identity, "institution_permissions", revoke_in_snapshot)
    monkeypatch.setattr(
        tenant_read, "feature_allowed",
        lambda *args, **kwargs: pytest.fail("entitlement must not be consulted after permission denial"),
    )

    response = client.get("/api/school/classes")

    assert response.status_code == 403
    assert response.get_json() == {"error": "permission_denied"}


def test_read_snapshot_rejects_identity_revoked_after_middleware_and_clears_session(
    tenant_read_env, monkeypatch
):
    env = tenant_read_env
    client = _session_client(env, env["admin"])
    original = identity.active_tenant_identity

    def revoke_in_snapshot(conn, **kwargs):
        if conn.in_transaction:
            return False
        return original(conn, **kwargs)

    monkeypatch.setattr(identity, "active_tenant_identity", revoke_in_snapshot)
    response = client.get("/api/school/classes")

    assert response.status_code == 401
    assert response.get_json() == {"error": "school_identity_inactive"}
    with client.session_transaction() as session:
        assert "user_id" not in session
        assert "institution_id" not in session


def test_read_snapshot_refuses_to_nest_in_a_caller_transaction():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("BEGIN")
    with pytest.raises(ValueError, match="connection_has_open_transaction"):
        with tenant_read.tenant_read_transaction(
            conn, user_id="user-a", institution_id="tenant-a"
        ):
            pass
    assert conn.in_transaction
    conn.rollback()
    conn.close()
