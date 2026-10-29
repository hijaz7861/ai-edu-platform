"""Fail-closed application security configuration for production deployments."""
from __future__ import annotations

import os
import secrets


_PLACEHOLDER_VALUES = {
    "change-me",
    "changeme",
    "replace-me",
    "replace-with-a-long-random-secret",
    "replace-with-a-long-unique-password",
    "secret",
    "password",
    "admin",
}


def configure_security(app, environ=None):
    """Configure session security and reject weak production credentials.

    Production requires an explicitly supplied, stable signing key and unique
    administrator credentials. Development may use a temporary key, but that
    key is intentionally not suitable for production.
    """
    env = os.environ if environ is None else environ
    production = str(env.get("APP_ENV", "")).strip().lower() == "production"

    secret = str(env.get("SECRET_KEY", "") or "").strip()
    username = str(env.get("ADMIN_USERNAME", "") or "").strip()
    password = str(env.get("ADMIN_PASSWORD", "") or "")
    required = ("SECRET_KEY", "ADMIN_USERNAME", "ADMIN_PASSWORD")
    missing = [name for name in required if not str(env.get(name, "") or "").strip()]
    if production and missing:
        raise RuntimeError(
            "Production configuration is incomplete: " + ", ".join(missing)
        )

    if production:
        if len(secret) < 32 or secret.casefold() in _PLACEHOLDER_VALUES:
            raise RuntimeError(
                "Production SECRET_KEY must be at least 32 characters and not a placeholder"
            )
        if username.casefold() in _PLACEHOLDER_VALUES:
            raise RuntimeError("Production ADMIN_USERNAME must not be a placeholder")
        if len(password) < 16 or password.casefold() in _PLACEHOLDER_VALUES:
            raise RuntimeError(
                "Production ADMIN_PASSWORD must be at least 16 characters and not a placeholder"
            )
        if secrets.compare_digest(username.casefold(), password.casefold()):
            raise RuntimeError("Production ADMIN_USERNAME and ADMIN_PASSWORD must differ")

    if not secret:
        secret = secrets.token_hex(32)
        app.logger.warning(
            "SECRET_KEY unset; using a temporary development key. "
            "Set a stable SECRET_KEY before deployment."
        )

    try:
        max_content_length = int(env.get("MAX_CONTENT_LENGTH", str(25 * 1024 * 1024)))
    except (TypeError, ValueError):
        raise RuntimeError("MAX_CONTENT_LENGTH must be a positive integer") from None
    if max_content_length <= 0:
        raise RuntimeError("MAX_CONTENT_LENGTH must be a positive integer")

    try:
        api_rate_limit = int(env.get("API_RATE_LIMIT_PER_MINUTE", "120"))
    except (TypeError, ValueError):
        raise RuntimeError("API_RATE_LIMIT_PER_MINUTE must be an integer from 1 to 10000") from None
    if not 1 <= api_rate_limit <= 10000:
        raise RuntimeError("API_RATE_LIMIT_PER_MINUTE must be an integer from 1 to 10000")

    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production or str(env.get("COOKIE_SECURE", "0")) == "1",
        MAX_CONTENT_LENGTH=max_content_length,
        API_RATE_LIMIT_PER_MINUTE=api_rate_limit,
    )
    return app.config
