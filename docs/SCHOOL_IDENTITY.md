# School identity and tenant-bound access

## What this increment implements

- School users are stored in the existing `users` table with a Werkzeug scrypt password hash; plaintext passwords are never persisted.
- The database initialization applies an additive `password_hash` migration to existing SQLite databases.
- A platform administrator can provision a school user through `POST /api/admin/school-users` with a JSON body containing `email`, `password`, `institution_id`, and `role_name`. This endpoint is protected by the existing platform-admin session and CSRF middleware.
- Supported initial roles are institution-scoped only: Institution Admin, Principal, Academic Coordinator, Examiner, and HR/Attendance Officer. Platform-wide, class-scoped, and department-scoped role assignments are deliberately rejected until their route authorization rules are implemented.
- School users sign in through the School staff form at `/auth/school-login`. CSRF protection and the existing persistent login throttling apply.
- The school portal exposes `GET /api/school/me`, `GET /api/school/classes`, `POST /api/school/classes`, `GET /api/school/question-banks`, `GET /api/school/exam-blueprints`, `GET /api/school/attendance`, `GET /api/school/leave-requests`, `POST /api/school/jobs`, and `GET /api/school/jobs/<job_id>`. Every route/method is explicitly allowlisted. Class, question-bank and exam-blueprint reads filter by the institution stored in the authenticated database identity, never a request-supplied tenant selector. Question-bank metadata requires `manage_institution`, `manage_curriculum`, or `verify_questions` plus the `question_bank_analysis` entitlement in production. Exam-blueprint metadata requires `manage_institution`, `manage_curriculum`, or `publish_exam` plus the `exam_generation` entitlement in production. Responses expose metadata only, not question content, answers, source text or generated exam contents. Class creation requires the institution-scoped `manage_institution` permission, rejects campus IDs belonging to another institution, and enforces the `class_management` subscription entitlement in production.
- Each school API request revalidates that the user and institution remain active and that the user still has an institution-scoped role assignment. Authenticated role boundaries run before the development legacy-mode bypass: school sessions are limited to the school portal, public health/authentication routes, and tenant-bound `/api/school/*`; unknown/retired roles cannot reach the platform dashboard or business routes. School logout is POST-only and CSRF-protected. The school API namespace also uses an exact endpoint/method allowlist; new routes and methods are denied until reviewed and added with explicit tenant and role checks. The route audit test compares every registered school endpoint/method against that allowlist.

## Provisioning example

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

This is a bounded first implementation, not a claim of complete production identity management. Password recovery, invitation/verification emails, MFA, account lockout policy review, session revocation/versioning, audit events for user provisioning, institution-admin user management UI, class/department role enforcement, route-by-route permission mapping for institution roles, and tenant-scoped authorization for future school-facing legacy APIs remain outstanding. The regression suite now enumerates every registered Flask route and dashboard blueprint method and denies school-session access outside the explicit school/public allowlist; this is a boundary test, not proof that every platform-admin data query is institution-scoped. Existing legacy APIs continue to be platform-admin-only; they must not be exposed to school sessions until each route has explicit tenant and permission checks. A real production launch still requires full regression CI, deployment-specific secrets, database/backups/restore drills, monitoring, and live staging verification.

## Current school API permission matrix

The following table describes only the school routes currently implemented.
All authenticated institution roles may read their own `/api/school/me`
profile. Class listing is tenant-scoped and permits Institution Admin,
Principal and Academic Coordinator (`manage_curriculum`). Class creation and
job enqueue require `manage_institution`. Job status is tenant-scoped and
permits Institution Admin and Principal (`view_all_reports`).

| Institution role | Read own profile | List own classes | Create class | List question-bank metadata | List exam-blueprint metadata | Read attendance | List leave requests | Enqueue job | Read tenant job |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Institution Admin | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Principal | Yes | Yes | No | No | Yes | Yes | Yes | No | Yes |
| Academic Coordinator | Yes | Yes | No | Yes | Yes | No | No | No | No |
| Examiner | Yes | No | No | Yes | Yes | No | No | No | No |
| HR/Attendance Officer | Yes | No | No | No | No | Yes | Yes | No | No |

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
- This is a read-only API. Mutation methods are denied by the exact school route/method allowlist. Existing platform-admin attendance write endpoints remain platform-admin-only; school-side check-in/out, leave workflows, corrections and approval actions are not implemented by this increment.

## Tenant-scoped leave-request read API

- `GET /api/school/leave-requests` returns at most 200 leave-request metadata rows for the tenant bound to the revalidated school identity.
- The query requires both the linked teacher and leave type to belong to the authenticated institution. This is deliberate because legacy `leave_requests` rows have no direct tenant column; rows with inconsistent teacher/type ownership are excluded.
- Access requires an institution-scoped `manage_institution`, `approve_leave`, or `view_all_reports` permission and the `leave_management` subscription entitlement when entitlement enforcement is enabled.
- The optional `status` filter accepts one of `pending`, `approved`, or `rejected`. Duplicate/invalid status values and unsupported query parameters are rejected; a supplied `institution_id` is ignored.
- The response includes only request ID, teacher ID, leave-type name, dates, status and creation timestamp. It excludes free-text reasons, substitute-teacher IDs, approver identities and approval comments.
- The route is read-only. Leave submission, correction, approval and rejection mutations remain platform-admin-only/not exposed to school sessions until transactional tenant/role/idempotency rules are implemented.
