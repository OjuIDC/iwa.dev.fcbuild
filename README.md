# East Coasters FC 27 Platform (`iwa.dev.fcbuild`)

- **Status**: Current
- **Audience**: Developers, Operators, Commissioners, Security Reviewers
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `src/`, `config/`, `migrations/`, `docker-compose.yml`, `config.example.env`, `tests/`
- **Supersedes**: `docs/archive/prompts/ORIGINAL_REQUEST.md`, `docs/archive/reports/IMPLEMENTATION_REPORT.md`

## 0. Five-Minute Operator Quickstart (Current Real-Data Path)

For **East Coasters (`CLUB_ID=330323`, `PLATFORM=common-gen5`, `GAME_VERSION=FC27`)**, the working end-to-end architecture is:

```text
pinned FC27 community provider (fc-clubs-api-community-v1 / LIVE_EA_CLUBS_COMMUNITY_CLIENT)
  -> repository endpoint contracts & schema validation (src/fc27_clubs_api_contracts.py)
  -> normalization, sensitive-field rejection & plr_ecf_<12-hex> pseudonymization
  -> optional single-transaction PostgreSQL 16 persistence (src/fc27_repository.py)
  -> Search context assembly (src/fc27_search_scout.py)
  -> Scout evidence synthesis (src/fc27_search_scout.py)
  -> Analyst evidence synthesis (src/fc27_analyst.py)
  -> Coach observational hypotheses (src/fc27_coach.py)
  -> Tactics experimental candidates (src/fc27_tactics.py)
  -> Director evidence reconciliation (src/fc27_director.py)
  -> Discord slash-command analytics & manual Match Results bot (src/fc27_discord_bot.py, src/fc27_match_results.py)
```

1. **Working Polling Worker & Status Check (`scripts/fc27_polling_worker.py`)**:
   - Provider ID: `fc-clubs-api-community-v1` (`LIVE_EA_CLUBS_COMMUNITY_CLIENT`, documented in [`docs/FC27_POLLING_WORKER.md`](docs/FC27_POLLING_WORKER.md)).
   - Run deterministic offline status check (zero network requests):
     ```powershell
     .\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status
     ```
2. **Start Disposable PostgreSQL 16 & Run Persistence / Analytics (`Search -> Scout -> Analyst`)**:
   ```powershell
   docker run -d --name fc27-test-postgres -e POSTGRES_USER=fc27test -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 postgres:16 -c max_connections=300
   $env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
   $env:TEST_POSTGRES_ADMIN_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
   .\.venv\Scripts\pytest.exe tests/test_fc27_persistence_and_queries.py -q
   ```
3. **Inspect Discord Bot & Polling Status (`scripts/fc27_discord_bot.py`)**:
   ```powershell
   .\.venv\Scripts\python.exe scripts/fc27_discord_bot.py --status
   ```
4. **Run Tests**:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/test_fc27_community_provider.py tests/test_fc27_pandas_view.py tests/test_fc27_privacy.py tests/test_fc27_discord_bot.py -q
   ```
5. **Disabled Features & Historical Material**:
   - Continuous unthrottled polling, browser scraping, raw response storage, player aliases/handles, autonomous Coach/Tactics/Director recommendations (`FC27_RECOMMENDATIONS_ENABLED=false`), and player dispatch (`DISPATCH_ENABLED=false`) remain **disabled**.
   - Historical and superseded documents/scripts live in [`docs/archive/README.md`](docs/archive/README.md), [`docs/archive/ARCHIVE_MANIFEST.md`](docs/archive/ARCHIVE_MANIFEST.md), and `scripts/archive/` (indexed in [`scripts/README.md`](scripts/README.md)).

## 1. Project Purpose

`iwa.dev.fcbuild` is a security-hardened, human-in-the-loop football analytics, tactical evaluation, and decision-support platform built for **EA Sports FC 27 Pro Clubs**. Throughout the platform, **models advise and humans decide**: autonomous agents and analytical pipelines may summarize evidence and propose structured candidates, but only an authenticated human Commissioner (or explicitly authorized human delegate/player within scoped boundaries) can record an authoritative decision.

## 2. Current Supported Use Case

- **Primary Architecture (Community Provider & Endpoint Contract Boundary)**: Fixed FC 27 endpoint contracts (`src/fc27_clubs_api_contracts.py`), normalized-object caching (`src/fc27_api_cache.py`), conservative rate/budget/circuit-breaker controls (`src/fc27_api_rate_limit.py`), privacy and pseudonymization boundary (`src/fc27_privacy.py`), and the bounded community provider adapter (`src/fc27_community_provider.py`, `src/fc27_polling_worker.py`) are implemented and verified against hermetic mocks and disposable PostgreSQL 16 databases.
- **Manual Match Results & Discord Surface**: Role-ID-gated manual Match Results (`src/fc27_match_results.py`) and read-only analytics slash commands (`src/fc27_discord_bot.py`) provide club operators and members with structured match logging and `/fc27 status` operational readback inside Discord.
- **PostgreSQL 16 Governance & Audit Foundation**: Role-separated PostgreSQL 16 storage (`migrations/0001_initial.sql`–`migrations/0013_fc27_manual_match_results.sql`, Grants Policy Version `15`), deterministic observational Coach/Tactics/Director advisory engines (`src/fc27_coach.py`, `src/fc27_tactics.py`, `src/fc27_director.py`), and historical Phase 1/2 LangGraph/WebAuthn/HMAC control-plane schemas (superseded runtime modules `src/graph.py`, `src/fc27_coaching.py`, `src/identity/`, `src/approval_store.py` removed in Stage B3) are verified against local PostgreSQL 16.

## 3. East Coasters Identity

| Attribute | Value | Governance Status |
|---|---|---|
| **Canonical Club Name** | `East Coasters` (aliases: `EastCoasters FC`, `ECF`) | Target Club |
| **Club ID (`FC27_CLUB_ID`)** | `330323` | `CORROBORATED` / `PROVISIONAL` (not live-provider `VERIFIED`) |
| **Platform (`FC_PLATFORM`)** | `common-gen5` | Target Platform |
| **Game Version** | `FC27` (`fc_version: "27"`) | Target Edition |
| **Manager Authority Status** | `UNVERIFIED` | Club identity never self-grants human authority |

## 4. Current Architecture

- **FC27 Community Polling & Discord Runtime (`src/fc27_polling_worker.py`, `src/fc27_discord_bot.py`, `src/fc27_match_results.py`, `src/fc27_advisory_cache.py`)**: Bounded 12-hour community-provider polling worker (`fc27-community-worker`) and Discord slash-command analytics and manual Match Results bot (`fc27-discord-bot`) backed by deterministic `Search -> Scout -> Analyst -> Coach -> Tactics -> Director` advisory engines (historical `graph-api` (`src/graph_api.py`, `src/graph.py`) superseded and removed in Stage B3).
- **PostgreSQL 16 (`migrations/0001_initial.sql`–`migrations/0013_fc27_manual_match_results.sql`, `migrations/post/grants.sql`)**: Single authoritative audit of record across `graph_threads`, `recommendation_packages`, `approval_requests`, `approval_audit`, `fc27_decision_evidence`, `domain_events`, `outbox`, identity tables (`fc27_users`, `fc27_sessions`, `fc27_webauthn_credentials`, `fc27_challenges`), normalized FC 27 performance tables (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`), and isolated manual Match Results tables (`fc27_manual_matches`, `fc27_manual_match_players`, `fc27_manual_match_events`, `fc27_manual_match_audit`). LangGraph `PostgresSaver` tables (`checkpoints*`) store workflow state only and are strictly separated from normalized FC 27 tables and authorization state.
- **Redis (`redis:7.4.2-alpine`)**: Ephemeral pub/sub and watcher lease transport only. **Redis is never audit of record or authorization authority.**
- **Safe FC 27 Provider & Contract Layer (`src/fc27_community_provider.py`, `src/fc27_clubs_api_contracts.py`, `src/fc27_api_cache.py`, `src/fc27_api_rate_limit.py`)**: Repository-native adaptation of fixed FC 27 endpoint contracts, strict Pydantic response schemas, 23-bit match event bitmap (`ev`) decoding, bounded normalized-object caching, token-bucket rate limiting, per-run request budgets (`max_attempts=1`, `max_retries=0`), and a 3-state circuit breaker.

## 5. Implemented Capabilities

1. **Least-Privilege PostgreSQL 16 Foundation**: Versioned migrations (`0001`–`0013`) and Grants Policy Version `15` with separated `fc27_owner` (`NOLOGIN`), `fc27_migrator`, `fc27_app` (DML-only, zero DDL, zero `DELETE` on FC 27 tables), `fc27_match_results_writer` (narrow DML on manual Match Results tables only), `fc27_dispatch`, and `fc27_relay` roles.
2. **LangGraph Single-Interrupt Approval Workflow**: Persisted recommendation packages (`package_version` SHA-256), single `interrupt()` at node `approval`, and mandatory PostgreSQL decision re-read on resume (`ApprovalNotCommitted` if missing or mismatched).
3. **Human Identity, WebAuthn Step-Up, & CLI HMAC Verification (Historical Control-Plane Schema)**: Phase 2 user/session/passkey/TOTP/delegation schema tables, 14-field bound `fc27_decision_evidence` with mandatory WebAuthn User Verification (`require_user_verification=True`), and constant-time symmetric HMAC-SHA256 verification (historical runtime modules `src/identity/`, `src/approval.py`, `scripts/commissioner_decide.py` superseded and removed in Stage B3).
4. **FC 27 Safe Provider Adapter & Contracts**: 11 fixed endpoint operations, 13 strict response schemas, match event bitmap (`ev`) decoder, `plr_ecf_<12-hex>` pseudonymization with collision detection (`PSEUDONYM_COLLISION`), `FC27SourceProvenance`, bounded normalized cache, token-bucket rate limiter, request budget, circuit breaker, and pure retry classifier.
5. **Normalized Performance Storage, Read-Only Analytics, Search/Scout Foundation, & Analyst Evidence Synthesis**: Repository persistence (`src/fc27_repository.py`), pure deterministic analytics (`src/fc27_analytics.py`, `FORMULA_VERSION = "fc27-v1.0"`), deterministic Search context assembly and Scout evidence/hypothesis synthesis (`src/fc27_search_scout.py`, `FC27AnalyticsReader`, `search_node`, `scout_node`), deterministic Analyst evidence validation, clustering, counter-evidence, confounder/uncertainty analysis, and bounded observational finding synthesis (`src/fc27_analyst.py`, `analyst_evidence_node`), read-only PostgreSQL dashboard queries (`src/fc27_dashboard_queries.py`, `READ ONLY`, `5000ms` statement timeout), and deterministic pre-approval validation contracts (`src/schemas.py`, `src/fc27_coach.py`; historical `src/fc27_coaching.py` superseded in Stage B3).
6. **FC 27 Privacy, Pseudonymization, & Sanitization Boundary (`src/fc27_privacy.py`)**: Centralized deterministic pseudonymization (`plr_ecf_<12-hex>`), sensitive-field rejection (`SENSITIVE_KEYS_LOWER`), canonical SHA-256 hashing (`canonical_json_sha256`), Discord mention suppression (`suppress_discord_mentions`), CSV formula escaping (`escape_csv_formula_cell`), URL validation (`reject_external_urls`), and secret/DSN redaction (`sanitize_log_text`). (Historical loopback demo/export/capture paths `src/fc27_demo.py`, `src/fc27_demo_api.py`, `src/fc27_user_export.py`, `src/fc27_capture.py` removed in Stage B4; polling and repository status exposed via Discord `/fc27 status`.)
7. **Disabled-by-Default FC 27 Community Polling Worker (`fc27-community-worker`)**: Production-shaped background polling worker (`src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`, `Dockerfile.community-worker`, `docker-compose.community-worker.yml`; documented in [`docs/FC27_POLLING_WORKER.md`](docs/FC27_POLLING_WORKER.md) and [`docs/FC27_DAILY_POLLING_OPERATIONS.md`](docs/FC27_DAILY_POLLING_OPERATIONS.md)) with single-flight PostgreSQL advisory locking, 12-hour intervals (`POLL_INTERVAL_SECONDS=43200`, `ONCE_EVERY_12_HOURS`, max 2 requests/24h), 1-request/0-retry budgets, transactional persistence before analytics, provider window continuity tracking, and full advisory analytics (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`) on new matches only (`FC27_COMMUNITY_POLLING_ENABLED=false` by default).
8. **Local Database Backup & Restore Verification (`src/fc27_backup_restore.py`)**: Custom-format `pg_dump -Fc` domain backups (`scripts/fc27_backup.py`), 7-backup retention, SHA-256 manifest verification, and disposable temporary-database restore verification (`scripts/fc27_restore_verify.py`; documented in [`docs/FC27_BACKUP_AND_RECOVERY.md`](docs/FC27_BACKUP_AND_RECOVERY.md)).
9. **East Coasters Discord Analytics & Manual Match Results Bot (`src/fc27_discord_bot.py`, `src/fc27_match_results.py`)**: Disabled-by-default (`FC27_DISCORD_BOT_ENABLED=false`), slash-command-only bot (`scripts/fc27_discord_bot.py`, `Dockerfile.discord`, `docker-compose.discord.yml`; documented in [`docs/FC27_DISCORD_BOT.md`](docs/FC27_DISCORD_BOT.md)) with zero provider authority (`DISCORD_PROVIDER_REQUESTS=0`), read-only analytics access, `/fc27 status` polling/continuity/repository status inspection, role-ID-gated manual Match Results commands (`matchday`, `result`, `editresult`, `lastresult`, `match`, `results`, `topscorers`, `playerstats`, `deletematch`) via `fc27_match_results_writer`, and guild/channel allowlists.
10. **Deterministic Read-Only Advisory Pipeline (`Coach -> Tactics -> Director`)**: Observational coaching hypotheses (`src/fc27_coach.py`, [`docs/FC27_COACH_HYPOTHESIS_ENGINE.md`](docs/FC27_COACH_HYPOTHESIS_ENGINE.md)), single-variable experimental tactical candidates (`src/fc27_tactics.py`, [`docs/FC27_TACTICS_CANDIDATE_ENGINE.md`](docs/FC27_TACTICS_CANDIDATE_ENGINE.md)), and non-overlapping experiment sequencing (`src/fc27_director.py`, [`docs/FC27_DIRECTOR_RECONCILIATION.md`](docs/FC27_DIRECTOR_RECONCILIATION.md)) with `recommendation=False`, `approvable=False`, and `dispatchable=False`.
11. **Bounded Performance Telemetry, Optimization Baseline, & Immutable Advisory-Report Cache (`src/fc27_performance.py`, `src/fc27_advisory_cache.py`)**: Disabled-by-default (`FC27_PERFORMANCE_TELEMETRY_ENABLED=false`) privacy-safe telemetry, deterministic multi-scale benchmark scenarios (`SMALL_10_MATCHES`, `MEDIUM_100_MATCHES`, `LARGE_1000_MATCHES`, `STRESS_10000_MATCHES`), disposable PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` inspection, Python stdlib profiling (`cProfile`, `pstats`, `tracemalloc`), Polars comparison (`NOT_JUSTIFIED`), thread-safe single-flight immutable six-report advisory bundle caching (`FC27AdvisoryCache`), internal validation deduplication on verified frozen models, language decision matrix (`KEEP_PYTHON` default), and local inference boundary specification (documented in [`docs/FC27_PERFORMANCE_BASELINE.md`](docs/FC27_PERFORMANCE_BASELINE.md), [`docs/FC27_ADVISORY_CACHE.md`](docs/FC27_ADVISORY_CACHE.md), [`docs/FC27_LANGUAGE_DECISION_MATRIX.md`](docs/FC27_LANGUAGE_DECISION_MATRIX.md), and [`docs/FC27_LOCAL_INFERENCE_BOUNDARY.md`](docs/FC27_LOCAL_INFERENCE_BOUNDARY.md)).

## 6. Disabled Capabilities

All external networking, persistence writes, analytics execution, recommendation generation, and player dispatch are **disabled by default** (`fail-closed`):

- **Live EA / Provider Access**: Disabled (`FC27_PROVIDER_ENABLED=false`, `FC27_STANDARD_HTTP_TRANSPORT_ENABLED=false`, `FC27_LIVE_INGESTION_ENABLED=false`, `SELECTED_PROVIDER=NONE`, circuit breaker `OPEN`). Zero live EA requests are executed.
- **Community Polling Worker**: Disabled by default (`FC27_COMMUNITY_POLLING_ENABLED=false`, `FC27_COMMUNITY_POLL_MAX_REQUESTS=1`, `FC27_COMMUNITY_POLL_MAX_RETRIES=0`). Requires explicit `--confirm-live-polling` and opt-in profile `fc27-polling`.
- **PostgreSQL FC 27 Persistence Apply**: Disabled by default (`FC27_PERSISTENCE_ENABLED=false`).
- **Read-Only Analytics Queries**: Disabled by default (`FC27_ANALYTICS_ENABLED=false`).
- **Coaching Recommendations**: Disabled by default (`FC27_RECOMMENDATIONS_ENABLED=false`). Unverified tactical claims (`4-2-1-3`, `4-1-2-1-2 Narrow`, role/attribute/PlayStyle+ claims, and FC25/FC26 tactics codes) are classified as `EXTERNAL_TACTICAL_HYPOTHESIS` (`NOT_APPROVABLE`).
- **Player Dispatch**: Strictly disabled (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`). No player delivery occurs.

## 7. Security Posture

- **Models Advise; Humans Decide**: No agent, LLM, Graph node, or service token (`GRAPH_SERVICE_TOKEN`, `bff_service`) can approve or reject a package. Inactivity or timeout never approves.
- **Approval Is Not Dispatch**: Approved workflows terminate in `APPROVED, NOT DISPATCHED` (or `REJECTED, NO ACTION TAKEN`).
- **Resume Payload Is Not Authorization**: `Command(resume=...)` and `POST /v1/runs/{thread_id}/resume` only wake the graph; the `approval` node re-reads the committed decision from PostgreSQL.
- **No Browser or TLS Impersonation**: Upstream `Impit` (`impit-node`), Chromium/Playwright automation, JA3/JA4/HTTP2 TLS fingerprint spoofing, fake Chrome headers (`Sec-CH-UA*`, `Sec-Fetch-*`), cookies, and session replay are **rejected** and excluded from the codebase.
- **Privacy & Non-Retention**: Raw HTTP responses and raw upload files are never persisted (`raw_response_retained=False`). Gamer tags, EA Account IDs, Nucleus IDs, and Persona IDs are stripped or pseudonymized (`plr_ecf_<12-hex>`); display aliases are suppressed by default.
- **MatchPulse Isolation**: `..\matchpulse-live-public` is an isolated sibling repository enforced by `scripts/check_matchpulse_boundary.py`.

## 8. Data Flow

```text
[Approved Provider / Mock Fixture]
   │  (gated by FC27_PROVIDER_ENABLED, FC27_LIVE_INGESTION_ENABLED, Circuit Breaker)
   ▼
[FC27AuthorizedTransportClient & FC27ClubsEndpointOperation Contracts]
   │  (HTTPS, host allowlist, max 512 KiB, 0 redirects, 0 cookies, 0 retries by default)
   ▼
[Strict Schema Validation -> Sensitive-Field Scan -> Normalization -> Pseudonymization (plr_ecf_<12-hex>)]
   │  (FC27BoundedNormalizedCache & FC27ProviderTokenBucketLimiter / FC27RequestBudget)
   ▼
[Replay / Idempotency Planning -> PostgreSQL 16 Normalized Storage (migrations/0012)]
   │  (gated by FC27_PERSISTENCE_ENABLED + explicit --apply --confirm-apply)
   ▼
[Read-Only Analytics (src/fc27_analytics.py, src/fc27_dashboard_queries.py)]
   │  (gated by FC27_ANALYTICS_ENABLED; COMPLETED matches only; aliases off by default)
   ▼
[LangGraph StateGraph & Pre-Approval Validator (src/fc27_coaching.py)]
   │  (gated by FC27_RECOMMENDATIONS_ENABLED; pauses at single interrupt() in approval node)
   ▼
[Human Commissioner Decision (WebAuthn Step-Up or CLI HMAC-SHA256)]
   │  (atomic single-transaction commit in PostgreSQL -> Graph re-reads PostgreSQL -> APPROVED, NOT DISPATCHED)
   ▼
[Dispatch Gate Boundary (DISPATCH_ENABLED=false; G-MIGRATION..G-OUTBOX blocked)]
```

## 9. Local Development

1. Create and activate the Python 3.11+ virtual environment and install dependencies from `requirements.txt` and `requirements-dev.txt`.
2. In **Visual Studio Enterprise 2026**, open `iwa.dev.fcbuild.sln` (`iwa.dev.fcbuild.pyproj`) rather than Folder View so the IDE reuses the existing repository-root `.venv` and configures pytest discovery automatically (see [`docs/VISUAL_STUDIO_SETUP.md`](docs/VISUAL_STUDIO_SETUP.md)).
3. Copy `config.example.env` to `.env` for local configuration templates (never put database URIs or plaintext Commissioner tokens in `.env`; use `./secrets/` as documented in [`MIGRATION_GUIDE.md`](MIGRATION_GUIDE.md) and [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)).
4. Run static bytecode and boundary verification:
   ```powershell
   .\.venv\Scripts\python.exe -m compileall -q src scripts tests
   .\.venv\Scripts\python.exe scripts/check_matchpulse_boundary.py
   .\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status
   ```

## 10. Testing

- **Focused FC 27 Contracts, Cache, Rate-Limit, Community Provider, Search/Scout, Analyst, Privacy, & Discord Tests (No DB required)**:
  ```powershell
  .\.venv\Scripts\pytest.exe tests/test_fc27_clubs_api_contracts.py tests/test_fc27_api_cache.py tests/test_fc27_api_rate_limit.py tests/test_fc27_community_provider.py tests/test_fc27_search.py tests/test_fc27_scout.py tests/test_fc27_analyst.py tests/test_fc27_privacy.py tests/test_fc27_discord_bot.py -q
  ```
- **Documentation, Packaging, Workflow, & Archive Integrity Tests**:
  ```powershell
  .\.venv\Scripts\pytest.exe tests/test_documentation_consistency.py tests/test_packaging.py tests/test_github_workflows.py tests/test_archive_integrity.py -q
  ```
- **Full Suite with Ephemeral PostgreSQL 16**: See [`docs/TESTING.md`](docs/TESTING.md) and dated verification results in [`TEST_RESULTS.md`](TEST_RESULTS.md).

## 11. PostgreSQL Requirements

- **Engine**: PostgreSQL 16 (`postgres:16-alpine` pinned by digest in `docker-compose.yml`).
- **Migrations**: `migrations/0001_initial.sql` through `migrations/0013_fc27_manual_match_results.sql` + post-migration `migrations/post/grants.sql` (Grants Policy Version `15`).
- **Role Separation**: `python -m src.db_roles bootstrap --confirm` initializes `fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_match_results_writer`, `fc27_dispatch`, and `fc27_relay`. `python -m src.db_migrate` applies all migrations and `PostgresSaver` tables under `fc27_owner`.

## 12. Docker Compose Status

- **Compose Files**:
  - `docker-compose.yml` — Base topology (`postgres`, `redis`, `db-bootstrap`, and `db-migrate`).
  - `docker-compose.community-worker.yml` — Disabled-by-default 12-hour polling worker overlay (`fc27-community-worker`, `--profile fc27-polling`).
  - `docker-compose.discord.yml` — Disabled-by-default East Coasters Discord analytics and Match Results bot overlay (`fc27-discord-bot`, `--profile fc27-discord`).
  - `docker-compose.ghcr.yml` — Optional digest-pinned private GHCR deployment overlay for `fc27-community-worker` and `fc27-discord-bot` (documented in [`docs/FC27_GHCR_RUNTIME_PROMOTION.md`](docs/FC27_GHCR_RUNTIME_PROMOTION.md)).
- **Deployment Validation Status**: Static Compose model resolution and secret/network isolation are verified via `tests/test_compose_isolation.py`, and database migrations/stores are verified against live PostgreSQL 16 containers.

## 13. Feature Flags

All runtime feature flags default to disabled/fail-closed except circuit-breaker protection and dry-run protection (`FC27_API_CIRCUIT_BREAKER_ENABLED=true`, `FC27_PROVIDER_DRY_RUN=true`). See [`docs/FEATURE_FLAGS.md`](docs/FEATURE_FLAGS.md) for the complete reference:

| Flag | Default | Purpose |
|---|---|---|
| `FC27_PROVIDER_ENABLED` | `false` | Master gate for FC 27 provider adapter execution |
| `FC27_STANDARD_HTTP_TRANSPORT_ENABLED` | `false` | Gate for standards-compliant HTTPS provider transport |
| `FC27_LIVE_INGESTION_ENABLED` | `false` | Gate for live FC 27 provider ingestion |
| `FC27_API_CACHE_ENABLED` | `false` | Gate for bounded in-memory normalized object cache |
| `FC27_API_RATE_LIMIT_ENABLED` | `false` | Gate for provider token-bucket rate limiter |
| `FC27_API_CIRCUIT_BREAKER_ENABLED` | `true` | Enables fail-closed 3-state circuit breaker protection |
| `FC27_PERSISTENCE_ENABLED` | `false` | Gate for PostgreSQL normalized performance apply writes |
| `FC27_ANALYTICS_ENABLED` | `false` | Gate for read-only PostgreSQL analytics queries |
| `FC27_RECOMMENDATIONS_ENABLED` | `false` | Gate for FC 27 coaching recommendation generation |
| `FC27_PERFORMANCE_TELEMETRY_ENABLED` | `false` | Gate for local performance telemetry file retention (`.local-artifacts/performance/`) |
| `DISPATCH_ENABLED` | `false` | Master gate for player dispatch creation (must remain `false`) |
| `DISPATCH_WORKER_ENABLED` | `false` | Master gate for background dispatch worker (must remain `false`) |

## 14. Human Approval Model

1. **Deterministic Pre-Approval Validation (Historical Control-Plane Contract)**: Candidate packages are checked against sample-size gates (`>= 10` completed matches required for `PROVISIONAL_TRENDS` / `HIGH_SAMPLE_READINESS`), evidence-reference bindings, and the tactical hypothesis blocklist (`src/schemas.py`, `src/fc27_coach.py`; historical `src/fc27_coaching.py` superseded in Stage B3) before reaching human review.
2. **Single Graph Interrupt (Historical `src/graph.py` Superseded in Stage B3)**: The historical workflow paused at `interrupt()` in the `approval` node with an immutable `package_version` hash.
3. **Cryptographic Human Proof (Historical `src/identity/`, `src/approval_store.py` Superseded in Stage B3)**:
   - **WebAuthn Step-Up**: Requires hardware/biometric User Verification (`require_user_verification=True`) and binds 14 fields in `public.fc27_decision_evidence` over RFC-8785 canonical JSON.
   - **CLI HMAC-SHA256**: Binds a single-use nonce in `public.approval_nonces` signed with the shared secret `APPROVAL_SIGNING_SECRET` (historical `scripts/commissioner_decide.py` superseded in Stage B3).
4. **Atomic Commit Before Resume**: The decision, request status update, thread status update, `approval_audit` row, and `domain_events` row commit in one PostgreSQL transaction before the graph resumes.

## 15. Documentation Map

- **Index**: [`docs/README.md`](docs/README.md)
- **Architecture & Status**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/FC27_COMPONENT_STATUS.md`](docs/FC27_COMPONENT_STATUS.md), [`docs/ADR_FC27_EAST_COASTERS_PLATFORM.md`](docs/ADR_FC27_EAST_COASTERS_PLATFORM.md)
- **Development, Testing, & Operations**: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md), [`docs/VISUAL_STUDIO_SETUP.md`](docs/VISUAL_STUDIO_SETUP.md), [`docs/TESTING.md`](docs/TESTING.md), [`docs/FC27_CI_TEST_STRATEGY.md`](docs/FC27_CI_TEST_STRATEGY.md), [`docs/FC27_TEST_ISOLATION_MATRIX.md`](docs/FC27_TEST_ISOLATION_MATRIX.md), [`docs/FC27_CI_BROWSER_REPORTS.md`](docs/FC27_CI_BROWSER_REPORTS.md), [`docs/FC27_CONTAINER_PUBLISHING.md`](docs/FC27_CONTAINER_PUBLISHING.md), [`docs/FC27_GHCR_RUNTIME_PROMOTION.md`](docs/FC27_GHCR_RUNTIME_PROMOTION.md), [`docs/OPERATIONS.md`](docs/OPERATIONS.md), [`MIGRATION_GUIDE.md`](MIGRATION_GUIDE.md), [`TEST_RESULTS.md`](TEST_RESULTS.md)
- **Security & Feature Flags**: [`docs/SECURITY_BOUNDARIES.md`](docs/SECURITY_BOUNDARIES.md), [`docs/FEATURE_FLAGS.md`](docs/FEATURE_FLAGS.md), [`docs/APPROVAL_SECURITY.md`](docs/APPROVAL_SECURITY.md), [`docs/DEPLOYMENT_AND_SECURITY.md`](docs/DEPLOYMENT_AND_SECURITY.md), [`SECURITY.md`](SECURITY.md)
- **Provider, Data Model, Analytics, Polling, Discord, Approval, & Hypotheses**: [`docs/FC27_PROVIDER_INTEGRATION.md`](docs/FC27_PROVIDER_INTEGRATION.md), [`docs/FC27_POLLING_WORKER.md`](docs/FC27_POLLING_WORKER.md), [`docs/FC27_DAILY_POLLING_OPERATIONS.md`](docs/FC27_DAILY_POLLING_OPERATIONS.md), [`docs/FC27_DISCORD_BOT.md`](docs/FC27_DISCORD_BOT.md), [`docs/FC27_DATA_MODEL.md`](docs/FC27_DATA_MODEL.md), [`docs/FC27_ANALYTICS.md`](docs/FC27_ANALYTICS.md), [`docs/FC27_APPROVAL_FLOW.md`](docs/FC27_APPROVAL_FLOW.md), [`docs/FC27_TACTICAL_HYPOTHESES.md`](docs/FC27_TACTICAL_HYPOTHESES.md)
- **Dispatch, Player Identity, & Archive**: [`docs/DISPATCH_ACTIVATION.md`](docs/DISPATCH_ACTIVATION.md), [`docs/EA_PLAYER_IDENTITY_VERIFICATION.md`](docs/EA_PLAYER_IDENTITY_VERIFICATION.md), [`docs/archive/README.md`](docs/archive/README.md)

## 16. Current Limitations

- **No Approved Live EA Provider Transport**: Direct requests to `proclubs.ea.com` return HTTP 403 without browser/TLS impersonation (which is rejected). Live provider ingestion remains blocked (`SELECTED_PROVIDER=NONE`) pending formal EA written authorization (`EA_APPROVED_ACCESS_REQUEST_DRAFT`) or an approved licensed partner API.
- **Unverified Tactical Hypotheses**: External FC 27 meta claims (`4-2-1-3`, `4-1-2-1-2 Narrow`, role/attribute thresholds, PlayStyle+ claims, tactics codes) have not been validated on East Coasters match data (`EAST_COASTERS_VALIDATION=NOT_RUN`) and are non-approvable.
- **Target-Host Stack Validation**: Full multi-container Docker Compose runtime with live GPU/CPU Ollama inference and optional voice/RTSP profiles is `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED`.

## 17. Next Development Phase

Following the Search and Scout Analytical Foundation (`src/fc27_search_scout.py`) and Analyst Evidence Synthesis (`src/fc27_analyst.py`), the ordered roadmap is:
1. **Coach Hypothesis Generation** (next phase)
2. **Tactics Candidate Construction**
3. **Director Conflict Resolution**
4. **Recommendation Review**
5. **Separate Dispatch-Activation Review**
6. **Live-Provider Integration** (only if and when a compliant, authorized transport exists)
