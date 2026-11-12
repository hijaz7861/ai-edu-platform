"""Adversarial tests for tenant-bound job enqueue, claims and execution."""
import json
import re
import sqlite3
import sys
import uuid
import pytest
from db import get_connection, init_db
from services import core, identity
from services.tenant_job_handlers import HANDLERS
from services.tenant_jobs import (RetryableJobError, LeaseLost, _finish_success, claim_next_job, enqueue_job, get_tenant_job, renew_job_lease, run_once)


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    path = str(tmp_path / "tenant-jobs.sqlite")
    conn = init_db(path)
    core.seed_default_roles(conn)
    suffix = uuid.uuid4().hex[:10]
    a = core.create_institution(conn, "Job Tenant A", "JA-" + suffix)
    b = core.create_institution(conn, "Job Tenant B", "JB-" + suffix)
    ca = core.create_campus(conn, a, "Main", "JA-MAIN-" + suffix)
    cb = core.create_campus(conn, b, "Main", "JB-MAIN-" + suffix)
    core.create_class(conn, a, ca, "A class", "2026")
    core.create_class(conn, b, cb, "B class", "2026")
    ua = identity.create_school_user(
        conn, email=f"a-{suffix}@school.example", password="correct horse battery staple",
        institution_id=a, role_name="Institution Admin", granted_by="platform-admin")
    ub = identity.create_school_user(
        conn, email=f"b-{suffix}@school.example", password="correct horse battery staple",
        institution_id=b, role_name="Institution Admin", granted_by="platform-admin")
    uc = identity.create_school_user(
        conn, email=f"c-{suffix}@school.example", password="correct horse battery staple",
        institution_id=a, role_name="Academic Coordinator", granted_by="platform-admin")
    conn.close()
    import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", path)
    app_module.app.config.update(TESTING=True)
    yield {"path": path, "app": app_module, "a": a, "b": b, "ua": ua, "ub": ub, "uc": uc,
           "client_a": app_module.app.test_client(), "client_b": app_module.app.test_client(),
           "client_c": app_module.app.test_client()}


def login(client, user):
    page = client.get("/")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
    assert match
    response = client.post("/auth/school-login", data={
        "email": user["email"], "password": "correct horse battery staple",
        "csrf_token": match.group(1)})
    assert response.status_code == 302
    with client.session_transaction() as session:
        return session["csrf_token"]


def enqueue(conn, tenant, user, key, resources=None):
    return enqueue_job(conn, institution_id=tenant, actor_id=user["id"],
                       job_type="tenant_health_check",
                       payload={"resources": resources} if resources is not None else {},
                       idempotency_key=key)


def test_enqueue_derives_tenant_and_actor_from_session(job_env):
    e = job_env
    csrf = login(e["client_a"], e["ua"])
    response = e["client_a"].post("/api/school/jobs", json={"resources": ["classes"]},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "tenant-a-job-0001"})
    assert response.status_code == 202
    conn = init_db(e["path"])
    row = conn.execute("SELECT institution_id, actor_id, status FROM tenant_jobs WHERE id=?",
                       (response.get_json()["id"],)).fetchone()
    assert row["institution_id"] == e["a"]
    assert row["actor_id"] == e["ua"]["id"]
    assert row["status"] == "queued"
    conn.close()


def test_api_rejects_client_tenant_actor_and_missing_idempotency(job_env):
    e = job_env
    csrf = login(e["client_a"], e["ua"])
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "tenant-a-job-0002"}
    response = e["client_a"].post("/api/school/jobs",
        json={"institution_id": e["b"], "resources": ["classes"]}, headers=headers)
    assert response.status_code == 400
    response = e["client_a"].post("/api/school/jobs", json={"actor_id": e["ub"]["id"]},
        headers=headers)
    assert response.status_code == 400
    response = e["client_a"].post("/api/school/jobs", json={"resources": ["classes"]},
        headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400


def test_idempotency_replay_and_payload_conflict(job_env):
    e = job_env
    csrf = login(e["client_a"], e["ua"])
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "tenant-a-job-0003"}
    first = e["client_a"].post("/api/school/jobs", json={"resources": ["classes"]}, headers=headers)
    replay = e["client_a"].post("/api/school/jobs", json={"resources": ["classes"]}, headers=headers)
    conflict = e["client_a"].post("/api/school/jobs", json={"resources": ["books"]}, headers=headers)
    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.get_json()["id"] == first.get_json()["id"]
    assert conflict.status_code == 409


def test_foreign_job_is_not_disclosed_and_non_admin_is_denied(job_env):
    e = job_env
    csrf = login(e["client_a"], e["ua"])
    created = e["client_a"].post("/api/school/jobs", json={"resources": ["classes"]},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "tenant-a-job-0004"})
    login(e["client_b"], e["ub"])
    foreign = e["client_b"].get("/api/school/jobs/" + created.get_json()["id"])
    assert foreign.status_code == 404
    csrf_c = login(e["client_c"], e["uc"])
    # A principal/coordinator role without manage_institution cannot read
    # another user's job result even though the job belongs to the same tenant.
    admin_job = e["client_a"].post("/api/school/jobs", json={"resources": ["classes"]},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "tenant-a-job-0005"})
    denied = e["client_c"].post("/api/school/jobs", json={"resources": ["classes"]},
        headers={"X-CSRF-Token": csrf_c, "Idempotency-Key": "tenant-a-job-0006"})
    assert denied.status_code == 403
    read_denied = e["client_c"].get("/api/school/jobs/" + admin_job.get_json()["id"])
    assert read_denied.status_code == 403


def test_worker_revalidates_permission_before_execution(job_env):
    e = job_env
    conn = init_db(e["path"])
    job_id, _ = enqueue(conn, e["a"], e["ua"], "worker-auth-0001", ["classes"])
    roles = core.seed_default_roles(conn)
    conn.execute("DELETE FROM role_assignments WHERE user_id=?", (e["ua"]["id"],))
    conn.commit()
    core.assign_role(conn, e["ua"]["id"], roles["Academic Coordinator"], "institution",
                     e["a"], granted_by="platform-admin")
    called = []
    result = run_once(conn, "worker-auth-test",
                      {"tenant_health_check": lambda *_: called.append(True) or {"ok": True}})
    row = conn.execute("SELECT status,last_error_code FROM tenant_jobs WHERE id=?", (job_id,)).fetchone()
    assert result == {"job_id": job_id, "status": "failed"}
    assert called == []
    assert row["last_error_code"] == "authorization_revoked"
    conn.close()


def test_worker_rejects_tampered_tenant_binding(job_env):
    e = job_env
    conn = init_db(e["path"])
    job_id, _ = enqueue(conn, e["a"], e["ua"], "worker-tenant-0001", ["classes"])
    conn.execute("UPDATE tenant_jobs SET institution_id=? WHERE id=?", (e["b"], job_id))
    conn.commit()
    result = run_once(conn, "worker-tenant-test", HANDLERS)
    assert result == {"job_id": job_id, "status": "failed"}
    conn.close()


def test_worker_result_is_scoped_to_own_tenant(job_env):
    e = job_env
    conn = init_db(e["path"])
    job_id, _ = enqueue(conn, e["a"], e["ua"], "worker-result-0001", ["classes"])
    assert run_once(conn, "worker-result-test", HANDLERS) == {
        "job_id": job_id, "status": "succeeded"}
    job = get_tenant_job(conn, job_id=job_id, institution_id=e["a"])
    assert job["result"]["resource_counts"] == {"classes": 1}
    assert get_tenant_job(conn, job_id=job_id, institution_id=e["b"]) is None
    conn.close()


def test_claim_is_single_owner_and_expired_lease_is_reclaimed(job_env):
    e = job_env
    first = init_db(e["path"])
    second = get_connection(e["path"])
    job_id, _ = enqueue(first, e["a"], e["ua"], "worker-lease-0001", ["classes"])
    claim = claim_next_job(first, "worker-one", lease_seconds=30)
    assert claim["id"] == job_id and claim["attempt_count"] == 1
    assert claim_next_job(second, "worker-two", lease_seconds=30) is None
    second.execute("UPDATE tenant_jobs SET lease_expires_at=? WHERE id=?",
                   ("2000-01-01T00:00:00+00:00", job_id))
    second.commit()
    reclaimed = claim_next_job(second, "worker-two", lease_seconds=30)
    assert reclaimed["id"] == job_id and reclaimed["attempt_count"] == 2
    # The old worker cannot publish a result after another worker reclaimed its lease.
    with pytest.raises(LeaseLost):
        _finish_success(first, job_id=job_id, worker_id="worker-one",
                        result_json='{"stale":true}')
    first.close()
    second.close()


def test_worker_context_heartbeat_renews_only_its_live_lease(job_env):
    e = job_env
    conn = init_db(e["path"])
    job_id, _ = enqueue(conn, e["a"], e["ua"], "worker-heartbeat-0001", ["classes"])
    observed = {}

    def handler(_conn, context):
        before = conn.execute(
            "SELECT lease_expires_at FROM tenant_jobs WHERE id=?", (job_id,)
        ).fetchone()["lease_expires_at"]
        renewed = context.heartbeat()
        after = conn.execute(
            "SELECT lease_expires_at FROM tenant_jobs WHERE id=?", (job_id,)
        ).fetchone()["lease_expires_at"]
        observed.update(before=before, renewed=renewed, after=after)
        with pytest.raises(LeaseLost):
            renew_job_lease(conn, job_id, "different-worker", lease_seconds=30)
        return {"heartbeat": "verified"}

    result = run_once(
        conn, "worker-heartbeat-owner", {"tenant_health_check": handler},
        lease_seconds=30,
    )
    assert result == {"job_id": job_id, "status": "succeeded"}
    assert observed["renewed"] == observed["after"]
    assert observed["after"] > observed["before"]
    conn.close()


def test_expired_worker_cannot_revive_lease_and_heartbeat_rejects_open_transaction(job_env):
    e = job_env
    conn = init_db(e["path"])
    job_id, _ = enqueue(conn, e["a"], e["ua"], "worker-heartbeat-0002", ["classes"])
    claim = claim_next_job(conn, "worker-old", lease_seconds=30)
    assert claim["id"] == job_id

    conn.execute(
        "UPDATE tenant_jobs SET lease_expires_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", job_id),
    )
    conn.commit()
    with pytest.raises(LeaseLost):
        renew_job_lease(conn, job_id, "worker-old", lease_seconds=30)

    reclaimed = claim_next_job(conn, "worker-new", lease_seconds=30)
    assert reclaimed["id"] == job_id
    with pytest.raises(LeaseLost):
        renew_job_lease(conn, job_id, "worker-old", lease_seconds=30)
    assert renew_job_lease(conn, job_id, "worker-new", lease_seconds=30)

    conn.execute("BEGIN")
    with pytest.raises(ValueError, match="connection_has_open_transaction"):
        renew_job_lease(conn, job_id, "worker-new", lease_seconds=30)
    conn.rollback()
    conn.close()


def test_school_route_permissions_match_institution_role_matrix(job_env):
    e = job_env
    admin_csrf = login(e["client_a"], e["ua"])
    admin_job = e["client_a"].post(
        "/api/school/jobs", json={"resources": ["classes"]},
        headers={"X-CSRF-Token": admin_csrf, "Idempotency-Key": "role-matrix-admin-job"},
    )
    assert admin_job.status_code == 202
    admin_job_id = admin_job.get_json()["id"]

    roles = [
        ("Institution Admin", e["ua"], True, True, True),
        ("Principal", None, True, True, False),
        ("Academic Coordinator", None, True, False, False),
        ("Examiner", None, False, False, False),
        ("HR/Attendance Officer", None, False, False, False),
    ]
    conn = init_db(e["path"])
    users = []
    suffix = uuid.uuid4().hex[:10]
    for index, (role_name, existing_user, can_view_classes, can_read_jobs, can_manage) in enumerate(roles):
        user = existing_user or identity.create_school_user(
            conn,
            email=f"role-{index}-{suffix}@school.example",
            password="correct horse battery staple",
            institution_id=e["a"],
            role_name=role_name,
            granted_by="platform-admin",
        )
        users.append((role_name, user, can_view_classes, can_read_jobs, can_manage))
    conn.close()

    for index, (role_name, user, can_view_classes, can_read_jobs, can_manage) in enumerate(users):
        client = e["app"].app.test_client()
        csrf = login(client, user)

        # Self-profile is safe for every authenticated institution role.
        assert client.get("/api/school/me").status_code == 200

        classes = client.get("/api/school/classes")
        assert classes.status_code == (200 if can_view_classes else 403), role_name

        create_class = client.post(
            "/api/school/classes",
            json={"name": f"RBAC {role_name} {index}", "academic_year": "2026-2027"},
            headers={"X-CSRF-Token": csrf},
        )
        assert create_class.status_code == (201 if can_manage else 403), role_name

        create_job = client.post(
            "/api/school/jobs",
            json={"resources": ["classes"]},
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"role-matrix-job-{index:02d}",
            },
        )
        assert create_job.status_code == (202 if can_manage else 403), role_name

        read_job = client.get("/api/school/jobs/" + admin_job_id)
        assert read_job.status_code == (200 if can_read_jobs else 403), role_name


def test_only_explicit_retryable_failure_is_retried(job_env):
    e = job_env
    conn = init_db(e["path"])
    job_id, _ = enqueue(conn, e["a"], e["ua"], "worker-retry-0001", ["classes"])
    calls = []
    def handler(_conn, _ctx):
        calls.append(True)
        if len(calls) == 1:
            raise RetryableJobError()
        return {"ok": True}
    first = run_once(conn, "worker-retry-test", {"tenant_health_check": handler})
    assert first["status"] == "queued"
    conn.execute("UPDATE tenant_jobs SET available_at=? WHERE id=?",
                 ("2000-01-01T00:00:00+00:00", job_id))
    conn.commit()
    second = run_once(conn, "worker-retry-test", {"tenant_health_check": handler})
    assert second == {"job_id": job_id, "status": "succeeded"}
    assert len(calls) == 2
    conn.close()


def test_handler_exception_text_is_not_persisted(job_env):
    e = job_env
    conn = init_db(e["path"])
    job_id, _ = enqueue(conn, e["a"], e["ua"], "worker-error-0001", ["classes"])
    def broken(_conn, _ctx):
        raise RuntimeError("provider-secret=must-not-persist")
    result = run_once(conn, "worker-error-test", {"tenant_health_check": broken})
    row = conn.execute("SELECT last_error_code FROM tenant_jobs WHERE id=?", (job_id,)).fetchone()
    events = conn.execute("SELECT detail_json FROM tenant_job_events WHERE job_id=?", (job_id,)).fetchall()
    assert result["status"] == "failed"
    assert row["last_error_code"] == "handler_error"
    assert "provider-secret" not in json.dumps([dict(x) for x in events])
    conn.close()


def test_job_events_are_append_only_and_init_is_repeatable(job_env):
    e = job_env
    conn = init_db(e["path"])
    job_id, _ = enqueue(conn, e["a"], e["ua"], "worker-audit-0001", ["classes"])
    event = conn.execute("SELECT id FROM tenant_job_events WHERE job_id=? LIMIT 1",
                         (job_id,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE tenant_job_events SET event_type='job_failed' WHERE id=?",
                     (event["id"],))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM tenant_job_events WHERE id=?", (event["id"],))
    conn.rollback()
    conn.close()
    reopened = init_db(e["path"])
    assert reopened.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tenant_jobs'").fetchone()[0] == 1
    reopened.close()


def test_enqueue_rejects_open_transactions_and_unsupported_inputs(job_env):
    e = job_env
    conn = init_db(e["path"])
    conn.execute("BEGIN")
    with pytest.raises(ValueError, match="connection_has_open_transaction"):
        enqueue(conn, e["a"], e["ua"], "worker-open-0001", ["classes"])
    conn.rollback()
    with pytest.raises(ValueError, match="unsupported_job_type"):
        enqueue_job(conn, institution_id=e["a"], actor_id=e["ua"]["id"],
                    job_type="run_arbitrary_code", payload={},
                    idempotency_key="worker-unknown-0001")
    with pytest.raises(ValueError, match="unsupported_job_payload"):
        enqueue_job(conn, institution_id=e["a"], actor_id=e["ua"]["id"],
                    job_type="tenant_health_check", payload={"institution_id": e["b"]},
                    idempotency_key="worker-payload-0001")
    conn.close()


def test_registered_school_job_routes_match_exact_allowlist(job_env):
    from tests.test_school_legacy_route_denials import SCHOOL_API_ROUTE_METHODS
    app = job_env["app"].app
    registered = {
        (rule.endpoint, method) for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api/school/")
        for method in (set(rule.methods or ()) - {"HEAD", "OPTIONS"})
    }
    assert registered == SCHOOL_API_ROUTE_METHODS



def test_worker_cli_sigterm_finishes_current_job_then_stops(monkeypatch, capsys):
    from tools import run_tenant_jobs as worker_cli

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    connections = []

    def make_connection(*_args, **_kwargs):
        connection = FakeConnection()
        connections.append(connection)
        return connection

    signal_handlers = {}

    def fake_signal(signum, handler):
        previous = signal_handlers.get(signum, "default-handler")
        signal_handlers[signum] = handler
        return previous

    monkeypatch.setattr(sys, "argv", [
        "run_tenant_jobs.py", "--loop", "--worker-id", "worker-test",
        "--poll-interval", "0.25",
    ])
    monkeypatch.setattr(worker_cli.signal, "signal", fake_signal)
    monkeypatch.setattr(worker_cli, "init_db", make_connection)
    monkeypatch.setattr(worker_cli, "get_connection", make_connection)
    monkeypatch.setattr(worker_cli, "HANDLERS", {})
    calls = []

    def run_active_job(conn, worker_id, handlers):
        calls.append(worker_id)
        signal_handlers[worker_cli.signal.SIGTERM](
            worker_cli.signal.SIGTERM, None
        )
        return {"job_id": "job-1", "status": "succeeded"}

    monkeypatch.setattr(worker_cli, "run_once", run_active_job)
    monkeypatch.setattr(
        worker_cli.threading.Event, "wait",
        lambda _self, _interval: pytest.fail("worker waited after shutdown was requested"),
    )

    assert worker_cli.main() == 0
    assert calls == ["worker-test"]
    assert all(connection.closed for connection in connections)
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output == [
        {"job_id": "job-1", "status": "succeeded"},
        {"status": "shutdown"},
    ]


def test_worker_cli_sigterm_during_idle_poll_prevents_another_claim(monkeypatch, capsys):
    from tools import run_tenant_jobs as worker_cli

    class FakeConnection:
        def close(self):
            self.closed = True

    signal_handlers = {}
    calls = []

    class FakeEvent:
        def __init__(self):
            self.flag = False

        def set(self):
            self.flag = True

        def is_set(self):
            return self.flag

        def wait(self, _interval):
            if not self.flag:
                signal_handlers[worker_cli.signal.SIGTERM](
                    worker_cli.signal.SIGTERM, None
                )
            return self.flag

    def fake_signal(signum, handler):
        previous = signal_handlers.get(signum, "default-handler")
        signal_handlers[signum] = handler
        return previous

    monkeypatch.setattr(worker_cli.threading, "Event", FakeEvent)
    monkeypatch.setattr(sys, "argv", [
        "run_tenant_jobs.py", "--loop", "--worker-id", "worker-idle",
        "--poll-interval", "0.25",
    ])
    monkeypatch.setattr(worker_cli.signal, "signal", fake_signal)
    monkeypatch.setattr(worker_cli, "init_db", lambda *_args: FakeConnection())
    monkeypatch.setattr(worker_cli, "get_connection", lambda *_args: FakeConnection())
    monkeypatch.setattr(worker_cli, "HANDLERS", {})

    def idle_once(_conn, _worker_id, _handlers):
        calls.append("claimed")
        return None

    monkeypatch.setattr(worker_cli, "run_once", idle_once)

    assert worker_cli.main() == 0
    assert calls == ["claimed"]
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output == [{"status": "shutdown"}]
