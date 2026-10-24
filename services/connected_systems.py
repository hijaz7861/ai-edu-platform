"""Secure metadata-only Connected Systems registry.

Credentials are referenced by environment-variable name; raw credentials are never
stored in SQLite or returned by the API. Outbound HTTPS is allowlist-only, DNS
addresses are validated and pinned for the connection, and redirects are not followed.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit


VALID_STATUSES = {"registered", "active", "paused", "revoked"}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,99}$")
_MAX_RESPONSE_BYTES = 65536


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id():
    return str(uuid.uuid4())


def _event(conn, system_id, actor_id, event_type, detail):
    conn.execute(
        """INSERT INTO connected_system_events
           (id, system_id, actor_id, event_type, detail_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (_id(), system_id, actor_id, event_type, json.dumps(detail, sort_keys=True), _now()),
    )


def _resolve_public_addresses(host, port, *, allow_private=False, resolver=None):
    resolve = resolver or socket.getaddrinfo
    try:
        records = resolve(host, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        raise ValueError("connected_system_host_unresolvable") from None
    addresses = []
    for record in records:
        raw_ip = record[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise ValueError("connected_system_dns_invalid") from None
        if not allow_private and not ip.is_global:
            raise ValueError("connected_system_private_address_blocked")
        if raw_ip not in addresses:
            addresses.append(raw_ip)
    if not addresses:
        raise ValueError("connected_system_host_unresolvable")
    return addresses


def validate_base_url(base_url, *, allowed_hosts=None, allow_private=False, resolver=None):
    """Validate HTTPS URL, exact host allowlist, port, and resolved IPs."""
    value = str(base_url or "").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
    except ValueError:
        raise ValueError("connected_system_url_invalid") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port != 443
    ):
        raise ValueError("connected_system_url_invalid")

    host = parsed.hostname.lower().rstrip(".")
    hosts = {str(item).strip().lower().rstrip(".") for item in (allowed_hosts or []) if str(item).strip()}
    if not hosts or host not in hosts:
        raise ValueError("connected_system_host_not_allowlisted")
    addresses = _resolve_public_addresses(host, port, allow_private=allow_private, resolver=resolver)
    path = parsed.path.rstrip("/")
    normalized = "https://" + host + (path if path else "")
    return normalized, host, port, addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the validated IP while preserving hostname TLS/SNI verification."""

    def __init__(self, host, port, pinned_ip, timeout):
        super().__init__(host=host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def create_system(
    conn,
    *,
    name,
    base_url,
    created_by,
    auth_env_var=None,
    institution_id=None,
    scopes=None,
    allowed_hosts=None,
    allow_private=False,
):
    name = str(name or "").strip()
    actor = str(created_by or "").strip()
    if not name or len(name) > 120:
        raise ValueError("connected_system_name_invalid")
    if not actor:
        raise ValueError("connected_system_actor_required")
    if auth_env_var:
        auth_env_var = str(auth_env_var).strip()
        if not _ENV_NAME.fullmatch(auth_env_var):
            raise ValueError("connected_system_auth_env_var_invalid")
    else:
        auth_env_var = None
    scopes = scopes or []
    if not isinstance(scopes, list) or any(not isinstance(s, str) or not s.strip() or len(s) > 80 for s in scopes):
        raise ValueError("connected_system_scopes_invalid")
    if len(scopes) > 30 or len(set(scopes)) != len(scopes):
        raise ValueError("connected_system_scopes_invalid")
    if institution_id:
        row = conn.execute("SELECT id FROM institutions WHERE id = ?", (institution_id,)).fetchone()
        if not row:
            raise ValueError("institution_not_found")
    normalized, _, _, _ = validate_base_url(
        base_url, allowed_hosts=allowed_hosts, allow_private=allow_private
    )
    system_id = _id()
    now = _now()
    conn.execute(
        """INSERT INTO connected_systems
           (id, name, base_url, auth_env_var, institution_id, scopes_json, status,
            created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'registered', ?, ?, ?)""",
        (system_id, name, normalized, auth_env_var, institution_id,
         json.dumps(scopes, sort_keys=True), actor, now, now),
    )
    _event(conn, system_id, actor, "registered", {"institution_id": institution_id, "scopes": scopes})
    conn.commit()
    return system_id


def list_systems(conn):
    rows = conn.execute(
        """SELECT id, name, base_url, auth_env_var, institution_id, scopes_json,
                  status, last_health_status, last_health_at, last_error_code,
                  created_by, created_at, updated_at
           FROM connected_systems ORDER BY created_at DESC"""
    ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        env_name = item.pop("auth_env_var", None)
        item["authentication_configured"] = bool(env_name and os.environ.get(env_name, "").strip())
        item["scopes"] = json.loads(item.pop("scopes_json") or "[]")
        results.append(item)
    return results


def _health_request(base_url, host, port, addresses, token=None, timeout=8):
    parsed = urlsplit(base_url)
    path = (parsed.path.rstrip("/") + "/health") or "/health"
    connection = _PinnedHTTPSConnection(host, port, addresses[0], max(1, min(int(timeout), 15)))
    headers = {"Accept": "application/json", "User-Agent": "AI-Edu-Connected-Systems/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = int(response.status)
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("connected_system_response_too_large")
        if 300 <= status < 400:
            raise RuntimeError("connected_system_redirect_blocked")
        if not 200 <= status < 300:
            raise RuntimeError("connected_system_http_" + str(status))
        return {"status": "healthy", "http_status": status}
    except RuntimeError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError):
        raise RuntimeError("connected_system_connection_failed") from None
    finally:
        connection.close()


def test_connection(conn, system_id, *, actor_id, environ=None, allowed_hosts=None, allow_private=False, timeout=8):
    env = os.environ if environ is None else environ
    row = conn.execute("SELECT * FROM connected_systems WHERE id = ?", (system_id,)).fetchone()
    if not row:
        raise ValueError("connected_system_not_found")
    if row["status"] in {"paused", "revoked"}:
        raise ValueError("connected_system_not_testable_in_current_state")

    checked_at = _now()
    try:
        normalized, host, port, addresses = validate_base_url(
            row["base_url"], allowed_hosts=allowed_hosts, allow_private=allow_private
        )
        token = None
        if row["auth_env_var"]:
            token = str(env.get(row["auth_env_var"], "") or "").strip()
            if not token:
                raise RuntimeError("connected_system_credential_not_configured")
        result = _health_request(normalized, host, port, addresses, token=token, timeout=timeout)
        health_status = "healthy"
        error_code = None
        detail = {"status": health_status, "http_status": result["http_status"]}
    except ValueError as exc:
        health_status = "unhealthy"
        error_code = str(exc)
        detail = {"status": health_status, "error_code": error_code}
        result = None
    except RuntimeError as exc:
        health_status = "unhealthy"
        error_code = str(exc)
        detail = {"status": health_status, "error_code": error_code}
        result = None

    conn.execute(
        """UPDATE connected_systems
           SET last_health_status = ?, last_health_at = ?, last_error_code = ?, updated_at = ?
           WHERE id = ?""",
        (health_status, checked_at, error_code, checked_at, system_id),
    )
    _event(conn, system_id, str(actor_id or ""), "health_check", detail)
    conn.commit()
    if result is None:
        raise RuntimeError(error_code or "connected_system_health_check_failed")
    result["checked_at"] = checked_at
    return result


def change_status(conn, system_id, *, status, actor_id, reason):
    if status not in VALID_STATUSES:
        raise ValueError("connected_system_status_invalid")
    actor = str(actor_id or "").strip()
    reason = str(reason or "").strip()
    if not actor or not reason:
        raise ValueError("connected_system_actor_and_reason_required")
    row = conn.execute(
        "SELECT status, last_health_status FROM connected_systems WHERE id = ?",
        (system_id,),
    ).fetchone()
    if not row:
        raise ValueError("connected_system_not_found")
    current = row["status"]
    if current == "revoked":
        raise ValueError("connected_system_revoked_is_terminal")
    if status == "active" and row["last_health_status"] != "healthy":
        raise ValueError("connected_system_health_check_required")
    if current == status:
        raise ValueError("connected_system_status_unchanged")
    if status == "registered" and current != "registered":
        raise ValueError("connected_system_status_transition_invalid")
    now = _now()
    conn.execute(
        "UPDATE connected_systems SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, system_id),
    )
    _event(conn, system_id, actor, "status_changed", {"from": current, "to": status, "reason": reason})
    conn.commit()
    return status
