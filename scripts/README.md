# Repository Scripts Index (`scripts/`)

- **Status**: Current
- **Audience**: Developers, Operators, CI/Security Reviewers
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `scripts/`, `src/db_migrate.py`, `src/db_roles.py`, `tests/`

This document is the authoritative index of all scripts in `scripts/` (`ORPHAN_CURRENT_SCRIPTS=0`).

For the overall architecture and quickstart path, see [`../README.md`](../README.md) and [`../docs/README.md`](../docs/README.md).

---

## Prominent Current Operational Path Commands

1. **Community Polling Worker (`CURRENT_PROVIDER_COMMAND`)**: [`fc27_polling_worker.py`](fc27_polling_worker.py)
2. **Database Role Bootstrap & Migration Runner (`CURRENT_MIGRATION_COMMAND`)**: `python -m src.db_roles` (`src/db_roles.py`) and `python -m src.db_migrate` (`src/db_migrate.py`)
3. **Discord Analytics & Match Results Bot (`CURRENT_OPERATOR_COMMAND`)**: [`fc27_discord_bot.py`](fc27_discord_bot.py)
4. **Documentation Consistency Validator (`CURRENT_CI_COMMAND` / `CURRENT_SECURITY_COMMAND`)**: [`check_documentation_consistency.py`](check_documentation_consistency.py)
5. **MatchPulse Repository Isolation Guard (`CURRENT_SECURITY_COMMAND` / `CURRENT_CI_COMMAND`)**: [`check_matchpulse_boundary.py`](check_matchpulse_boundary.py)

---

## Complete Script Classification Summary

| Script | Classification | Status | Replacement |
|---|---|---|---|
| [`fc27_polling_worker.py`](fc27_polling_worker.py) | `CURRENT_PROVIDER_COMMAND` | Active (Disabled-by-Default 12h Polling Worker) | `NONE` |
| [`fc27_backup.py`](fc27_backup.py) | `CURRENT_OPERATOR_COMMAND` | Active (Local PostgreSQL Domain Backup CLI) | `NONE` |
| [`fc27_restore_verify.py`](fc27_restore_verify.py) | `CURRENT_OPERATOR_COMMAND` | Active (Disposable Restore Verification CLI) | `NONE` |
| [`fc27_discord_bot.py`](fc27_discord_bot.py) | `CURRENT_OPERATOR_COMMAND` | Active (Disabled-by-Default Discord Bot CLI) | `NONE` |
| [`classify_ci_changes.py`](classify_ci_changes.py) | `CURRENT_CI_COMMAND` | Active (CI Changed-File & Trusted-Context Classifier) | `NONE` |
| [`build_test_report.py`](build_test_report.py) | `CURRENT_CI_COMMAND` | Active (Sanitized Browser Test Report Builder) | `NONE` |
| [`build_container_report.py`](build_container_report.py) | `CURRENT_CI_COMMAND` | Active (Container Image Validator, Trivy/SBOM Gate & Report Builder) | `NONE` |
| [`aggregate_test_reports.py`](aggregate_test_reports.py) | `CURRENT_CI_COMMAND` | Active (Shard Report Aggregator & Pages Tree Builder) | `NONE` |
| [`sanitize_test_report.py`](sanitize_test_report.py) | `CURRENT_SECURITY_COMMAND` | Active (Fail-Closed Report Privacy & HTML Scanner) | `NONE` |
| [`check_documentation_consistency.py`](check_documentation_consistency.py) | `CURRENT_CI_COMMAND` | Active (CI & Security Gate) | `NONE` |
| [`check_matchpulse_boundary.py`](check_matchpulse_boundary.py) | `CURRENT_SECURITY_COMMAND` | Active (CI & Security Gate) | `NONE` |
| [`test_postgres.sh`](test_postgres.sh) | `TEST_HELPER` | Active (Disposable PostgreSQL Test Runner) | `NONE` |
| [`archive/experiments/matchpulse_conformance.py`](archive/experiments/matchpulse_conformance.py) | `SUPERSEDED` | Archived (`DO_NOT_USE`) | `scripts/check_matchpulse_boundary.py` |

> **Note on Database Migrations (`CURRENT_MIGRATION_COMMAND`) & Performance Baseline (`python -m src.fc27_performance`)**: PostgreSQL 16 schema migrations (`migrations/0001_initial.sql` through `migrations/0013_fc27_manual_match_results.sql`) and role grants (`migrations/post/grants.sql`) are executed via the module entrypoints `python -m src.db_roles` (`src/db_roles.py`) and `python -m src.db_migrate` (`src/db_migrate.py`), or via `scripts/test_postgres.sh`. Repeatable offline performance benchmarks and baseline generation are executed via the module entrypoint `python -m src.fc27_performance` (`src/fc27_performance.py`, documented in `docs/FC27_PERFORMANCE_BASELINE.md`).

---

## Detailed Command Reference (All Current Scripts)

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

### 9. Historical Removed Scripts (Recorded in `config/fc27_removed_items_manifest.json`)
- **Classification**: `SUPERSEDED` (historical Commissioner approval and Stage B4 demo/export/capture scripts removed and registered in `config/fc27_removed_items_manifest.json`).

### 10. `scripts/test_postgres.sh`
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

### 13. `scripts/fc27_polling_worker.py`
- **Classification**: `CURRENT_PROVIDER_COMMAND`
- **Purpose**: Runs the disabled-by-default East Coasters (`330323`, `common-gen5`, `FC27`, `leagueMatch`, `max_results=10`) community polling worker in status (`--status`), single-cycle (`--once`), or bounded continuous (`--worker`) mode with single-flight PostgreSQL advisory locking (`pg_try_advisory_lock`) and conditional analytics (`Search -> Scout -> Analyst` only when `inserted_match_count > 0`).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status
  ```
- **Required Feature Flags**:
  - `--status`: None (`0` network calls, `0` provider calls).
  - `--once` / `--worker`: Requires `FC27_COMMUNITY_POLLING_ENABLED=true`, `FC27_COMMUNITY_PROVIDER_ENABLED=true`, `FC27_PERSISTENCE_ENABLED=true`, and `--confirm-live-polling`.
- **Network Behavior**: Zero network calls in `--status` mode or when any gate is disabled. When enabled with `--confirm-live-polling`, issues at most `1` provider request per cycle (`clubs/matches`, `retries=0`).
- **Database Behavior**: Connects as least-privilege `fc27_app`, acquires session-level `pg_try_advisory_lock`, persists new normalized matches transactionally (`persist_polled_match_envelope`), and runs read-only analytics only when `inserted_match_count > 0`.
- **Expected Output**: Sanitized JSON polling health (`--status`) or `FC27PollResult` / `FC27PollingHealth` JSON summary.
- **Teardown**: Releases PostgreSQL advisory lock in `finally` before inter-cycle sleep or process exit; handles `SIGINT`/`SIGTERM` cleanly.
- **Replacement Status**: Current authoritative polling worker CLI (`NONE`).

### 14. `scripts/fc27_backup.py`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: Creates a local custom-format PostgreSQL backup (`pg_dump -Fc`) of the 6 FC27 domain tables and writes a SHA-256 manifest under `.local-data/backups/` while retaining the 7 most recent backups (`BACKUP_RETENTION_COUNT=7`).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_backup.py --help
  ```
- **Replacement Status**: Current authoritative backup CLI (`NONE`).

### 15. `scripts/fc27_restore_verify.py`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: Verifies a `.local-data/backups/` archive SHA-256 against its manifest, restores into an ephemeral temporary database, verifies all 6 table counts and record hashes, and runs `Search -> Scout -> Analyst` readback before dropping the temporary database.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_restore_verify.py --help
  ```
- **Replacement Status**: Current authoritative restore verification CLI (`NONE`).

### 16. `scripts/fc27_discord_bot.py`
- **Classification**: `CURRENT_OPERATOR_COMMAND`
- **Purpose**: Runs the disabled-by-default read-only East Coasters FC27 Discord bot (`--status`, `--command`, `--serve`) with zero provider authority (`DISCORD_PROVIDER_REQUESTS=0`) and zero database write authority (`DISCORD_DATABASE_WRITE_ACCESS=false`).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_discord_bot.py --status
  ```
- **Replacement Status**: Current authoritative read-only Discord bot CLI (`NONE`).

### 17. `scripts/classify_ci_changes.py`
- **Classification**: `CURRENT_CI_COMMAND`
- **Purpose**: Classifies changed repository paths across the 11 canonical FC27 CI categories, enforces fail-closed broad coverage fallbacks, and evaluates trusted-context and GitHub Pages eligibility (`PAGES_PUBLICATION_TRUSTED_CONTEXT_ONLY=YES`, `UNTRUSTED_FORK_PAGES_PUBLICATION=NO`).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/classify_ci_changes.py --files docs/TESTING.md
  ```
- **Replacement Status**: Current authoritative CI changed-file classifier (`NONE`).

### 18. `scripts/build_test_report.py`
- **Classification**: `CURRENT_CI_COMMAND`
- **Purpose**: Merges JUnit XML test outputs, strips `pytest-html` environment tables, renders a 100% self-contained HTML test report (`HTML_EXTERNAL_RESOURCES=0`), builds the bounded GitHub Actions Job Summary (`$GITHUB_STEP_SUMMARY`), reconciles counts fail-closed (`REPORT_COUNT_RECONCILIATION=PASS`), and runs `scripts/sanitize_test_report.py`.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/build_test_report.py --help
  ```
- **Replacement Status**: Current authoritative CI job report builder (`NONE`).

### 19. `scripts/aggregate_test_reports.py`
- **Classification**: `CURRENT_CI_COMMAND`
- **Purpose**: Aggregates shard test reports across workflow jobs, verifies that the aggregate count equals the sum of shard counts (and expected full-suite collection total), builds the bounded static Pages report tree (`PAGES_REPORT_RETENTION_COUNT=30`), writes the final aggregate Job Summary, and scans all outputs via `scripts/sanitize_test_report.py`.
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/aggregate_test_reports.py --help
  ```
- **Replacement Status**: Current authoritative CI report aggregator (`NONE`).

### 20. `scripts/sanitize_test_report.py`
- **Classification**: `CURRENT_SECURITY_COMMAND`
- **Purpose**: Scans HTML reports, JUnit XML files, Markdown Job Summaries, JSON duration/metadata files, and logs for forbidden credentials, tokens, URIs, host/UNC paths, private IPs, Discord snowflakes, player pseudonyms, raw provider fields, environment dumps, and external HTML resources, failing closed without printing matched secret values (`FORBIDDEN_VALUES_PUBLISHED=0`).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/sanitize_test_report.py .local-artifacts/ci/report-preview
  ```
- **Replacement Status**: Current authoritative CI report privacy and security scanner (`NONE`).

### 21. `scripts/build_container_report.py`
- **Classification**: `CURRENT_CI_COMMAND`
- **Purpose**: Validates `.dockerignore` build-context exclusions, inspects built container image history and filesystem for secret leaks (`IMAGE_SECRET_SCAN=PASS`), verifies non-root `10001:65534` and `--read-only` rootfs compatibility, parses Trivy JSON vulnerability reports (`TRIVY_HIGH_CRITICAL_GATE=PASS`) and SPDX JSON SBOMs, and generates sanitized GitHub Actions Job Summaries and self-contained HTML reports (`container-browser-report`).
- **Safe Invocation**:
  ```powershell
  .\.venv\Scripts\python.exe scripts/build_container_report.py --help
  ```
- **Replacement Status**: Current authoritative container validation and report builder (`NONE`).

---

## Archived Scripts (`scripts/archive/`)

Archived scripts are non-operational (`DO_NOT_USE`), carry prominent `ARCHIVED: HISTORICAL REFERENCE ONLY` headers, and are never imported or executed by active code, workflows, Dockerfiles, Compose files, or tests:
- [`archive/experiments/matchpulse_conformance.py`](archive/experiments/matchpulse_conformance.py) — Superseded placeholder conformance harness; replaced by `scripts/check_matchpulse_boundary.py`. See [`../docs/archive/ARCHIVE_MANIFEST.md`](../docs/archive/ARCHIVE_MANIFEST.md) (`ARCH-023`).
