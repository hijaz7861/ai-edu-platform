# Connected Systems and Integration Hub

## Status

This document defines the target design and acceptance criteria for connecting other products built by the same owner or approved third parties to AI Edu Platform. It does not claim that a working integration UI, connector runtime, OAuth flow, or production API credentials are already implemented.

## Product requirement

Add an **Admin → Integrations → Connected Systems** area. An administrator should be able to register a separately deployed system, verify its connection, choose allowed capabilities, view health and recent activity, rotate/revoke credentials, pause it, and disconnect it without merging the systems into one repository.

Examples include a separately built automation engine, business-research platform, reporting service, or another education-related application. Each remains an independent service and can be deployed/versioned separately.

## Connection types

Support a provider-neutral connector contract so individual integrations can be added without rewriting the platform:

- **HTTPS REST API:** base URL, API version, authentication method, and supported capability list.
- **OAuth 2.0 / OpenID Connect:** use authorization-code flow with PKCE where appropriate; validate issuer, audience, state, nonce, redirect URI, scopes, and token expiry.
- **Signed webhooks:** event subscriptions with signature verification, timestamp/replay protection, idempotency, delivery retries, and an event log.
- **Service-to-service credentials:** scoped API token or client credentials for systems that cannot use an interactive OAuth flow. Store secrets encrypted server-side and show them only at creation/rotation where practical.
- **MCP or tool connectors:** optional adapter for approved tools, with an explicit tool allowlist and per-tool authorization. A ChatGPT-facing MCP app/server is a separate integration surface and must not be confused with a ChatGPT account login.

Prefer standard protocols. Do not require a shared database or combine separate repositories to connect systems.

## Registration and administration flow

1. An authorized platform administrator selects **Add Connected System**.
2. The administrator supplies a display name, environment (development/staging/production), HTTPS endpoint, authentication type, and requested capabilities.
3. The platform validates the endpoint and configuration, then runs a low-risk health/capability check. A successful health check alone does not grant access to business data.
4. The administrator reviews and approves the exact permissions and tenant scope. High-risk scopes require stronger approval.
5. The system is stored as pending_review, connected, degraded, disabled, revoked, or error; show last check time and a redacted reason for failures.
6. Every call is authenticated, authorized, rate-limited, time-bounded, correlated, and audited. Failed calls must not silently fall back to broader privileges.
7. Administrators can test, pause, resume, rotate credentials, inspect redacted logs, edit approved scopes, and revoke a connection. Revocation must take effect promptly.

## Connector record

Store a versioned connector record with stable internal ID, tenant/owner scope, display name, environment, endpoint, protocol, authentication reference (not the secret itself), approved scopes/capabilities, lifecycle state, created/updated timestamps, last health check, last successful call, error summary, rate limits, and audit metadata.

Secrets belong in a dedicated secret store or encrypted-at-rest secret table with strict server-side access controls. Never expose stored secrets to the browser after saving; redact them from logs, errors, exports, and support views. Support rotation and revocation.

## Authorization and tenant isolation

- Authenticate the calling system using a verified machine identity or approved OAuth token. Do not trust a caller-supplied system name or tenant ID.
- Authorize each operation using the connector identity, granted scopes, tenant boundary, user context when applicable, and the target resource's ownership.
- Apply least privilege: read-only by default; write, billing, payment, student-data, and administrative scopes are separate explicit grants.
- Keep platform-owner administration separate from school/institution administration. A connection approved for one institution must not read or mutate another institution's records.
- Do not let a connector bypass subscription entitlements, approval gates, rate limits, audit requirements, or provider-cost budgets.
- Provide per-connector quotas, timeout limits, concurrency limits, and a kill switch.
- Do not allow connectors to use the integration layer as an unrestricted proxy to internal services, cloud metadata endpoints, localhost, or arbitrary network destinations.

## Network and application security

- Require HTTPS in production; validate TLS certificates and define a controlled development exception that cannot be enabled accidentally in production.
- Defend against SSRF: reject loopback, link-local, private, multicast, reserved, and cloud-metadata destinations unless a separately configured private-network connector explicitly authorizes a known target. Re-check DNS/IP resolution at connection time and prevent redirects from escaping the approved destination policy.
- Restrict outbound destinations and methods; validate response size, content type, schemas, and timeouts. Treat all connector responses as untrusted input.
- Use signed webhooks with timestamp tolerance, constant-time signature comparison where applicable, replay protection, idempotency keys, and durable retry/dead-letter handling.
- Apply CSRF protection to browser mutations, authentication and permission checks to admin endpoints, rate limits, audit events, and safe error messages.
- Keep separate credentials and allowlists for development, staging, and production. Never put secrets in source control, client-side code, URLs, or test fixtures.

## Data exchange and reliability

- Define versioned request/response schemas and event contracts for each connector. Include correlation IDs, timestamps, idempotency keys for mutations, and explicit error codes.
- Start with explicit actions rather than unrestricted data synchronization. Document which system is authoritative for each data type and how conflicts, retries, deletions, and schema changes are handled.
- Use a durable outbox/queue for asynchronous work, bounded retries with backoff, dead-letter handling, and safe replay.
- Set per-connector rate and usage budgets; expose request volume, latency, error rate, webhook lag, and last successful sync without logging sensitive payloads.
- Make disconnect semantics clear: stop new calls, revoke credentials/subscriptions, drain or cancel queued work safely, preserve required audit records, and document data deletion/retention behavior.
- Provide a connector compatibility/version policy and a migration/deprecation path.

## Suggested UI

1. **Connected Systems list:** name, environment, status, enabled scopes, last successful contact, and actions.
2. **Add/Edit connection wizard:** endpoint, protocol, authentication, requested permissions, test connection, and approval.
3. **Capabilities and permissions:** clear read/write labels, tenant scope, sensitive-data warnings, quotas, and approval state.
4. **Activity and diagnostics:** redacted audit trail, webhook deliveries, errors, latency, retries, and correlation IDs.
5. **Credential management:** rotate, revoke, and view metadata only; never reveal saved secret values.
6. **Health and controls:** test, pause, resume, disable, revoke, and provider-wide kill switch.
7. **Data mapping/sync:** only for explicitly supported connector types, with preview, conflict rules, and dry-run before writes.

## Acceptance gates

- Tests for authentication failures, expired/revoked credentials, insufficient scopes, tenant isolation, and cross-tenant access attempts.
- Tests for SSRF attempts, DNS rebinding/redirect restrictions, invalid TLS, oversized responses, malformed schemas, timeouts, and rate limiting.
- Webhook tests for invalid signatures, stale timestamps, replay, duplicate and out-of-order events, provider outage, retries, dead-letter handling, and idempotent replay.
- Tests proving a disabled/revoked connector cannot make new calls and that credential rotation invalidates old credentials as designed.
- Audit tests for create, approve, edit, test, pause, resume, rotate, scope change, and revoke actions; confirm logs redact secrets and sensitive payloads.
- End-to-end test with a separate staging service that proves only explicitly approved operations and tenant-scoped records are accessible.
- Document each connector's owner, data classification, permissions, network destinations, failure behavior, rate budget, and rollback procedure before production enablement.

## Implementation sequence

1. Add database schema/migrations and server-side connector registry.
2. Add authenticated, permission-checked admin APIs and a basic Connected Systems page.
3. Implement a single HTTPS REST connector with health check, secret handling, scoped requests, timeouts, and audit events.
4. Add signed webhooks and durable idempotent event processing.
5. Connect the first separately deployed internal system in staging; define a versioned contract and run isolation/security tests.
6. Add OAuth/MCP or additional protocol adapters only when a concrete system requires them.

Keep this work in review until the UI, API, secret storage, network protections, and tests are implemented. A registered URL or green health check is not proof that the integration is secure or production-ready.
