# East Coasters FC 27 Community Polling Worker (`fc27-community-worker`)

- **Status**: Current
- **Audience**: Platform Operators, Developers, Security Reviewers
- **Last verified commit**: `bbb28b44774a89dbb40c252f53c500e51d7a0acc`
- **Source of truth**: `src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`, `src/settings.py`, `src/fc27_discord_bot.py`, `Dockerfile.community-worker`, `docker-compose.community-worker.yml`, `tests/test_fc27_polling_worker.py`

> **Default Runtime Posture**: **DISABLED BY DEFAULT** (`FC27_COMMUNITY_POLLING_ENABLED=false`, `IMPLEMENTED_AND_TESTED_DISABLED_BY_DEFAULT`, schedule `ONCE_EVERY_12_HOURS` / `43200s`, max `2` requests per 24 hours, recommendations `DISABLED`, dispatch `DISABLED`)
>
> Continuous polling never starts during `pytest`, module imports, or default `docker compose up`. Live polling requires explicit operator activation across four independent fail-closed gates (`FC27_COMMUNITY_POLLING_ENABLED=true`, `FC27_COMMUNITY_PROVIDER_ENABLED=true`, `FC27_PERSISTENCE_ENABLED=true`, and `--confirm-live-polling`). See also [`FC27_DAILY_POLLING_OPERATIONS.md`](FC27_DAILY_POLLING_OPERATIONS.md).

---

## 1. Architecture & Supported Operational Path

For **East Coasters (`club_id="330323"`, `club_name="East Coasters"`, `platform="common-gen5"`, `game_version="FC27"`, `approved_match_types=("leagueMatch", "playoffMatch", "friendlyMatch")`, `max_results=10`)**, the polling worker extends the validated community-provider path into a bounded, single-flight 12-hour background worker with deterministic multi-match-type rotation (`leagueMatch -> playoffMatch -> friendlyMatch -> leagueMatch`):

```text
fc-clubs-api-community-v1 (LIVE_EA_CLUBS_COMMUNITY_CLIENT)
  -> FC27PollingPolicy (POLL_INTERVAL_SECONDS=43200, ONCE_EVERY_12_HOURS, max 2 req/24h across all match types) + fail-closed activation & HMAC key gates
  -> FC27SingleFlightLeaseContext (in-process lock + PostgreSQL pg_try_advisory_lock)
  -> FC27BoundedNormalizedCache (TTL = 43200s; normalized objects only, keyed by match_type)
  -> FC27AdapterCircuitBreaker + FC27ProviderTokenBucketLimiter + FC27RequestBudget (max_requests=1, max_retries=0)
  -> FC27CommunityProviderAdapter (clubs/matches, active match_type in rotation, maxResultCount=10, raw_response_retained=False)
  -> Repository validation, sensitive-field rejection, HMAC-SHA256 alias fingerprinting & plr_ecf_<12-hex> pseudonymization
  -> Atomic PostgreSQL 16 persistence via fc27_poll_writer BEFORE analytics (matches + fc27_player_alias_fingerprints + fc27_discord_jobs post_new_results)
  -> Conditional read-only advisory pipeline ONLY when inserted_match_count > 0 (Search -> Scout -> Analyst -> Coach -> Tactics -> Director)
  -> Read-only Discord status card (/fc27 status in src/fc27_discord_bot.py)
```

---

## 2. Fail-Closed Activation Gates & Configuration

All polling settings in `src/settings.py` and `config.example.env` fail closed on missing, invalid, or out-of-bounds values:

| Environment Variable | Default | Allowed / Enforced Range | Purpose |
|---|---|---|---|
| `FC27_COMMUNITY_POLLING_ENABLED` | `false` | `false` / `true` | Master gate for polling cycle execution |
| `FC27_COMMUNITY_PROVIDER_ENABLED` | `false` | `false` / `true` | Required community provider gate (`fc-clubs-api-community-v1`) |
| `FC27_PERSISTENCE_ENABLED` | `false` | `false` / `true` | Required PostgreSQL persistence gate |
| `FC27_APPROVED_MATCH_TYPES` | `leagueMatch,playoffMatch,friendlyMatch` | Subset/order of `leagueMatch`, `playoffMatch`, `friendlyMatch` | Deterministic match-type rotation sequence |
| `FC27_POLL_WRITER_URI_FILE` | `/run/secrets/fc27_poll_writer_uri` | Secret file path | Least-privilege `fc27_poll_writer` PostgreSQL connection URI |
| `FC27_ALIAS_HMAC_KEY_FILE` | `/run/secrets/fc27_alias_hmac_key` | Secret file (`>= 32` bytes) | Required HMAC-SHA256 key for privacy-safe player alias fingerprinting |
| `FC27_COMMUNITY_POLL_INTERVAL_SECONDS` | `43200` | `43200` (strictly `43200`) | Fixed 12-hour inter-cycle interval (`ONCE_EVERY_12_HOURS`) |
| `FC27_COMMUNITY_POLL_MIN_SECONDS` | `43200` | `43200` (strictly `43200`) | Minimum inter-cycle sleep interval (12 hours) |
| `FC27_COMMUNITY_POLL_MAX_SECONDS` | `43200` | `43200` (strictly `43200`) | Maximum inter-cycle sleep interval (12 hours) |
| `FC27_COMMUNITY_POLL_MAX_REQUESTS` | `1` | `1` (strictly `1`) | Maximum provider HTTP requests per poll cycle |
| `FC27_COMMUNITY_POLL_MAX_RETRIES` | `0` | `0` (strictly `0`) | Automatic retry count (retries prohibited) |
| `FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY` | `true` | `true` (strictly `true`) | Run `Search -> Scout -> Analyst -> Coach -> Tactics -> Director` only when `inserted_match_count > 0` |
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

- **Least-Privilege Runtime Role (`fc27_app` / `fc27_poll_writer`)**:
  - Connects via `POSTGRES_URI` / `POSTGRES_URI_FILE` (`./secrets/fc27_app_uri`) or `FC27_POLL_WRITER_URI_FILE` (`./secrets/fc27_poll_writer_uri`, `fc27_poll_writer` in Grants Policy Version `16`) and validates `check_runtime_posture()` (rejecting superuser, `fc27_owner`, `CREATE`, `BYPASSRLS`, or `REPLICATION`).
  - `GuardedPollingConnection` enforces table-level isolation, raising `FC27PollingTableAccessDeniedError` (`psycopg.errors.InsufficientPrivilege`) if any query touches `checkpoints*`, `approval_*`, `fc27_decision_evidence`, `fc27_approval_inconsistencies`, or `dispatch*`.
- **Idempotency & Replay Conflict Quarantine**:
  - Identical `match_hash` rows are ignored (`duplicate_match_count`), returning `NO_NEW_MATCHES` when `inserted_match_count == 0`.
  - Changed `match_hash` for an existing `(club_id, match_id)` is recorded in `fc27_quarantine_records` (`REPLAY_CONFLICT`), rolls back the match batch, and trips the circuit breaker to `OPEN`.
- **Analytics Only on New Matches & Advisory Cache Invalidation**:
  - `run_analytics_from_persisted_community_records` (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`) executes **if and only if** `inserted_match_count > 0` (`NEW_MATCHES_PERSISTED`), invalidating any prior `FC27AdvisoryCache` entry (`reason="NEW_MATCHES_PERSISTED"`) and pre-warming the cache with the new six-report advisory bundle (`src/fc27_advisory_cache.py`, documented in [`docs/FC27_ADVISORY_CACHE.md`](FC27_ADVISORY_CACHE.md)).
  - When `inserted_match_count == 0` (`NO_NEW_MATCHES`), `analytics_executed` is `False` and any valid `FC27AdvisoryCache` entry for the current persisted batch key is preserved.
  - On `REPLAY_CONFLICT`, the worker immediately invalidates the advisory cache (`reason="REPLAY_CONFLICT"`).

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

## 6. Read-Only Discord `/fc27 status` Integration

`/fc27 status` (`src/fc27_discord_bot.py`) exposes read-only polling health and repository metadata (`polling_enabled`, `worker_running`, `last_attempt_at`, `last_success_at`, `last_new_match_at`, `last_status`, `consecutive_failures`, `circuit_state`, `next_eligible_at`, `total_matches_inserted`, `total_duplicates_ignored`, `total_replay_conflicts`, `continuity_status`, `database_available`, `persisted_match_count`, `latest_match_timestamp`, `manual_match_results_available`).

The Discord bot provides no command or button to trigger live polling (`DISCORD_PROVIDER_REQUESTS=0`).

---

## 7. C1 Activation Prerequisites: Alias-HMAC Contract and Queue Payload

### 7.1 Versioned alias-HMAC normalization

Player-alias fingerprinting is a purpose-specific contract, distinct from ordinary display-label and pseudonym normalization (which remains NFC-based). The authoritative version is `ALIAS_HMAC_NORMALIZATION_VERSION = "fc27-alias-hmac-nfkc-v1"` (`src/fc27_privacy.py`).

`compute_alias_hmac_fingerprint` performs, in order:

1. require a string input;
2. Unicode NFKC normalization;
3. rejection of prohibited control characters;
4. trimming leading and trailing whitespace;
5. collapse of internal whitespace runs to one ASCII space;
6. Unicode casefold;
7. enforcement of the bounded maximum length;
8. UTF-8 encoding;
9. `HMAC-SHA256` with the configured alias-HMAC key (minimum 32 bytes);
10. exactly 64 lowercase hexadecimal characters.

The transformation is explicitly versioned because fingerprints persisted under an earlier contract would otherwise become silently incomparable. The version is a stable, non-dynamically-derived constant and is safe to persist only as metadata when a version distinction is required. Normalized aliases, plaintext aliases, and key material are never logged or included in exceptions. The retained `fc27_player_alias_fingerprints` table was empty at remediation time, so no fingerprint data migration was required and no schema change was made.

### 7.2 Minimal versioned `post_new_results` payload

Each new match enqueues one job whose payload is the minimal, explicitly versioned reference below:

| Field | Value |
|---|---|
| `schema_version` | `fc27-post-new-results-v1` |
| `event_type` | `post_new_results` |
| `match_ref` | privacy-safe `mref_ecf_<12hex>` match reference |
| `club_id` | club scope |
| `platform` | platform scope |
| `game_version` | game-version scope |
| `match_type` | approved match type for the poll cycle |

Duplicated or mutable fields previously carried for consumer convenience (`export_id`, `played_at`, `match_hash`, `opponent_name`, `goals_for`, `goals_against`, `result`) were removed. A future consumer must load authoritative match details from PostgreSQL by `match_ref`; the scoping fields (`club_id`, `platform`, `game_version`) are retained so the reference cannot become ambiguous. Every payload is validated by the typed payload model and the authoritative sensitive-field scanner before insertion, and must remain within the 4096-byte bound.

### 7.3 Canonical complete-payload idempotency

For an existing `fc27_discord_jobs` row sharing the deterministic idempotency key, equivalence compares `job_type` and the complete canonical versioned payload using deterministic canonical JSON (sorted keys, compact separators). Key order is irrelevant; any missing field, additional field, changed value, changed type, or changed schema version is materially different.

A materially different payload raises `FC27JobIdempotencyConflictError` with a bounded safe category that contains neither payload nor sensitive identifiers. The complete poll transaction rolls back: the match, player-stat rows, alias fingerprints, export batch/checkpoint, and Discord job are all abandoned, and the existing queue row is left unchanged. Existing jobs are never updated, replaced, or deleted to force equivalence. The polling worker remains producer-only: it does not claim, consume, complete, fail, retry, or delete jobs.

### 7.4 Operational prerequisites (not satisfied by this document)

- **Credential rotation remains required operationally before activation** because credentials were exposed during a readiness diagnostic run. This document does not record, repeat, or identify any exposed value, and rotation is not performed by this change.
- Live polling remains disabled by default; the retained community worker and Discord bot remain stopped.
- The retained development database remains at migration `0014` with Grants Policy Version `16` until the activation-readiness gate is repeated.
- This change does not assert activation readiness; the C1 activation-readiness gate must be re-run and pass before any bounded live poll is separately authorized.

---

## 8. Live Polling Replay-Conflict Reconciliation (`fc27-match-equivalence-v1`)

See [`FC27_REPLAY_CONFLICT_RECONCILIATION.md`](FC27_REPLAY_CONFLICT_RECONCILIATION.md) for the complete specification of:

- versioned immutable-fact equivalence (`MATCH_EQUIVALENCE_CONTRACT_VERSION = "fc27-match-equivalence-v1"` in `src/fc27_privacy.py`);
- backward compatibility with retained `comm_eval_*` stored `match_hash` digests without rewriting retained PostgreSQL rows;
- sanitized `PENDING_REVIEW` `REPLAY_CONFLICT` quarantine persistence (`public.fc27_quarantine_records`) and exact `replay_conflict_count` accounting on materially different match facts;
- restart-durable circuit-breaker tripping when unresolved polling replay-conflict quarantine rows exist, audited `manual_reset(authorization_reference=...)`, and post-merge operator prerequisites before any separately authorized second canary.
