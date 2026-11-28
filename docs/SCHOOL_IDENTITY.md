# School identity and tenant-bound access

## What this increment implements

- School users are stored in the existing `users` table with a Werkzeug scrypt password hash; plaintext passwords are never persisted.
- The database initialization applies an additive `password_hash` migration to existing SQLite databases.
- A platform administrator can provision any supported school role through `POST /api/admin/school-users`. An active Institution Admin with the institution-scoped `manage_users` permission can provision only a least-privilege Teacher Portal identity through `POST /api/school/teacher-identities`; the service revalidates actor, tenant and permission inside the same `BEGIN IMMEDIATE` transaction as account/link/audit creation. Tenant self-service cannot grant staff roles or accept a client-selected institution/actor.
- Supported institution-scoped roles are Institution Admin, Principal, Academic Coordinator, Examiner, HR/Attendance Officer, and the least-privilege Teacher Portal role. The existing class-scoped Teacher role is distinct and is not accepted as a school-login role. Platform-wide, class-scoped, and department-scoped role assignments are deliberately rejected until their route authorization rules are implemented.
- School users sign in through the School staff form at `/auth/school-login`. CSRF protection and the existing persistent login throttling apply.
- The school portal exposes `GET /api/school/me`, `GET /api/school/classes`, `POST /api/school/classes`, `GET /api/school/question-banks`, `GET /api/school/exam-blueprints`, `GET /api/school/attendance`, `GET /api/school/leave-requests`, `POST /api/school/leave-requests`, `GET /api/school/teacher/me`, `GET /api/school/teacher/attendance`, `POST /api/school/teacher/attendance/events`, `GET /api/school/teacher/leave-requests`, `POST /api/school/teacher/leave-requests`, `POST /api/school/teachers/<teacher_id>/identity/revoke`, `POST /api/school/teacher-identities`, `POST /api/school/jobs`, and `GET /api/school/jobs/<job_id>`. Every route/method is explicitly allowlisted. Class, question-bank and exam-blueprint reads filter by the institution stored in the authenticated database identity, never a request-supplied tenant selector. Question-bank metadata requires `manage_institution`, `manage_curriculum`, or `verify_questions` plus the `question_bank_analysis` entitlement in production. Exam-blueprint metadata requires `manage_institution`, `manage_curriculum`, or `publish_exam` plus the `exam_generation` entitlement in production. Responses expose metadata only, not question content, answers, source text or generated exam contents. Class creation requires the institution-scoped `manage_institution` permission and `class_management` entitlement, revalidates both inside `BEGIN IMMEDIATE`, verifies campus ownership, and commits any default-campus creation, class row and audit event atomically. An audit failure rolls the whole write back.
- Each school API request revalidates that the user and institution remain active and that the user still has an institution-scoped role assignment. Authenticated role boundaries run before the development legacy-mode bypass: school sessions are limited to the school portal, public health/authentication routes, and tenant-bound `/api/school/*`; unknown/retired roles cannot reach the platform dashboard or business routes. School logout is POST-only and CSRF-protected. The school API namespace also uses an exact endpoint/method allowlist; new routes and methods are denied until reviewed and added with explicit tenant and role checks. The route audit test compares every registered school endpoint/method against that allowlist.

## Platform-admin provisioning example

Only run as an authenticated platform administrator; include the CSRF token in the `X-CSRF-Token` header.

```http
POST /api/admin/school-users
Content-Type: application/json
X-CSRF-Token: <session-csrf-token>

{
  "email": "principal@example.edu",
  "password": "<unique password of at least 12 characters>",
  "institution_id": "<existing active institution ID>",
  "role_name": "Principal"
}
```

## Remaining limitations — not launch-ready by itself

This is a bounded first implementation, not a claim of complete production identity management. The 19 currently exposed school route/method pairs now have a centralized, fail-closed any-of permission registry enforced by middleware and an integration matrix across all six supported institution roles. Password recovery, invitation/verification emails, MFA, account lockout policy review, session revocation/versioning, institution-admin user management UI, class/department role enforcement, and tenant-scoped authorization for future school-facing legacy APIs remain outstanding. The regression suite now enumerates every registered Flask route and dashboard blueprint method and denies school-session access outside the explicit school/public allowlist; this is a boundary test, not proof that every platform-admin data query is institution-scoped. Existing legacy APIs continue to be platform-admin-only; they must not be exposed to school sessions until each route has explicit tenant and permission checks. A real production launch still requires full regression CI, deployment-specific secrets, database/backups/restore drills, monitoring, and live staging verification.

## Current school API permission matrix

The following table describes only the school routes currently implemented.
All authenticated institution roles may read their own `/api/school/me`
profile. Class listing is tenant-scoped and permits Institution Admin,
Principal and Academic Coordinator (`manage_curriculum`). Class creation and
job enqueue require `manage_institution`. Teacher identity revocation requires
`manage_users` and is limited to the authenticated institution. Job status is
tenant-scoped and
permits Institution Admin and Principal (`view_all_reports`). Attendance reads
require `manage_institution`, `view_all_reports`, or `manage_attendance_policy`
and the `attendance` entitlement. Leave-request reads require
`manage_institution`, `approve_leave`, or `view_all_reports` and the
`leave_management` entitlement.

| Institution role | Read own profile | List own classes | Create class | List question-bank metadata | List exam-blueprint metadata | Read attendance | List leave requests | Create leave request | Decide leave approval | Enqueue job | Read tenant job | Revoke teacher identity |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Institution Admin | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | No | Yes | Yes | Yes |
| Principal | Yes | Yes | No | No | Yes | Yes | Yes | Yes | Yes | No | Yes | No |
| Academic Coordinator | Yes | Yes | No | Yes | Yes | No | No | No | No | No | No | No |
| Examiner | Yes | No | No | Yes | Yes | No | No | No | No | No | No | No |
| HR/Attendance Officer | Yes | No | No | No | No | Yes | Yes | Yes | Yes | No | No | No |
| Teacher Portal | Yes | No | No | No | No | No | No | No | No | No | No | No |

The permission matrix is enforced centrally by `_SCHOOL_ROUTE_PERMISSION_REQUIREMENTS` in `app.py`, keyed by the exact Flask endpoint and HTTP method. The middleware resolves permissions only from the active institution-scoped database assignments; it never accepts role or permission claims from request data. The route audit test requires exact coverage between the method allowlist and permission registry, checks the seeded-role matrix, and exercises all 18 route/method pairs under each of the six supported school roles. Domain handlers continue to enforce teacher-link ownership, tenant resource joins, feature entitlements and transactional checks for sensitive writes.

This is not a blanket permission for future endpoints. New routes and methods
remain denied until explicitly added to the exact route allowlist and covered
by tenant, entitlement and role-negative tests.

## Tenant academic read API details

- `GET /api/school/question-banks` returns at most 200 rows of question-bank metadata for the authenticated institution. It does not return question text, answer keys, verification details or source documents.
- `GET /api/school/exam-blueprints` returns at most 200 rows of blueprint metadata for the authenticated institution. It does not return generated exams, selected questions or answer keys.
- Both routes derive the tenant exclusively from the revalidated school session. A supplied `institution_id` query parameter is ignored and cannot switch the tenant.
- Question-bank reads require an institution-scoped `manage_institution`, `manage_curriculum` or `verify_questions` permission and the `question_bank_analysis` entitlement when entitlement enforcement is enabled.
- Exam-blueprint reads require an institution-scoped `manage_institution`, `manage_curriculum` or `publish_exam` permission and the `exam_generation` entitlement when entitlement enforcement is enabled.
- Regression coverage verifies two-tenant isolation, role-by-route denial, no platform-wildcard permission leakage, safe response field allowlists and fail-closed behavior without an active entitlement.

## Tenant-scoped attendance read API

- `GET /api/school/attendance` returns at most 200 teacher-attendance event metadata rows for the institution bound to the revalidated school identity.
- The endpoint requires an institution-scoped `manage_institution`, `view_all_reports`, or `manage_attendance_policy` permission and the `attendance` subscription entitlement when entitlement enforcement is enabled.
- The optional `date=YYYY-MM-DD` filter applies to the UTC `created_at` date. Duplicate or invalid date filters and unsupported query parameters are rejected. A client-supplied `institution_id` is ignored and cannot switch tenants.
- Both the attendance event and its linked teacher must belong to the authenticated institution. Responses contain only event/teacher identifiers, event type, time, method, status and creation timestamp; no email, reason, policy configuration or other tenant data is returned.
- This is a read-only API. Mutation methods are denied by the exact school route/method allowlist. Existing platform-admin attendance write endpoints remain platform-admin-only; school-side attendance writes and corrections are separate workflows. Tenant-scoped leave reads, submission and ordered approval decisions are documented below.

## Tenant-scoped leave-request read API

- `GET /api/school/leave-requests` returns at most 200 leave-request metadata rows for the tenant bound to the revalidated school identity.
- The query requires both the linked teacher and leave type to belong to the authenticated institution. This is deliberate because legacy `leave_requests` rows have no direct tenant column; rows with inconsistent teacher/type ownership are excluded.
- Access requires an institution-scoped `manage_institution`, `approve_leave`, or `view_all_reports` permission and the `leave_management` subscription entitlement when entitlement enforcement is enabled.
- The optional `status` filter accepts one of `pending`, `approved`, or `rejected`. Duplicate/invalid status values and unsupported query parameters are rejected; a supplied `institution_id` is ignored.
- The response includes only request ID, teacher ID, leave-type name, dates, status and creation timestamp. It excludes free-text reasons, substitute-teacher IDs, approver identities and approval comments.
- Staff-assisted leave submission is available through `POST /api/school/leave-requests`. It is limited to Institution Admin, Principal and HR/Attendance Officer, requires the `leave_management` entitlement when enforcement is enabled, and requires a CSRF token plus an `Idempotency-Key`. The service revalidates the actor and institution inside `BEGIN IMMEDIATE`, checks that the teacher, leave type and optional substitute teacher all belong to the authenticated institution, and atomically creates the request, configured approval rows, audit event and hashed idempotency record. Replaying the same key and payload by the same actor returns the original request; a different payload or actor receives 409. The response never returns the free-text reason. Leave approval/rejection decisions are exposed through the separate transactional, ordered decision endpoint described below; attendance and leave-correction mutations remain unexposed.


## Tenant-scoped leave-request submission

- `POST /api/school/leave-requests` creates a request for an active teacher in the institution bound to the revalidated school session. This is a staff-assisted submission endpoint; it is not teacher self-service because school identities are not yet linked to individual teacher records.
- Allowed roles are Institution Admin (`manage_institution`), Principal (`approve_leave`) and HR/Attendance Officer (`approve_leave` or `manage_attendance_policy`). Academic Coordinator and Examiner are denied.
- The `leave_management` entitlement is required whenever production or explicit entitlement enforcement is enabled. The route is POST-only and protected by the existing school-session CSRF check.
- The body accepts only `teacher_id`, `leave_type_id`, `start_date`, `end_date`, `reason`, and optional `substitute_teacher_id`. Tenant ID and actor ID are never accepted from the client. The teacher and optional substitute must be active and tenant-owned; the leave type must be tenant-owned. Foreign or inactive teacher references and foreign leave types return the same 404.
- Dates must be canonical `YYYY-MM-DD`, end date cannot precede start date, and the required reason is limited to 2,000 characters. The response contains only request ID and workflow status, never the reason.
- An `Idempotency-Key` is mandatory. The key is SHA-256 hashed at rest and scoped to institution. Its retry record expires after 24 hours and is pruned on the next leave submission for that institution; a global scheduled purge for dormant tenants remains an operations/retention task. A transaction creates the request, approval rows, audit event and idempotency record atomically. Same actor + same key + same payload within the retry window replays the original request; a different actor or payload returns 409.
- Leave types configured with one or two approval levels create the corresponding pending approval rows. A leave type explicitly configured with `requires_approval=0` is created as approved with no approval rows; NULL or malformed legacy values fail closed and require approval. Approval/rejection decisions are exposed by the separate ordered decision endpoint below.
- Regression coverage includes cross-tenant teacher/type/substitute denial, five-role allow/deny behavior, idempotency replay/conflict, CSRF and missing-key rejection, date/reason validation, production entitlement denial/allowance, multi-level and no-approval configuration, and response privacy.


## Leave date-overlap protection

- Leave submission checks for an existing pending or approved request only when both its teacher and leave type belong to the authenticated institution, and its inclusive date range overlaps the requested range. Inconsistent legacy rows that connect one tenant's teacher to another tenant's leave type are ignored so corrupt cross-tenant history cannot deny service to either tenant.
- The check runs after BEGIN IMMEDIATE and before insertion, so concurrent submissions against the same SQLite database are serialized; a conflicting request returns HTTP 409 with leave_dates_overlap.
- Rejected requests do not reserve dates. Adjacent ranges are allowed (for example, an existing request ending on 12 December and a new request starting on 13 December).
- This is a date-overlap guard, not a leave-balance or working-day calculation. Annual balance accounting, holidays/weekends, partial-day leave, cancellation and corrections remain outstanding.


## Tenant-scoped leave approval decisions

- `POST /api/school/leave-requests/<request_id>/decision` accepts only `decision` (`approved` or `rejected`) and optional `comments` (maximum 2,000 characters). Tenant and actor are derived from the revalidated school session; the endpoint requires CSRF and the `leave_management` entitlement when entitlement enforcement is active.
- Only an institution-scoped `approve_leave` permission can decide. `manage_institution` by itself is deliberately insufficient: Institution Admins can submit and manage other school settings but cannot approve leave unless they are separately granted `approve_leave`. Principal and HR/Attendance Officer have the default permission.
- The service revalidates the active user/institution/role and permission inside `BEGIN IMMEDIATE`, then joins both the request's teacher and leave type to the authenticated institution. A foreign-tenant or inconsistent legacy request returns the same 404 as an unknown request.
- Only the next pending approval level can be decided. Earlier levels must be approved; future levels must still be pending. The request creator cannot approve their own submission, and one actor cannot approve multiple levels of the same request. Multi-level requests therefore require distinct approvers. Legacy requests without a recorded creation-audit actor fail closed with 409 because separation of duties cannot be proven.
- The decision row, parent request status and audit record commit in one transaction. Rejection is terminal; approval changes the request to `approved` only when every level has approved. Auto-approved leave types have no approval rows and cannot be decided again.
- A retry by the same approver with the same decision and comments returns the original level and current aggregate status without adding another audit event. A changed decision/comment or a second actor attempting to reuse a completed level receives 409. Comments are stored with the approval record but are not returned in the API response or copied into the audit event.
- This is an ordered approval workflow, not leave-balance accounting. Annual balances, working-day/holiday calculations, partial-day leave, cancellation and corrections remain outstanding.


## Tenant-scoped attendance event writes

- `POST /api/school/attendance/events` records one staff-assisted check-in or check-out for an active teacher in the authenticated institution. It is not teacher self-service because school identities are not yet linked to individual teacher records.
- The exact payload is `teacher_id`, `policy_id`, `event_type` (`check_in` or `check_out`) and `time` (`HH:MM`). Client-supplied institution, actor, date and method fields are rejected. The server assigns the event day from its UTC clock; historical/backdated entry and institution-local calendar semantics are not implemented.
- Only institution-scoped `manage_institution` or `manage_attendance_policy` permission can write. Institution Admin and HR/Attendance Officer are allowed by the default role matrix; Principal, Academic Coordinator and Examiner are denied unless separately granted the required permission. The `attendance` entitlement is enforced in production or explicit entitlement-enforcement mode.
- The service revalidates the active school identity and permission inside `BEGIN IMMEDIATE`, verifies the active teacher belongs to the tenant, and verifies the selected policy belongs to that tenant, is currently effective, and matches any department/campus scope. Foreign or inactive references return the same 404.
- Same-day event ordering is serialized in the transaction: check-out requires a preceding check-in; duplicate check-in/check-out and non-increasing HH:MM sequences are rejected. A subsequent check-in is allowed only after a check-out. Inconsistent legacy event history fails closed instead of being silently ignored.
- An `Idempotency-Key` is mandatory, SHA-256 hashed at rest and scoped to the institution. Same actor + same key + same payload replays the original event; changed payload or another actor returns 409. The 24-hour retry record is pruned on the next event write for that institution; a global purge for dormant tenants remains an operations/retention task.
- The attendance event, actor-attributed audit record and idempotency row commit atomically. The audit actor is the authenticated school user, not the teacher whose attendance is being recorded. The API never accepts a caller-selected method or stores the raw idempotency key.
- Regression coverage includes cross-tenant teacher/policy denial, five-role authorization, CSRF and required-key checks, replay/conflict, check-in/check-out ordering, malformed inputs, production entitlement denial/allowance, corrupt legacy-history denial and concurrent duplicate check-in serialization.
- Attendance corrections/approval, historical entry, timezone-specific day boundaries, leave-balance accounting, exports/uploads and full legacy route authorization remain outstanding. The Teacher Portal self-service scope is limited to active linked teachers and does not provide attendance correction or approval rights.


## Idempotency-key retention maintenance

- Leave-submission and attendance-event retry keys expire after 24 hours. Normal writes prune expired keys only for the tenant being used, so dormant tenants require a global maintenance pass.
- `python tools/purge_expired_idempotency.py` performs a consistent dry-run by default and reports only row counts. It does not delete anything.
- An operator must explicitly pass `--apply` to delete expired rows from `tenant_leave_request_keys` and `tenant_attendance_event_keys`. Both counts and deletions run under one `BEGIN IMMEDIATE` transaction; unexpired keys, leave requests, attendance events and audit records are preserved.
- This utility is not scheduled or automatically executed by the application. Production operations must choose and document a retention cadence, backup/restore procedure and operator authorization before scheduling it.



## Teacher Portal identity linking and self-service

- A platform administrator may provision a Teacher Portal school identity through POST /api/admin/school-users by supplying role_name: Teacher Portal and one teacher_id. The teacher must be active, belong to the supplied institution and have no existing teachers.user_id binding. Non-Teacher-Portal roles may not supply teacher_id; Teacher Portal accounts cannot be created without it. Every school-user provisioning operation also writes a privacy-minimized school_user_created audit row in the same transaction, so an audit-write failure rolls back the account, role assignment and teacher link.
- Migration v6 creates `teacher_identity_links` with unique user and teacher keys, tenant scope, foreign keys and insert-time scope validation. Migration v7 adds immutable `link_id` values and the append-only `teacher_identity_events` ledger. Link updates are blocked; deletion is allowed only when a matching immutable revocation event exists. User or teacher tenant reassignment is blocked while linked.
- The distinct Teacher Portal role is institution-scoped and grants only self_attendance, self_leave_request, and view_own_records. It does not grant staff-wide attendance/leave reads, class management, leave approval, or institution administration.
- Every school API request and school login revalidates the bidirectional Teacher Portal invariant: a Teacher Portal role must have exactly one active same-tenant teacher link and exactly one institution role; a teacher link without the Teacher Portal role is also invalid. Role escalation, role removal or stale links clear the session rather than converting the account into staff. Ordinary staff identities may still hold multiple institution roles.
- GET /api/school/teacher/me returns the linked teacher's minimal profile. GET /api/school/teacher/attendance and GET /api/school/teacher/leave-requests return only the linked teacher's own bounded metadata. The optional legacy institution_id selector is ignored; tenant scope is always derived from the authenticated identity.
- POST /api/school/teacher/attendance/events accepts only policy_id and event_type; both the linked teacher ID and current UTC time are derived server-side, so teachers cannot submit a backdated or client-selected time. POST /api/school/teacher/leave-requests accepts only leave_type_id, start_date, end_date, and reason. Both require CSRF, the corresponding tenant entitlement when enforcement is active, and an Idempotency-Key. Their transactional services revalidate the active link and exact self-service permission inside BEGIN IMMEDIATE.
- Teacher self-service leave requests still enter the configured approval workflow. Teacher Portal users cannot approve their own requests. Attendance remains server-UTC-day based; teacher-local timezone, historical entry, corrections, cancellation, leave balances and working-day calculations are not implemented.
- `POST /api/school/teachers/<teacher_id>/identity/revoke` lets only an active same-tenant `manage_users` actor revoke a Teacher Portal link. It requires CSRF, a reason code and an idempotency key; it atomically writes an append-only `teacher_identity_events` row plus the general audit row, clears `teachers.user_id`, suspends the old school user and deletes the active link. The teacher can then be provisioned to a new school user. Same-key/same-payload retries return the original revocation; key reuse with a different payload returns 409. Cross-tenant targets return 404; Principal, HR/Attendance Officer and Teacher Portal roles are denied by default.
- Password recovery, email verification/invitations, MFA, session revocation/versioning, teacher profile management, and live staging acceptance remain open production identity gates.

## Transaction-level leave entitlement enforcement

- The leave submission and approval routes perform an early feature check for a fast response, but that check is not the authorization boundary.
- `create_school_leave_request()` and `decide_school_leave_approval()` independently recheck the `leave_management` entitlement after `BEGIN IMMEDIATE`, alongside the active tenant identity and role permission.
- This serializes the entitlement decision with SQLite writers: a subscription suspension/expiry committed before the write transaction is observed and denies the operation; a concurrent writer cannot change the subscription until the leave transaction completes.
- The service raises `LeaveEntitlementError`, which maps to `403 subscription_required`. Denial occurs before idempotency replay or any leave/approval/audit mutation.
- Regression tests revoke the subscription after route preflight but before the service transaction and verify that neither a leave request nor an approval decision/audit event is persisted.

## Transactional authorization for paid tenant reads

The following paid read routes now run active identity validation, institution-role
permission checks, subscription entitlement checks, and the protected SELECT in one
SQLite read transaction:

- GET /api/school/classes
- GET /api/school/question-banks
- GET /api/school/exam-blueprints
- GET /api/school/attendance
- GET /api/school/leave-requests
- GET /api/school/teacher/attendance
- GET /api/school/teacher/leave-requests
- GET /api/school/jobs/{job_id} (including each resource entitlement in the job payload)

The first identity SELECT establishes the read snapshot. A revocation committed before
that snapshot is denied; identity, role, entitlement and returned rows are then read
from one consistent database version. The helper fails closed on an already-open
caller transaction, invalid identity/permission input, missing entitlement, or a
transaction ended by the caller. It is a SQLite single-database consistency boundary,
not a claim of distributed transaction or multi-host queue support.


## Institution-admin Teacher Portal provisioning

An active Institution Admin may create a Teacher Portal account for one active,
unlinked teacher in the authenticated institution. The route accepts only
`email`, `password`, and `teacher_id`; institution, actor and role are
server-selected. It requires the session CSRF token and the `manage_users`
permission. The service repeats the identity and permission checks inside the
write transaction, so a role revocation after middleware cannot create an
account. Cross-tenant or already-linked teacher IDs return the same 404
response. Audit insertion is part of the same transaction and failure rolls
back the user, role assignment and teacher link.

```http
POST /api/school/teacher-identities
Content-Type: application/json
X-CSRF-Token: <session-csrf-token>

{
  "email": "teacher@example.edu",
  "password": "<unique password of at least 12 characters>",
  "teacher_id": "<active unlinked teacher ID in this institution>"
}
```

This endpoint intentionally cannot create Institution Admin, Principal,
Academic Coordinator, Examiner or HR/Attendance Officer accounts. Those
staff-role grants remain platform-admin provisioned until a separate
delegation/approval model is reviewed. User invitations, password recovery,
email verification and MFA are still outstanding.


For tenant-provisioning privacy, an email address already registered anywhere
returns the same generic `409 teacher_identity_conflict` response; the endpoint
does not disclose which institution owns the existing account.