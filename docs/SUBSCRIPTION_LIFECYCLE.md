# Subscription lifecycle and concurrency

## Scope

The subscription service records access decisions. It is not a payment gateway and does not verify bank transfers, checkout redirects, provider signatures, settlement, refunds, or disputes. A future payment adapter must authenticate and verify provider events before calling these services.

## Current lifecycle

Allowed status transitions are defined in `services/licensing.py`:

- `trialing` → `active`, `past_due`, `suspended`, `cancelled`, or `expired`
- `active` → `past_due`, `suspended`, `cancelled`, or `expired`
- `past_due` → `active`, `suspended`, `cancelled`, or `expired`
- `suspended` → `active`, `cancelled`, or `expired`
- `cancelled` → `expired`
- `expired` → no further transition

Repeated requests to set the current status are no-ops and do not append another lifecycle event. A cancelled subscription cannot be silently reactivated; create a new subscription instead.

## Transaction and concurrency contract

`grant_subscription()` and `change_subscription_status()` acquire a SQLite `BEGIN IMMEDIATE` transaction before reading lifecycle state. This serializes concurrent writers across independent connections, so two simultaneous grants cannot both pass the “already active” check and concurrent duplicate status events produce only one state-change event.

These service methods own their transaction and require a clean connection. If the connection already has an open transaction, they raise `subscription_write_requires_clean_connection` rather than committing unrelated caller work. Each successful state mutation and its audit event commit together; failures roll back both.

The current eligibility check still uses the subscription's status, plan state, start time, and end time. No schema migration or production database mutation is performed by this change.

## Verification

The licensing regression suite includes independent-connection concurrency tests for competing grants and repeated status changes, plus a test that verifies the clean-connection transaction contract.

## Remaining payment integration work

- Select a provider based on merchant eligibility, supported countries/currencies, settlement and fees.
- Verify provider-specific signatures and timestamps over the exact raw request body.
- Persist and uniquely deduplicate provider event IDs before applying state changes.
- Handle duplicate, delayed, out-of-order, failed, refunded, disputed, and replayed events.
- Reconcile provider settlement against subscriptions and an append-only double-entry ledger.
- Run sandbox contract tests with approved credentials; production payments remain disabled until separate approval.
