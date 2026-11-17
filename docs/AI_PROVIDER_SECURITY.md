# AI provider transport security

The native Gemini, Mistral Agent and OpenAI-compatible adapters share a bounded HTTP transport:

- Provider request bodies are capped at 256 KiB before network I/O.
- Provider response bodies are capped at 1 MiB, including responses without a trustworthy `Content-Length`.
- Responses must be UTF-8 JSON objects.
- Redirects are not followed, preventing prompts and API credentials from being forwarded to another host.
- Each adapter retains its bounded request timeout and sanitized HTTP/network errors.

The limits are intentionally conservative defaults in `providers/http.py`; adjust them only with corresponding tests and an explicit operational requirement.

## Process-local provider circuit breaker

- A provider host circuit opens after five consecutive connection/time-out failures or HTTP 408, 425, 429, or 5xx responses.
- While open, requests are not sent upstream and the API returns HTTP 503 with a bounded `Retry-After` value.
- After 30 seconds, one half-open probe is permitted. Concurrent probes are rejected; a successful probe closes the circuit, while a transient failure reopens it.
- Permanent 4xx responses (for example, invalid credentials or request validation) do not count as transient availability failures.
- The transport does not automatically retry POST requests. This avoids duplicate billable generation requests when provider-side idempotency is not guaranteed.
- Cross-provider fallback is disabled by default. Operators may set `AI_PROVIDER_FALLBACK_ENABLED=1` only after approving the data-processing/vendor policy; fallback is considered only when `AI_PROVIDER` is not explicitly pinned and the selected provider circuit is open. An explicit `AI_PROVIDER` is always honored without silent vendor switching. Invalid credentials, malformed responses and other non-circuit errors are not masked by fallback.
- Circuit state is process-local, keyed only by normalized host/port, and bounded to 128 hosts. It is not shared across web workers or hosts and must not be treated as a distributed availability or spend-control mechanism.

Tests cover open, half-open, recovery, permanent-error handling, host isolation, API `Retry-After`, opt-in automatic fallback on an open circuit, fallback-disabled-by-default, explicit-provider pinning and refusal to mask non-circuit errors. They use mocked upstream responses; no live provider credentials or paid requests are used.

## Response-body circuit accounting

The provider circuit remains open until the bounded response body has been read.
A connection that succeeds at the HTTP-header stage but resets or times out while
streaming its body counts as a transient failure. A complete but malformed or
oversized response releases a half-open probe as a reachable-upstream protocol
error; it does not masquerade as a transport outage. This is still process-local
state and does not coordinate separate worker processes.

## Still required before production

This transport hardening is not a provider quota or spend-control system. Per-institution daily/monthly usage budgets, token/currency accounting, provider-side spend caps, distributed circuit state, secret rotation, live provider contract tests, staging credentials and operational cost alerts remain outstanding. Real provider credentials were not used by these tests.
