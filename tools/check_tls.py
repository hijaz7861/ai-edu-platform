#!/usr/bin/env python3
"""Verify a public HTTPS endpoint's TLS certificate using the system trust store."""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import socket
import ssl
import sys
from urllib.parse import urlsplit


def parse_https_url(value: str) -> tuple[str, int]:
    """Return (hostname, port), rejecting non-HTTPS and credential-bearing URLs."""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError(f"invalid URL/port: {exc}") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("target must use https://")
    if not parsed.hostname:
        raise ValueError("target must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in the target URL are not allowed")
    if parsed.fragment:
        raise ValueError("URL fragments are not allowed")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return parsed.hostname.encode("idna").decode("ascii"), port


def resolve_public_addresses(hostname: str, port: int) -> list[tuple[int, str]]:
    """Resolve once, reject non-public destinations, and return pinned socket addresses."""
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RuntimeError(f"DNS resolution failed: {exc}") from exc
    addresses: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for family, _socktype, _proto, _canonname, sockaddr in results:
        address = sockaddr[0]
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise RuntimeError(f"refusing non-public destination address: {ip}")
        item = (family, address)
        if item not in seen:
            seen.add(item)
            addresses.append(item)
    if not addresses:
        raise RuntimeError("hostname did not resolve to a public address")
    return addresses


def days_until_expiry(not_after: str, now: dt.datetime | None = None) -> float:
    """Return certificate lifetime remaining in days; input uses OpenSSL's notAfter format."""
    expiry = dt.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=dt.timezone.utc
    )
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return (expiry - current.astimezone(dt.timezone.utc)).total_seconds() / 86400


def check_tls(url: str, timeout: float = 8.0) -> dict[str, object]:
    hostname, port = parse_https_url(url)
    addresses = resolve_public_addresses(hostname, port)
    context = ssl.create_default_context()
    last_error: Exception | None = None
    for family, address in addresses:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as raw:
                raw.settimeout(timeout)
                sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
                raw.connect(sockaddr)
                with context.wrap_socket(raw, server_hostname=hostname) as secured:
                    cert = secured.getpeercert()
                    remaining = days_until_expiry(cert["notAfter"])
                    result: dict[str, object] = {
                        "url": f"https://{hostname}:{port}" if port != 443 else f"https://{hostname}",
                        "hostname": hostname,
                        "tls_version": secured.version(),
                        "cipher": secured.cipher()[0] if secured.cipher() else None,
                        "days_until_expiry": round(remaining, 2),
                        "certificate_valid": remaining > 0,
                        "verified_by_system_trust_store": True,
                    }
                    if remaining <= 0:
                        raise RuntimeError("certificate is expired")
                    return result
        except (OSError, ssl.SSLError, KeyError, RuntimeError) as exc:
            last_error = exc
    raise RuntimeError(f"TLS verification failed: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Public HTTPS URL, e.g. https://service.example.com/health")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--warn-days", type=int, default=30)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.warn_days < 0:
        parser.error("--warn-days cannot be negative")
    try:
        report = check_tls(args.url, timeout=args.timeout)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **report}, indent=2))
    if float(report["days_until_expiry"]) <= args.warn_days:
        print(f"WARNING: certificate expires within {args.warn_days} days.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
