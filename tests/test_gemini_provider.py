import io
import json
import urllib.error

import pytest

from providers.gemini import GeminiProvider


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

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
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

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    provider = GeminiProvider(api_key="private-key-value", model="gemini-3-flash-preview")
    with pytest.raises(RuntimeError, match="HTTP 429") as exc:
        provider.generate_question({"name": "gravity"}, "definition", "easy")
    assert "private-key-value" not in str(exc.value)


def test_gemini_rejects_non_json_model_output(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
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
