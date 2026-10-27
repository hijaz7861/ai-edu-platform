import pytest
from flask import Flask

from services.security_config import configure_security


def make_app():
    return Flask(__name__)


def production_env(**overrides):
    env = {
        "APP_ENV": "production",
        "SECRET_KEY": "s" * 48,
        "ADMIN_USERNAME": "platform-admin",
        "ADMIN_PASSWORD": "correct-horse-battery-staple-2026",
        "COOKIE_SECURE": "0",
    }
    env.update(overrides)
    return env


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
    env = production_env()
    config = configure_security(app, env)
    assert config["SESSION_COOKIE_SECURE"] is True
    assert app.secret_key == env["SECRET_KEY"]


@pytest.mark.parametrize("secret", ["short", "replace-with-a-long-random-secret", "password"])
def test_production_rejects_short_or_placeholder_secret(secret):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        configure_security(make_app(), production_env(SECRET_KEY=secret))


@pytest.mark.parametrize("username", ["admin", "replace-me", "password"])
def test_production_rejects_placeholder_admin_username(username):
    with pytest.raises(RuntimeError, match="ADMIN_USERNAME"):
        configure_security(make_app(), production_env(ADMIN_USERNAME=username))


@pytest.mark.parametrize("password", ["short", "replace-with-a-long-unique-password", "admin"])
def test_production_rejects_weak_or_placeholder_admin_password(password):
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        configure_security(make_app(), production_env(ADMIN_PASSWORD=password))


def test_production_rejects_same_admin_username_and_password():
    with pytest.raises(RuntimeError, match="must differ"):
        configure_security(
            make_app(),
            production_env(ADMIN_USERNAME="same-credential-value", ADMIN_PASSWORD="same-credential-value"),
        )


@pytest.mark.parametrize("value", ["0", "-1", "invalid", None])
def test_invalid_request_size_is_rejected(value):
    with pytest.raises(RuntimeError, match="MAX_CONTENT_LENGTH"):
        configure_security(make_app(), {"MAX_CONTENT_LENGTH": value})


@pytest.mark.parametrize("value", ["0", "-1", "10001", "not-a-number"])
def test_invalid_api_rate_limit_is_rejected(value):
    with pytest.raises(RuntimeError, match="API_RATE_LIMIT_PER_MINUTE"):
        configure_security(make_app(), {"API_RATE_LIMIT_PER_MINUTE": value})
