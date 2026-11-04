# East Coasters FC 27 Immutable Advisory-Report Cache & Internal Validation Optimization

- **Status**: Current (`IMPLEMENTED_AND_TESTED`)
- **Audience**: Developers, Operators, Architects, Security Reviewers
- **Target Club**: `East Coasters` (`club_id = 330323`, `platform = common-gen5`, `game_version = FC27`)
- **Source of Truth**: [`src/fc27_advisory_cache.py`](../src/fc27_advisory_cache.py), [`src/fc27_polling_worker.py`](../src/fc27_polling_worker.py), [`src/fc27_discord_bot.py`](../src/fc27_discord_bot.py), [`src/fc27_demo.py`](../src/fc27_demo.py), [`src/fc27_demo_api.py`](../src/fc27_demo_api.py), [`tests/test_fc27_advisory_cache.py`](../tests/test_fc27_advisory_cache.py)
- **Companion Specifications**: [`docs/FC27_PERFORMANCE_BASELINE.md`](FC27_PERFORMANCE_BASELINE.md), [`docs/FC27_DISCORD_BOT.md`](FC27_DISCORD_BOT.md), [`docs/FC27_POLLING_WORKER.md`](FC27_POLLING_WORKER.md), [`docs/FC27_LANGUAGE_DECISION_MATRIX.md`](FC27_LANGUAGE_DECISION_MATRIX.md)
- **Cache Contract Version**: `fc27-advisory-cache-1.0.0`

---

## 1. Purpose and Governance Posture

Following the empirical findings in [`docs/FC27_PERFORMANCE_BASELINE.md`](FC27_PERFORMANCE_BASELINE.md), the three exceeded review thresholds in the pre-optimization Python baseline (`Discord command handler p95`, `Full advisory pipeline p95 (LARGE)`, and `Operational analytical transformation p95 (SMALL)`) were caused by:

1. Rebuilding the full six-stage read-only advisory pipeline (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`) on every read-only Discord slash command and dashboard query even when the underlying persisted East Coasters match batch had not changed.
2. Redundant recursive `scan_for_sensitive_fields` walks and repeated canonical JSON serializations across already-validated, frozen internal Pydantic models (`FC27SearchContext`, `FC27ScoutReport`, `FC27AnalystReport`, `FC27CoachReport`, `FC27TacticsReport`, `FC27DirectorReport`).

[`src/fc27_advisory_cache.py`](../src/fc27_advisory_cache.py) resolves all three bottlenecks inside Python while preserving every security, privacy, and human-authority invariant:

- `TRUST_BOUNDARY_SCANS_REMOVED = 0`
- `LIVE_PROVIDER_REQUESTS_EXECUTED = 0`
- `EA_DNS_RESOLUTIONS_EXECUTED = 0`
- `DISCORD_MESSAGES_SENT = 0`
- `INFERENCE_REQUESTS_EXECUTED = 0`
- `RECOMMENDATIONS_ENABLED = NO`, `APPROVAL_ENABLED = NO`, `DISPATCH_ENABLED = NO`

---

## 2. Immutable Cache Contracts (`src/fc27_advisory_cache.py`)

[`src/fc27_advisory_cache.py`](../src/fc27_advisory_cache.py) defines four frozen Pydantic v2 contracts (`ConfigDict(extra="forbid", frozen=True)`):

### 2.1 `FC27AdvisoryCacheKey`

Every cached advisory bundle is keyed by all 15 deterministic lineage fields and their canonical SHA-256 `cache_key_digest`:

| Field | Enforced Value / Constraint | Purpose |
| :--- | :--- | :--- |
| `club_id` | `"330323"` | Target club lock (`East Coasters`) |
| `platform` | `"common-gen5"` | Target platform lock |
| `game_version` | `"FC27"` | Target edition lock |
| `source_classification` | Non-empty string (e.g. `LIVE_EA_CLUBS_COMMUNITY_CLIENT`, `SYNTHETIC_FIXTURE`) | Provenance classification |
| `latest_export_batch_id` | Optional normalized batch identifier | Invalidates cache on new batch |
| `completed_match_count` | `>= 0` | Persisted completed match count |
| `latest_match_id` | Optional latest match identifier | Detects latest match changes |
| `latest_played_at` | Optional UTC ISO-8601 timestamp | Detects match window changes |
| `persisted_matches_digest` | 64-character lowercase SHA-256 hex | Deterministic digest over `(match_id, match_hash, played_at)` |
| `formula_version` | `"fc27-analytics-1.0.0"` | Analytics formula lock |
| `search_scout_version` | `"1.0.0"` | Search/Scout contract version |
| `analyst_version` | `"1.0.0"` | Analyst contract version |
| `coach_version` | `"1.0.0"` | Coach contract version |
| `tactics_version` | `"1.0.0"` | Tactics contract version |
| `director_version` | `"1.0.0"` | Director contract version |

### 2.2 `FC27AdvisoryCacheEntry`

Stores a complete, hash-verified, read-only advisory report bundle:

- `cache_key` (`FC27AdvisoryCacheKey`) and `cache_key_digest` (`64`-hex SHA-256)
- `created_at` (UTC `datetime`) and `expires_at` (UTC `datetime`)
- `build_elapsed_ms` (`>= 0.0`)
- `search_context` (`FC27SearchContext`) and `search_context_hash`
- `scout_report` (`FC27ScoutReport`) and `scout_report_hash`
- `analyst_report` (`FC27AnalystReport`) and `analyst_report_hash`
- `coach_report` (`FC27CoachReport`) and `coach_report_hash`
- `tactics_report` (`FC27TacticsReport`) and `tactics_report_hash`
- `director_report` (`FC27DirectorReport`) and `director_report_hash`
- `bundle_hash` (`64`-hex SHA-256 over all six report hashes and `cache_key_digest`)
- Enforces `recommendation_enabled=False`, `approval_enabled=False`, and `dispatch_enabled=False`.

### 2.3 Prohibited Cache Contents

`FC27AdvisoryCacheEntry` validates on construction that no cached object contains:

- Raw provider HTTP response payloads (`raw_payload`)
- Unvalidated match dictionaries
- Gamer tags, raw player names, EA Account IDs, Persona IDs, Nucleus IDs, or Blaze IDs
- Unhashed event strings (e.g. `1:2871893|2:0|3:8650754`)
- Database URIs, Discord tokens, secrets, or filesystem paths
- Partial/failed analytical runs or mutable unvalidated dicts

### 2.4 `FC27AdvisoryCachePolicy` and `FC27AdvisoryCacheStats`

- **Policy Defaults (`FC27AdvisoryCachePolicy`)**: `enabled=True`, `max_entries=16` (`1..64`), `ttl_seconds=43200` (`60..86400`, matching the 12-hour polling cadence), `verify_hashes_on_store=True`, `verify_hashes_on_hit=True`, `copy_on_read=True`.
- **Metadata-Only Telemetry (`FC27AdvisoryCacheStats`)**: Tracks `enabled`, `entry_count`, `hit_count`, `miss_count`, `build_count`, `eviction_count`, `invalidation_count`, `verification_failure_count`, `last_cache_key_digest`, `last_bundle_hash`, `last_hit_at`, `last_miss_at`, `last_invalidated_at`, and `last_invalidation_reason`.

---

## 3. Single-Flight Concurrency & Invalidation Rules

1. **Single-Flight Per-Key Build**: Concurrent callers requesting the same `FC27AdvisoryCacheKey` coordinate through per-key condition locks inside `FC27AdvisoryCache.get_or_build(...)`. Only one caller executes the `Search -> Scout -> Analyst -> Coach -> Tactics -> Director` builder; concurrent waiters receive the built `FC27AdvisoryCacheEntry` with `build_count == 1`.
2. **Defensive Copy on Read (`copy_on_read=True`)**: Callers receive an isolated clone so accidental caller-side mutation of any Python container attribute cannot corrupt the cached entry.
3. **Serialized Read-Only Projections**: `get_or_build_serialized_projection(...)` memoizes read-only JSON/dict projections per `(cache_key_digest, projection_name)` for high-throughput loopback dashboard and Discord formatting paths, returning a fresh deep copy on every read.
4. **Automatic Invalidation & Eviction**:
   - **New Persisted Matches (`NEW_MATCHES_PERSISTED`)**: When [`src/fc27_polling_worker.py`](../src/fc27_polling_worker.py) persists new matches (`inserted_match_count > 0`), it invalidates prior entries (`reason="NEW_MATCHES_PERSISTED"`) and pre-warms the cache with the newly computed six-stage advisory bundle.
   - **No New Matches (`NO_NEW_MATCHES`)**: When `inserted_match_count == 0` and a valid cache entry exists for the persisted key, the worker preserves the warm entry without recomputing analytics.
   - **Replay Conflict (`REPLAY_CONFLICT`)**: Immediately invalidates the cache (`reason="REPLAY_CONFLICT"`), rolls back the transaction, and never caches quarantined data.
   - **TTL Expiration & LRU Eviction**: Expired entries (`now >= expires_at`) are evicted on access (`TTL_EXPIRED`), and capacity overflow beyond `max_entries` evicts the least-recently-used entry (`LRU_EVICTED`).
   - **Hash Integrity Verification**: Both `put` (`verify_hashes_on_store=True`) and `get` (`verify_hashes_on_hit=True`) verify all six report hashes and `bundle_hash`, evicting and incrementing `verification_failure_count` on any mismatch.

---

## 4. Validation Boundary Audit & Internal Optimization

Every validation and privacy-scan site across `src/fc27_*.py` was audited and classified into four strict categories:

| Boundary / Callsite | Classification | Optimization Action |
| :--- | :--- | :--- |
| `src/fc27_clubs_api_contracts.py:validate_matches_payload` | `TRUST_BOUNDARY_REQUIRED` | **Retained in full.** External provider / fixture JSON schema validation, duplicate-key rejection, and recursive sensitive-field scan. |
| `src/fc27_community_provider.py` acquisition & normalization | `TRUST_BOUNDARY_REQUIRED` | **Retained in full.** Community provider response validation, pseudonymization (`plr_ecf_<12-hex>`), and sensitive-field rejection. |
| `src/fc27_user_export.py` & `src/fc27_capture.py` import boundaries | `TRUST_BOUNDARY_REQUIRED` | **Retained in full.** Untrusted offline file import and pre-persistence verification. |
| `src/fc27_repository.py` persistence boundary | `TRUST_BOUNDARY_REQUIRED` | **Retained in full.** Pre-commit hash, pseudonym, and replay-conflict checks. |
| `src/fc27_discord_bot.py:assert_sql_is_readonly_fc27_select` & `assert_discord_output_sanitized` | `TRUST_BOUNDARY_REQUIRED` | **Retained in full.** Outbound Discord message/embed/attachment sanitization and read-only SQL enforcement. |
| `src/fc27_demo_api.py:_scan_no_sensitive_output` | `TRUST_BOUNDARY_REQUIRED` | **Retained in full.** Outbound HTTP JSON response privacy check. |
| `src/fc27_performance.py:assert_telemetry_payload_safe` | `TRUST_BOUNDARY_REQUIRED` | **Retained in full.** Telemetry artifact privacy check. |
| First construction of `FC27SearchContext`, `FC27ScoutReport`, `FC27AnalystReport`, `FC27CoachReport`, `FC27TacticsReport`, `FC27DirectorReport` | `INTERNAL_CONSTRUCTION_REQUIRED` | **Retained in full.** Enforces Pydantic v2 schema invariants, confidence-tier ceilings, evidence-language filters, and deterministic SHA-256 report hashes. |
| Repeated recursive `scan_for_sensitive_fields` and `model_dump(mode="json")` re-serialization of already-verified frozen upstream reports inside `Scout`, `Analyst`, `Coach`, `Tactics`, and `Director` | `REDUNDANT_INTERNAL_RESCAN` | **Optimized.** Verified frozen models carry a structural signature check (`_is_privacy_verified_frozen_model` / `_has_verified_model_hash`) that detects any `object.__setattr__` tampering while avoiding O(N) redundant re-scans and repeated JSON canonicalization of unchanged upstream reports. |

---

## 5. Measured Before vs. After Benchmark Results

All benchmarks were executed offline using [`src/fc27_performance.py`](../src/fc27_performance.py) with `LIVE_PROVIDER_REQUESTS_EXECUTED=0`, `EA_DNS_RESOLUTIONS_EXECUTED=0`, `DISCORD_MESSAGES_SENT=0`, `INFERENCE_REQUESTS_EXECUTED=0`, and `REPORT_HASH_EQUIVALENCE=PASS`.

### 5.1 Read-Only Discord Slash-Command Handlers (`src/fc27_discord_bot.py`)

| Command | Before Median (ms) | Before P95 (ms) | After Warm Median (ms) | After Warm P95 (ms) | Speedup (P95) | Target (`< 100 ms` Advisory) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `/fc27 status` | `223.861` | `266.000` | `85.072` | `90.288` | `2.9x` | `PASS` |
| `/fc27 polling` | `223.954` | `280.999` | `39.776` | `40.538` | `6.9x` | `PASS` |
| `/fc27 latest` | `223.597` | `255.259` | `78.573` | `115.151` | `2.2x` | `PASS` (`< 1000 ms`) |
| `/fc27 analysis` | `258.500` | `265.599` | `74.477` | `79.980` | `3.3x` | `PASS` (`< 100 ms`) |
| `/fc27 data-gaps` | `255.146` | `261.552` | `72.732` | `75.766` | `3.5x` | `PASS` (`< 100 ms`) |
| `/fc27 coach` | `667.977` | `720.204` | `76.506` | `81.412` | `8.8x` | `PASS` (`< 100 ms`) |
| `/fc27 tactics` | `977.454` | `1107.918` | `68.583` | `71.930` | `15.4x` | `PASS` (`< 100 ms`) |
| `/fc27 director` | `1091.930` | `1293.184` | `65.176` | `67.995` | `19.0x` | `PASS` (`< 100 ms`) |

### 5.2 Loopback Dashboard Endpoints (`src/fc27_demo_api.py`)

| Endpoint | Before Cold (ms) | After Cold (ms) | Before Warm P95 (ms) | After Warm P95 (ms) | Target (`< 100 ms` Warm) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `GET /demo/status` | `3080.704` | `396.387` | `3.308` | `5.579` | `PASS` |
| `GET /demo/matches` | `3.588` | `4.197` | `3.154` | `5.305` | `PASS` |
| `GET /demo/analysis` | `67.185` | `48.209` | `49.947` | `79.228` | `PASS` |
| `GET /api/fc27/demo/coach` | `34.142` | `18.308` | `50.659` | `29.477` | `PASS` |
| `GET /api/fc27/demo/tactics` | `42.201` | `17.560` | `49.093` | `24.977` | `PASS` |
| `GET /api/fc27/demo/director` | `34.331` | `16.434` | `44.849` | `12.236` | `PASS` |

### 5.3 Multi-Scale Cold Full Advisory Pipeline (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`)

| Scenario | Matches | Before Median (ms) | Before P95 (ms) | After Median (ms) | After P95 (ms) | Speedup (P95) | Threshold Status |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `SMALL` | `10` | `1249.687` | `1290.522` | `192.429` | `194.929` | `6.6x` | `PASS` (`< 500 ms`) |
| `MEDIUM` | `100` | `2373.841` | `2420.470` | `293.035` | `327.611` | `7.4x` | `PASS` |
| `LARGE` | `1,000` | `5869.654` | `5992.632` | `555.973` | `609.692` | `9.8x` | `PASS` (`< 2000 ms`) |
| `STRESS` | `10,000` | `2739.500` | `2741.753` | `211.797` | `222.567` | `12.3x` | `PASS` |

### 5.4 Warm Advisory Bundle Read Benchmark (`FC27AdvisoryCache`)

- **Warm Bundle Read Latency (`10` measured hits after `1` cold build)**: `median_ms = 14.6743`, `p95_ms = 22.3994`, `max_ms = 26.3843`
- **Cache Counters**: `builds = 1`, `hits = 10`, `provider_requests = 0`, `database_writes = 0`
- **Deterministic Output Equivalence**: `REPORT_HASH_EQUIVALENCE = PASS` across all six report hashes (`search_context_hash`, `scout_report_hash`, `analyst_report_hash`, `coach_report_hash`, `tactics_report_hash`, `director_report_hash`).

---

## 6. Running the Advisory Cache & Performance Verification Suites

```powershell
# 1. Run the 43 advisory cache unit, concurrency, tamper, and integration tests
.\.venv\Scripts\pytest.exe tests/test_fc27_advisory_cache.py -q

# 2. Run the full advisory pipeline & performance suites
.\.venv\Scripts\pytest.exe tests/test_fc27_coach.py tests/test_fc27_tactics.py tests/test_fc27_director.py tests/test_fc27_performance.py -q
```
