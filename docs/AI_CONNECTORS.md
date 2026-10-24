# AI Provider and ChatGPT Connector Architecture

## Purpose

The platform should support interchangeable AI providers without coupling education workflows to one vendor. Provider credentials stay server-side and must never be sent to browser JavaScript or exposed by status endpoints.

## Connector categories

1. **Model API providers** — direct server-to-server calls using a provider API key. The existing `AIProvider` contract supports question generation, question verification, and command classification.
2. **OpenAI-compatible APIs** — providers exposing a compatible chat-completions endpoint can use `AI_API_KEY`, `AI_BASE_URL`, and `AI_MODEL`. This can include OpenAI-compatible endpoints from multiple vendors, subject to each vendor's supported request/response features.
3. **Vendor-specific adapters** — APIs whose authentication, payloads, response formats, or agent model differ need a dedicated adapter and contract tests. The repository currently contains a Mistral Agent adapter; do not assume every vendor/model is supported merely because it appears in a settings list.
4. **ChatGPT as a client** — a ChatGPT account/subscription is not an OpenAI API credential. To expose platform capabilities to ChatGPT, implement and deploy a separate authenticated MCP server (or another supported ChatGPT app integration). The MCP server should call the platform's authenticated API; it must not bypass session, role, tenant, entitlement, or approval checks.
5. **Local models** — an OpenAI-compatible local inference endpoint can be configured if it is reachable from the server. Plain HTTP must remain disabled except for an explicitly isolated local development setup.

## Current implementation status

- The `AIProvider` contract and deterministic offline stub exist.
- A generic OpenAI-compatible chat-completions adapter exists.
- A Mistral Agent API adapter exists and requires both `MISTRAL_API_KEY` and `MISTRAL_AGENT_ID`.
- The current question-generation route chooses Mistral Agent first, then the configured OpenAI-compatible provider. This is not yet a user-facing provider selector or a complete capability-aware failover router.
- Gemini may be usable through a vendor-supported OpenAI-compatible endpoint, but that endpoint and model must be configured and tested against the exact request format. No native Gemini adapter or verified Gemini live call is claimed here.
- A ChatGPT-facing MCP server, OAuth/identity hand-off, per-school connector settings, encrypted per-tenant secrets, connector management UI, usage metering, budget limits, retries/circuit breakers, and live provider contract tests remain to be implemented and verified.

## Required design for a production connector manager

- Store connector metadata separately from credentials; encrypt secrets at rest with a managed key and never return secret values after save.
- Permit configuration only to authorized platform administrators, and scope institution-specific connectors to that institution.
- Validate HTTPS endpoints, model identifiers, timeouts, payload limits, and allowed capabilities. Do not permit arbitrary unauthenticated URL fetching from the provider settings UI.
- Offer a **Test connection** action that performs a bounded server-side request and reports a redacted result (configured, reachable, authentication failed, quota/rate limited, unsupported response, or timeout). A configuration-only check must not be labelled as a live connection test.
- Select providers by explicit administrator policy and supported capability. Fail closed if a required provider is unavailable; do not silently represent a stub response as a real model response.
- Apply per-school usage limits, request timeouts, bounded retries, rate limiting, audit events, and spend tracking. Never log API keys, authorization headers, raw sensitive student data, or full prompts by default.
- Validate model output against schemas and treat model output as untrusted input. AI-generated educational content must be labelled unverified until the platform's verification workflow completes.
- For MCP tools, use least privilege, explicit tool schemas, per-request authorization, tenant context from trusted identity (not caller-supplied institution IDs alone), audit logging, and approval gates for consequential actions.

## Acceptance criteria before calling this complete

1. Unit tests cover each adapter's request serialization, response parsing, timeouts, non-2xx responses, malformed output, and credential redaction using mocked transport at the network boundary.
2. Integration tests run against each provider's actual API in an explicitly configured environment; test results include timestamp, model, and provider, without secrets.
3. The UI can add, edit, disable, test, and select providers without exposing stored credentials.
4. A school cannot read or use another school's connector, data, usage, or entitlements.
5. ChatGPT/MCP tools cannot bypass authentication, role checks, tenant isolation, entitlement checks, CSRF protections for browser routes, or approval gates.
6. CI passes and the application boots with no provider configured; offline fallback behavior is clearly labelled.
7. Operational controls cover quotas, budget caps, rate limiting, timeout policy, observability, secret rotation, incident response, and backup/restore.

This document is an implementation plan and status statement, not a claim that these unfinished capabilities already exist.
