# Test Execution Results (`iwa.dev.fcbuild`)

> **Historical Phase 1 Acceptance Trace**: The original 2026-10-03 Phase 1 acceptance test trace is preserved in [`docs/archive/test-results/TEST_RESULTS_2026-10-03_PHASE1.md`](docs/archive/test-results/TEST_RESULTS_2026-10-03_PHASE1.md).

---

## 1. Verification Metadata

| Field | Value |
|---|---|
| **Verification Date** | `2026-10-31` |
| **Base Commit (`main`)** | `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0` |
| **Cleanup Branch** | `chore/fc27-current-path-cleanup` |
| **Operating System** | Windows 11 (`win32`, Python `3.14.8`, `pytest-8.4.2`) |
| **PostgreSQL Test Engine** | PostgreSQL `16` (`postgres:16 -c max_connections=300` ephemeral test container on `127.0.0.1:55432`) |
| **Skipped Tests Count** | `15` (intentional Phase 3 dormancy and optional dependency guards) |
| **External Network Requests Executed** | `0` (`NETWORK_REQUESTS_EXECUTED=0`, `LIVE_PROVIDER_REQUESTS_EXECUTED=0`) |
| **EA Hostname DNS Resolutions** | `0` (`EA_DNS_RESOLUTIONS_EXECUTED=0`) |

---

## 2. Suite Execution Summary

### 2.1 Focused FC 27 Safe Adapter Suite (`217 passed`)
Command:
```powershell
.\.venv\Scripts\pytest.exe tests/test_fc27_clubs_api_contracts.py tests/test_fc27_api_cache.py tests/test_fc27_api_rate_limit.py tests/test_fc27_authorized_provider.py tests/test_fc27_authorized_api.py -q
```
Result: **`217 passed` in `1.88s`**
- `tests/test_fc27_clubs_api_contracts.py`: Verifies all 11 `FC27ClubsEndpointOperation` contracts, strict Pydantic response schemas, 23-bit `ev` bitmap decoding, unknown bit flagging, and forbidden browser header rejection.
- `tests/test_fc27_api_cache.py`: Verifies `FC27BoundedNormalizedCache` (`max_entries=128`, `256 KiB`), deterministic cache keys, `stale_if_error_seconds=0`, and rejection of raw provider payloads (`FC27RawCacheProhibitedError`).
- `tests/test_fc27_api_rate_limit.py`: Verifies `FC27ProviderTokenBucketLimiter` (`capacity=1`, `300s`), `FC27RequestBudget` (`max_attempts=1`, `max_retries=0`), 3-state `FC27AdapterCircuitBreaker` (`OPEN`/`CLOSED`/`HALF_OPEN`), and pure retry classification (`should_retry=False`).
- `tests/test_fc27_authorized_provider.py`: Verifies `FC27AuthorizedProviderAdapter` across all 8 operations, cache hit/miss/bypass, rate-limit integration, circuit-breaker trip on `HTTP 429`/`403`, and `FC27SourceProvenance` attachment.
- `tests/test_fc27_authorized_api.py`: Verifies `FC27AuthorizedHTTPTransport` loopback fixtures, DNS resolution blocking for unapproved hosts, secret redaction, and preflight CLI behavior.

### 2.2 Full FC 27 Domain, Analytics, Coaching, & Repository Regression Suite (`267 passed`)
Command (with `POSTGRES_DSN` and `POSTGRES_SUPERUSER_DSN` pointing to PostgreSQL 16):
```powershell
.\.venv\Scripts\pytest.exe tests/test_fc27_*.py -q
```
Result: **`267 passed` in `10.17s`**
- Includes `tests/test_fc27_persistence_and_queries.py`, `tests/test_fc27_dashboard_queries.py`, `tests/test_fc27_preapproval_validator.py`, `tests/test_fc27_api_ingestion.py`, `tests/test_fc27_operator_capture.py`, `tests/test_fc27_user_export.py`, `tests/test_fc27_platform.py`, `tests/test_fc27_clubs_compatibility.py`, plus the 5 focused adapter test modules above, executing real SQL against `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, and `fc27_quarantine_records` under Grants Policy Version 14.

### 2.3 Documentation Consistency, Archive Manifest, & Packaging Guard Suite
Command:
```powershell
.\.venv\Scripts\pytest.exe tests/test_fc27_archive_manifest.py tests/test_fc27_repository_cleanup.py tests/test_documentation_consistency.py tests/test_packaging.py tests/test_workflow.py tests/test_archive_integrity.py tests/test_phase1_scope.py tests/test_compose_isolation.py -q
```
Result: **All passed (`0 failed`)**
- Verifies documentation truth-table alignment, archive headers and manifest completeness (`config/fc27_archive_manifest.json` and `config/fc27_removed_items_manifest.json`), relative Markdown link resolution, feature-flag documentation accuracy, HMAC shared-secret semantics, tactical hypothesis `NOT_APPROVABLE` classification, and Docker Compose localhost isolation (`24 passed` in `tests/test_compose_isolation.py`).

### 2.4 Full Repository Test Suite (PostgreSQL 16 Enabled)
Command:
```powershell
.\.venv\Scripts\pytest.exe -q
```
Result:
- **PostgreSQL 16 Schema, Role Bootstrap, Least-Privilege Grants (`GRANTS_POLICY_VERSION = 14`), WebAuthn Step-Up, 14-Field `fc27_decision_evidence`, `PostgresSaver` Graph Interrupt/Resume, Dispatch Outbox Isolation, MCP HTTP/Stdio Servers, and FC 27 Suites**: **`3,555 passed`, `15 skipped`, `0 failed` (`3,570` total)**.

---

## 3. Safety & Boundary Verification Commands

| Check | Command | Result |
|---|---|---|
| **Bytecode Compilation** | `.\.venv\Scripts\python.exe -m compileall -q src scripts tests` | `EXIT_CODE=0` |
| **MatchPulse Sibling Boundary** | `.\.venv\Scripts\python.exe scripts/check_matchpulse_boundary.py` | `OK: Zero references to matchpulse-live-public found across repository files.` |
| **Provider Preflight (Default Fail-Closed)** | `.\.venv\Scripts\python.exe -c "from src.fc27_authorized_api import run_fc27_provider_preflight; print(run_fc27_provider_preflight())"` | `status: BLOCKED_BY_GATE` (`network_requests_executed: 0`) |
| **Capture Preflight CLI** | `.\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py --help` | `EXIT_CODE=0` (zero network requests) |
| **Git Whitespace & Conflict Check** | `git diff --check` | `EXIT_CODE=0` (clean) |
