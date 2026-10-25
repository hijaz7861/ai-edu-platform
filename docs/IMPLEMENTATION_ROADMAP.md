# AI Education Platform — Implementation Roadmap and Completion Tracker

**Repository:** https://github.com/hijaz7861/ai-edu-platform  
**Baseline reviewed:** `main` at `241aaf1eb468cea4a6d9863a84e3de7ef079fbd8`  
**Purpose:** Track every known gap to a production-capable multi-tenant education SaaS. This is a living checklist; a box may be marked complete only when code, targeted tests, CI evidence, and (where required) live/staging evidence exist.

## Completion rules

- Work in phase order; resolve blockers before starting dependent phases.
- Inspect the current code and tests before changing behavior.
- Prefer small, reviewable changes with regression tests.
- Distinguish implemented foundations from fully operational features.
- Never record a live provider, payment, connector, TLS deployment, backup/restore, or production deployment as verified unless it was actually exercised and evidence is linked.
- Keep secrets out of Git, logs, issues, and test output.
- Keep PRs unmerged until the required checks and review gates pass.

## Phase 0 — Baseline and evidence

- [x] Record the current main commit and repository structure.
- [x] Record current open implementation backlog in [Issue #3](https://github.com/hijaz7861/ai-edu-platform/issues/3).
- [x] Confirm CI and dependency-audit workflows exist.
- [x] Confirm a Render deployment manifest exists.
- [ ] Re-run and record the latest main CI and dependency-audit results after each implementation batch.
- [ ] Review each critical route and service against the intended security/tenant boundary.
- [ ] Establish a deployment target and verify the public URL; current Render service creation attempt returned HTTP 402 requesting billing information, so no deployment is confirmed.

## Phase 1 — P0 security, identity, and tenant isolation

- [ ] Persistent user identities and password hashing.
- [ ] Secure account recovery; optional MFA with recovery controls.
- [x] Platform-admin session-role guard exists; this is not full school-user RBAC.
- [ ] Complete server-side RBAC for every route, export, and background job.
- [ ] Bind authenticated users to trusted institution/tenant IDs on the server; never trust client-supplied tenant IDs.
- [ ] Add institution-scoped authorization and cross-tenant denial tests for reads, writes, lists, exports, uploads, and jobs.
- [x] Production configuration fails closed; secure-cookie/session hardening has prior CI evidence.
- [x] Persistent failed-login throttling and API rate-limiter foundations have prior CI evidence.
- [ ] Secret scanning and broader application security review.
- [ ] Append-only audit trail for sensitive admin, identity, entitlement, and security events.
- [ ] Verify security headers, CSRF protections, session rotation/expiry, cookie flags, and error/log redaction in integration tests.

**Exit gate:** No cross-tenant access in adversarial tests; all protected routes have explicit authorization; security checks pass.

## Phase 2 — P0 entitlements and subscription lifecycle

- [x] Feature-gate foundations exist for class access, attendance, AI generation, question-bank analysis, and exam generation.
- [ ] Audit and enforce entitlement checks on every paid endpoint and background job.
- [ ] Test absent, expired, suspended, malformed, downgraded, and cross-tenant subscriptions for every gated capability.
- [ ] Define validated subscription state transitions, event history, and idempotency.
- [ ] Ensure entitlement changes are based on trusted admin decisions or verified payment events, not client redirects or screenshots.
- [ ] Verify manual free/trial access and school-grant flows end-to-end.
- [ ] Add regression tests for plan changes, expiry, suspension, renewal, cancellation, grace, and past-due states.

**Exit gate:** Every paid capability is checked server-side and subscription transitions are deterministic, auditable, and idempotent.

## Phase 3 — P1 database durability and operations

- [ ] Select and configure a persistent production database; SQLite may remain for local development/tests where appropriate.
- [ ] Add versioned database migrations and tested upgrade paths.
- [ ] Configure least-privilege database credentials and production connection settings.
- [ ] Add automated backups, retention policy, and documented restore procedure.
- [ ] Perform and record a restore drill.
- [ ] Add structured logs, health/readiness checks, metrics, alerts, and error monitoring without secrets/PII leakage.
- [ ] Define data retention, export/deletion, privacy, incident-response, and disaster-recovery procedures.
- [ ] Test concurrency, timeouts, retries, graceful shutdown, and deployment rollback.

**Exit gate:** A clean environment can be migrated, backed up, restored, monitored, and rolled back using documented procedures.

## Phase 4 — P1 real AI providers and education workflow

- [x] Native Gemini adapter and provider-selection tests exist; live credentials/quota/network are not proven by unit tests.
- [x] OpenAI-compatible provider foundation exists.
- [ ] Run opt-in live contract tests for Gemini, Mistral, and OpenAI-compatible providers using configured sandbox credentials.
- [ ] Keep secrets out of the database and logs; use a managed secret store or documented encrypted-at-rest mechanism, with rotation.
- [ ] Add timeouts, bounded retries, rate/quota handling, cost limits, provider health checks, and safe fallback behavior.
- [ ] Implement real question generation, concept extraction, answer verification, and NLU; clearly label offline stub mode.
- [ ] Complete upload/PDF/OCR job lifecycle, validation, retries, source provenance, and notifications.
- [ ] Add end-to-end tests from document upload through reviewed questions and exam generation.
- [ ] Verify output quality, citations/source traceability where applicable, and failure behavior.

**Exit gate:** Configured provider contract tests and the document-to-reviewed-question-to-exam workflow pass; stub mode is never misrepresented as a live model.

## Phase 5 — P1 Connected Systems and ChatGPT-facing integration

- [x] Connected Systems registry/admin UI and secure health-check foundation exist.
- [ ] Implement real connector actions with tenant-bound scopes and least privilege.
- [ ] Add protected per-tenant credential storage, rotation, and revocation.
- [ ] Add signed, timestamp-checked, replay-resistant, idempotent webhooks.
- [ ] Add bounded retries, timeouts, audit events, and a global connector kill switch.
- [ ] Test against an independently deployed staging service.
- [ ] Implement a separate authenticated ChatGPT-facing MCP/app component if still required by product scope.
- [ ] Test authorization, SSRF protections, host allowlists, DNS/IP validation, redirect handling, and connector failure modes.

**Exit gate:** A real staging integration completes a scoped action, rejects unauthorized/cross-tenant actions, and records auditable results.

## Phase 6 — P1 payments and paid access

- [ ] Select a payment provider based on Pakistan/business eligibility, supported currencies, settlement, fees, and required verification.
- [ ] Implement hosted checkout without storing raw card data.
- [ ] Verify webhook signatures, timestamps, event IDs, and idempotency server-side.
- [ ] Process verified settlement, refund, dispute, renewal, cancellation, grace, and past-due events.
- [ ] Reconcile provider records against internal subscription/ledger records.
- [ ] Add sandbox tests for success, failure, duplicate/out-of-order webhooks, refund, and dispute.
- [ ] Keep manual/free access clearly distinct from payment-confirmed access.
- [ ] Add invoices, supported-jurisdiction tax configuration, and currency-safe accounting as required.

**Exit gate:** Sandbox event flows are verified and access is granted only from a trusted, verified event; no production payment claim without a real provider verification.

## Phase 7 — P2 HTTPS, deployment, and release controls

- [x] TLS checker, manual TLS workflow, and deployment guide exist.
- [ ] Choose an available deployment target and record its service URL.
- [ ] Deploy from the current GitHub main commit using a reproducible build.
- [ ] Verify `/health`, login, API authorization, persistence, logs, and restart behavior on the actual deployment.
- [ ] Verify the public hostname certificate, chain, expiry, redirects, and security headers.
- [ ] Configure scheduled certificate-expiry monitoring after the public URL is known.
- [ ] Add CI gates for tests, dependency/security scans, configuration checks, and integration/regression tests.
- [ ] Document rollback and verify it in staging.
- [ ] Check free-tier limits, sleep/ephemeral storage, database persistence, and billing before choosing a hosting configuration.

**Exit gate:** A public or explicitly private staging deployment is reachable and passes smoke/security checks; its URL and evidence are recorded. Do not claim deployment while hosting is blocked.

## Phase 8 — P2 finance, accessibility, and launch readiness

- [ ] Add append-only double-entry ledger if financial balances/transactions are in product scope.
- [ ] Add historical FX handling and decimal/currency-safe arithmetic if multi-currency accounting is in scope.
- [ ] Complete privacy/data export/deletion and retention controls.
- [ ] Run accessibility and responsive UI checks across mobile and desktop.
- [ ] Complete threat model, security review, dependency audit, and release checklist.
- [ ] Publish a capability matrix distinguishing complete, partial, stubbed, and externally blocked features.
- [ ] Complete staging acceptance, backup restore, incident, and rollback drills.

**Exit gate:** Release review accepts the evidence pack and all known P0 items are closed or explicitly risk-accepted by the owner.

## External blockers / decisions

- **Render:** An attempted service creation through the connected integration returned HTTP 402 asking for billing information. No service creation or public URL is confirmed. Do not add a payment card or upgrade a plan without explicit owner approval.
- **Live AI:** Requires valid provider credentials, available quota, network access, and opt-in live contract testing. Never print keys in logs or commit them.
- **Payments:** Requires provider eligibility and account onboarding; provider choice is not yet verified.
- **Production readiness:** Passing unit/CI tests alone does not prove live integrations, tenant isolation across all routes, restore readiness, or a production deployment.

## Progress log

| Date | Phase | Change/evidence | Status |
|---|---|---|---|
| 2026-10-25 | Baseline | Main commit `241aaf1eb468cea4a6d9863a84e3de7ef079fbd8`; platform CI and dependency audit both reported success for that commit. | Verified at baseline |
| 2026-10-25 | Hosting | Render service creation attempt returned HTTP 402 requiring billing information. | Blocked; no deployment confirmed |

