"""Bounded, redirect-safe HTTP primitives shared by AI provider adapters."""
from __future__ import annotations

import json
import urllib.request


MAX_PROVIDER_REQUEST_BYTES = 256 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward prompts or credentials to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def encode_provider_payload(payload, provider_name: str) -> bytes:
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise RuntimeError(f"{provider_name} provider request payload is invalid") from None
    if len(body) > MAX_PROVIDER_REQUEST_BYTES:
        raise RuntimeError(f"{provider_name} provider request exceeds size limit")
    return body


def open_provider_request(request, *, timeout: int):
    """Open a provider request without following redirects."""
    opener = urllib.request.build_opener(NoRedirectHandler())
    return opener.open(request, timeout=timeout)


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
