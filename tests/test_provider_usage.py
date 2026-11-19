import io
import json

import pytest

from providers.usage import normalize_provider_usage


def test_normalizes_gemini_usage_and_preserves_unclassified_tokens():
    usage = normalize_provider_usage(
        {
            "promptTokenCount": 12,
            "candidatesTokenCount": 8,
            "totalTokenCount": 22,
            "thoughtsTokenCount": 2,
            "cachedContentTokenCount": 3,
        },
        provider="gemini-native",
        model="gemini-3-flash-preview",
    )
    assert usage == {
        "schema_version": 1,
        "provider": "gemini-native",
        "model": "gemini-3-flash-preview",
        "status": "reported",
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 22,
        "unclassified_tokens": 2,
    }


@pytest.mark.parametrize(
    "provider,raw",
    [
        (
            "openai-compatible",
            {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        ),
        (
            "mistral-agent",
            {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 22},
        ),
    ],
)
def test_normalizes_openai_and_mistral_usage(provider, raw):
    usage = normalize_provider_usage(raw, provider=provider, model="approved-test-model")
    assert usage["status"] == "reported"
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 8
    assert usage["total_tokens"] == raw["total_tokens"]
    assert usage["unclassified_tokens"] == raw["total_tokens"] - 20


def test_missing_provider_usage_is_unreported_not_zero():
    usage = normalize_provider_usage({}, provider="mistral-agent")
    assert usage["status"] == "unreported"
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["total_tokens"] is None


def test_partial_usage_is_preserved_but_not_marked_billable_complete():
    usage = normalize_provider_usage(
        {"prompt_tokens": 17},
        provider="openai-compatible",
    )
    assert usage["status"] == "partial"
    assert usage["input_tokens"] == 17
    assert usage["output_tokens"] is None
    assert usage["total_tokens"] is None


@pytest.mark.parametrize(
    "raw",
    [
        {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
        {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
        {"prompt_tokens": 1.0, "completion_tokens": 1, "total_tokens": 2},
        {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 4},
        {"prompt_tokens": 1 << 63, "completion_tokens": 0, "total_tokens": 1 << 63},
    ],
)
def test_invalid_or_inconsistent_usage_is_not_usable_for_costing(raw):
    usage = normalize_provider_usage(raw, provider="openai-compatible")
    assert usage["status"] == "invalid"
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["total_tokens"] is None


def test_unrecognized_usage_shape_is_not_guessed():
    usage = normalize_provider_usage(
        {"tokens": 100, "connector_tokens": 7},
        provider="mistral-agent",
    )
    assert usage["status"] == "unreported"
    assert usage["total_tokens"] is None


def _response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def test_gemini_adapter_returns_usage_separately_from_question(monkeypatch):
    from providers.gemini import GeminiProvider

    monkeypatch.setattr(
        "providers.gemini.open_provider_request",
        lambda request, timeout: _response({
            "candidates": [{
                "content": {"parts": [{"text": json.dumps({"content": "Define gravity."})}]}
            }],
            "usageMetadata": {
                "promptTokenCount": 9,
                "candidatesTokenCount": 4,
                "totalTokenCount": 13,
            },
        }),
    )
    provider = GeminiProvider(api_key="test-key", model="gemini-test")
    question, usage = provider.generate_question_with_usage(
        {"name": "gravity"}, "definition", "easy"
    )
    assert question["content"] == "Define gravity."
    assert "_provider_usage" not in question
    assert usage["status"] == "reported"
    assert usage["input_tokens"] == 9
    assert usage["output_tokens"] == 4
    assert usage["total_tokens"] == 13


def test_openai_compatible_adapter_returns_usage_separately(monkeypatch):
    from providers.base import OpenAICompatibleProvider

    monkeypatch.setattr(
        "providers.base.open_provider_request",
        lambda request, timeout: _response({
            "choices": [{
                "message": {"content": json.dumps({"content": "Define gravity."})}
            }],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 5,
                "total_tokens": 16,
            },
        }),
    )
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://provider.example.test/v1",
        model="test-model",
    )
    question, usage = provider.generate_question_with_usage(
        {"name": "gravity"}, "definition", "easy"
    )
    assert question["content"] == "Define gravity."
    assert "_provider_usage" not in question
    assert usage["status"] == "reported"
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 5
    assert usage["total_tokens"] == 16


def test_mistral_agent_adapter_returns_usage_separately(monkeypatch):
    from providers.mistral_agent import MistralAgentProvider

    monkeypatch.setattr(
        "providers.mistral_agent.open_provider_request",
        lambda request, timeout: _response({
            "outputs": [{
                "type": "message.output",
                "content": json.dumps({"content": "Define gravity."}),
            }],
            "usage": {
                "prompt_tokens": 15,
                "completion_tokens": 6,
                "total_tokens": 23,
            },
        }),
    )
    provider = MistralAgentProvider(api_key="test-key", agent_id="test-agent")
    question, usage = provider.generate_question_with_usage(
        {"name": "gravity"}, "definition", "easy"
    )
    assert question["content"] == "Define gravity."
    assert "_provider_usage" not in question
    assert usage["status"] == "reported"
    assert usage["input_tokens"] == 15
    assert usage["output_tokens"] == 6
    assert usage["total_tokens"] == 23
    assert usage["unclassified_tokens"] == 2


def test_legacy_stub_provider_usage_is_explicitly_unreported():
    from providers.base import StubProvider

    question, usage = StubProvider().generate_question_with_usage(
        {"name": "gravity"}, "definition", "easy"
    )
    assert question["provider"] == "stub-offline-v1"
    assert usage["status"] == "unreported"
