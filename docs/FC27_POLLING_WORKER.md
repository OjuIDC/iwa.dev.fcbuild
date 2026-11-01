# East Coasters FC 27 Community Polling Worker (`fc27-community-worker`)

- **Status**: Current
- **Audience**: Platform Operators, Developers, Security Reviewers
- **Last verified commit**: `bbb28b44774a89dbb40c252f53c500e51d7a0acc`
- **Source of truth**: `src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`, `src/settings.py`, `src/fc27_demo_api.py`, `Dockerfile.community-worker`, `docker-compose.community-worker.yml`, `tests/test_fc27_polling_worker.py`

> **Default Runtime Posture**: **DISABLED BY DEFAULT** (`FC27_COMMUNITY_POLLING_ENABLED=false`, `IMPLEMENTED_AND_TESTED_DISABLED_BY_DEFAULT`, continuous polling `NOT_ACTIVATED`, recommendations `DISABLED`, dispatch `DISABLED`, Discord `NOT_IMPLEMENTED`)
>
> Continuous polling never starts during `pytest`, `uvicorn src.fc27_demo_api:create_demo_app`, module imports, or default `docker compose up`. Live polling requires explicit operator activation across four independent fail-closed gates (`FC27_COMMUNITY_POLLING_ENABLED=true`, `FC27_COMMUNITY_PROVIDER_ENABLED=true`, `FC27_PERSISTENCE_ENABLED=true`, and `--confirm-live-polling`).

---

## 1. Architecture & Supported Operational Path

For **East Coasters (`club_id="330323"`, `club_name="East Coasters"`, `platform="common-gen5"`, `game_version="FC27"`, `match_type="leagueMatch"`, `max_results=10`)**, the polling worker extends the validated community-provider path into a bounded, single-flight background worker:

```text
fc-clubs-api-community-v1 (LIVE_EA_CLUBS_COMMUNITY_CLIENT)
  -> FC27PollingPolicy + 4 fail-closed activation gates
  -> FC27SingleFlightLeaseContext (in-process lock + PostgreSQL pg_try_advisory_lock)
  -> FC27BoundedNormalizedCache (TTL = 300s; normalized objects only)
  -> FC27AdapterCircuitBreaker + FC27ProviderTokenBucketLimiter + FC27RequestBudget (max_requests=1, max_retries=0)
  -> FC27CommunityProviderAdapter (clubs/matches, leagueMatch, maxResultCount=10, raw_response_retained=False)
  -> Repository validation, sensitive-field rejection & plr_ecf_<12-hex> pseudonymization
  -> Transactional PostgreSQL 16 persistence via fc27_app (persist_polled_match_envelope)
  -> Conditional read-only analytics ONLY when inserted_match_count > 0 (Search -> Scout -> Analyst)
  -> Read-only dashboard status card (GET /demo/status, GET /demo)
```

---

## 2. Fail-Closed Activation Gates & Configuration

All polling settings in `src/settings.py` and `config.example.env` fail closed on missing, invalid, or out-of-bounds values:

| Environment Variable | Default | Allowed / Enforced Range | Purpose |
|---|---|---|---|
| `FC27_COMMUNITY_POLLING_ENABLED` | `false` | `false` / `true` | Master gate for polling cycle execution |
| `FC27_COMMUNITY_PROVIDER_ENABLED` | `false` | `false` / `true` | Required community provider gate (`fc-clubs-api-community-v1`) |
| `FC27_PERSISTENCE_ENABLED` | `false` | `false` / `true` | Required PostgreSQL persistence gate |
| `FC27_COMMUNITY_POLL_MIN_SECONDS` | `300` | `300..3600` (`>= 300`) | Minimum inter-cycle sleep interval (5 minutes) |
| `FC27_COMMUNITY_POLL_MAX_SECONDS` | `600` | `min_seconds..3600` (`<= 3600`) | Maximum inter-cycle sleep interval (10 minutes) |
| `FC27_COMMUNITY_POLL_MAX_REQUESTS` | `1` | `1` (strictly `1`) | Maximum provider HTTP requests per poll cycle |
| `FC27_COMMUNITY_POLL_MAX_RETRIES` | `0` | `0` (strictly `0`) | Automatic retry count (retries prohibited) |
| `FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY` | `true` | `true` (strictly `true`) | Run `Search -> Scout -> Analyst` only when `inserted_match_count > 0` |
| `FC27_COMMUNITY_POLL_SINGLE_FLIGHT` | `true` | `true` (strictly `true`) | Require both in-process and PostgreSQL advisory single-flight lease |
| `FC27_COMMUNITY_POLL_ONCE` | `false` | `false` / `true` | Execute at most one bounded poll cycle and exit |

---

## 3. Single-Flight Concurrency & Rate Controls

1. **Single-Flight Lease (`FC27SingleFlightLeaseContext`)**:
   - Acquires a non-blocking process-local `threading.Lock` first.
   - Acquires a non-blocking PostgreSQL session-level advisory lock (`SELECT pg_try_advisory_lock(%s)`) using the deterministic 64-bit signed key derived from `fc27_poll:fc-clubs-api-community-v1:common-gen5:330323:leagueMatch`.
   - If another worker or CLI process holds the lease, the cycle returns `SINGLE_FLIGHT_LOCKED` immediately with `requests_executed=0`.
   - The advisory lock is released in a `finally` block (`SELECT pg_advisory_unlock(%s)`) before any inter-cycle sleep.
2. **Cache, Rate Limit, Request Budget, and Circuit Breaker**:
   - **Cache (`FC27BoundedNormalizedCache`)**: Caches validated, pseudonymized `FC27NormalizedMatchesEnvelope` objects for `300` seconds. Cache hits return `CACHE_HIT_SKIPPED` with `requests_executed=0`.
   - **Rate Limiter (`FC27ProviderTokenBucketLimiter`)**: Enforces `1` token per `300.0` seconds (`max_queue_depth=0`).
   - **Request Budget (`FC27RequestBudget`)**: Enforces `max_attempts=1` and `max_retries=0`.
   - **Circuit Breaker (`FC27AdapterCircuitBreaker`)**: Trips immediately to `OPEN` on `HTTP_403`, `SCHEMA_FAILURE`, `IDENTITY_MISMATCH`, `SENSITIVE_FIELD_VIOLATION`, or `REPLAY_CONFLICT`, and after `3` consecutive transient timeouts (`TRANSIENT_TIMEOUT`).

---

## 4. Transactional Persistence, Least Privilege, & Conditional Analytics

- **Least-Privilege Runtime Role (`fc27_app`)**:
  - Connects via `POSTGRES_URI` / `POSTGRES_URI_FILE` (`./secrets/fc27_app_uri`) and validates `check_runtime_posture()` (rejecting superuser, `fc27_owner`, `CREATE`, `BYPASSRLS`, or `REPLICATION`).
  - `GuardedPollingConnection` enforces table-level isolation, raising `FC27PollingTableAccessDeniedError` (`psycopg.errors.InsufficientPrivilege`) if any query touches `checkpoints*`, `approval_*`, `fc27_decision_evidence`, `fc27_approval_inconsistencies`, or `dispatch*`.
- **Idempotency & Replay Conflict Quarantine**:
  - Identical `match_hash` rows are ignored (`duplicate_match_count`), returning `NO_NEW_MATCHES` when `inserted_match_count == 0`.
  - Changed `match_hash` for an existing `(club_id, match_id)` is recorded in `fc27_quarantine_records` (`REPLAY_CONFLICT`), rolls back the match batch, and trips the circuit breaker to `OPEN`.
- **Analytics Only on New Matches**:
  - `run_analytics_from_persisted_community_records` (`Search -> Scout -> Analyst`) executes **if and only if** `inserted_match_count > 0` (`NEW_MATCHES_PERSISTED`).
  - When `inserted_match_count == 0`, `analytics_executed` is `False`.

---

## 5. Operator CLI & Docker Compose Usage

### 5.1 Inspect Polling Status (Zero Network / Zero Provider Calls)
```powershell
.\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status
```

### 5.2 Execute One Bounded Poll Cycle (`--once`)
Requires all gates enabled and `--confirm-live-polling`:
```powershell
$env:FC27_COMMUNITY_POLLING_ENABLED = "true"
$env:FC27_COMMUNITY_PROVIDER_ENABLED = "true"
$env:FC27_PERSISTENCE_ENABLED = "true"
$env:FC27_ANALYTICS_ENABLED = "true"

.\.venv\Scripts\python.exe scripts/fc27_polling_worker.py `
  --once `
  --confirm-live-polling
```

### 5.3 Docker Compose Opt-In Profile (`fc27-polling`)
`docker-compose.community-worker.yml` defines `fc27-community-worker` under the opt-in `fc27-polling` profile (`read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, `cpus: 0.50`, `mem_limit: 256m`, `pids_limit: 64`, no host ports, no Docker socket):
```powershell
# Inspect container status without starting continuous polling:
docker compose -f docker-compose.yml -f docker-compose.community-worker.yml `
  --profile fc27-polling run --rm fc27-community-worker --status
```

---

## 6. Read-Only Dashboard Status Integration

`GET /demo/status` and `GET /demo` (`src/fc27_demo_api.py`) expose read-only polling health metadata (`polling_enabled`, `worker_running`, `last_attempt_at`, `last_success_at`, `last_new_match_at`, `last_status`, `consecutive_failures`, `circuit_state`, `next_eligible_at`, `total_matches_inserted`, `total_duplicates_ignored`, `total_replay_conflicts`). When `FC27_COMMUNITY_POLLING_ENABLED=false`, the dashboard displays:

`Polling Disabled (FC27_COMMUNITY_POLLING_ENABLED=false)`

The dashboard provides no button, form, or endpoint to trigger live polling.
