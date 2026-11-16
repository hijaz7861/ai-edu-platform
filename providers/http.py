"""Bounded, redirect-safe HTTP primitives shared by AI provider adapters.

The circuit breaker is process-local and keyed by provider host. It protects a
single worker process from repeatedly calling a failing upstream; it is not a
distributed breaker or a substitute for provider-side spend limits.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


MAX_PROVIDER_REQUEST_BYTES = 256 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_RESET_SECONDS = 30
MAX_PROVIDER_CIRCUITS = 128


class CircuitOpenError(RuntimeError):
    """Raised while a provider host is in the open or half-open circuit state."""

    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__("AI provider temporarily unavailable")


class _CircuitState:
    def __init__(self):
        self.failures = 0
        self.opened_at = None
        self.probe_in_flight = False
        self.last_used = 0.0


_circuit_lock = threading.Lock()
_circuits = {}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward prompts or credentials to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _circuit_key(request):
    """Key by normalized host/port only; never retain URL paths or credentials."""
    try:
        parsed = urllib.parse.urlsplit(getattr(request, "full_url", ""))
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        host, port = "", None
    return host or "unknown-provider-host", port


def _get_circuit_locked(key, now):
    state = _circuits.get(key)
    if state is not None:
        return state
    if len(_circuits) >= MAX_PROVIDER_CIRCUITS:
        evictable = [
            (candidate.last_used, candidate_key)
            for candidate_key, candidate in _circuits.items()
            if candidate.opened_at is None and not candidate.probe_in_flight
        ]
        if not evictable:
            raise RuntimeError("provider circuit registry capacity reached")
        _, oldest_key = min(evictable)
        del _circuits[oldest_key]
    state = _CircuitState()
    state.last_used = now
    _circuits[key] = state
    return state


def _enter_circuit(key):
    now = time.monotonic()
    with _circuit_lock:
        state = _get_circuit_locked(key, now)
        state.last_used = now
        if state.opened_at is None:
            return
        remaining = CIRCUIT_RESET_SECONDS - (now - state.opened_at)
        if remaining > 0:
            raise CircuitOpenError(math.ceil(remaining))
        if state.probe_in_flight:
            raise CircuitOpenError(1)
        # Exactly one request may probe a recovered upstream.
        state.probe_in_flight = True


def _record_circuit_success(key):
    now = time.monotonic()
    with _circuit_lock:
        state = _circuits.get(key)
        if state is None:
            return
        state.failures = 0
        state.opened_at = None
        state.probe_in_flight = False
        state.last_used = now


def _record_circuit_failure(key):
    now = time.monotonic()
    with _circuit_lock:
        state = _circuits.get(key)
        if state is None:
            return
        state.failures += 1
        state.last_used = now
        if state.probe_in_flight or state.failures >= CIRCUIT_FAILURE_THRESHOLD:
            state.opened_at = now
            state.probe_in_flight = False


def _is_transient_http_error(exc):
    try:
        status = int(exc.code)
    except (TypeError, ValueError):
        return True
    return status in {408, 425, 429} or 500 <= status <= 599


def encode_provider_payload(payload, provider_name: str) -> bytes:
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise RuntimeError(f"{provider_name} provider request payload is invalid") from None
    if len(body) > MAX_PROVIDER_REQUEST_BYTES:
        raise RuntimeError(f"{provider_name} provider request exceeds size limit")
    return body


def open_provider_request(request, *, timeout: int):
    """Open a bounded-time provider request without redirects or retry loops.

    Consecutive connection/time-out failures and HTTP 408/425/429/5xx responses
    open the host circuit after five failures. After 30 seconds one half-open
    probe is allowed. Permanent 4xx responses indicate a reachable upstream and
    do not count as transient failures.
    """
    key = _circuit_key(request)
    _enter_circuit(key)
    try:
        opener = urllib.request.build_opener(NoRedirectHandler())
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if _is_transient_http_error(exc):
            _record_circuit_failure(key)
        else:
            _record_circuit_success(key)
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        _record_circuit_failure(key)
        raise
    except Exception:
        # Release a half-open probe if a local programming/configuration error
        # occurs; such errors must not poison upstream availability state.
        _record_circuit_success(key)
        raise
    _record_circuit_success(key)
    return response


def read_provider_json(response, provider_name: str) -> dict:
    """Read at most 1 MiB and accept only a UTF-8 JSON object."""
    headers = getattr(response, "headers", None)
    content_length = headers.get("Content-Length") if headers is not None else None
    if content_length:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > MAX_PROVIDER_RESPONSE_BYTES:
            raise RuntimeError(f"{provider_name} provider response exceeds size limit")

    raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise RuntimeError(f"{provider_name} provider response exceeds size limit")
    try:
        result = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f"{provider_name} provider returned invalid JSON") from None
    if not isinstance(result, dict):
        raise RuntimeError(f"{provider_name} provider response must be a JSON object")
    return result
