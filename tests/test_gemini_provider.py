import io
import json
import urllib.error

import pytest

from providers.gemini import GeminiProvider
from providers.http import CircuitOpenError


def response_for(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def test_gemini_generates_question_and_sends_key_in_header(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["key"] = request.get_header("X-goog-api-key")
        seen["timeout"] = timeout
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return response_for({
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({
                            "content": "What is photosynthesis?",
                            "answer": "The process plants use to convert light energy.",
                            "explanation": "It stores energy in chemical form.",
                        })
                    }]
                }
            }]
        })

    monkeypatch.setattr("providers.gemini.open_provider_request", fake_urlopen)
    provider = GeminiProvider(api_key="test-secret-never-log", model="gemini-3-flash-preview")
    result = provider.generate_question({"name": "photosynthesis"}, "short_answer", "easy")

    assert result["content"] == "What is photosynthesis?"
    assert result["provider"] == "gemini-native"
    assert result["verification_status"] == "ai_generated_unverified"
    assert seen["url"].startswith("https://generativelanguage.googleapis.com/")
    assert "test-secret-never-log" not in seen["url"]
    assert seen["key"] == "test-secret-never-log"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["timeout"] == 30


def test_gemini_missing_key_fails_closed():
    provider = GeminiProvider(api_key="", model="gemini-3-flash-preview")
    assert provider.health_check() == "not_configured"
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not configured"):
        provider.generate_question({"name": "gravity"}, "definition", "easy")


def test_gemini_http_error_does_not_expose_credentials(monkeypatch):
    def fail_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "quota", {}, None)

    monkeypatch.setattr("providers.gemini.open_provider_request", fail_urlopen)
    provider = GeminiProvider(api_key="private-key-value", model="gemini-3-flash-preview")
    with pytest.raises(RuntimeError, match="HTTP 429") as exc:
        provider.generate_question({"name": "gravity"}, "definition", "easy")
    assert "private-key-value" not in str(exc.value)


def test_gemini_rejects_non_json_model_output(monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.open_provider_request",
        lambda request, timeout: response_for({
            "candidates": [{"content": {"parts": [{"text": "not json"}]}}]
        }),
    )
    provider = GeminiProvider(api_key="test-key", model="gemini-3-flash-preview")
    with pytest.raises(RuntimeError, match="invalid JSON response"):
        provider.generate_question({"name": "gravity"}, "definition", "easy")


def test_gemini_normalizes_model_prefix_and_clamps_confidence(monkeypatch):
    provider = GeminiProvider(api_key="test-key", model="models/gemini-3-flash-preview", timeout=999)
    assert provider.model == "gemini-3-flash-preview"
    assert provider.timeout == 120

    monkeypatch.setattr(
        provider,
        "_generate_json",
        lambda instruction, payload: {"intent": "unknown", "confidence": 3.5},
    )
    assert provider.parse_command("do something") == {"intent": "other", "confidence": 1.0}


def test_default_registry_registers_gemini_when_configured(monkeypatch):
    for name in ("MISTRAL_API_KEY", "MISTRAL_AGENT_ID", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-flash-preview")

    from providers.base import get_default_registry

    registry = get_default_registry()
    assert "gemini-native" in registry
    assert registry["gemini-native"].health_check() == "available"
    assert "stub-offline-v1" in registry


def test_question_generation_route_uses_explicit_gemini_provider(monkeypatch):
    import app as app_module

    class FakeGemini:
        name = "gemini-native"

        def generate_question(self, concept, question_type, difficulty):
            return {
                "content": "What is gravity?",
                "difficulty": difficulty,
                "provider": self.name,
                "verification_status": "ai_generated_unverified",
            }

    monkeypatch.setenv("AI_PROVIDER", "gemini-native")
    monkeypatch.setenv("ADMIN_USERNAME", "gemini-route-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password-9284")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setattr(app_module, "get_default_registry", lambda: {"gemini-native": FakeGemini()})
    app_module.app.config.update(TESTING=True)

    with app_module.app.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        login = client.post(
            "/auth/login",
            data={
                "username": "gemini-route-admin",
                "password": "test-only-password-9284",
                "csrf_token": csrf,
            },
        )
        assert login.status_code == 302
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        response = client.post(
            "/api/questions/generate",
            json={"concept": {"name": "gravity"}, "question_type": "short_answer", "difficulty": "easy"},
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 200
    assert response.get_json()["provider"] == "gemini-native"
    assert response.get_json()["question"]["content"] == "What is gravity?"



def test_question_generation_route_returns_retry_after_for_open_provider_circuit(monkeypatch):
    import app as app_module

    class OpenCircuitProvider:
        name = "gemini-native"

        def generate_question(self, concept, question_type, difficulty):
            raise CircuitOpenError(17)

    monkeypatch.setenv("AI_PROVIDER", "gemini-native")
    monkeypatch.setenv("ADMIN_USERNAME", "circuit-route-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password-9284")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    monkeypatch.setattr(
        app_module,
        "get_default_registry",
        lambda: {"gemini-native": OpenCircuitProvider()},
    )
    app_module.app.config.update(TESTING=True)

    with app_module.app.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        login = client.post(
            "/auth/login",
            data={
                "username": "circuit-route-admin",
                "password": "test-only-password-9284",
                "csrf_token": csrf,
            },
        )
        assert login.status_code == 302
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        response = client.post(
            "/api/questions/generate",
            json={
                "concept": {"name": "gravity"},
                "question_type": "short_answer",
                "difficulty": "easy",
            },
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "17"
    assert response.get_json() == {"error": "AI provider temporarily unavailable"}



def _post_question_generation_as_admin(
    monkeypatch, app_module, registry, *, preferred=None, fallback_enabled=False
):
    monkeypatch.setenv("ADMIN_USERNAME", "provider-fallback-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password-9284")
    monkeypatch.setenv("REQUIRE_API_AUTH", "1")
    if preferred is None:
        monkeypatch.delenv("AI_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("AI_PROVIDER", preferred)
    if fallback_enabled:
        monkeypatch.setenv("AI_PROVIDER_FALLBACK_ENABLED", "1")
    else:
        monkeypatch.delenv("AI_PROVIDER_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(app_module, "get_default_registry", lambda: registry)
    app_module.app.config.update(TESTING=True)

    with app_module.app.test_client() as client:
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        login = client.post(
            "/auth/login",
            data={
                "username": "provider-fallback-admin",
                "password": "test-only-password-9284",
                "csrf_token": csrf,
            },
        )
        assert login.status_code == 302
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        return client.post(
            "/api/questions/generate",
            json={
                "concept": {"name": "gravity"},
                "question_type": "short_answer",
                "difficulty": "easy",
            },
            headers={"X-CSRF-Token": csrf},
        )


def test_automatic_provider_selection_falls_back_only_when_circuit_is_open(monkeypatch):
    import app as app_module

    class OpenCircuitProvider:
        name = "mistral-agent"

        def __init__(self):
            self.calls = 0

        def generate_question(self, concept, question_type, difficulty):
            self.calls += 1
            raise CircuitOpenError(13)

    class HealthyProvider:
        name = "gemini-native"

        def __init__(self):
            self.calls = 0

        def generate_question(self, concept, question_type, difficulty):
            self.calls += 1
            return {
                "content": "What is gravity?",
                "difficulty": difficulty,
                "provider": self.name,
                "verification_status": "ai_generated_unverified",
            }

    primary = OpenCircuitProvider()
    fallback = HealthyProvider()
    response = _post_question_generation_as_admin(
        monkeypatch,
        app_module,
        {"mistral-agent": primary, "gemini-native": fallback},
        fallback_enabled=True,
    )

    assert response.status_code == 200
    assert response.get_json()["provider"] == "gemini-native"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_explicit_provider_selection_does_not_silently_fallback(monkeypatch):
    import app as app_module

    class OpenCircuitProvider:
        name = "mistral-agent"

        def generate_question(self, concept, question_type, difficulty):
            raise CircuitOpenError(11)

    class HealthyProvider:
        name = "gemini-native"

        def __init__(self):
            self.calls = 0

        def generate_question(self, concept, question_type, difficulty):
            self.calls += 1
            return {"content": "What is gravity?"}

    fallback = HealthyProvider()
    response = _post_question_generation_as_admin(
        monkeypatch,
        app_module,
        {
            "mistral-agent": OpenCircuitProvider(),
            "gemini-native": fallback,
        },
        preferred="mistral-agent",
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "11"
    assert fallback.calls == 0


def test_automatic_provider_selection_does_not_mask_non_circuit_errors(monkeypatch):
    import app as app_module

    class InvalidCredentialsProvider:
        name = "mistral-agent"

        def generate_question(self, concept, question_type, difficulty):
            raise RuntimeError("provider returned HTTP 401")

    class HealthyProvider:
        name = "gemini-native"

        def __init__(self):
            self.calls = 0

        def generate_question(self, concept, question_type, difficulty):
            self.calls += 1
            return {"content": "What is gravity?"}

    fallback = HealthyProvider()
    response = _post_question_generation_as_admin(
        monkeypatch,
        app_module,
        {
            "mistral-agent": InvalidCredentialsProvider(),
            "gemini-native": fallback,
        },
    )

    assert response.status_code == 502
    assert fallback.calls == 0



def test_automatic_provider_fallback_is_disabled_without_explicit_opt_in(monkeypatch):
    import app as app_module

    class OpenCircuitProvider:
        name = "mistral-agent"

        def generate_question(self, concept, question_type, difficulty):
            raise CircuitOpenError(9)

    class HealthyProvider:
        name = "gemini-native"

        def __init__(self):
            self.calls = 0

        def generate_question(self, concept, question_type, difficulty):
            self.calls += 1
            return {"content": "What is gravity?"}

    fallback = HealthyProvider()
    response = _post_question_generation_as_admin(
        monkeypatch,
        app_module,
        {
            "mistral-agent": OpenCircuitProvider(),
            "gemini-native": fallback,
        },
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "9"
    assert fallback.calls == 0
