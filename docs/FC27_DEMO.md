# East Coasters FC 27 One-Shot Ingestion & Analytics Demo

- **Status**: Current
- **Audience**: Developers, Operators, Reviewers
- **Last verified commit**: `6dc09452371570a1f00c77c7f1b930059a8ab938`
- **Source of truth**: `src/fc27_demo.py`, `src/fc27_demo_api.py`, `scripts/fc27_demo_fixture_server.py`, `scripts/fc27_demo_ingest.py`, `tests/test_fc27_demo.py`
- **Supersedes**: `NONE`

This guide documents the local, loopback-only East Coasters FC 27 one-shot ingestion and analytics demonstration vertical slice (`club_id="330323"`, `club_name="East Coasters"`, `platform="common-gen5"`, `game_version="FC27"`).

---

## 1. Demo Purpose

The FC 27 Ingestion and Analytics Demo proves the complete repository-native vertical data path end to end without contacting external servers, generating coaching recommendations, or enabling player dispatch:

1. Serve deterministic synthetic FC 27 Pro Clubs HTTP payloads over loopback (`127.0.0.1`).
2. Execute a single-request bounded HTTP transport call (`max_attempts=1`, `max_retries=0`, `timeout<=10.0s`, `max_response_bytes=524288`).
3. Validate endpoint query contracts (`src/fc27_clubs_api_contracts.py`) and strict Pydantic response schemas.
4. Scan for prohibited sensitive fields (`email`, `password`, `token`, `cookie`, `account_id`, `persona_id`, `nucleus_id`, `gamer_tag`, `psn_id`, `xbox_id`) and fail closed on detection.
5. Normalize club, match, and player records, decode 23-bit match event bitmaps (`ev`), and pseudonymize player references to `plr_ecf_<12-hex>` with collision detection (`FC27PseudonymCollisionDetector`).
6. Zero and discard raw HTTP response buffers (`raw_response_retained=False`).
7. Apply bounded normalized-object caching (`src/fc27_api_cache.py`), token-bucket rate limiting, per-run request budgets, and 3-state circuit breaker protection (`src/fc27_api_rate_limit.py`).
8. Optionally persist normalized records to PostgreSQL 16 (`src/fc27_repository.py`) only when both `--persist` and `FC27_PERSISTENCE_ENABLED=true` are supplied.
9. Run deterministic Search context assembly (`build_fc27_search_context`), Scout evidence/hypothesis synthesis (`build_fc27_scout_report`), and Analyst observational finding synthesis (`build_fc27_analyst_report`).
10. Expose browser-readable JSON and a self-contained HTML inspection view on `127.0.0.1` when `FC27_DEMO_MODE=true`.

---

## 2. Fixture-Only Default

- The default and mandatory demo data source is `FC27_DEMO_SOURCE=fixture` (`--source fixture`).
- Every fixture run starts or connects to a loopback-only synthetic HTTP server bound strictly to `127.0.0.1` (`scripts/fc27_demo_fixture_server.py`).
- No non-loopback network connection or DNS lookup is performed during fixture operation (`NETWORK_REQUESTS_EXECUTED=LOOPBACK_ONLY`, `LIVE_EA_REQUESTS_EXECUTED=0`, `EA_DNS_RESOLUTIONS_EXECUTED=0`).

---

## 3. Synthetic-Data Classification

- All fixture payloads and derived artifacts carry `source_classification="DEMO_FIXTURE_SYNTHETIC"` and `authority_status="SYNTHETIC_FIXTURE_ONLY"`.
- Synthetic player identifiers (`fixture_player_001` through `fixture_player_004`) are deterministically pseudonymized into `plr_ecf_<12-hex>` tokens before entering normalized storage, cache, or analytics.
- Synthetic fixture data is strictly observational (`OBSERVATIONAL_ONLY`, `NOT_A_RECOMMENDATION`) and can never be treated as verified live EA evidence or human Commissioner approval.

---

## 4. Architecture

```text
[Synthetic Loopback Fixture Server (127.0.0.1, scripts/fc27_demo_fixture_server.py)]
   │  (HTTP GET /api/fc/clubs/info | /api/fc/clubs/overallStats | /api/fc/clubs/matches)
   ▼
[FC27DemoLoopbackTransport (src/fc27_demo.py)]
   │  (127.0.0.1 only, max 512 KiB, 0 redirects, 0 cookies, 0 retries, raw buffer zeroed)
   ▼
[Endpoint Contract & Strict Schema Validation (src/fc27_clubs_api_contracts.py)]
   │  (Sensitive-field scan -> Normalization -> 23-bit ev decode -> plr_ecf_<12-hex>)
   ▼
[FC27BoundedNormalizedCache + FC27ProviderTokenBucketLimiter + FC27RequestBudget + Circuit Breaker]
   │  (Cache stores normalized objects only; max_attempts=1; circuit trips on 401/403/drift)
   ▼
[Optional PostgreSQL 16 Persistence (src/fc27_repository.py, migrations/0012_fc27_performance_and_quarantine.sql)]
   │  (Requires BOTH --persist AND FC27_PERSISTENCE_ENABLED=true; skipped by default)
   ▼
[Deterministic Analytics Pipeline (src/fc27_search_scout.py -> src/fc27_analyst.py)]
   │  (Search Context -> Scout Evidence Report -> Analyst Synthesis Report)
   ▼
[Minimal Local Demo API (src/fc27_demo_api.py, gated by FC27_DEMO_MODE=true on 127.0.0.1)]
   └──► GET /demo | GET /demo/status | GET /demo/matches | GET /demo/analysis | POST /demo/ingest-fixture
```

---

## 5. Startup Commands

### Run One-Shot Fixture CLI (No Server Setup Required)
`scripts/fc27_demo_ingest.py` automatically launches an ephemeral loopback fixture server on `127.0.0.1:0`, executes a bounded one-shot ingestion and analytics pass, prints metadata-only `KEY=VALUE` lines, and shuts down the fixture server:

```powershell
.\.venv\Scripts\python.exe scripts/fc27_demo_ingest.py --source fixture --operation full-demo
```

### Start the Local Demo API & Browser View (`127.0.0.1:8097`)
```powershell
$env:FC27_DEMO_MODE = "true"
.\.venv\Scripts\uvicorn.exe src.fc27_demo_api:create_demo_app --factory --host 127.0.0.1 --port 8097
```

---

## 6. CLI Commands

### Standalone Fixture Server (`scripts/fc27_demo_fixture_server.py`)
```powershell
# Start the loopback fixture server on 127.0.0.1 with an ephemeral port
.\.venv\Scripts\python.exe scripts/fc27_demo_fixture_server.py --host 127.0.0.1 --port 0 --scenario valid
```

### One-Shot Ingestion CLI (`scripts/fc27_demo_ingest.py`)
```powershell
# Full fixture demo (recent matches + Search + Scout + Analyst)
.\.venv\Scripts\python.exe scripts/fc27_demo_ingest.py --source fixture --operation full-demo

# Club identity validation only
.\.venv\Scripts\python.exe scripts/fc27_demo_ingest.py --source fixture --operation club-identity

# Recent matches validation and normalization
.\.venv\Scripts\python.exe scripts/fc27_demo_ingest.py --source fixture --operation recent-matches

# Empty match history scenario (INSUFFICIENT_DATA / ZERO_COMPLETED_MATCHES)
.\.venv\Scripts\python.exe scripts/fc27_demo_ingest.py --source fixture --operation full-demo --scenario empty_history

# Duplicate match payload scenario (idempotent deduplication)
.\.venv\Scripts\python.exe scripts/fc27_demo_ingest.py --source fixture --operation full-demo --scenario duplicates
```

---

## 7. API Endpoints

All Demo API routes in `src/fc27_demo_api.py` require `FC27_DEMO_MODE=true` (returning HTTP `403` when `FC27_DEMO_MODE=false`) and bind strictly to `127.0.0.1`:

| Method & Route | Content-Type | Description |
|---|---|---|
| `GET /demo` | `text/html; charset=utf-8` | Self-contained local HTML view with `"Synthetic Fixture Data"` and `"Recommendations Disabled"` banners, club identity, match table, Search/Scout/Analyst summaries, and zero external JS/CSS/CDN assets |
| `GET /demo/status` | `application/json` | Returns demo mode status, East Coasters identity (`330323`, `common-gen5`, `FC27`), formula/policy versions, circuit state, cache entry count, and safety flags (`recommendations_enabled=false`, `dispatch_enabled=false`, `live_ea_requests_executed=0`) |
| `GET /demo/matches` | `application/json` | Returns normalized match summaries (`match_id`, `played_at`, `match_type`, `result`, `goals_for`, `goals_against`, `opponent_club_id`, `player_count`) with zero raw payloads or aliases |
| `GET /demo/analysis` | `application/json` | Returns deterministic `search_context`, `scout_report`, `analyst_report`, `search_hash`, `scout_hash`, `analyst_hash`, `confidence_tier`, `findings`, `counter_evidence`, `confounders`, and `data_gaps` |
| `POST /demo/ingest-fixture` | `application/json` | Triggers one bounded loopback fixture ingestion (`scenario` in `valid`, `empty_history`, `duplicates`) and refreshes in-memory demo artifacts |

---

## 8. Expected Output

Running `scripts/fc27_demo_ingest.py --source fixture --operation full-demo` exits with code `0` and emits deterministic `KEY=VALUE` lines:

```text
DEMO_MODE=YES
SOURCE_MODE=DEMO_FIXTURE_LOOPBACK
OPERATION=full-demo
CLUB_IDENTITY_DECISION=ACCEPTED_PROVISIONAL (330323/East Coasters/common-gen5/FC27)
VALIDATION_STATUS=PASS
REQUEST_COUNT=1
CACHE_HITS=0
CACHE_MISSES=1
RATE_LIMIT_DECISION=ALLOWED (1/1 used)
REQUEST_BUDGET_DECISION=USED_1_OF_1
CIRCUIT_STATE=CLOSED
NORMALIZED_CLUB_IDENTITY_COUNT=1
NORMALIZED_CLUB_STATS_COUNT=0
NORMALIZED_MATCH_COUNT=6
NORMALIZED_PLAYER_PERFORMANCE_COUNT=24
DISTINCT_PSEUDONYMOUS_PLAYERS=4
ACCEPTED_COUNT=6
DUPLICATE_COUNT=0
CONFLICT_COUNT=0
PERSISTENCE_STATE=DISABLED
SEARCH_HASH=7532e5a9f61344b00e6150d93d405586f1090e9f67903f8534ad01ff4567c4d3
SCOUT_HASH=e724b7362c97614dd702430d4ff32b07a2bd0ecf6725d9f861e935998cb381b8
ANALYST_HASH=318e2f79a67a7f4baa82095982ff08107db7250cdd2adff67f5238e5c4f4f6ed
EXECUTION_HASH=68e41477d717774190d3691289de3e3c323fca4cf443fc73ee149ba07165a13b
CONFIDENCE=LOW_CONFIDENCE
FINDING_COUNT=20
DATA_GAP_COUNT=4
RAW_RESPONSE_RETAINED=NO
RECOMMENDATIONS_ENABLED=NO
DISPATCH_ENABLED=NO
LIVE_PROBE_EXECUTED=NO
```

---

## 9. Persistence Gate

PostgreSQL persistence is **disabled by default** in the demo:

1. Without `--persist`, the demo runs purely in memory (`PERSISTENCE_ENABLED=NO`, `PERSISTED_MATCHES=0`) and does not connect to PostgreSQL.
2. Passing `--persist` while `FC27_PERSISTENCE_ENABLED=false` fails closed with `REASON_CODE=PERSISTENCE_DISABLED` (exit code `2`) before opening any database connection.
3. Setting `FC27_PERSISTENCE_ENABLED=true` without passing `--persist` keeps persistence disabled (`PERSISTENCE_ENABLED=NO`).
4. Only when **both** `--persist` and `FC27_PERSISTENCE_ENABLED=true` (plus a valid PostgreSQL 16 connection URI) are provided does `run_fc27_demo_ingestion` persist normalized club, batch, match, and player appearance rows to `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, and `fc27_player_match_stats` (`migrations/0012_fc27_performance_and_quarantine.sql`) and verify read-back via `PostgresFC27AnalyticsReader`.

---

## 10. Optional Live-Probe Boundary

`src/fc27_demo.py` defines `FC27LiveProbeConfig` (`validate_live_probe_gates`) as a fail-closed boundary for future authorized provider probing:

- Requires `--source live`, `--live-confirm`, `FC27_PROVIDER_ENABLED=true`, `FC27_STANDARD_HTTP_TRANSPORT_ENABLED=true`, `FC27_LIVE_INGESTION_ENABLED=true`, `FC27_PROVIDER_DRY_RUN=false`, a non-empty `FC27_PROVIDER_AUTH_REFERENCE`, and an HTTPS allowlisted provider host.
- Enforces `max_requests=1`, `max_retries=0`, `allow_redirects=False`, `allow_cookies=False`, and `allow_browser_impersonation=False`.
- Even when all configuration gates are validated in unit tests, `execute_live_probe()` raises `FC27ProviderAuthorizationError("LIVE_PROBE_EXECUTION_DISABLED_IN_THIS_PHASE")`.

---

## 11. Live Probe Not Executed Statement

- **`LIVE_PROBE_EXECUTED=NO`**: The optional live probe was **not** executed during this phase.
- **`LIVE_EA_REQUESTS_EXECUTED=0`**: Zero HTTP requests were sent to EA or any external host.
- **`EA_DNS_RESOLUTIONS_EXECUTED=0`**: Zero DNS resolutions were performed for `ea.com`, `proclubs.ea.com`, or any external domain.

---

## 12. Privacy Behavior

- **Pseudonymization**: All player identifiers are converted to `plr_ecf_<12-hex>` via SHA-256 (`pseudonymize_player_key`) and checked against `FC27PseudonymCollisionDetector`.
- **Alias Suppression**: `display_alias` is always `None` (`include_aliases=False`).
- **Sensitive-Field Rejection**: Payloads containing `gamer_tag`, `account_id`, `persona_id`, `nucleus_id`, `email`, `cookie`, `token`, `password`, `psn_id`, or `xbox_id` fail closed with `SENSITIVE_FIELD_DETECTED` before normalization or caching.
- **No Raw Response Retention**: Raw response bytes are zeroed in a `finally:` block immediately after parsing (`RAW_RESPONSE_RETAINED=NO`).

---

## 13. Cache and Rate Controls

- **Bounded Normalized Cache (`FC27BoundedNormalizedCache`)**: Stores only validated, normalized, pseudonymized objects (`max_entries=32`, `max_value_bytes=262144` in demo adapter). Raw HTTP payloads and failed responses are never cached.
- **Token-Bucket Rate Limiter (`FC27ProviderTokenBucketLimiter`)**: Enforces conservative request pacing (`max_queue_depth=0`).
- **Request Budget (`FC27RequestBudget`)**: Hard-caps each one-shot CLI or API invocation to `max_attempts=1` (`FC27_DEMO_MAX_REQUESTS=1`) and `max_retries=0` (`FC27_DEMO_MAX_RETRIES=0`).

---

## 14. Circuit Behavior

`FC27AdapterCircuitBreaker` protects the demo transport pipeline:
- Starts `CLOSED` only for the approved loopback fixture declaration (`127.0.0.1`).
- Trips immediately to `OPEN` on HTTP `401`, HTTP `403`, HTML challenge/CAPTCHA responses, redirect responses (`301`/`302`), timeout, oversized response (`> 524,288` bytes), schema validation failure, or sensitive-field detection.
- While `OPEN`, subsequent calls fail closed immediately with `CIRCUIT_OPEN` (`request_count=0`) without contacting the socket.

---

## 15. Known Limitations

1. **Synthetic Sample Size (`5` matches)**: The default `valid` fixture scenario provides `5` completed matches (`CONFIDENCE_TIER=EXPLORATORY`, `preliminary_only=True`). Coaching package readiness requires `>= 10` completed matches (`PROVISIONAL_TRENDS` / `HIGH_SAMPLE_READINESS`) and remains disabled (`FC27_RECOMMENDATIONS_ENABLED=false`).
2. **Deferred Downstream Roles**: `Coach`, `Tactics`, and `Director` synthesis phases remain paused while observational `Search`, `Scout`, and `Analyst` outputs are demonstrated.
3. **No Live EA Provider**: Direct requests to `proclubs.ea.com` remain blocked pending written EA authorization or an approved partner API.

---

## 16. Troubleshooting

| Symptom / Reason Code | Cause | Resolution |
|---|---|---|
| `DEMO_MODE_DISABLED` / HTTP `403` on `/demo/*` | `FC27_DEMO_MODE` is unset or `false` | Set `$env:FC27_DEMO_MODE = "true"` before starting the local Demo API |
| `DEMO_BIND_HOST_INVALID` | `FC27_DEMO_BIND_HOST` set to `0.0.0.0` or non-loopback IP | Set `FC27_DEMO_BIND_HOST=127.0.0.1` |
| `PERSISTENCE_DISABLED` (exit code `2`) | `--persist` passed without `FC27_PERSISTENCE_ENABLED=true` | Set `$env:FC27_PERSISTENCE_ENABLED = "true"` and configure PostgreSQL 16 URI |
| `LIVE_PROBE_DISABLED` (exit code `2`) | `--source live` invoked without live gates | Use `--source fixture` (live probe execution is disabled in this phase) |

---

## 17. Teardown

1. Stop any running local Demo API process (`Ctrl+C` in the terminal running `uvicorn`).
2. Clear temporary environment overrides in your PowerShell session:
   ```powershell
   Remove-Item Env:\FC27_DEMO_MODE -ErrorAction SilentlyContinue
   Remove-Item Env:\FC27_PERSISTENCE_ENABLED -ErrorAction SilentlyContinue
   ```
3. If a disposable PostgreSQL 16 test container was started, remove it:
   ```powershell
   docker rm -f -v fc27-test-postgres
   ```

---

## 18. Screenshots Checklist

When capturing local demo artifacts for review:
- Verify the `"Synthetic Fixture Data"` and `"Recommendations Disabled"` banners are visible at the top of `http://127.0.0.1:8097/demo`.
- Confirm club identity displays `East Coasters` (`330323`, `common-gen5`, `FC27`).
- Confirm all player identifiers appear strictly as `plr_ecf_<12-hex>` pseudonyms with zero gamer tags or personal names.
- Do not commit screenshots or browser exports into tracked repository paths (keep any local captures inside git-ignored `.local-artifacts/`).

---

## 19. Next Steps

With the end-to-end synthetic ingestion, normalization, pseudonymization, optional PostgreSQL 16 persistence, `Search -> Scout -> Analyst` pipeline, and local Demo API verified, the subsequent roadmap phases are:
1. **Coach Hypothesis Generation**
2. **Tactics Candidate Construction**
3. **Director Conflict Resolution**
4. **Recommendation Review**
5. **Separate Dispatch-Activation Review**
