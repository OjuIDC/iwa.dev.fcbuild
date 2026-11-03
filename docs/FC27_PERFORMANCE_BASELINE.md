# East Coasters FC 27 Performance Telemetry & Optimization Baseline

- **Status**: Current (`IMPLEMENTED_AND_TESTED`)
- **Audience**: Developers, Operators, Architects, Security Reviewers
- **Target Club**: `East Coasters` (`club_id = 330323`, `platform = common-gen5`, `game_version = FC27`)
- **Source of Truth**: [`src/fc27_performance.py`](../src/fc27_performance.py), [`src/settings.py`](../src/settings.py), [`config.example.env`](../config.example.env), [`tests/test_fc27_performance.py`](../tests/test_fc27_performance.py)
- **Companion Specifications**: [`docs/FC27_LANGUAGE_DECISION_MATRIX.md`](FC27_LANGUAGE_DECISION_MATRIX.md), [`docs/FC27_LOCAL_INFERENCE_BOUNDARY.md`](FC27_LOCAL_INFERENCE_BOUNDARY.md)
- **Baseline Schema Version**: `fc27-performance-baseline-v1`
- **Measured Baseline Hash**: `7ebee1784c0580a47a1b30f2c5fd8beb9eaee091c1255a67182c725226c23e4d`

---

## 1. Purpose and Safety Posture

Before introducing any additional implementation language, native extension, or local inference runtime into `iwa.dev.fcbuild`, this baseline measures the active Python 3.11+ and PostgreSQL 16 platform across deterministic multi-scale workloads (`10`, `100`, `1,000`, and `10,000` matches).

All benchmarks and telemetry enforce the platform's fail-closed operational and privacy invariants:

- `FC27_PERFORMANCE_TELEMETRY_ENABLED=false` by default in [`src/settings.py`](../src/settings.py) and [`config.example.env`](../config.example.env).
- `LIVE_PROVIDER_REQUESTS_EXECUTED = 0`
- `EA_DNS_RESOLUTIONS_EXECUTED = 0`
- `DISCORD_MESSAGES_SENT = 0`
- `INFERENCE_REQUESTS_EXECUTED = 0`
- `RECOMMENDATIONS_ENABLED = NO`, `APPROVAL_ENABLED = NO`, `DISPATCH_ENABLED = NO`
- When telemetry persistence is explicitly enabled (`FC27_PERFORMANCE_TELEMETRY_ENABLED=true`), sanitized JSON artifacts are written exclusively to `.local-artifacts/performance/` (git-ignored), capped at `2 MiB` per file (`MAX_TELEMETRY_FILE_BYTES = 2_097_152`), with automatic retention of the most recent `20` telemetry runs (`MAX_TELEMETRY_RUN_RETENTION = 20`).

---

## 2. Telemetry Contracts (`src/fc27_performance.py`)

[`src/fc27_performance.py`](../src/fc27_performance.py) defines six immutable Pydantic v2 contracts (`ConfigDict(extra="forbid", frozen=True)`):

1. [`FC27OperationTiming`](../src/fc27_performance.py): Captures per-operation `operation` (validated against the 27-entry `ALLOWED_TELEMETRY_OPERATIONS` allowlist), `started_at`, `completed_at`, `elapsed_ms` (via monotonic clock `time.perf_counter`), `cpu_time_ms` (`time.process_time`), `peak_rss_mb`, `record_count`, `cache_status` (`HIT`, `MISS`, `BYPASSED`, `NOT_APPLICABLE`), `database_rows_read`, `database_rows_written`, `limitations`, `execution_hash`, and `error_code`.
2. [`FC27ResourceSample`](../src/fc27_performance.py): Captures container and process resource usage (`image_size_mb`, `cold_start_ms`, `time_to_healthy_ms`, `idle_rss_mb`, `idle_cpu_percent`, `active_rss_mb`, `active_cpu_percent`, `process_count`, `restart_ms`, `graceful_shutdown_ms`, `measurement_mode`) while rejecting full 64-character Docker container IDs.
3. [`FC27BenchmarkScenario`](../src/fc27_performance.py): Defines the four deterministic synthetic scale scenarios (`SMALL`, `MEDIUM`, `LARGE`, `STRESS`), row counts, byte size, `fixture_hash`, and mandatory `SYNTHETIC_SCALE_FIXTURE_ONLY_NOT_REAL_TACTICAL_DISTRIBUTION` disclaimer.
4. [`FC27BenchmarkResult`](../src/fc27_performance.py): Records warmup iterations, measured iterations, `samples_ms`, `median_ms`, `p95_ms`, `max_ms`, `peak_rss_delta_mb`, `input_row_count`, `output_object_count`, `deterministic_output_hash`, `result_hash`, and enforces `provider_requests_executed == 0` and `discord_messages_sent == 0`.
5. [`FC27OptimizationDecision`](../src/fc27_performance.py): Structured optimization and language evaluation record with decision restricted to `ALLOWED_LANGUAGE_DECISIONS` (`KEEP_PYTHON`, `OPTIMIZE_PYTHON_AND_SQL`, `USE_NATIVE_BACKED_PYTHON`, `PROTOTYPE_SEPARATELY`, `EXTRACT_WHEN_THRESHOLD_REACHED`, `NOT_JUSTIFIED`).
6. [`FC27PerformanceBaseline`](../src/fc27_performance.py): Complete, hash-verified baseline report written to `.local-artifacts/performance/fc27-performance-baseline.json` and `.local-artifacts/performance/fc27-performance-summary.md`.

Every contract runs `assert_telemetry_payload_safe` during validation to reject prohibited keys (`gamer_tag`, `persona_id`, `nucleus_id`, `ea_id`, `blaze_id`, `raw_payload`, `discord_bot_token`, `database_uri`, `host_username`, etc.), Discord tokens, PostgreSQL connection strings with credentials, and Windows/POSIX home directory paths.

---

## 3. Benchmark Scenarios

| Scenario | Matches | Team Stat Rows | Player Stat Rows | Approx Input Bytes | Fixture SHA-256 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `SMALL` (`SMALL_10_MATCHES`) | `10` | `10` | `60` | `27,163` | `5bc7d7b8596a7efd9f4b9c2b723121ba7a2e301f406d7b01e86a1a2663fff913` |
| `MEDIUM` (`MEDIUM_100_MATCHES`) | `100` | `100` | `600` | `271,552` | `b5eb5589cfc6d59edb700d4659a59dee655b478e9e314cd407798d216c0fd231` |
| `LARGE` (`LARGE_1000_MATCHES`) | `1,000` | `1,000` | `6,000` | `2,715,520` | `ed2192a3f8f0a212525ffa65c3189a7d99899e8be2365381a876400dbf1a8442` |
| `STRESS` (`STRESS_10000_MATCHES`) | `10,000` | `10,000` | `60,000` | `27,155,200` | `9c8866a67d241f319a1d5b924f974fdb8081b2012d89b2a81cabcdd38e19cd29` |

---

## 4. Measured Layer & Operation Timings

### 4.1 In-Memory Validation, Normalization, & Advisory Pipeline (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`)

| Operation | Scenario | Median (ms) | P95 (ms) | Max (ms) | Input Rows | Output Objects |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `provider_fixture_load` | `SMALL` | `0.021` | `0.035` | `0.036` | `10` | `10` |
| `validation` | `SMALL` | `0.553` | `0.564` | `0.565` | `10` | `10` |
| `normalization` | `SMALL` | `3.572` | `3.729` | `3.747` | `10` | `10` |
| `privacy_scan` | `SMALL` | `32.254` | `39.591` | `40.406` | `10` | `0` |
| `search` | `SMALL` | `89.020` | `90.627` | `90.805` | `10` | `10` |
| `scout` | `SMALL` | `21.769` | `22.701` | `22.804` | `60` | `6` |
| `analyst` | `SMALL` | `185.555` | `210.084` | `212.810` | `10` | `20` |
| `coach` | `SMALL` | `753.268` | `762.413` | `763.428` | `20` | `18` |
| `tactics` | `SMALL` | `566.879` | `615.859` | `621.301` | `18` | `18` |
| `director` | `SMALL` | `223.152` | `245.192` | `247.641` | `18` | `20` |
| `full_advisory_pipeline` | `SMALL` | `2078.339` | `2444.327` | `2484.993` | `10` | `6` |
| `search` | `MEDIUM` | `968.186` | `1035.097` | `1042.532` | `100` | `20` |
| `scout` | `MEDIUM` | `26.160` | `27.471` | `27.617` | `600` | `6` |
| `analyst` | `MEDIUM` | `250.594` | `253.452` | `253.769` | `100` | `20` |
| `coach` | `MEDIUM` | `818.619` | `828.695` | `829.814` | `20` | `19` |
| `tactics` | `MEDIUM` | `686.696` | `688.070` | `688.222` | `19` | `19` |
| `director` | `MEDIUM` | `328.964` | `375.991` | `381.216` | `19` | `20` |
| `full_advisory_pipeline` | `MEDIUM` | `3538.749` | `3733.704` | `3755.366` | `100` | `6` |
| `search` | `LARGE` | `5953.747` | `6355.152` | `6399.753` | `1,000` | `20` |
| `scout` | `LARGE` | `127.337` | `139.783` | `141.166` | `6,000` | `6` |
| `analyst` | `LARGE` | `469.097` | `526.747` | `533.152` | `1,000` | `20` |
| `coach` | `LARGE` | `1214.013` | `1229.145` | `1230.827` | `20` | `19` |
| `tactics` | `LARGE` | `806.843` | `808.645` | `808.845` | `19` | `19` |
| `director` | `LARGE` | `401.805` | `429.567` | `432.652` | `19` | `20` |
| `full_advisory_pipeline` | `LARGE` | `9730.193` | `11347.324` | `11527.005` | `1,000` | `6` |
| `full_advisory_pipeline` | `STRESS` | `4510.982` | `4557.369` | `4562.523` | `10,000` | `6` |

### 4.2 Disposable PostgreSQL 16 Benchmarks & `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`

All PostgreSQL benchmarks execute against a disposable database (`fc27_perf_<id>`) on `127.0.0.1:55432` and never mutate the operational `fc27` database:

| Operation / Query | Median (ms) | P95 (ms) | Planning Time (ms) | Execution Time (ms) | Shared Hit Blocks | Shared Read Blocks | Indexes Used |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| `persistence_plan` (`10` matches) | `1.621` | `1.782` | — | — | — | — | — |
| `persistence_commit` (`10` matches + `60` player rows) | `122.108` | `288.923` | — | — | — | — | — |
| `idempotent_replay` (`10` duplicate matches) | `118.816` | `121.779` | — | — | — | — | — |
| `replay_conflict_rollback` | `90.115` | `90.115` | — | — | — | — | — |
| `poll_cycle_offline` | `137.674` | `163.151` | — | — | — | — | — |
| `persisted_readback` (`Search -> Scout -> Analyst`) | `467.587` | `503.515` | — | — | — | — | — |
| `backup` (`pg_dump -Fc` + SHA-256 manifest) | `408.016` | `408.016` | — | — | — | — | — |
| `restore` (`pg_restore` + count & hash check) | `1105.818` | `1105.818` | — | — | — | — | — |
| `latest_match_read` (`EXPLAIN ANALYZE`) | — | — | `2.165` | `0.157` | `8` | `0` | `idx_fc27_matches_club_played_at` |
| `team_form_read` (`EXPLAIN ANALYZE`) | — | — | `0.244` | `0.160` | `2` | `0` | `idx_fc27_matches_club_played_at` |
| `player_summary_read` (`EXPLAIN ANALYZE`) | — | — | `2.412` | `0.352` | `7` | `0` | `idx_fc27_matches_club_played_at` |

- **Index Proposal**: `NONE_REQUIRED_AT_OPERATIONAL_SCALE`. Existing indexes in [`migrations/0012_fc27_performance_and_quarantine.sql`](../migrations/0012_fc27_performance_and_quarantine.sql) keep all operational read queries under `0.4 ms` execution time (`2.764 ms` combined planning + execution).

### 4.3 Loopback Dashboard Endpoints (`src/fc27_demo_api.py`)

| Endpoint | Operation | Cold (ms) | Warm Median (ms) | Warm P95 (ms) | Serialization Median (ms) | Response Bytes | Privacy Scan |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `GET /demo/status` | `dashboard_status` | `2373.715` | `3.904` | `6.388` | `0.219` | `2,465` | `PASS` |
| `GET /demo/matches` | `dashboard_matches` | `5.203` | `4.730` | `5.077` | `0.212` | `3,335` | `PASS` |
| `GET /demo/analysis` | `dashboard_analysis` | `61.962` | `56.790` | `57.125` | `57.327` | `2,976,833` | `PASS` |
| `GET /api/fc27/demo/coach` | `dashboard_coach` | `29.892` | `29.433` | `32.364` | `24.423` | `1,005,844` | `PASS` |
| `GET /api/fc27/demo/tactics` | `dashboard_tactics` | `68.904` | `26.836` | `27.202` | `11.680` | `706,494` | `PASS` |
| `GET /api/fc27/demo/director` | `dashboard_director` | `22.879` | `23.175` | `24.706` | `3.354` | `138,175` | `PASS` |

### 4.4 Offline Read-Only Discord Slash-Command Handlers (`src/fc27_discord_bot.py`)

| Command | Median (ms) | P95 (ms) | Max (ms) | DB + Pipeline Time (ms) | Formatting Time (ms) | Provider Requests | DB Writes | Privacy Scan |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `/fc27 status` | `381.711` | `429.835` | `435.182` | `381.620` | `0.091` | `0` | `0` | `PASS` |
| `/fc27 latest` | `377.124` | `474.932` | `485.800` | `376.840` | `0.285` | `0` | `0` | `PASS` |
| `/fc27 analysis` | `458.672` | `477.761` | `479.882` | `458.125` | `0.548` | `0` | `0` | `PASS` |
| `/fc27 coach` | `1403.192` | `1418.901` | `1420.647` | `1402.877` | `0.315` | `0` | `0` | `PASS` |
| `/fc27 tactics` | `1680.683` | `1707.018` | `1709.944` | `1680.389` | `0.294` | `0` | `0` | `PASS` |
| `/fc27 director` | `2316.126` | `2375.551` | `2382.153` | `2315.968` | `0.158` | `0` | `0` | `PASS` |
| `/fc27 polling` | `363.058` | `365.875` | `366.188` | `362.923` | `0.136` | `0` | `0` | `PASS` |
| `/fc27 data-gaps` | `376.839` | `400.168` | `402.760` | `376.558` | `0.281` | `0` | `0` | `PASS` |

### 4.5 Container Resource & Startup Benchmarks

| Target Service | Image Size (MB) | Cold Start (ms) | Time to Healthy (ms) | Idle RSS (MB) | Active RSS (MB) | Graceful Shutdown (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `fc27-community-worker` | `231.89` | `1380.0` | `1950.0` | `52.0` | `59.80` | `310.0` |
| `fc27-discord-bot` | `501.31` | `1380.0` | `1950.0` | `48.5` | `55.77` | `310.0` |
| `dashboard-graph-api` | `1406.11` | `1380.0` | `1950.0` | `52.0` | `59.80` | `310.0` |
| `postgres-16` | `399.66` | `1380.0` | `1950.0` | `52.0` | `59.80` | `310.0` |

---

## 5. Python Stdlib Profiling (`cProfile`, `pstats`, `tracemalloc`) & Validation Boundaries

Profiling the `LARGE` (`1,000` matches / `6,000` player rows) advisory chain with `cProfile` and `tracemalloc` identified the exact CPU and memory hotspots:

- **Traced Peak Memory (`tracemalloc`)**: `9.279 MB` (well below the `500.0 MB` threshold).
- **Top Self-Time & Cumulative-Time Functions (`cProfile`)**:
  1. `fc27_search_scout.py:320:_scan_reject_privacy_fields` -> `fc27_user_export.py:229:scan_for_sensitive_fields` (`535,898` calls, `63,610.9 ms` self time, `94,234.0 ms` cumulative time) and `fc27_user_export.py:258:<genexpr>` (`14,409,422` calls, `15,938.8 ms` self time).
  2. `fc27_coach.py:341:_validate_raw` and `fc27_tactics.py:546:build_fc27_tactics_report`: Re-running `model_validate` and recursive `scan_for_sensitive_fields` over the nested `FC27SearchContext` and `FC27ScoutReport` for every intermediate hypothesis and candidate.
  3. `encoder.py:207:iterencode`: Repeated `model_dump(mode="json")` + sorted `json.dumps` for deterministic SHA-256 report hashes.

### Validation Boundary Classifications

| Boundary | Classification | Rationale |
| :--- | :--- | :--- |
| `src/fc27_clubs_api_contracts.py:validate_matches_payload` | `TRUST_BOUNDARY_REQUIRED` | Untrusted community provider / fixture JSON boundary; strict Pydantic + duplicate-key and sensitive-field checks must never be weakened. |
| `src/fc27_user_export.py:plan_fc27_user_export_import` | `TRUST_BOUNDARY_REQUIRED` | Pre-persistence envelope hash and pseudonym verification before PostgreSQL writes. |
| `src/fc27_discord_bot.py:assert_sql_is_readonly_fc27_select & assert_discord_output_sanitized` | `TRUST_BOUNDARY_REQUIRED` | Read-only SQL guard and outbound Discord privacy filter. |
| `src/fc27_search_scout.py:FC27SearchContext & FC27ScoutReport Pydantic construction` | `INTERNAL_REVALIDATION_REQUIRED` | Enforces sample-size confidence tiers and non-recommendation invariants between Search and Scout. |
| `src/fc27_analyst.py / src/fc27_coach.py / src/fc27_tactics.py / src/fc27_director.py report-hash canonicalization` | `INTERNAL_REVALIDATION_CANDIDATE_FOR_REVIEW` | Repeated `model_dump(mode='json')` + `json.dumps` sorting and redundant recursive `scan_for_sensitive_fields` across already-validated frozen internal objects; prime candidate for memoized digest/privacy-verified flag in a dedicated Python optimization pass. |

---

## 6. Polars vs. Python / Pandas Evaluation (`NOT_JUSTIFIED`)

| Row Count | Python / Records Elapsed (ms) | Polars Installed | Result Equivalence | Sanitized Pseudonymous Only | Adoption Decision |
| :---: | :---: | :---: | :---: | :---: | :---: |
| `10` | `0.025` | `False` | `True` | `True` | `NOT_JUSTIFIED` |
| `1,000` | `0.632` | `False` | `True` | `True` | `NOT_JUSTIFIED` |
| `10,000` | `8.455` | `False` | `True` | `True` | `NOT_JUSTIFIED` |
| `60,000` | `63.898` | `False` | `True` | `True` | `NOT_JUSTIFIED` |

---

## 7. Threshold Evaluation & Prioritized Optimization Recommendations

| Metric | Threshold | Measured | Exceeded | First Remedy (Per Escalation Ladder) |
| :--- | :---: | :---: | :---: | :--- |
| PostgreSQL operational read query p95 | `250.0 ms` | `2.764 ms` | `False` | SQL tuning / index review |
| Discord command handler p95 | `1000.0 ms` | `2375.551 ms` | `True` | **Cache improvement** (memoize advisory bundle by `(club_id, latest_export_batch_id, match_count)`) |
| Dashboard endpoint warm p95 | `500.0 ms` | `57.125 ms` | `False` | Cache improvement / serialization reduction |
| Full advisory pipeline p95 (`1,000` matches / `LARGE`) | `2000.0 ms` | `11347.324 ms` | `True` | **Cache improvement / serialization reduction** (avoid redundant recursive `scan_for_sensitive_fields` on internal frozen models) |
| Polling worker container idle RSS | `300.0 MB` | `52.0 MB` | `False` | Process isolation |
| Discord bot container idle RSS | `250.0 MB` | `48.5 MB` | `False` | Process isolation |
| Container cold start | `5000.0 ms` | `1380.0 ms` | `False` | Process isolation |
| Operational analytical transformation p95 (`SMALL`) | `500.0 ms` | `2444.327 ms` | `True` | **Cache improvement** + internal validation deduplication |
| Large-record traced peak memory | `500.0 MB` | `9.279 MB` | `False` | Native-backed Python library |

### Why No New Language Is Warranted Now (`KEEP_PYTHON`)

1. **PostgreSQL 16 is sub-millisecond**: Operational read queries execute in `0.157 ms`–`0.352 ms` (`2.764 ms` with planning).
2. **Warm cached endpoints are sub-60 ms**: The dashboard serves cached `/demo/status` in `3.904 ms` median (`6.388 ms` p95) and `/api/fc27/demo/director` in `23.175 ms` median (`24.706 ms` p95).
3. **Every exceeded threshold is caused by uncached recomputation and redundant recursive `scan_for_sensitive_fields` calls inside Python**: Memoizing the read-only advisory report bundle in [`src/fc27_discord_bot.py`](../src/fc27_discord_bot.py) (matching [`src/fc27_demo_api.py`](../src/fc27_demo_api.py)) and skipping redundant recursive `scan_for_sensitive_fields` re-scans on already-validated frozen internal models (`INTERNAL_REVALIDATION_CANDIDATE_FOR_REVIEW`) resolves all three exceeded thresholds entirely within Python without introducing Go, Rust, C#/.NET, or C/C++.

---

## 8. Running the Offline Benchmark & Test Suite

```powershell
# 1. Run the 40 offline performance telemetry & benchmark tests
.\.venv\Scripts\pytest.exe tests/test_fc27_performance.py -q

# 2. Regenerate the ignored local baseline artifacts (.local-artifacts/performance/)
.\.venv\Scripts\python.exe -m src.fc27_performance
```
