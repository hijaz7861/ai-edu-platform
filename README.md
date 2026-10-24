# AI Education Platform — Runnable Reference Implementation

A runnable Flask/SQLite reference implementation for education workflows and the foundation of a multi-tenant SaaS. It is **not yet a complete or production-ready education SaaS**. Read the status table below before relying on any capability.

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

The app listens on port 8080. OCR additionally requires the Tesseract system executable. Configure `.env` using `.env.example` before logging in.

Required local settings:

- `SECRET_KEY`: stable random secret for signed sessions
- `ADMIN_USERNAME` and `ADMIN_PASSWORD`: the current single configured admin account
- `REQUIRE_API_AUTH=1`: keep business APIs protected
- `COOKIE_SECURE=1` when serving HTTPS outside local development

For production, set `APP_ENV=production`, provide a stable `SECRET_KEY` and configured admin credentials, and use HTTPS. The app now rejects missing production configuration and forces secure session cookies in production. This is a configuration safeguard, not a complete production deployment review.

## Run tests

```bash
python3 -m pytest -q
```

Latest verified CI for commit `829d27d8100b502ac8dcecfac06694adfa351d00`:
- 57 tests passed.
- Python syntax audit passed.
- Production configuration check passed.

CI evidence: [GitHub Actions run 40099404683](https://github.com/hijaz7861/ai-edu-platform/actions/runs/40099404683). This result applies to that exact commit and test suite; it does not certify all listed future capabilities.

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
| AI question generation | Provider interface exists; offline stub is not a real LLM; real provider adapters and live contract tests need verification |
| Exam assembly | Blueprint-driven selection logic exists and rejects insufficient question pools |
| Performance tracking | Stored-answer/topic-performance service logic exists |
| Notifications and voice/NLU | Real email/SMS/push delivery and full model-backed NLU are not complete |
| Agent governance | Approval gates, audit redaction, retention, and provider selection foundations exist |
| Login and sessions | One environment-configured platform-admin login with session/CSRF protections; no full multi-user identity system, recovery, or MFA |
| SaaS plans and school access | Subscription plan/grant/status foundation exists in PR #1; it does not process payments |
| Entitlement enforcement | Feature-check helper exists; enforcement on every protected endpoint and background job is **not verified** |
| Connected Systems | Architecture documented in PR #2; registry UI/runtime and live connector are not implemented |
| Payment gateway | Not connected; manual paid-access records do not prove payment settlement |
| Accounting, tax and FX | Design guidance only; no production-grade ledger/tax engine is claimed |
| Production operations | Backup/restore drills, monitoring, disaster recovery, production deployment and rollback are not verified |

## API security notes

Business API routes require a signed-in session and CSRF protection by default (`REQUIRE_API_AUTH=1`). Browser-session JSON mutations must send the session CSRF token using `X-CSRF-Token`. Admin subscription APIs remain protected even if legacy demo mode is explicitly enabled. Do not disable authentication on a network-exposed server.

The current login is a **single configured platform-admin account**. Do not treat that as complete RBAC or as a school-user login system.

## Subscription administration

The `/admin/subscriptions` page supports creating plans and recording admin-approved free, trial, or manually recorded paid access. It records an actor/reason and audit events. It does not charge cards, verify a bank transfer, or confirm a payment. A real billing provider and verified, idempotent server-side webhooks must be implemented before online payment can activate access.

## Remaining work and release gates

The complete prioritized backlog is in [docs/REMAINING_CAPABILITIES.md](docs/REMAINING_CAPABILITIES.md), with tracking issue [#3](https://github.com/hijaz7861/ai-edu-platform/issues/3).

The most important outstanding work is:
1. Persistent identities and complete server-side RBAC.
2. Verified tenant isolation across every route, export and background job.
3. Enforced subscription entitlements on every paid operation.
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
services/attendance.py       # attendance workflows
services/ingestion.py        # OCR/ingestion helpers
services/questions.py        # duplicate detection and question-bank logic
services/exams.py            # exam blueprint assembly
services/performance.py      # performance analysis
services/governance.py       # approval gates, audit and provider governance
providers/base.py            # provider interface and configured/offline provider logic
templates/                   # login, dashboard and subscription UI
tests/                       # automated tests
docs/                        # architecture, integration, finance and release-gate documents
```
