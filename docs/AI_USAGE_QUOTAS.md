# AI generation quotas

AI question generation is metered per institution with atomic UTC windows:

- Default limits: `AI_GENERATION_DAILY_LIMIT=100` and `AI_GENERATION_PER_MINUTE_LIMIT=10`.
- A plan may override these with integer entitlements `ai_generation_daily_limit` and `ai_generation_per_minute_limit`; zero intentionally disables generation.
- The usage transaction serializes concurrent requests with SQLite `BEGIN IMMEDIATE`, so parallel requests cannot race past a tenant's cap.
- The quota is consumed immediately before the provider call. Failed provider calls still consume a slot to protect against retry-driven spend.
- Minute counters older than 48 hours and daily counters older than 30 days are pruned during quota use.
- Exceeded quotas return HTTP 429 and `Retry-After`; successful responses include daily/minute limit and remaining headers.

This is request-count metering, not token/currency accounting. It does not replace provider-side spend limits, billing alerts, circuit breakers, or live cost reconciliation. Development calls without an institution ID remain local/demo-only; production entitlement enforcement requires the institution ID.
