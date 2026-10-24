# Roles, Permissions, and Tool Categories

## Role selection is not authorization

The UI may let an authorized administrator assign a role/category to a user, but a user must never grant themselves privileges by selecting a label. The server resolves permissions from the authenticated identity, assigned role, institution/tenant scope, and explicit permission grants on every request. Deny by default. Log role changes and require a reason. Protect the last platform administrator from accidental removal/demotion and require re-authentication or step-up confirmation for high-risk changes.

## Suggested built-in role categories

These are suggested defaults for implementation, not proof that all permissions are already enforced in the current application.

| Role | Intended scope | Typical capabilities |
|---|---|---|
| Platform Owner / Super Admin | Entire platform | Manage platform settings, tenant onboarding, role catalog, global AI connectors, plan catalog, free-access grants, usage limits, audit logs, emergency disablement |
| Platform Admin | Platform operations | Manage institutions, users, plans, connector policies and support workflows; destructive or security-critical actions may require owner confirmation |
| Institution Admin | One assigned school/tenant | Manage that school's profile, campuses, classes, staff, students, school settings and approved integrations |
| Manager / Principal | Assigned school and delegated scope | Review dashboards, approve selected workflows, manage assigned teams and reports; no platform-wide secrets or cross-tenant data |
| Teacher / Staff | Assigned classes and permitted records | Attendance, learning content, questions, exams and student progress within assigned scope |
| Student | Own account and authorized learning scope | View assigned courses, complete activities, see own results and permitted feedback |
| Parent / Guardian | Linked student(s) only | View permitted attendance, progress, notices and reports for linked dependents |
| Finance / Billing | Assigned billing scope | View invoices, plans and billing status; no student academic records or API secrets |
| Auditor / Read-only Support | Explicitly scoped records | Read approved reports and audit events; no mutations by default |
| Integration / AI Agent | Narrow machine identity | Only explicitly granted API tools, scoped tenant data, rate/budget limits and auditable actions |

Roles should be assignable per institution where needed, but platform-wide roles must require separate, highly privileged assignment. Support multiple roles only if the permission resolver combines them predictably and safely; a deny/explicit restriction should not be bypassed by an unrelated role.

## Permission model

Use permissions such as `institutions.read`, `institutions.manage`, `users.invite`, `roles.assign`, `attendance.write`, `exams.generate`, `reports.read`, `subscriptions.grant_free`, `billing.manage`, `ai_connectors.configure`, `ai_connectors.use`, `tools.execute`, `audit.read`, and `platform.settings.manage`.

Every protected API and tool must enforce authorization server-side. Enforce tenant scope in the database query itself, not just in UI filters. Caller-supplied institution IDs are not trusted identity context. Check role, permission, tenant, subscription entitlement, and action approval where applicable. Test cross-tenant denial with at least two tenants.

## Tool catalog and connector categories

Provide a catalog where admins can enable only approved tools and assign them by role, tenant, or integration identity.

- **AI providers:** OpenAI-compatible APIs, Mistral, Gemini (dedicated adapter or tested compatible endpoint), Anthropic, and approved local models. Each vendor must pass its own contract tests before being marked supported.
- **Education tools:** question generation/review, exam assembly, curriculum/topic analysis, learning-resource search, student-progress summaries.
- **School operations:** attendance, class and timetable management, notices, approvals, institution reporting.
- **Communication:** email, SMS, push notifications and messaging providers, each with consent, delivery logs and rate controls.
- **Storage and documents:** controlled file upload, OCR, report export, approved cloud storage.
- **Analytics and reporting:** scoped dashboards, exports, usage and cost reporting.
- **External integration:** calendar, LMS, CRM or other systems through individually configured connectors and least-privilege credentials.
- **ChatGPT/MCP:** expose narrowly scoped tools such as `get_school_summary`, `list_authorized_classes`, `draft_questions`, `get_attendance_report`, and `request_approval`. A separate high-privilege administrative tool set may exist, but must require strong identity, explicit permission, tenant checks, audit events, and confirmation/approval for sensitive mutations.

Do not expose a universal `run_any_command`, arbitrary SQL, shell access, or unrestricted HTTP-fetch tool to the model. Treat tool arguments and model outputs as untrusted. Tool descriptions are not authorization.

## Admin UI options

The administration interface should have separate sections for:
1. Users and role assignment.
2. Permission matrix and custom roles.
3. AI provider connections and connection tests.
4. Tool catalog and per-role/per-tenant tool grants.
5. Subscription plans, free grants and entitlement policies.
6. Usage, quotas, spend caps and rate limits.
7. Audit trail, approvals and emergency disablement.

Secret values must be write-only in the UI. A test-connection operation must show a redacted result and must not claim a live test when it only validates saved configuration.

## Acceptance gates before production

- Test every role against both allowed and denied actions.
- Test cross-tenant read and write attempts for each sensitive API and AI tool.
- Verify role assignment, privilege escalation prevention, last-admin protection, session expiry, CSRF, and audit trails.
- Verify entitlement enforcement on each paid route and tool.
- Verify connector secret encryption, rotation, redaction, per-tenant scoping, timeout, rate limits and budget caps.
- Run end-to-end tests for MCP authentication and confirm that tools cannot bypass the same policy enforcement as the web API.
- Run migration, backup/restore and production configuration checks.

The current repository has a single configured platform-admin login and does not yet implement this complete role/permission matrix or enforce tenant isolation across every endpoint. This document defines the target and must not be treated as evidence that the implementation is complete.
