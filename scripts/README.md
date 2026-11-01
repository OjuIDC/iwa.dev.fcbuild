# Repository Scripts Index (`scripts/`)

- **Status**: Current
- **Audience**: Developers, Operators, CI/Security Reviewers
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `scripts/`, `src/db_migrate.py`, `src/db_roles.py`, `tests/`

This document is the authoritative index of all scripts in `scripts/` (`ORPHAN_CURRENT_SCRIPTS=0`).

For the overall architecture and quickstart path, see [`../README.md`](../README.md) and [`../docs/README.md`](../docs/README.md).

---

## Prominent Current Operational Path Commands

1. **Community Provider One-Shot Evaluation & Persistence (`CURRENT_PROVIDER_COMMAND`)**: [`fc27_community_eval.py`](fc27_community_eval.py)
2. **Database Role Bootstrap & Migration Runner (`CURRENT_MIGRATION_COMMAND`)**: `python -m src.db_roles` (`src/db_roles.py`) and `python -m src.db_migrate` (`src/db_migrate.py`)
3. **Deterministic Local Loopback Demo Ingest (`CURRENT_DEMO_COMMAND`)**: [`fc27_demo_ingest.py`](fc27_demo_ingest.py)
4. **Documentation Consistency Validator (`CURRENT_CI_COMMAND` / `CURRENT_SECURITY_COMMAND`)**: [`check_documentation_consistency.py`](check_documentation_consistency.py)
5. **MatchPulse Repository Isolation Guard (`CURRENT_SECURITY_COMMAND` / `CURRENT_CI_COMMAND`)**: [`check_matchpulse_boundary.py`](check_matchpulse_boundary.py)

---

## Complete Script Classification Summary

| Script | Classification | Status | Replacement |
|---|---|---|---|
| [`fc27_community_eval.py`](fc27_community_eval.py) | `CURRENT_PROVIDER_COMMAND` | Active (Primary Real-Data Path) | `NONE` |
| [`fc27_demo_ingest.py`](fc27_demo_ingest.py) | `CURRENT_DEMO_COMMAND` | Active (Synthetic Loopback Demo) | `NONE` |
| [`fc27_demo_fixture_server.py`](fc27_demo_fixture_server.py) | `CURRENT_DEMO_COMMAND` | Active (Synthetic Loopback Fixture Server) | `NONE` |
| [`check_documentation_consistency.py`](check_documentation_consistency.py) | `CURRENT_CI_COMMAND` | Active (CI & Security Gate) | `NONE` |
| [`check_matchpulse_boundary.py`](check_matchpulse_boundary.py) | `CURRENT_SECURITY_COMMAND` | Active (CI & Security Gate) | `NONE` |
| [`fc27_user_export_import.py`](fc27_user_export_import.py) | `CURRENT_OPERATOR_COMMAND` | Active (Offline Export/Capture Import) | `NONE` |
| [`fc27_capture_preflight.py`](fc27_capture_preflight.py) | `CURRENT_OPERATOR_COMMAND` | Active (Offline Capture Preflight) | `NONE` |
| [`fc27_club_lookup.py`](fc27_club_lookup.py) | `CURRENT_OPERATOR_COMMAND` | Active (Local Identity Verification CLI) | `NONE` |
| [`commissioner_decide.py`](commissioner_decide.py) | `CURRENT_OPERATOR_COMMAND` | Active (Commissioner Approval CLI) | `NONE` |
| [`model_health.py`](model_health.py) | `CURRENT_OPERATOR_COMMAND` | Active (Local Ollama Health Check) | `NONE` |
| [`ollama-init.sh`](ollama-init.sh) | `CURRENT_OPERATOR_COMMAND` | Active (Compose Model Bootstrap) | `NONE` |
| [`test_postgres.sh`](test_postgres.sh) | `TEST_HELPER` | Active (Disposable PostgreSQL Test Runner) | `NONE` |
| [`archive/experiments/matchpulse_conformance.py`](archive/experiments/matchpulse_conformance.py) | `SUPERSEDED` | Archived (`DO_NOT_USE`) | `scripts/check_matchpulse_boundary.py` |

> **Note on Database Migrations (`CURRENT_MIGRATION_COMMAND`)**: PostgreSQL 16 schema migrations (`migrations/0001_initial.sql` through `migrations/0012_fc27_performance_and_quarantine.sql`) and role grants (`migrations/post/grants.sql`) are executed via the module entrypoints `python -m src.db_roles` (`src/db_roles.py`) and `python -m src.db_migrate` (`src/db_migrate.py`), or via `scripts/test_postgres.sh`.

---

## Detailed Command Reference (All Current Scripts)

### 1. `scripts/fc27_community_eval.py`
- **Classification**: `CURRENT_PROVIDER_COMMAND`
- **Purpose**: Executes bounded, read-only `fc-clubs-api-community-v1` (`LIVE_EA_CLUBS_COMMUNITY_CLIENT`) evaluation for East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`), including offline fixture verification (`--operation mock-eval`), single-transaction PostgreSQL persistence (`--persist`), and one-shot live operations (`--operation identity-once`, `--operation recent-matches-once`, `--operation full-once`, gated by `--confirm` and `FC27_COMMUNITY_PROVIDER_ENABLED=true`).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_community_eval.py --operation mock-eval
  ```
- **Required Feature Flags**:
  - Offline mode (`--operation mock-eval`): None (`FC27_COMMUNITY_PROVIDER_ENABLED` not required).
  - Persistence apply (`--persist`): `--persist` CLI flag AND `FC27_PERSISTENCE_ENABLED=true`.
  - Live one-shot (`--operation identity-once`, `--operation full-once`): `--confirm` CLI flag AND `FC27_COMMUNITY_PROVIDER_ENABLED=true`.
- **Network Behavior**: Zero network requests in `--operation mock-eval` mode. In live modes (`--confirm` + `FC27_COMMUNITY_PROVIDER_ENABLED=true`), issues at most 1 (`identity-once`) or 2 (`full-once`) HTTPS GET requests to `https://proclubs.ea.com/api/fc/clubs/info` and `https://proclubs.ea.com/api/fc/clubs/matches` with `retries=0`.
- **Database Behavior**: No database access in default `mock-eval` mode. Writes normalized rows (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`) in a single atomic PostgreSQL transaction when `--persist` and `FC27_PERSISTENCE_ENABLED=true` are supplied; retains zero raw provider payloads.
- **Expected Output**: Deterministic `KEY=VALUE` summary lines (`PROVIDER_ID=fc-clubs-api-community-v1`, `SOURCE_CLASSIFICATION=LIVE_EA_CLUBS_COMMUNITY_CLIENT`, `MATCH_SCHEMA_STATUS=PASS`, `RAW_RESPONSE_RETAINED=NO`, etc.).
- **Teardown**: Stateless CLI process; close/remove any disposable PostgreSQL test container when finished.
- **Replacement Status**: Current authoritative provider command (`NONE`).

### 2. `scripts/fc27_demo_ingest.py`
- **Classification**: `CURRENT_DEMO_COMMAND`
- **Purpose**: Runs the deterministic local loopback fixture demo pipeline (`DEMO_FIXTURE_LOOPBACK` -> endpoint contracts -> response validation -> normalization & pseudonymization -> optional PostgreSQL persistence -> Search -> Scout -> Analyst).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_demo_ingest.py --source fixture --operation full-demo --scenario valid
  ```
- **Required Feature Flags**: `--source fixture` runs deterministically with `--demo-mode` enabled by default for fixture CLI execution; optional `--persist` requires `FC27_PERSISTENCE_ENABLED=true`.
- **Network Behavior**: Binds and connects exclusively to ephemeral `127.0.0.1` loopback fixture server (`DEMO_FIXTURE_LOOPBACK`); zero external network requests.
- **Database Behavior**: In-memory by default (`persistence_status=SKIPPED_BY_DEFAULT`); writes normalized PostgreSQL tables only when `--persist` and `FC27_PERSISTENCE_ENABLED=true` are both set.
- **Expected Output**: Sanitized JSON summary (`FC27DemoResult`) with `source_mode`, `normalized_counts`, `search_context_hash`, `scout_report_hash`, `analyst_report_hash`, and `execution_hash`.
- **Teardown**: Automatically shuts down its ephemeral loopback fixture server in a `finally` block.
- **Replacement Status**: Current authoritative synthetic demo command (`NONE`).

### 3. `scripts/fc27_demo_fixture_server.py`
- **Classification**: `CURRENT_DEMO_COMMAND`
- **Purpose**: Provides the loopback-only (`127.0.0.1`) synthetic FC27 HTTP fixture server used by `scripts/fc27_demo_ingest.py`, `src/fc27_demo.py`, and offline test suites.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_demo_fixture_server.py --host 127.0.0.1 --port 0 --scenario valid
  ```
- **Required Feature Flags**: None (strictly rejects non-`127.0.0.1` bind hosts).
- **Network Behavior**: Listens only on `127.0.0.1`; serves fixed `/api/fc/clubs/info`, `/api/fc/clubs/overallStats`, and `/api/fc/clubs/matches` synthetic payloads.
- **Database Behavior**: Zero database access.
- **Expected Output**: JSON status line with bound loopback host/port.
- **Teardown**: Exits cleanly on `SIGINT`/`SIGTERM`.
- **Replacement Status**: Current authoritative fixture server (`NONE`).

### 4. `scripts/check_documentation_consistency.py`
- **Classification**: `CURRENT_CI_COMMAND`
- **Purpose**: Validates documentation truth-table rules, archive notice headers, archive manifest (`config/fc27_archive_manifest.json`), removed-items manifest (`config/fc27_removed_items_manifest.json`), internal Markdown links, and zero runtime/workflow/Docker/Compose references to archived files.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/check_documentation_consistency.py --check --strict
  ```
- **Required Feature Flags**: None.
- **Network Behavior**: Offline static file checker; zero network access.
- **Database Behavior**: Zero database access.
- **Expected Output**: `Result: PASS` and exit code `0`.
- **Teardown**: Stateless CLI process.
- **Replacement Status**: Current authoritative documentation consistency validator (`NONE`).

### 5. `scripts/check_matchpulse_boundary.py`
- **Classification**: `CURRENT_SECURITY_COMMAND`
- **Purpose**: Enforces the hard repository isolation boundary between `iwa.dev.fcbuild` and `matchpulse-live-public` (`docs/MATCHPULSE_INTEGRATION_BOUNDARY.md`).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/check_matchpulse_boundary.py
  ```
- **Required Feature Flags**: None.
- **Network Behavior**: Offline static file checker; zero network access.
- **Database Behavior**: Zero database access.
- **Expected Output**: Boundary pass confirmation and exit code `0`.
- **Teardown**: Stateless CLI process.
- **Replacement Status**: Current authoritative boundary check (`NONE`; replaces archived `scripts/archive/experiments/matchpulse_conformance.py`).

### 6. `scripts/fc27_user_export_import.py`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: Validates, plans, and optionally imports user-supplied or operator-captured East Coasters (`330323`, `common-gen5`, `FC27`) JSON export envelopes into normalized PostgreSQL tables.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_user_export_import.py --help
  ```
- **Required Feature Flags**: `--apply` requires `FC27_PERSISTENCE_ENABLED=true` and `--confirm-apply`.
- **Network Behavior**: Offline local file processor; zero external HTTP calls.
- **Database Behavior**: Zero database writes in `--validate` and `--dry-run`; single-transaction normalized write in `--apply --confirm-apply`.
- **Expected Output**: JSON validation and import plan summary.
- **Teardown**: Stateless CLI process.
- **Replacement Status**: Current supporting offline import CLI (`NONE`).

### 7. `scripts/fc27_capture_preflight.py`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: Performs offline preflight validation on operator-captured East Coasters match files before import.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py --help
  ```
- **Required Feature Flags**: None for offline validation and dry-run modes.
- **Network Behavior**: Offline local file processor; zero external HTTP calls.
- **Database Behavior**: Zero database writes in default preflight and dry-run modes.
- **Expected Output**: JSON preflight verification report.
- **Teardown**: Stateless CLI process.
- **Replacement Status**: Current supporting capture preflight CLI (`NONE`).

### 8. `scripts/fc27_club_lookup.py`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: Verifies East Coasters (`330323`, `common-gen5`, `FC27`) provisional or verified club identity records against local contracts and prohibited-source policies.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_club_lookup.py --help
  ```
- **Required Feature Flags**: Live mode remains blocked unless `FC27_PROVIDER_ENABLED=true` and an approved live provider is supplied.
- **Network Behavior**: Zero network requests in default provisional/fixture modes.
- **Database Behavior**: Zero database writes by default.
- **Expected Output**: JSON `ClubIdentityRecord` payload.
- **Teardown**: Stateless CLI process.
- **Replacement Status**: Current supporting club identity lookup CLI (`NONE`).

### 9. `scripts/commissioner_decide.py`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: CLI utility for recording or inspecting human Commissioner approval decisions signed with the symmetric shared secret `APPROVAL_SIGNING_SECRET`.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/commissioner_decide.py --help
  ```
- **Required Feature Flags**: Requires PostgreSQL connection and `APPROVAL_SIGNING_SECRET_FILE` when recording decisions.
- **Network Behavior**: Connects only to local PostgreSQL / Graph API when invoked operationally.
- **Database Behavior**: Reads/writes `approval_requests`, `approval_nonces`, `approval_audit`, and `domain_events`.
- **Expected Output**: CLI status / decision confirmation.
- **Teardown**: Stateless CLI process.
- **Replacement Status**: Current supporting governance CLI (`NONE`).

### 10. `scripts/model_health.py`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: Verifies connectivity and loaded model availability against a local Ollama instance (`OLLAMA_BASE_URL`) for the optional LangGraph runtime.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/model_health.py --help
  ```
- **Required Feature Flags**: Uses `OLLAMA_BASE_URL` (defaults to local Ollama endpoint).
- **Network Behavior**: Queries only the configured local Ollama HTTP health/tags endpoint.
- **Database Behavior**: Zero database access.
- **Expected Output**: Model availability status and exit code `0` when healthy.
- **Teardown**: Stateless CLI process.
- **Replacement Status**: Current supporting model health CLI (`NONE`).

### 11. `scripts/ollama-init.sh`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: Container initialization shell script referenced by `docker-compose.yml` (`ollama-init` service) to pull configured local Ollama models when running the full multi-container stack.
- **Safe Invocation**: Invoked inside the `ollama-init` container via `docker compose up`.
- **Required Feature Flags**: None.
- **Network Behavior**: Connects to the internal `ollama` service container (`http://ollama:11434`).
- **Database Behavior**: Zero database access.
- **Expected Output**: Ollama model pull progress logs.
- **Teardown**: One-shot init container exits upon completion.
- **Replacement Status**: Current Compose init script (`NONE`).

### 12. `scripts/test_postgres.sh`
- **Classification**: `TEST_HELPER`
- **Purpose**: Starts a disposable `postgres:16` Docker container, runs database bootstrap/migrations (`python -m src.db_roles` and `python -m src.db_migrate`) and `pytest`, and tears down the container on exit.
- **Safe Invocation**:
  ```bash
  bash scripts/test_postgres.sh
  ```
- **Required Feature Flags**: Requires local Docker daemon.
- **Network Behavior**: Binds disposable PostgreSQL to `127.0.0.1`.
- **Database Behavior**: Creates ephemeral database, applies migrations `0001`–`0012`, and runs the test suite.
- **Expected Output**: Migration logs and `pytest` summary.
- **Teardown**: Trap handler (`trap cleanup EXIT`) automatically removes the test container.
- **Replacement Status**: Current POSIX test helper (`NONE`).

---

## Archived Scripts (`scripts/archive/`)

Archived scripts are non-operational (`DO_NOT_USE`), carry prominent `ARCHIVED: HISTORICAL REFERENCE ONLY` headers, and are never imported or executed by active code, workflows, Dockerfiles, Compose files, or tests:
- [`archive/experiments/matchpulse_conformance.py`](archive/experiments/matchpulse_conformance.py) — Superseded placeholder conformance harness; replaced by `scripts/check_matchpulse_boundary.py`. See [`../docs/archive/ARCHIVE_MANIFEST.md`](../docs/archive/ARCHIVE_MANIFEST.md) (`ARCH-023`).
