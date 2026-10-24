# Remaining Capabilities and Release Gates

This is the implementation backlog for taking the AI Education Platform from its current reference implementation to a reviewed, production-capable SaaS. A schema, design document, or UI mock is not considered a delivered capability. Each item must be implemented, tested, documented, and verified in the target deployment before it is marked complete.

## P0 — Security and tenant boundaries

- [ ] Replace the single environment-configured platform login with persistent user identities, secure password hashing, session rotation, login throttling/lockout, recovery, and optional MFA.
- [ ] Enforce server-side RBAC for every route and action; role names or UI visibility alone must never grant access.
- [ ] Bind every institution-owned record and query to the authenticated user's authorized institution scope; add cross-tenant denial tests for reads, writes, exports, and background jobs.
- [ ] Add an explicit platform-admin permission boundary and separate school administrators, teachers, students, reviewers, and service identities.
- [ ] Fail closed when production secrets/configuration are missing; enforce secure cookies, trusted-host/proxy settings, request limits, and safe error responses.
- [x] Add persistent failed-login throttling, baseline response security headers, and no-store policy on sensitive pages; CI run [40100321674](https://github.com/hijaz7861/ai-edu-platform/actions/runs/40100321674) passed 74 tests on commit `09f2676104ba459b440f527d0aacc1905acd16ca`.
- [x] Add a persistent per-client/per-endpoint API rate limiter with bounded state and 429/Retry-After responses; CI run [40100618345](https://github.com/hijaz7861/ai-edu-platform/actions/runs/40100618345) passed 82 tests on commit `0869359ab92f188fb6762e44001a22dc21724095`.
- [ ] Add dependency audit, secret scanning, and an actionable security response process.
- [ ] Add append-only security/audit events for privilege changes, connector actions, access grants, exports, and administrative decisions.

## P0 — Subscription and entitlement enforcement

- [x] Add production feature gates for class access, attendance, AI generation, question-bank analysis, and exam generation; CI run [40099977782](https://github.com/hijaz7861/ai-edu-platform/actions/runs/40099977782) passed 68 tests on commit `900af7ccaf3c2ca9a79b7e0850d6f01f8a765da0`.
- [ ] Complete enforcement on every paid endpoint and asynchronous job, including trusted user-to-tenant binding.
- [ ] Add tests for absent, inactive, expired, suspended, malformed, and cross-tenant subscription cases.
- [ ] Add a safe subscription state transition model and idempotent audit events.
- [ ] Keep manual paid-access records distinct from verified payment settlement; never imply that an admin record proves a charge succeeded.

## P1 — AI providers and education workflows

- [x] Add native Gemini REST adapter, registry integration, explicit provider selection, and controlled-response/route-selection tests (64 tests passed in CI run [40099716796](https://github.com/hijaz7861/ai-edu-platform/actions/runs/40099716796) on commit `29c65fab061812ad66bb83d016d0eb921894c651`). This does not verify live credentials, quotas, or network access.
- [ ] Complete live provider contract tests and verify Mistral Agent/OpenAI-compatible provider behavior against configured sandbox accounts.
- [ ] Add provider configuration UI/API with encrypted-at-rest secrets, redaction, validation, rotation, revocation, model/capability allowlists, timeouts, quotas, and cost controls.
- [ ] Test provider selection, fallback, quota/rate-limit responses, malformed outputs, timeouts, and provider unavailability without exposing credentials.
- [ ] Replace deterministic question-generation stubs with actual provider-backed generation; retain explicit offline mode and never label a stub as a real model.
- [ ] Implement model-assisted concept extraction, question verification, and language/NLU with provenance and human review for uncertain outputs.
- [ ] Complete document ingestion for validated uploads, PDF/page processing, OCR job status/retry, source references, and failure recovery.
- [ ] Implement real notification delivery adapters (email first; SMS/push only when configured), delivery status, retries, and opt-out controls.
- [ ] Add end-to-end tests from source document to reviewed question bank and generated exam.

## P1 — Connected Systems

- [ ] Implement an admin Connected Systems registry and lifecycle management (register, verify, pause, rotate, revoke, delete).
- [ ] Authenticate service-to-service requests and enforce tenant-bound scopes on every connector action.
- [ ] Protect connector secrets; never return secret values in API responses or logs.
- [ ] Implement health checks, capability discovery, timeouts, bounded retries, idempotency, audit records, and a global kill switch.
- [ ] Enforce outbound host/IP allowlists and SSRF defenses, including redirects, DNS rebinding, private/link-local addresses, and metadata endpoints.
- [ ] Verify signed webhooks with timestamp/replay protection and idempotent event handling.
- [ ] Add a staging connection test against one separately deployed system before enabling production connectors.
- [ ] Implement a ChatGPT-facing MCP/app integration as a separate authenticated component; a ChatGPT account is not an API credential.

## P1 — Payments and entitlements

- [ ] Select a real payment provider based on supported countries, currencies, business eligibility, fees, and compliance requirements.
- [ ] Implement hosted checkout and provider-side signature-verified webhook processing; do not trust browser redirects, screenshots, SMS, or user-entered transaction references.
- [ ] Make webhook handling idempotent, replay-resistant, observable, and safe under duplicate/out-of-order delivery.
- [ ] Activate or change access only after verified settlement/refund/dispute events and record the provider event reference.
- [ ] Implement subscription renewal, cancellation, grace period, past-due, refund, dispute, and failed-payment transitions.
- [ ] Add reconciliation reports and sandbox tests for success, decline, duplicate events, refund, dispute, and delayed webhook delivery.
- [ ] Document provider-specific support and eligibility for cards, local wallets, bank transfers, and any crypto-related flow before implementation; do not claim availability until verified.

## P2 — Finance, compliance, and operations

- [ ] Implement append-only double-entry accounting with balanced journal entries and immutable source references before financial reporting is treated as authoritative.
- [ ] Preserve historical FX rates and currency minor-unit rules; avoid floating-point money arithmetic.
- [ ] Implement tax/invoice rules only for supported jurisdictions, with reviewed configuration and auditability.
- [ ] Establish data retention/deletion, export, privacy, accessibility, and incident-response procedures.
- [ ] Add tested database migrations, scheduled backups, restore drills, disaster recovery targets, monitoring, alerting, structured logs, and operational runbooks.
- [ ] Establish CI gates for unit, integration, security, migration, and regression tests; pin dependencies and run vulnerability checks.
- [ ] Test concurrency/race conditions, database integrity, pagination, bounded resource usage, and graceful shutdown.
- [ ] Verify production deployment configuration and rollback procedures in a staging environment.

## Release gates

A capability can only be checked off when all applicable conditions are met:

1. Implementation exists in the application, not only in documentation.
2. Tests cover normal, invalid, unauthorized, cross-tenant, failure, and retry paths as appropriate.
3. CI results identify the exact commit tested.
4. External integrations have evidence from a sandbox/contract test; live money movement is not required for routine CI.
5. Configuration, migration, rollback, privacy, and operational behavior are documented.
6. The status report distinguishes verified behavior from planned work and known limitations.

## Current known baseline

- The repository contains a runnable Flask/SQLite reference implementation and provider abstraction, with several education workflows implemented.
- PR #1 adds a subscription/access administration foundation; its reported CI run passed 50 tests, but this does not prove full RBAC, tenant isolation, payment processing, or enforcement on every paid endpoint.
- PR #2 currently defines AI connector, connected-systems, and payment architecture. Those documents are specifications, not operational implementations.
- The README in the default branch describes an earlier snapshot and must be reconciled with the latest branch-specific implementation and test results.
