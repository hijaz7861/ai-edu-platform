import datetime as dt
import socket

import pytest

from tools.check_tls import days_until_expiry, parse_https_url, resolve_public_addresses


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com", ("example.com", 443)),
        ("https://example.com/health", ("example.com", 443)),
        ("https://example.com:8443/health", ("example.com", 8443)),
    ],
)
def test_parse_https_url(value, expected):
    assert parse_https_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "example.com",
        "https://user:password@example.com",
        "https://example.com/#fragment",
        "https://example.com:not-a-port",
    ],
)
def test_rejects_invalid_or_unsafe_urls(value):
    with pytest.raises(ValueError):
        parse_https_url(value)


def test_days_until_expiry_uses_utc_and_supports_deterministic_now():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert days_until_expiry("Jan 11 00:00:00 2026 GMT", now) == pytest.approx(10)


def test_expired_certificate_has_negative_remaining_days():
    now = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    assert days_until_expiry("Jan 01 00:00:00 2026 GMT", now) == pytest.approx(-1)



def test_resolve_public_addresses_rejects_private_destinations(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(RuntimeError, match="non-public destination"):
        resolve_public_addresses("example.com", 443)


def test_resolve_public_addresses_returns_pinned_public_address(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    assert resolve_public_addresses("example.com", 443) == [(socket.AF_INET, "8.8.8.8")]
