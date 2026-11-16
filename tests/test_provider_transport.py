import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from providers.http import (
    CIRCUIT_RESET_SECONDS,
    MAX_PROVIDER_REQUEST_BYTES,
    MAX_PROVIDER_RESPONSE_BYTES,
    CircuitOpenError,
    NoRedirectHandler,
    encode_provider_payload,
    open_provider_request,
    read_provider_json,
)


@pytest.fixture(autouse=True)
def isolate_provider_circuits(monkeypatch):
    monkeypatch.setattr("providers.http._circuits", {})


class FakeResponse(io.BytesIO):
    def __init__(self, content=b"", headers=None):
        super().__init__(content)
        self.headers = headers or {}


def _request(host="provider.example.test"):
    return urllib.request.Request(f"https://{host}/v1/generate", data=b"{}")


def _http_error(request, status):
    return urllib.error.HTTPError(request.full_url, status, "upstream", {}, None)


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
    response = open_provider_request(_request(), timeout=7)
    assert isinstance(captured["handler"], NoRedirectHandler)
    assert captured["timeout"] == 7
    assert read_provider_json(response, "test") == {}


def test_provider_circuit_opens_after_five_transient_http_failures(monkeypatch):
    attempts = {"count": 0}
    request = _request()

    class FailingOpener:
        def open(self, request, timeout):
            attempts["count"] += 1
            raise _http_error(request, 503)

    monkeypatch.setattr(
        "urllib.request.build_opener", lambda handler: FailingOpener()
    )
    for _ in range(5):
        with pytest.raises(urllib.error.HTTPError):
            open_provider_request(request, timeout=1)

    with pytest.raises(CircuitOpenError) as exc:
        open_provider_request(request, timeout=1)

    assert attempts["count"] == 5
    assert 1 <= exc.value.retry_after <= CIRCUIT_RESET_SECONDS


def test_permanent_http_errors_do_not_open_provider_circuit(monkeypatch):
    attempts = {"count": 0}
    request = _request()

    class UnauthorizedOpener:
        def open(self, request, timeout):
            attempts["count"] += 1
            raise _http_error(request, 401)

    monkeypatch.setattr(
        "urllib.request.build_opener", lambda handler: UnauthorizedOpener()
    )
    for _ in range(8):
        with pytest.raises(urllib.error.HTTPError):
            open_provider_request(request, timeout=1)

    assert attempts["count"] == 8


def test_provider_circuits_are_isolated_by_host(monkeypatch):
    attempts = {"unhealthy": 0, "healthy": 0}

    class MixedOpener:
        def open(self, request, timeout):
            if "unhealthy" in request.full_url:
                attempts["unhealthy"] += 1
                raise _http_error(request, 503)
            attempts["healthy"] += 1
            return FakeResponse(b"{}")

    monkeypatch.setattr("urllib.request.build_opener", lambda handler: MixedOpener())
    unhealthy = _request("unhealthy.example.test")
    healthy = _request("healthy.example.test")

    for _ in range(5):
        with pytest.raises(urllib.error.HTTPError):
            open_provider_request(unhealthy, timeout=1)

    with pytest.raises(CircuitOpenError):
        open_provider_request(unhealthy, timeout=1)

    assert read_provider_json(open_provider_request(healthy, timeout=1), "test") == {}
    assert attempts == {"unhealthy": 5, "healthy": 1}


def test_half_open_provider_circuit_allows_one_probe_and_resets_on_success(monkeypatch):
    now = {"value": 100.0}
    monkeypatch.setattr("providers.http.time.monotonic", lambda: now["value"])
    attempts = {"count": 0}
    entered = threading.Event()
    release = threading.Event()
    request = _request()

    class RecoveringOpener:
        def open(self, request, timeout):
            attempts["count"] += 1
            if attempts["count"] <= 5:
                raise _http_error(request, 503)
            if attempts["count"] == 6:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test probe release timed out")
            return FakeResponse(b"{}")

    monkeypatch.setattr(
        "urllib.request.build_opener", lambda handler: RecoveringOpener()
    )
    for _ in range(5):
        with pytest.raises(urllib.error.HTTPError):
            open_provider_request(request, timeout=1)

    now["value"] += CIRCUIT_RESET_SECONDS + 1
    responses = []
    failures = []

    def run_probe():
        try:
            responses.append(open_provider_request(request, timeout=1))
        except Exception as exc:  # captured for assertion in the main test thread
            failures.append(exc)

    worker = threading.Thread(target=run_probe)
    worker.start()
    assert entered.wait(1)

    with pytest.raises(CircuitOpenError):
        open_provider_request(request, timeout=1)

    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert failures == []
    assert len(responses) == 1
    assert read_provider_json(responses[0], "test") == {}

    # The successful half-open probe closed the circuit.
    assert read_provider_json(open_provider_request(request, timeout=1), "test") == {}
    assert attempts["count"] == 7


def test_failed_half_open_probe_reopens_provider_circuit(monkeypatch):
    now = {"value": 200.0}
    monkeypatch.setattr("providers.http.time.monotonic", lambda: now["value"])
    attempts = {"count": 0}
    request = _request()

    class AlwaysFailingOpener:
        def open(self, request, timeout):
            attempts["count"] += 1
            raise _http_error(request, 503)

    monkeypatch.setattr(
        "urllib.request.build_opener", lambda handler: AlwaysFailingOpener()
    )
    for _ in range(5):
        with pytest.raises(urllib.error.HTTPError):
            open_provider_request(request, timeout=1)

    now["value"] += CIRCUIT_RESET_SECONDS + 1
    with pytest.raises(urllib.error.HTTPError):
        open_provider_request(request, timeout=1)

    with pytest.raises(CircuitOpenError):
        open_provider_request(request, timeout=1)

    assert attempts["count"] == 6


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
