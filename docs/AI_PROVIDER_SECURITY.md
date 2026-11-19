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


## Provider-reported token usage contract

The three configured adapters now expose `generate_question_with_usage(...)`,
which returns a pair: the unchanged question payload and a separate normalized
usage record. The legacy `generate_question(...)` method remains compatible
and returns only the question.

- Gemini maps `usageMetadata.promptTokenCount`, `candidatesTokenCount`, and
  `totalTokenCount`.
- OpenAI-compatible Chat Completions maps `usage.prompt_tokens`,
  `completion_tokens`, and `total_tokens`.
- Mistral Agent Conversations maps `usage.prompt_tokens`,
  `completion_tokens`, and `total_tokens`.
- Only non-negative JSON integers within SQLite's signed 64-bit range are
  accepted. Booleans, floats, negative values, overflow, and inconsistent
  totals are marked `invalid`.
- A complete input/output/total triplet is `reported`; incomplete metadata is
  `partial`; missing metadata is `unreported`. Missing values are never
  estimated or treated as zero.
- A provider total greater than input plus output is preserved as
  `unclassified_tokens` because providers may include reasoning, tool, or
  connector tokens. Pricing and currency are intentionally absent.

The normalized record contains only a schema version, provider, configured
model (when known), status and bounded token counts. Raw upstream usage objects,
prompts, credentials, request IDs and pricing are not copied. Usage is returned
separately from generated question content and is not injected into the public
question payload.

This is an adapter-level capture contract only. The API route does not yet
persist these records, enforce currency budgets, or reserve spend before a
provider call. The offline stub and providers that do not report usage remain
explicitly `unreported`; no token or cost estimate may be inferred from text
length or request counts.

The field mappings follow the provider API references:
- [Gemini GenerateContent response usage metadata](https://ai.google.dev/api/generate-content)
- [OpenAI Chat Completions usage](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [Mistral Beta Conversations usage](https://docs.mistral.ai/api/endpoint/beta/conversations)
