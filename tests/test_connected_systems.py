import json
import socket

import pytest

from db import init_db
import services.connected_systems as systems


PUBLIC_DNS_RECORD = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))


@pytest.fixture
def conn():
    connection = init_db(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def allow_public_dns(monkeypatch):
    monkeypatch.setattr(systems.socket, "getaddrinfo", lambda *args, **kwargs: [PUBLIC_DNS_RECORD])


def create_test_system(conn, monkeypatch, **overrides):
    allow_public_dns(monkeypatch)
    options = {
        "name": "Research service",
        "base_url": "https://api.example.com",
        "created_by": "platform-admin",
        "auth_env_var": "CONNECTED_TEST_TOKEN",
        "scopes": ["health:read"],
        "allowed_hosts": ["api.example.com"],
    }
    options.update(overrides)
    return systems.create_system(conn, **options)


def test_registry_stores_only_credential_reference(conn, monkeypatch):
    monkeypatch.setenv("CONNECTED_TEST_TOKEN", "never-store-this-secret")
    system_id = create_test_system(conn, monkeypatch)

    row = conn.execute(
        "SELECT * FROM connected_systems WHERE id = ?", (system_id,)
    ).fetchone()
    serialized = json.dumps(dict(row))
    assert "never-store-this-secret" not in serialized
    assert row["auth_env_var"] == "CONNECTED_TEST_TOKEN"
    listed = systems.list_systems(conn)
    assert listed[0]["authentication_configured"] is True
    assert "never-store-this-secret" not in json.dumps(listed)


def test_registry_rejects_unlisted_hosts(conn, monkeypatch):
    with pytest.raises(ValueError, match="host_not_allowlisted"):
        systems.create_system(
            conn,
            name="Bad host",
            base_url="https://not-allowed.example",
            created_by="platform-admin",
            allowed_hosts=["api.example.com"],
        )


def test_registry_blocks_private_dns_results_by_default(conn, monkeypatch):
    private_record = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    monkeypatch.setattr(systems.socket, "getaddrinfo", lambda *args, **kwargs: [private_record])
    with pytest.raises(ValueError, match="private_address_blocked"):
        systems.create_system(
            conn,
            name="Private host",
            base_url="https://api.example.com",
            created_by="platform-admin",
            allowed_hosts=["api.example.com"],
        )


def test_registry_rejects_http_and_url_credentials(conn, monkeypatch):
    with pytest.raises(ValueError, match="url_invalid"):
        systems.create_system(
            conn,
            name="Plain HTTP",
            base_url="http://api.example.com",
            created_by="platform-admin",
            allowed_hosts=["api.example.com"],
        )
    with pytest.raises(ValueError, match="url_invalid"):
        systems.create_system(
            conn,
            name="Embedded credential",
            base_url="https://user:pass@api.example.com",
            created_by="platform-admin",
            allowed_hosts=["api.example.com"],
        )


def test_health_check_uses_env_credential_and_requires_success(conn, monkeypatch):
    monkeypatch.setenv("CONNECTED_TEST_TOKEN", "secret-from-environment")
    system_id = create_test_system(conn, monkeypatch)
    captured = {}

    class FakeResponse:
        status = 200

        def read(self, size):
            return b'{"status":"ok"}'

    class FakeConnection:
        def __init__(self, host, port, pinned_ip, timeout):
            captured.update(host=host, port=port, pinned_ip=pinned_ip, timeout=timeout)

        def request(self, method, path, headers):
            captured.update(method=method, path=path, headers=headers)

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(systems, "_PinnedHTTPSConnection", FakeConnection)
    result = systems.test_connection(
        conn,
        system_id,
        actor_id="platform-admin",
        environ={"CONNECTED_TEST_TOKEN": "secret-from-environment"},
        allowed_hosts=["api.example.com"],
    )
    assert result["status"] == "healthy"
    assert result["http_status"] == 200
    assert captured["pinned_ip"] == "93.184.216.34"
    assert captured["headers"]["Authorization"] == "Bearer secret-from-environment"
    assert "secret-from-environment" not in json.dumps(systems.list_systems(conn))


def test_connection_failure_is_audited_without_secret(conn, monkeypatch):
    monkeypatch.setenv("CONNECTED_TEST_TOKEN", "secret-token")
    system_id = create_test_system(conn, monkeypatch)
    monkeypatch.setattr(
        systems,
        "_health_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("connected_system_http_503")),
    )
    with pytest.raises(RuntimeError, match="connected_system_http_503"):
        systems.test_connection(
            conn,
            system_id,
            actor_id="platform-admin",
            environ={"CONNECTED_TEST_TOKEN": "secret-token"},
            allowed_hosts=["api.example.com"],
        )
    row = conn.execute(
        "SELECT last_health_status, last_error_code FROM connected_systems WHERE id = ?",
        (system_id,),
    ).fetchone()
    assert row["last_health_status"] == "unhealthy"
    assert row["last_error_code"] == "connected_system_http_503"
    event = conn.execute(
        "SELECT event_type, detail_json FROM connected_system_events WHERE system_id = ?",
        (system_id,),
    ).fetchone()
    assert event["event_type"] == "health_check"
    assert "secret-token" not in event["detail_json"]


def test_activation_requires_healthy_check_and_revocation_is_terminal(conn, monkeypatch):
    system_id = create_test_system(conn, monkeypatch)
    with pytest.raises(ValueError, match="health_check_required"):
        systems.change_status(
            conn, system_id, status="active", actor_id="platform-admin", reason="activate"
        )

    monkeypatch.setattr(systems, "_health_request", lambda *args, **kwargs: {"status": "healthy", "http_status": 200})
    systems.test_connection(
        conn,
        system_id,
        actor_id="platform-admin",
        environ={},
        allowed_hosts=["api.example.com"],
    )
    assert systems.change_status(
        conn, system_id, status="active", actor_id="platform-admin", reason="health verified"
    ) == "active"
    assert systems.change_status(
        conn, system_id, status="revoked", actor_id="platform-admin", reason="retired"
    ) == "revoked"
    with pytest.raises(ValueError, match="revoked_is_terminal"):
        systems.change_status(
            conn, system_id, status="active", actor_id="platform-admin", reason="should fail"
        )
    assert conn.execute(
        "SELECT COUNT(*) AS total FROM connected_system_events WHERE system_id = ?", (system_id,)
    ).fetchone()["total"] >= 4


def test_missing_credential_does_not_attempt_network(conn, monkeypatch):
    system_id = create_test_system(conn, monkeypatch)

    def unexpected(*args, **kwargs):
        raise AssertionError("network should not be attempted without configured credential")

    monkeypatch.setattr(systems, "_health_request", unexpected)
    with pytest.raises(RuntimeError, match="credential_not_configured"):
        systems.test_connection(
            conn,
            system_id,
            actor_id="platform-admin",
            environ={},
            allowed_hosts=["api.example.com"],
        )
