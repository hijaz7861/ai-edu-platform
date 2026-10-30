import io
import urllib.request

import pytest

from providers.http import (
    MAX_PROVIDER_REQUEST_BYTES,
    MAX_PROVIDER_RESPONSE_BYTES,
    NoRedirectHandler,
    encode_provider_payload,
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
