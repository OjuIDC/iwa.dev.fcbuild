# FC27 all-match-types polling

The current rotation requests one type per 12-hour cycle. With three supported types, discovery can take about 36 hours for a type that was just missed. The opt-in all-types mode requests `leagueMatch`, `playoffMatch`, then `friendlyMatch` in one cycle. Each type is persisted separately; a timeout on one type does not roll back another type's committed results. Authentication, authorization, HTTP 429, and shared contract failures stop the remaining requests. Retries remain zero.

The source and example environment keep existing production defaults: a 12-hour interval, one request per cycle, two requests in a rolling 24-hour window, and all-types mode disabled. Every provider attempt is reserved in the PostgreSQL request ledger before transport, including attempts that fail. Scheduled and worker-triggered operations use the same ledger and single-flight lock. The ledger stores timestamps and sanitized outcome categories, never provider response content.

## Proposed activation profile

This profile is documentation only. It is not applied by this change and requires a separate production decision:

```dotenv
FC27_COMMUNITY_POLL_ALL_MATCH_TYPES=true
FC27_COMMUNITY_POLL_MATCH_TYPES=leagueMatch,playoffMatch,friendlyMatch
FC27_COMMUNITY_POLL_INTERVAL_SECONDS=14400
FC27_COMMUNITY_POLL_MIN_SECONDS=14400
FC27_COMMUNITY_POLL_MAX_SECONDS=14400
FC27_COMMUNITY_POLL_MAX_REQUESTS=3
FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_24_HOURS=18
FC27_COMMUNITY_POLL_INTER_REQUEST_DELAY_SECONDS=12
FC27_COMMUNITY_POLL_MAX_RETRIES=0
```

This would change the cadence to four hours and the ceiling from two requests per day to 18. The ceiling is shared across cycles and failed attempts count toward it. Invalid type lists, delays outside 10–60 seconds, retries, and incompatible cycle or interval limits fail closed.

## Operator-triggered polling

This change does not add `/pollnow`. If introduced later, the Discord bot should authenticate an operator and enqueue a bounded signal only; the community worker must remain the only provider caller and must consume the same single-flight lock and durable request budget.

## Activation and rollback

Activation requires a separate reviewed change to production settings and the Migration 0021 / Grants Policy 22 database rollout. Validate the deployed worker and database first, then change cadence and limits together in a controlled window. Roll back by disabling all-types mode and restoring the 12-hour / one-request / two-per-day settings; do not delete the request ledger. No production schedule, credentials, or containers are changed by this implementation.
