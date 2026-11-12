# Tenant-bound background jobs

## Implemented scope

This increment adds a persistent SQLite queue, an authenticated tenant enqueue/status API, a leased worker claim protocol and a separate worker CLI. The only registered handler is a read-only `tenant_health_check` that returns aggregate counts for selected tenant-owned tables. OCR, AI, payment, connector, upload and notification handlers remain disabled until their data ownership, provenance, entitlement and idempotent side-effect contracts receive dedicated review and tests.

## Security controls

- Enqueue derives `institution_id` and `actor_id` only from the authenticated school session; client-supplied tenant/actor fields are rejected.
- Enqueue requires the exact institution-scoped `manage_institution` permission. Status reads require `manage_institution` or `view_all_reports`.
- Every worker claim revalidates active user, active institution, current role assignment and the required permission. Suspension, reassignment or role downgrade fails the job before handler execution.
- Job lookup filters by both job ID and authenticated tenant; foreign jobs return the same 404 as missing jobs.
- Idempotency keys are SHA-256 hashed at rest and unique per institution; reuse with a different canonical request returns 409.
- Claims use `BEGIN IMMEDIATE`, worker-owned leases and bounded attempts. Expired leases can be reclaimed and stale workers cannot complete a job after losing the lease.
- Only explicit `RetryableJobError` exceptions retry with bounded backoff. Unexpected exception text is not persisted.
- Payload/result sizes are bounded; job transition events are append-only.

## Worker command

Use the same durable database path as the web process:

```bash
AI_EDU_DB=/path/to/platform.db python tools/run_tenant_jobs.py
AI_EDU_DB=/path/to/platform.db python tools/run_tenant_jobs.py --loop --poll-interval 2
```

The default performs one pass and exits. The loop option is intended for a dedicated worker process. The CLI handles `SIGTERM` and `SIGINT`: it stops claiming new jobs, lets the current synchronous `run_once` call return, closes its per-job database connection, and then exits. A supervisor must allow enough shutdown grace time for the current handler to finish. This is graceful between jobs, not cancellation of an in-flight handler; a blocked handler still needs bounded I/O timeouts and cooperative lease heartbeats. An abrupt kill can leave a running job leased until expiry.

No production worker process or scheduler is configured by this PR. SQLite is a single-host reference queue, not a multi-host distributed queue; multi-instance deployment requires a shared transactional database, connection pooling, migration work and lease heartbeat/cancellation design.

## Additive SQLite migrations — partial implementation

`db.init_db()` now serializes the idempotent base-schema bootstrap with `BEGIN IMMEDIATE` and a 10-second SQLite busy timeout. Additive upgrades are recorded in the append-only `schema_migrations` ledger and applied under a separate write transaction.

The current numbered migrations cover:

1. `users.password_hash` for databases created before school-user password authentication.
2. The tenant job and append-only job-event tables, indexes and triggers.

Migration code and its ledger entry commit atomically. Repeated initialization is idempotent. Existing case-variant duplicate emails are preserved; the case-insensitive unique index is installed only after duplicates are resolved. CI regressions cover legacy upgrade, repeated startup, duplicate preservation/reconciliation and four concurrent initializers.

This is not yet a complete migration history for every table in the original base schema, nor does it provide a migration path from SQLite to a production server database. Those remain release blockers.

## Remaining production gates

- No production/staging worker process or scheduler is configured; the CLI now supports graceful SIGTERM/SIGINT shutdown between jobs, but no live supervisor integration or worker health/metrics endpoint is configured.
- OCR/AI/connector/payment/notification handlers are not registered.
- Long-running jobs still need cooperative cancellation, per-side-effect authorization/idempotency and bounded I/O; graceful process shutdown between jobs is implemented, but no live supervisor integration exists.
- Full versioned migration coverage for the entire base schema and future schema changes remains outstanding.
- Off-site backup/restore, worker metrics/alerts and multi-node queue support remain outstanding.

## Cooperative lease heartbeat

Long-running handlers must call `context.heartbeat()` between bounded stages and
before/after any external side effect. The call extends the current worker's
lease only while that worker still owns an unexpired lease. It raises
`LeaseLost` if the lease expired, was reclaimed, or belongs to another worker;
an expired worker cannot revive its old lease.

Heartbeat is cooperative, not a background thread. A handler blocked inside a
non-cooperative network or native call can still lose its lease. Such handlers
must use bounded I/O timeouts, idempotency keys for each external side effect,
and check/renew ownership before publishing results. A heartbeat cannot make
an already-issued external side effect exactly-once.

The worker still uses a local SQLite database in this repository. A shared
multi-host queue, production scheduler, cancellation protocol, lease metrics,
and independently operated worker process remain release blockers.
