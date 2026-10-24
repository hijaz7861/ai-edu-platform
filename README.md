# AI Education Platform — Runnable Reference Implementation

This is a real, tested, runnable slice of the master spec — not a mockup. Everything
marked "real" below actually executes in this sandbox with no network access. It is
**not** integrated into your actual SuperPlatform repo (I don't have access to it) —
treat this as a reference to port logic from, not a drop-in replacement.

## Run it

```bash
pip install -r requirements.txt   # Flask, opencv-python, pytesseract, scikit-learn, pillow
                                   # (all were already available in the build sandbox)
python3 app.py                    # starts on http://localhost:8080
```


### API security

Business API routes require an authenticated session and CSRF protection by default (`REQUIRE_API_AUTH=1`). Configure `SECRET_KEY`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD` before using the login UI. Browser-session JSON requests must send the session CSRF token in the `X-CSRF-Token` header. The unauthenticated curl example below works only when `REQUIRE_API_AUTH=0` is explicitly set for an isolated local demo; do not disable API authentication on a network-exposed server.

Try it:
```bash
curl -X POST http://localhost:8080/institutions \
  -H "Content-Type: application/json" \
  -d '{"name":"Springfield Academy","code":"SPR"}'
```

## Run the tests

```bash
python3 -m pytest -q
```

39 tests passed on 2026-10-22 using `python3 -m pytest -q` in Ubuntu (Python 3.14.4, pytest 9.0.2).
One real bug was caught and fixed while building this: a verification heuristic
that incorrectly flagged short-but-valid definition questions as ambiguous
(`providers/base.py`). Another was caught wiring up the Flask app itself: a
single shared SQLite connection breaks across Flask's request threads — fixed
by switching to a per-request connection via `flask.g` (see `app.py`).

## What's genuinely real vs. honestly stubbed

| Area | Status |
|---|---|
| Tenancy, RBAC, multi-tenant isolation (Phase 1) | **Real** — tested, enforced in code |
| Attendance policy math, corrections, multi-level leave (Phase 2) | **Real** |
| OCR (Phase 3) | **Real** — actual Tesseract 5.3.4, real image preprocessing (OpenCV), tested against generated images |
| Structure detection (chapters/topics) | **Real but heuristic** — regex pattern matching, not an LLM. Works on clearly-formatted headings; won't do deep understanding. |
| Concept extraction / "deep academic analysis" (Phase 4) | **Not implemented here** — this genuinely needs an LLM call. The schema and pluggable-provider hook exist; the reasoning doesn't. |
| Question duplicate detection (Phase 5) | **Real** — TF-IDF + cosine similarity (scikit-learn), tested against genuinely similar vs. unrelated questions |
| Question *generation* wording, AI verification reasoning | **Stubbed** — `providers/base.py`'s `StubProvider` is deterministic and rule-based, clearly labeled as not a real LLM |
| Exam blueprint → paper assembly (Phase 6) | **Real** — genuine selection algorithm against the actual question pool; raises `InsufficientQuestionsError` rather than inventing questions when the pool is short |
| Student performance / topic mastery (Phase 7) | **Real** — traced through actual stored answers, not inferred |
| Notifications delivery, voice/NLU parsing (Phase 8) | **Not implemented here** — delivery needs real email/SMS providers (network); NLU beyond simple keyword matching needs a real model. Schema exists. |
| Agent registry, approval gates, self-improvement (Phase 9) | **Real** — gated actions are provably blocked until approved (tested) |
| Audit logging, credential redaction, provider fallback, retention deletion (Phase 10) | **Real** — all independently tested |
| Testing & acceptance tracking (Phase 11) | **This is it** — 39 tests passed in the latest verified run |

## Porting this into the real SuperPlatform repo

1. This uses SQLite + Flask because that's what was available offline in the
   build sandbox — **not necessarily your real stack**. Swap the DB layer
   (`db.py`) and web layer (`app.py`) for whatever SuperPlatform already uses;
   the `services/` logic is framework-agnostic and should port with minimal changes.
2. Wire a real `AIProvider` (implementing `providers/base.py`'s interface) for
   question generation, verification reasoning, deep concept extraction, and
   NLU — that requires network access to an actual model.
3. Wire real OCR input sources (PDF page rasterization, actual scanned uploads)
   — the OCR *engine* call itself (`services/ingestion.py::run_ocr`) is already real.
4. Wire real notification channels (email/SMS/push) — the routing logic in the
   schema is ready; delivery isn't implemented here.
5. Everything else — attendance, RBAC, exams, performance, approval gates,
   audit, retention — should port close to as-is.

## Layout

```
db.py                  # full schema (all 11 phases) + connection helper
services/core.py       # Phase 1: tenancy, RBAC
services/attendance.py # Phase 2
services/ingestion.py  # Phase 3: real OCR
services/questions.py  # Phase 5: real dedup, verification gate
services/exams.py      # Phase 6: real blueprint assembly
services/performance.py# Phase 7
services/governance.py # Phase 9 + 10: approval gates, audit, providers, retention
providers/base.py       # pluggable AI provider interface + honest offline stub
app.py                  # runnable Flask server wiring the above
tests/                   # Automated test suite
```


---

## SaaS commercial-access foundation (work in progress)

The repository is being extended toward a multi-school service business. The
subscription foundation currently includes:

- **Plan catalog:** plans store a stable code, display name, currency, billing
  interval, price in integer minor units, and JSON feature entitlements.
- **Free school access:** a platform administrator can grant a school a free
  plan, record a required reason, and optionally set an expiry date.
- **Access checks:** feature access fails closed when there is no eligible
  subscription, a feature is missing, or its value is not explicitly `true`.
- **Audit trail:** grants and subscription status changes are recorded with
  the acting account and reason.
- **Admin UI:** open `/admin/subscriptions` after signing in to configure plans,
  grant school access, inspect records, and suspend an active grant.
- **Clean-checkout database startup:** the SQLite parent directory is created
  automatically, addressing the database-open error seen in GitHub Actions on
  2026-10-22.

### Important production limits

This is a foundation, **not yet a production-ready commercial SaaS release**.
The current login is one configured platform-admin credential, not a complete
school-user identity system. Payment collection, signed billing webhooks,
password reset, per-school user provisioning, enforced role-based authorization
on every business route, tenant-scoped dashboard queries, backups/restore,
monitoring, and a production deployment review still need implementation and
verification. A subscription record by itself must not be treated as proof that
a payment was received. Real payments must be reconciled from verified
server-side billing-provider events.

The new subscription endpoints are session-authenticated and CSRF-protected.
The entitlement service is available for business routes to call, but **each
paid feature must explicitly enforce its entitlement at the server boundary**
before this is advertised to paying schools.

### Current verification status

The previous main-branch CI run failed while importing the Flask app because
the SQLite storage directory did not exist in a fresh runner. The branch change
creates that directory before opening the database and adds tests for plan
grants, expiry, audit records, overlapping subscriptions, authentication, and
CSRF. These new tests must pass in GitHub Actions before the changes are merged.
Do not interpret the older 39-test result above as verification of this branch.
