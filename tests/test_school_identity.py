"""Integration tests for persistent school identities and tenant-bound sessions."""
import re
import uuid

import pytest

from db import init_db
from services import core, identity


@pytest.fixture
def school_app(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "0")
    db_path = str(tmp_path / "school-auth.sqlite")
    monkeypatch.setattr(app_module, "DB_PATH", db_path)

    conn = init_db(db_path)
    core.seed_default_roles(conn)
    suffix = uuid.uuid4().hex[:10]
    inst_a = core.create_institution(conn, "Tenant A", "TA-" + suffix)
    inst_b = core.create_institution(conn, "Tenant B", "TB-" + suffix)
    campus_a = core.create_campus(conn, inst_a, "Main", "A-" + suffix)
    campus_b = core.create_campus(conn, inst_b, "Main", "B-" + suffix)
    class_a = core.create_class(conn, inst_a, campus_a, "A class", "2026")
    core.create_class(conn, inst_b, campus_b, "B private class", "2026")
    created = identity.create_school_user(
        conn,
        email="admin+" + suffix + "@school.example",
        password="correct horse battery staple",
        institution_id=inst_a,
        role_name="Institution Admin",
        granted_by="platform-admin",
    )
    conn.close()

    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        yield {
            "client": client,
            "app": app_module,
            "user": created,
            "institution_a": inst_a,
            "institution_b": inst_b,
            "class_a": class_a,
            "db_path": db_path,
        }


def _csrf(client):
    response = client.get("/")
    assert response.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert token, "login page must render a CSRF token"
    return token.group(1)


def _login(client, user):
    token = _csrf(client)
    response = client.post(
        "/auth/school-login",
        data={"email": user["email"], "password": "correct horse battery staple", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/school")
    return response


def test_school_user_login_stores_hash_and_binds_tenant(school_app):
    conn = init_db(school_app["db_path"])
    row = conn.execute(
        "SELECT password_hash, institution_id FROM users WHERE id = ?",
        (school_app["user"]["id"],),
    ).fetchone()
    assert row["password_hash"] != "correct horse battery staple"
    assert row["password_hash"].startswith("scrypt:")
    assert row["institution_id"] == school_app["institution_a"]
    assert identity.authenticate_school_user(
        conn, email=school_app["user"]["email"], password="incorrect password"
    ) is None
    assert identity.authenticate_school_user(
        conn, email=school_app["user"]["email"], password="correct horse battery staple"
    )["institution_id"] == school_app["institution_a"]
    conn.close()


def test_school_user_sees_only_own_institution_classes(school_app):
    client = school_app["client"]
    _login(client, school_app["user"])
    response = client.get("/api/school/classes")
    assert response.status_code == 200
    payload = response.get_json()
    assert [item["id"] for item in payload["classes"]] == [school_app["class_a"]]
    assert all(item["name"] != "B private class" for item in payload["classes"])


def test_school_user_cannot_access_platform_admin_api(school_app):
    client = school_app["client"]
    _login(client, school_app["user"])
    response = client.get("/api/admin/plans")
    assert response.status_code == 403
    assert response.get_json()["error"] == "platform_admin_required"


def test_tampered_cross_tenant_session_is_rejected_and_cleared(school_app):
    client = school_app["client"]
    _login(client, school_app["user"])
    with client.session_transaction() as session:
        session["institution_id"] = school_app["institution_b"]
    response = client.get("/api/school/me")
    assert response.status_code == 401
    assert response.get_json()["error"] == "school_identity_inactive"
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_school_login_requires_csrf(school_app):
    response = school_app["client"].post(
        "/auth/school-login",
        data={"email": school_app["user"]["email"], "password": "correct horse battery staple"},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "csrf_invalid"}


def test_inactive_user_cannot_authenticate(school_app):
    conn = init_db(school_app["db_path"])
    conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (school_app["user"]["id"],))
    conn.commit()
    assert identity.authenticate_school_user(
        conn, email=school_app["user"]["email"], password="correct horse battery staple"
    ) is None
    conn.close()


def test_user_creation_rejects_platform_role_and_short_password(school_app):
    conn = init_db(school_app["db_path"])
    with pytest.raises(ValueError, match="unsupported_institution_role"):
        identity.create_school_user(
            conn, email="superadmin@example.com", password="correct horse battery staple",
            institution_id=school_app["institution_a"], role_name="Super Admin",
            granted_by="platform-admin",
        )
    with pytest.raises(ValueError, match="password_must_be_12_to_1024_characters"):
        identity.create_school_user(
            conn, email="shortpass@example.com", password="short",
            institution_id=school_app["institution_a"], role_name="Institution Admin",
            granted_by="platform-admin",
        )
    conn.close()



def test_case_insensitive_duplicate_email_is_rejected(school_app):
    conn = init_db(school_app["db_path"])
    with pytest.raises(ValueError, match="email_already_exists"):
        identity.create_school_user(
            conn,
            email=school_app["user"]["email"].upper(),
            password="another strong password",
            institution_id=school_app["institution_a"],
            role_name="Principal",
            granted_by="platform-admin",
        )
    conn.close()
