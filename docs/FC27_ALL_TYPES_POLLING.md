# FC27 all-match-types polling

The current rotation requests one match type per 12-hour cycle. With three supported match types, discovery can take up to 36 hours for a match type that was just missed. The opt-in all-types mode requests `leagueMatch`, `playoffMatch`, and `friendlyMatch` sequentially within a single scheduled cycle.

Each type is persisted separately in its own transactional boundary; a timeout or error on a later type does not roll back an earlier type's committed results. Authentication, authorization, HTTP 429, schema drift, and privacy rejections immediately stop remaining requests in the cycle. Retries remain strictly zero.

The source and environment defaults keep existing production posture unchanged: a 12-hour interval, one request per cycle, two requests in a rolling 24-hour window, and all-types mode disabled. Every provider attempt is durably reserved in the PostgreSQL request ledger before transport, including attempts that subsequently fail or time out. Scheduled and manual operations share the exact same ledger and single-flight lock. The ledger stores only sanitized timestamps and outcome categories—never provider response payloads, headers, or credentials.

## Migration and grants policy progression

- **Migration 0021**: Deployed with destination-aware delivery (`fc27_discord_destinations` and delivery attempts).
- **Grants Policy 21**: Historically reserved on an early branch of all-types polling before destination-aware delivery was merged. When PR #129 merged to `main`, it advanced the production grants policy directly to Grants Policy 22. Grants Policy 21 was thus bypassed.
- **Migration 0022**: Adds the durable request ledger table `public.fc27_provider_request_attempts` and two least-privilege `SECURITY DEFINER` procedures (`public.fc27_reserve_provider_request` and `public.fc27_finalize_provider_request`).
- **Grants Policy 23**: Grants `SELECT` on `public.fc27_provider_request_attempts` and `EXECUTE` on the two functions to `fc27_poll_writer`. Explicitly revokes `INSERT`, `UPDATE`, and `DELETE` on the table to enforce that all slot reservation and finalization logic occurs through the atomic procedures.

## Architecture and security invariants

1. **Fixed order execution**: Each cycle checks in deterministic sequence:
   1. `leagueMatch`
   2. `playoffMatch`
   3. `friendlyMatch`
2. **Deterministic delays**: Configurable delay between attempts (`inter_request_delay_seconds`, default 12 seconds, bounded to 10–60 seconds). Exactly two delays occur across the three requests.
3. **Zero retries**: `maximum_retries` is hardcoded to 0 and validated with `le=0`. Retries above 0 fail closed during configuration validation.
4. **Independent persistence**: Each match type persists in its own PostgreSQL transaction. If `leagueMatch` succeeds and `friendlyMatch` encounters a network timeout, `leagueMatch` remains committed.
5. **Immediate stop conditions**:
   - `PROVIDER_BLOCKED` (HTTP 401, 403, 429, challenge, CAPTCHA, provenance mismatch)
   - `SCHEMA_DRIFT` (contract violation, schema validation failure, payload size limit)
   - `IDENTITY_REJECTED` (club ID, platform, or game version mismatch)
   - `PRIVACY_REJECTED` (sensitive plain text, fingerprint collision)
6. **Continuation conditions**:
   - `TRANSIENT_FAILURE` (type-local timeout continues to next match type)
   - `NO_NEW_MATCHES` (empty result or duplicate matches continue to next match type)
7. **Single-flight advisory lease**: One operation-level lease (`fc27_single_flight_lease`) covers the entire multi-type cycle.
8. **Concurrency-safe final-slot reservation**: `public.fc27_reserve_provider_request` obtains a transaction-level advisory lock (`pg_advisory_xact_lock(hashtext('fc27_provider_budget'), hashtext(v_lock_key))`) before evaluating the rolling 24-hour count and cycle count, preventing concurrent workers from claiming the same final slot.
9. **Shared budget**: Scheduled cycles and manual polling commands consume from the exact same ledger, ensuring a unified rolling 24-hour budget.
10. **Restart-safe accounting**: Stale or unfinalized reservations (`outcome_category = 'RESERVED'`) continue to count toward the rolling daily limit to protect upstream rate limits across process crashes.

## Proposed activation profile

This profile is documentation only. It is not applied by this implementation and requires a separate production decision:

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

This would change the cadence to four hours (`14400` seconds) and the rolling ceiling from two requests per day to 18 (up to 3 requests per 4-hour cycle across 6 cycles per day).

## Future operator-triggered polling

This implementation does not implement `/pollnow`. If an enqueue-only `/pollnow` command is implemented in the future, the Discord bot should authenticate an operator using the `FC27 Operator` role and enqueue a bounded signal only; the community worker must remain the sole provider caller and must consume the exact same single-flight lease and durable request budget.

## Production activation and rollback plan

### Activation (future operator decision)
1. Apply Migration 0022 using repository migration tooling:
   `docker compose -p fc27-full-stack run --rm fc27-db-migrate`
2. Verify migration postconditions and Grants Policy 23.
3. Update environment variables in production `.env` to the activation profile above.
4. Recreate only `fc27-community-worker`:
   `docker compose -p fc27-full-stack -f docker-compose.yml -f docker-compose.community-worker.yml up -d --no-deps fc27-community-worker`
5. Verify initial cycle execution via health check:
   `python scripts/fc27_polling_worker.py --status`

### Rollback
1. Revert environment variables to default rotation mode:
   `FC27_COMMUNITY_POLL_ALL_MATCH_TYPES=false`
   `FC27_COMMUNITY_POLL_INTERVAL_SECONDS=43200`
   `FC27_COMMUNITY_POLL_MAX_REQUESTS=1`
   `FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_24_HOURS=2`
2. Recreate `fc27-community-worker`.
3. Do not drop or delete `public.fc27_provider_request_attempts`.
