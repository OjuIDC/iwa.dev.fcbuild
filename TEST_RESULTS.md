# Test Execution Results (`iwa.dev.fcbuild`)

> **Historical Phase 1 Acceptance Trace**: The original 2026-10-03 Phase 1 acceptance test trace is preserved in [`docs/archive/test-results/TEST_RESULTS_2026-10-03_PHASE1.md`](docs/archive/test-results/TEST_RESULTS_2026-10-03_PHASE1.md).

---

## 1. Verification Metadata

| Field | Value |
|---|---|
| **Verification Date** | `2026-11-09` |
| **Base Commit (`main`)** | `5d1823c1c37add1d1062eedfec086514a504cd11` |
| **Cleanup Branch** | `refactor/remove-legacy-control-plane-stack` |
| **Operating System** | Windows 11 (`win32`, Python `3.14.8`, `pytest-8.4.2`) |
| **PostgreSQL Test Engine** | PostgreSQL `16` (`postgres:16.15-alpine` ephemeral test container on `127.0.0.1:55432`) |
| **Skipped Tests Count** | `2` (optional dependency / MSBuild workload guards) |
| **External Network Requests Executed** | `0` (`NETWORK_REQUESTS_EXECUTED=0`, `LIVE_PROVIDER_REQUESTS_EXECUTED=0`) |
| **EA Hostname DNS Resolutions** | `0` (`EA_DNS_RESOLUTIONS_EXECUTED=0`) |

---

## 2. Suite Execution Summary

### 2.1 Focused FC 27 Safe Adapter & Community Provider Suite
Command:
```powershell
.\.venv\Scripts\pytest.exe tests/test_fc27_clubs_api_contracts.py tests/test_fc27_api_cache.py tests/test_fc27_api_rate_limit.py tests/test_fc27_community_provider.py tests/test_fc27_polling_worker.py -q
```
Result: **All passed**
- `tests/test_fc27_clubs_api_contracts.py`: Verifies all 11 `FC27ClubsEndpointOperation` contracts, strict Pydantic response schemas, 23-bit `ev` bitmap decoding, unknown bit flagging, `parse_json_strict_no_duplicates`, and forbidden browser header rejection (`PROHIBITED_HEADER_NAMES`).
- `tests/test_fc27_api_cache.py`: Verifies `FC27BoundedNormalizedCache` (`max_entries=128`, `256 KiB`), deterministic cache keys, `stale_if_error_seconds=0`, and rejection of raw provider payloads (`FC27RawCacheProhibitedError`).
- `tests/test_fc27_api_rate_limit.py`: Verifies `FC27ProviderTokenBucketLimiter` (`capacity=1`, `300s`), `FC27RequestBudget` (`max_attempts=1`, `max_retries=0`), 3-state `FC27AdapterCircuitBreaker` (`OPEN`/`CLOSED`/`HALF_OPEN`), and pure retry classification (`should_retry=False`).
- `tests/test_fc27_community_provider.py`: Verifies `FC27CommunityProviderAdapter`, East Coasters (`330323`, `common-gen5`) identity and match normalization, pseudonymization, and `raw_response_retained=False`.
- `tests/test_fc27_polling_worker.py`: Verifies disabled-by-default single-flight 12-hour polling worker gates, advisory locks, and conditional analytics execution.

### 2.2 Full FC 27 Domain, Analytics, Coaching, & Repository Regression Suite
Command (with `POSTGRES_DSN` and `POSTGRES_SUPERUSER_DSN` pointing to PostgreSQL 16):
```powershell
.\.venv\Scripts\pytest.exe tests/test_fc27_*.py -q
```
Result: **All passed**
- Includes `tests/test_fc27_persistence_and_queries.py`, `tests/test_fc27_dashboard_queries.py`, `tests/test_fc27_coach.py`, `tests/test_fc27_privacy.py`, `tests/test_fc27_platform.py`, `tests/test_fc27_discord_bot.py`, `tests/test_fc27_match_results.py`, plus the focused provider and contract test modules above, executing real SQL against `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, and `fc27_quarantine_records` under Grants Policy Version 15.

### 2.3 Documentation Consistency, Archive Manifest, & Packaging Guard Suite
Command:
```powershell
.\.venv\Scripts\pytest.exe tests/test_fc27_archive_manifest.py tests/test_fc27_repository_cleanup.py tests/test_documentation_consistency.py tests/test_packaging.py tests/test_github_workflows.py tests/test_archive_integrity.py tests/test_compose_isolation.py -q
```
Result: **All passed (`0 failed`)**
- Verifies documentation truth-table alignment, archive headers and manifest completeness (`config/fc27_archive_manifest.json` and `config/fc27_removed_items_manifest.json`), relative Markdown link resolution, feature-flag documentation accuracy, HMAC shared-secret semantics, tactical hypothesis `NOT_APPROVABLE` classification, and Docker Compose localhost isolation (`24 passed` in `tests/test_compose_isolation.py`).

### 2.4 Full Repository Test Suite (PostgreSQL 16 Enabled)
Command:
```powershell
.\.venv\Scripts\pytest.exe -q
```
Result:
- **PostgreSQL 16 Schema, Role Bootstrap, Least-Privilege Grants (`GRANTS_POLICY_VERSION = 15`), Advisory Pipeline (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`), Community Provider, Polling Worker, Discord Bot, Manual Match Results, Backup/Restore, and Quality Suites**: **`1,453 passed`, `2 skipped`, `0 failed` (`1,455` total)**.

---

## 3. Safety & Boundary Verification Commands

| Check | Command | Result |
|---|---|---|
| **Bytecode Compilation** | `.\.venv\Scripts\python.exe -m compileall -q src scripts tests` | `EXIT_CODE=0` |
| **MatchPulse Sibling Boundary** | `.\.venv\Scripts\python.exe scripts/check_matchpulse_boundary.py` | `boundary drift check: OK` |
| **Community Polling Worker Status (Offline)** | `.\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status` | `POLL_STATUS=DISABLED` (`0` network requests) |
| **Discord Bot Status (Offline)** | `.\.venv\Scripts\python.exe scripts/fc27_discord_bot.py --status` | `DISCORD_PROVIDER_REQUESTS=0` |
| **Git Whitespace & Conflict Check** | `git diff --check` | `EXIT_CODE=0` (clean) |
