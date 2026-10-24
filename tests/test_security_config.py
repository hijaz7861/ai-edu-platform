import logging

import pytest
from flask import Flask

from services.security_config import configure_security


def make_app():
    return Flask(__name__)


def test_development_can_use_temporary_secret_and_insecure_local_cookie():
    app = make_app()
    config = configure_security(app, {})
    assert len(app.secret_key) >= 32
    assert config["SESSION_COOKIE_HTTPONLY"] is True
    assert config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert config["SESSION_COOKIE_SECURE"] is False


def test_production_requires_stable_secret_and_admin_credentials():
    app = make_app()
    with pytest.raises(RuntimeError, match="SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD"):
        configure_security(app, {"APP_ENV": "production"})


def test_production_requires_secure_cookie_even_if_override_is_zero():
    app = make_app()
    env = {
        "APP_ENV": "production",
        "SECRET_KEY": "stable-test-secret",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "test-only-password",
        "COOKIE_SECURE": "0",
    }
    config = configure_security(app, env)
    assert config["SESSION_COOKIE_SECURE"] is True
    assert app.secret_key == "stable-test-secret"


@pytest.mark.parametrize("value", ["0", "-1", "invalid", None])
def test_invalid_request_size_is_rejected(value):
    app = make_app()
    env = {"MAX_CONTENT_LENGTH": value}
    with pytest.raises(RuntimeError, match="MAX_CONTENT_LENGTH"):
        configure_security(app, env)


@pytest.mark.parametrize("value", ["0", "-1", "10001", "not-a-number"])
def test_invalid_api_rate_limit_is_rejected(value):
    app = make_app()
    with pytest.raises(RuntimeError, match="API_RATE_LIMIT_PER_MINUTE"):
        configure_security(app, {"API_RATE_LIMIT_PER_MINUTE": value})
