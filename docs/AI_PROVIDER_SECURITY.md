# AI provider transport security

The native Gemini, Mistral Agent and OpenAI-compatible adapters share a bounded HTTP transport:

- Provider request bodies are capped at 256 KiB before network I/O.
- Provider response bodies are capped at 1 MiB, including responses without a trustworthy `Content-Length`.
- Responses must be UTF-8 JSON objects.
- Redirects are not followed, preventing prompts and API credentials from being forwarded to another host.
- Each adapter retains its bounded request timeout and sanitized HTTP/network errors.

The limits are intentionally conservative defaults in `providers/http.py`; adjust them only with corresponding tests and an explicit operational requirement.

## Still required before production

This transport hardening is not a provider quota or spend-control system. Per-institution daily/monthly usage budgets, token accounting, provider circuit breakers, secret rotation, live provider contract tests, staging credentials and operational cost alerts remain outstanding. Real provider credentials were not used by these tests.
