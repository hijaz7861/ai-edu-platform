import io
import json
import urllib.request

import pytest

from providers.http import (
    MAX_PROVIDER_REQUEST_BYTES,
    MAX_PROVIDER_RESPONSE_BYTES,
    NoRedirectHandler,
    encode_provider_payload,
    open_provider_request,
    read_provider_json,
)


class FakeResponse(io.BytesIO):
    def __init__(self, content=b"", headers=None):
        super().__init__(content)
        self.headers = headers or {}


def test_provider_request_payload_is_bounded():
    with pytest.raises(RuntimeError, match="request exceeds size limit"):
        encode_provider_payload({"text": "x" * MAX_PROVIDER_REQUEST_BYTES}, "test")


def test_provider_response_is_bounded_by_declared_content_length():
    class NoReadResponse:
        headers = {"Content-Length": str(MAX_PROVIDER_RESPONSE_BYTES + 1)}

        def read(self, size=-1):
            raise AssertionError("oversized response must be rejected before reading")

    with pytest.raises(RuntimeError, match="response exceeds size limit"):
        read_provider_json(NoReadResponse(), "test")


def test_provider_response_is_bounded_when_content_length_is_missing():
    response = FakeResponse(b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1))
    with pytest.raises(RuntimeError, match="response exceeds size limit"):
        read_provider_json(response, "test")


def test_provider_response_must_be_a_json_object():
    with pytest.raises(RuntimeError, match="must be a JSON object"):
        read_provider_json(FakeResponse(b"[]"), "test")


def test_provider_redirect_handler_refuses_redirects():
    handler = NoRedirectHandler()
    request = urllib.request.Request("https://provider.example.test/v1/chat")
    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "https://attacker.example.test/collect"
    )
    assert redirected is None



def test_open_provider_request_installs_no_redirect_handler(monkeypatch):
    captured = {}

    class FakeOpener:
        def open(self, request, timeout):
            captured["timeout"] = timeout
            return FakeResponse(b"{}")

    def fake_build_opener(handler):
        captured["handler"] = handler
        return FakeOpener()

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    response = open_provider_request(
        urllib.request.Request("https://provider.example.test/v1"),
        timeout=7,
    )
    assert isinstance(captured["handler"], NoRedirectHandler)
    assert captured["timeout"] == 7
    assert read_provider_json(response, "test") == {}


def test_openai_compatible_adapter_uses_bounded_transport(monkeypatch):
    from providers.base import OpenAICompatibleProvider

    payload = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "content": "Define gravity.",
                    "answer": "A force of attraction between masses.",
                })
            }
        }]
    }
    monkeypatch.setattr(
        "providers.base.open_provider_request",
        lambda request, timeout: FakeResponse(json.dumps(payload).encode("utf-8")),
    )
    provider = OpenAICompatibleProvider(
        api_key="test-secret",
        base_url="https://provider.example.test/v1",
        model="test-model",
        timeout=5,
    )
    result = provider.generate_question({"name": "gravity"}, "definition", "easy")
    assert result["content"] == "Define gravity."
    assert result["provider"] == "openai-compatible"


def test_mistral_agent_adapter_uses_bounded_transport(monkeypatch):
    from providers.mistral_agent import MistralAgentProvider

    payload = {
        "outputs": [{
            "type": "message.output",
            "content": json.dumps({
                "content": "Explain gravity.",
                "answer": "Masses attract each other.",
            }),
        }]
    }
    monkeypatch.setattr(
        "providers.mistral_agent.open_provider_request",
        lambda request, timeout: FakeResponse(json.dumps(payload).encode("utf-8")),
    )
    provider = MistralAgentProvider(api_key="test-secret", agent_id="test-agent")
    result = provider.generate_question({"name": "gravity"}, "short_answer", "easy")
    assert result["content"] == "Explain gravity."
    assert result["provider"] == "mistral-agent"
