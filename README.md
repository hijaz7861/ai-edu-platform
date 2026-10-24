# AI Education Platform — Runnable Reference Implementation

[![CI](https://github.com/hijaz7861/ai-edu-platform/actions/workflows/platform-ci.yml/badge.svg?branch=main)](https://github.com/hijaz7861/ai-edu-platform/actions/workflows/platform-ci.yml) [![TLS Certificate Check](https://github.com/hijaz7861/ai-edu-platform/actions/workflows/tls-check.yml/badge.svg)](https://github.com/hijaz7861/ai-edu-platform/actions/workflows/tls-check.yml) [![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/release/python-3130/)

A runnable Flask/SQLite reference implementation for education workflows and the foundation of a multi-tenant SaaS. It is **not yet a complete or production-ready education SaaS**. Read the status table below before relying on any capability.

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

The app listens on port 8080. OCR additionally requires the Tesseract system executable. Configure `.env` using `.env.example` before logging in. Gemini can be configured with `GEMINI_API_KEY` and `GEMINI_MODEL`; Mistral chat-completions can use the OpenAI-compatible adapter with `AI_BASE_URL=https://api.mistral.ai/v1`. Set `AI_PROVIDER` to pin a provider, or leave it blank to auto-select a configured real provider.

Required local settings:

- `SECRET_KEY`: stable random secret for signed sessions
- `ADMIN_USERNAME` and `ADMIN_PASSWORD`: the current single configured admin account
- `REQUIRE_API_AUTH=1`: keep business APIs protected
- `COOKIE_SECURE=1` when serving HTTPS outside local development

For production, set `APP_ENV=production`, provide a stable `SECRET_KEY` and configured admin credentials, and use HTTPS. The app now rejects missing production configuration and forces secure session cookies in production. This is a configuration safeguard, not a complete production deployment review. See [HTTPS/SSL-TLS setup and verification](docs/HTTPS_AND_SSL.md). The TLS badge reflects the latest manually triggered check only; it does not prove a certificate is active until a real deployed public URL has been checked.

## Run tests

```bash
python3 -m pytest -q
```

Latest verified CI for commit `b2f86a4b5b739a9c5155b8fb3a7271f7dfc4d622`:
- 92 tests passed.
- Python syntax audit passed.
- Production configuration check passed.
- Gemini adapter tests cover request format, key-header placement, response parsing, sanitized HTTP errors, registry configuration, and route-level provider selection.
- Entitlement tests cover production fail-closed behavior for missing institution IDs and missing subscriptions on gated routes.
- Login throttling, security-header, and API rate-limit tests cover the documented controls.
- Connected Systems tests cover HTTPS/host allowlisting, private-IP rejection, environment-referenced credentials, bounded health responses, audit events, lifecycle transitions, and admin API/page behavior.

CI evidence: [GitHub Actions run 40101527706](https://github.com/hijaz7861/ai-edu-platform/actions/runs/40101527706). Connected Systems tests use controlled DNS/network responses; no real external system was contacted. Gemini tests use controlled responses, not a live Gemini account/quota/network transaction. Entitlement gates currently cover class access, attendance, AI generation, question-bank analysis, and exam generation; complete coverage of every route/background job and trusted user-to-tenant binding is still outstanding.

The public TLS certificate checker and its URL/expiry/private-address tests were added after that CI run. A workflow run for the latest checker commit has not yet been found; the TLS status badge must not be read as a verified live certificate until the workflow is run against the actual deployed public URL.

## Capability status

| Capability | Current status |
|---|---|
| Flask API and SQLite schema | Implemented reference slice; production database/migration strategy still needed |
| Education data model | Institutions, campuses, departments, classes, users, teachers, students and role-assignment schema exist |
| RBAC service logic | Permission-resolution helpers exist; complete route-by-route RBAC integration is **not verified** |
| Tenant isolation | Scoped-query helper exists; complete tenant isolation across every route, export and background job is **not verified** |
| Attendance and leave | Core policy/workflow logic exists; end-to-end production workflows need broader integration tests |
| OCR | Tesseract/OpenCV processing helpers exist; complete upload/PDF/job/retry pipeline needs further integration |
| Knowledge extraction | Deep model-backed concept extraction is not complete |
| Question duplicate detection | TF-IDF/cosine-similarity implementation exists where dependencies are available |
| AI question generation | Native Gemini REST adapter and OpenAI-compatible adapter exist; Mistral Agent adapter is available when configured. Offline stub is not a real LLM. Adapter tests do not prove a live provider account/quota works |
| Exam assembly | Blueprint-driven selection logic exists and rejects insufficient question pools |
| Performance tracking | Stored-answer/topic-performance service logic exists |
| Notifications and voice/NLU | Real email/SMS/push delivery and full model-backed NLU are not complete |
| Agent governance | Approval gates, audit redaction, retention, and provider selection foundations exist |
| Login and sessions | One environment-configured platform-admin login with session/CSRF protections, persistent per-username/IP failed-login throttling, baseline security headers, and no-store on sensitive pages; sessions without the explicit platform-admin role are denied protected routes. No full multi-user identity system, recovery, or MFA |
| SaaS plans and school access | Subscription plan/grant/status foundation exists in PR #1; it does not process payments |
| Entitlement enforcement | Production gates now protect class access, attendance, AI generation, question-bank analysis, and exam generation. Full route/background-job coverage and tenant identity binding remain outstanding |
| Connected Systems | Admin UI and registry API now exist. Credentials are referenced by environment-variable name; HTTPS health checks use exact host allowlists, DNS/IP validation and pinning, no redirects, lifecycle gates and audit events. No real action connector/webhook or live system connection is verified |
| Payment gateway | Not connected; manual paid-access records do not prove payment settlement |
| Accounting, tax and FX | Design guidance only; no production-grade ledger/tax engine is claimed |
| Production operations | Backup/restore drills, monitoring, disaster recovery, production deployment and rollback are not verified |

## API security notes

Business API routes require a signed-in session and CSRF protection by default (`REQUIRE_API_AUTH=1`). Production automatically applies per-client/per-endpoint API rate limits and subscription feature gates on education routes; set `API_RATE_LIMIT_ENABLED=1` and `ENFORCE_ENTITLEMENTS=1` to exercise those controls in staging. The default configured API limit is 120 requests per endpoint/client per minute. Browser-session JSON mutations must send the session CSRF token using `X-CSRF-Token`. Admin subscription APIs remain protected even if legacy demo mode is explicitly enabled. Do not disable authentication on a network-exposed server.

The current login is a **single configured platform-admin account**. Do not treat that as complete RBAC or as a school-user login system.

## Subscription administration

The `/admin/subscriptions` page supports creating plans and recording admin-approved free, trial, or manually recorded paid access. It records an actor/reason and audit events. It does not charge cards, verify a bank transfer, or confirm a payment. A real billing provider and verified, idempotent server-side webhooks must be implemented before online payment can activate access.

## Remaining work and release gates

The prioritized implementation backlog is tracked in [issue #3](https://github.com/hijaz7861/ai-edu-platform/issues/3). SSL/TLS deployment and certificate verification steps are documented in [docs/HTTPS_AND_SSL.md](docs/HTTPS_AND_SSL.md).

The most important outstanding work is:
1. Persistent identities and complete server-side RBAC.
2. Verified tenant isolation across every route, export and background job.
3. Complete subscription enforcement on every paid operation and background job, including verified tenant identity binding.
4. Real AI provider adapters, secure provider configuration and live contract tests.
5. Connected Systems registry/runtime and an authenticated staging connector.
6. Payment-provider sandbox integration and verified webhook-to-entitlement processing.
7. Migrations, backup/restore, monitoring, security review and deployment/rollback verification.

A capability is not complete just because a schema, document, UI form or stub exists. Mark it complete only after implementation, relevant tests, and evidence from the exact commit or staging environment.

## Repository layout

```
app.py                       # Flask application and routes
db.py                        # SQLite schema and connection helpers
dashboard_api.py             # dashboard and subscription APIs
services/core.py             # education CRUD, role and scope helpers
services/licensing.py        # plans, grants, subscription status, entitlement helper
services/security_config.py  # production configuration validation
services/connected_systems.py # allowlisted Connected Systems registry and health checks
services/attendance.py       # attendance workflows
services/ingestion.py        # OCR/ingestion helpers
services/questions.py        # duplicate detection and question-bank logic
services/exams.py            # exam blueprint assembly
services/performance.py      # performance analysis
services/governance.py       # approval gates, audit and provider governance
providers/base.py            # provider interface, registry and OpenAI-compatible adapter
providers/gemini.py           # native Gemini REST adapter
templates/                   # login, dashboard, subscription and Connected Systems UI
tests/                       # automated tests, including Connected Systems security tests
docs/                        # architecture, integration, finance and release-gate documents
```
