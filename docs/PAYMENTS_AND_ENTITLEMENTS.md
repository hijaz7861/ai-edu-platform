# Payments, Subscriptions, and Entitlement Automation

## Status and scope

This is the target architecture and acceptance checklist. It is not evidence that live payment processing, bank integrations, cryptocurrency monitoring, or automatic entitlement enforcement are already implemented. Do not collect real payments until a payment provider is configured, tested in its sandbox, reviewed for the merchant's jurisdiction, and approved for production.

## Customer experience

1. The customer chooses a plan, billing interval, currency, and supported payment method.
2. The server creates a pending order tied to the authenticated customer/tenant, selected plan, exact amount, currency, and a unique internal order ID.
3. The customer pays using the selected hosted checkout, bank transfer instructions, supported local wallet, or supported crypto payment flow.
4. The platform verifies the result from a trusted provider API/webhook or reconciles a verified bank feed. A receipt or screenshot submitted by the customer is evidence for review, not proof of settlement.
5. Only a verified, settled payment activates the purchased plan's entitlements. Activation is idempotent: duplicate webhooks or repeated receipt submissions must never grant duplicate credits or extend a subscription twice.
6. The customer receives a receipt/status notification and can see invoice, plan, renewal date, payment history, cancellation options, and support status.
7. Renewal failure, expiry, refund, chargeback, or confirmed payment reversal updates the subscription state and entitlements according to explicit policy. Never revoke access based only on an unverified notification.

## Payment adapter strategy

Build a provider-neutral payment interface (create checkout/payment intent, fetch payment status, verify webhook, refund where supported, and reconcile transactions). Keep payment provider logic separate from subscription/entitlement logic.

- **Cards and international checkout:** integrate one or more reputable hosted-checkout providers that support the merchant's legal entity, country, currencies, and customer regions. Evaluate fees, recurring billing, chargebacks, tax/invoice support, payout settlement, and available payment methods before selection.
- **Local wallets and payment rails:** add region-specific providers (for example, local mobile wallets or instant-payment systems) only where the merchant account can legally onboard and the provider offers a verifiable API/webhook or settlement report.
- **Bank transfers:** prefer an official bank/Open Banking API, bank-approved aggregator, or signed provider notification plus settlement reconciliation where available. SMS/email notifications can be supplementary signals only unless the bank exposes an authenticated, reliable verification mechanism. Do not scrape online banking, ask users for bank credentials, or automate login to a bank website.
- **Manual bank transfer:** show a unique reference and exact amount/currency. Let the customer upload a receipt and transaction reference, but set the order to `under_review`. Finance staff verify the credit in the bank statement/official banking portal and record the reviewer and evidence. Two-person approval may be required for high-value or suspicious cases.
- **Cryptocurrency / Binance-related payments:** do not treat a screenshot or user-entered transaction hash as settled payment. Use a compliant payment processor or a dedicated wallet/address flow that verifies the transaction against the correct network, asset, destination, amount, and sufficient confirmations through a trusted node/provider. Handle underpayment, overpayment, wrong network/token, volatile exchange rates, late confirmations, and refunds explicitly. Binance account/deposit integrations require the applicable official API and account permissions; never request customer exchange passwords or withdrawal keys.
- **Cash or offline payments:** create a pending order and allow authorized staff to record a verified receipt with an audit trail. Do not auto-activate from an unverified message.

No single integration supports every bank, country, wallet, currency, and crypto asset. Use a provider catalog with explicit country/currency/method availability rather than promising universal acceptance. Prefer hosted checkout and tokenization; the platform must not store raw card numbers or CVVs.

## Subscription and entitlement state machine

Suggested payment states: `created`, `pending`, `under_review`, `authorized`, `paid`, `failed`, `expired`, `refunded`, `partially_refunded`, `disputed`, `reversed`.

Suggested subscription states: `pending_payment`, `trialing`, `active`, `past_due`, `grace_period`, `suspended`, `cancel_at_period_end`, `canceled`, `expired`.

- Entitlements are derived from a server-side subscription record and plan version, never from a browser-supplied plan name or payment-success URL.
- Define entitlements as explicit limits/features: allowed tools, seats, institutions, storage, AI requests/tokens, usage budget, and support level.
- Enforce entitlement checks in every protected API, background job, and MCP/AI tool before performing work or incurring provider cost.
- Paid upgrades become effective only after verified payment; downgrades and cancellations follow a documented effective-date policy.
- Free plans and administrator-issued grants must be explicit, scoped, expiring where appropriate, reasoned, and audit logged. An administrator grant must not be confused with a payment.
- Renewal handling needs provider subscription events, retry/dunning policy, grace periods, cancellation, and recovery after successful payment.
- Refunds, disputes, chargebacks, and confirmed reversals must trigger a policy-driven entitlement update and audit event.
- Store amounts as integer minor units plus ISO currency code; store crypto quantities with appropriate decimal precision. Never use floating-point arithmetic for money.

## Webhook and reconciliation security

- Verify webhook signatures against the raw request body using the provider's documented verification method; reject invalid signatures and stale/replayed events where supported.
- Store provider event IDs with a unique constraint and process events idempotently. Respond promptly and process heavy work asynchronously.
- Never trust a redirect query parameter, client callback, uploaded receipt, SMS text, email screenshot, or user-submitted transaction hash as sole proof of payment.
- Fetch the payment status from the provider server-to-server before changing an order to paid, where the provider supports it.
- Reconcile internal orders against provider transactions, bank settlements, refunds, and crypto confirmations on a schedule; alert on unmatched or inconsistent records.
- Use least-privilege provider keys, encrypted secrets, rotation, strict access controls, request timeouts, rate limits, redacted logs, and separate sandbox/production credentials.
- Maintain an append-only audit trail for payment state changes, manual approvals, refunds, grants, and entitlement changes. Do not log full card data, credentials, API secrets, or unnecessary personal/academic data.
- Require CSRF protection for browser mutations and authenticated, permission-checked finance/admin APIs. Use a separate, signature-verified webhook route that does not rely on browser sessions.
- Apply fraud controls, duplicate-payment detection, amount/currency matching, refund controls, and manual review thresholds.

## Admin and finance screens

1. **Plans and prices:** create/version plans, currencies, billing periods, features, quotas, trials, and tax/display rules.
2. **Payment providers:** enable/disable adapters, configure allowed currencies/methods, show redacted connection status, and test sandbox connections.
3. **Orders and transactions:** search by order/customer/provider reference; show status, amount, currency, timestamps, and reconciliation state.
4. **Receipt review queue:** approve/reject uploaded proof only after checking actual bank/processor settlement; require a reason and preserve reviewer identity.
5. **Subscriptions and entitlements:** inspect current plan, renewal date, usage, grace period, cancel/suspend/reactivate actions, and a preview of access changes.
6. **Refunds and disputes:** permission-restricted workflows with reason, limits, provider result, and audit log.
7. **Reconciliation and alerts:** unmatched transactions, stale pending orders, failed webhooks, suspicious duplicate events, settlement mismatches, and failed renewals.
8. **Free access and manual adjustments:** separate explicit grants from payment records; require reason, scope, duration, and audit trail.
9. **Customer billing portal:** invoices/receipts, payment history, renewal/cancellation controls, and support contact.

## Permissions

- Platform Owner: configure global provider policies and approve high-risk changes.
- Finance/Billing: inspect payments, reconcile settlements, and perform permitted refunds; no unrelated student academic data.
- Institution Admin: view/manage only the institution's subscription and billing records within delegated scope.
- Manager: view permitted usage and billing status; no provider secrets or refunds unless explicitly granted.
- Customer: access only their own orders, invoices, payment status, and subscription controls.
- Webhook worker: machine identity limited to processing validated provider events; no interactive admin access.

Every action must be authorized server-side, scoped to the tenant, and audited. The UI must not grant permissions merely because a role option was selected.

## Production acceptance gates

- Unit tests for every payment state transition, invalid transition, amount/currency mismatch, duplicate event, replay, and idempotent entitlement activation.
- Contract tests for webhook signature validation and provider API error handling; live sandbox tests for each configured provider.
- End-to-end tests: pending order -> verified payment -> active plan -> allowed tool succeeds; unpaid/failed/expired/refunded payment -> paid-only action is denied as policy requires.
- Tests proving customer A cannot read or alter customer B's orders/subscription and one tenant cannot access another tenant's billing or entitlements.
- Tests for manual receipt approval, rejection, reviewer audit, refund, chargeback, renewal failure, grace period, cancellation, and recovery.
- Bank reconciliation and crypto tests cover duplicate transaction references, wrong network/asset/destination, insufficient confirmations, underpayment, overpayment, and delayed settlement.
- Confirm local legal, tax, consumer-protection, privacy, AML/KYC, and crypto obligations with qualified advisers and the selected providers before launch.
- Keep production credentials out of source control. No real bank credentials, card data, withdrawal keys, or API secrets in logs or screenshots.
- Run database migration, backup/restore, incident response, monitoring, and alert tests before production launch.

Until these gates pass, payment providers and automatic activation must be marked as planned or sandbox-only; do not describe the platform as accepting real payments.
