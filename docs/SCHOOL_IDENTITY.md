# School identity and tenant-bound access

## What this increment implements

- School users are stored in the existing `users` table with a Werkzeug scrypt password hash; plaintext passwords are never persisted.
- The database initialization applies an additive `password_hash` migration to existing SQLite databases.
- A platform administrator can provision a school user through `POST /api/admin/school-users` with a JSON body containing `email`, `password`, `institution_id`, and `role_name`. This endpoint is protected by the existing platform-admin session and CSRF middleware.
- Supported initial roles are institution-scoped only: Institution Admin, Principal, Academic Coordinator, Examiner, and HR/Attendance Officer. Platform-wide, class-scoped, and department-scoped role assignments are deliberately rejected until their route authorization rules are implemented.
- School users sign in through the School staff form at `/auth/school-login`. CSRF protection and the existing persistent login throttling apply.
- The school portal exposes `GET /api/school/me` and `GET /api/school/classes`. Class results are filtered by the institution stored in the authenticated database identity, not by a request-supplied tenant identifier.
- Each school API request revalidates that the user and institution remain active and that the user still has an institution-scoped role assignment. A school session cannot access platform-admin endpoints.

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

This is a bounded first implementation, not a claim of complete production identity management. Password recovery, invitation/verification emails, MFA, account lockout policy review, session revocation/versioning, audit events for user provisioning, institution-admin user management UI, class/department role enforcement, route-by-route authorization for legacy APIs, and exhaustive cross-tenant tests across all existing business endpoints remain outstanding. Existing legacy APIs continue to be platform-admin-only; they must not be exposed to school sessions until each route has explicit tenant and permission checks. A real production launch still requires full regression CI, deployment-specific secrets, database/backups/restore drills, monitoring, and live staging verification.
