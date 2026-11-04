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
  -> existing local dashboard & JSON API (src/fc27_demo_api.py:create_demo_app) + optional read-only Discord bot (src/fc27_discord_bot.py)
```

1. **Working Provider & How to Run It (`scripts/fc27_community_eval.py`)**:
   - Provider ID: `fc-clubs-api-community-v1` (`LIVE_EA_CLUBS_COMMUNITY_CLIENT`, documented in [`docs/FC27_COMMUNITY_PROVIDER_EVALUATION.md`](docs/FC27_COMMUNITY_PROVIDER_EVALUATION.md)).
   - Run deterministic offline verification (zero network requests):
     ```powershell
     .\.venv\Scripts\python.exe scripts/fc27_community_eval.py --operation mock-eval
     ```
2. **Start Disposable PostgreSQL 16 & Run Persistence / Analytics (`Search -> Scout -> Analyst`)**:
   ```powershell
   docker run -d --name fc27-test-postgres -e POSTGRES_USER=fc27test -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 postgres:16 -c max_connections=300
   $env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
   $env:TEST_POSTGRES_ADMIN_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
   .\.venv\Scripts\python.exe scripts/fc27_demo_ingest.py --source fixture --operation full-demo --scenario valid
   ```
3. **Start the Existing Local Dashboard (`src.fc27_demo_api:create_demo_app`)**:
   ```powershell
   $env:FC27_DEMO_MODE = "true"
   .\.venv\Scripts\uvicorn.exe src.fc27_demo_api:create_demo_app --factory --host 127.0.0.1 --port 8001
   # Open http://127.0.0.1:8001/demo , /demo/status , /demo/matches , /demo/analysis
   ```
4. **Run Tests**:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/test_fc27_community_provider.py tests/test_fc27_pandas_view.py tests/test_fc27_demo.py -q
   ```
5. **Disabled Features & Historical Material**:
   - Continuous polling, browser scraping, raw response storage, player aliases/handles, autonomous Coach/Tactics/Director recommendations (`FC27_RECOMMENDATIONS_ENABLED=false`), and player dispatch (`DISPATCH_ENABLED=false`) remain **disabled**.
   - Historical and superseded documents/scripts live in [`docs/archive/README.md`](docs/archive/README.md), [`docs/archive/ARCHIVE_MANIFEST.md`](docs/archive/ARCHIVE_MANIFEST.md), and `scripts/archive/` (indexed in [`scripts/README.md`](scripts/README.md)).

## 1. Project Purpose

`iwa.dev.fcbuild` is a security-hardened, human-in-the-loop football analytics, tactical evaluation, and decision-support platform built for **EA Sports FC 27 Pro Clubs**. Throughout the platform, **models advise and humans decide**: autonomous agents and analytical pipelines may summarize evidence and propose structured candidates, but only an authenticated human Commissioner (or explicitly authorized human delegate/player within scoped boundaries) can record an authoritative decision.

## 2. Current Supported Use Case

- **Primary Architecture (API-First Provider Boundary)**: Provider-neutral FC 27 endpoint contracts (`src/fc27_clubs_api_contracts.py`), normalized-object caching (`src/fc27_api_cache.py`), conservative rate/budget/circuit-breaker controls (`src/fc27_api_rate_limit.py`), and the 18-step `FC27AuthorizedProviderAdapter` pipeline (`src/fc27_authorized_provider.py`) are implemented and verified against hermetic mocks, ready for a compliant authorized API provider.
- **Secondary Offline Fallback**: Consented user-export (`USER_SUPPLIED_EXPORT`) and operator-captured displayed data (`OPERATOR_CAPTURED_DISPLAYED_DATA`) validation and dry-run workflows (`src/fc27_user_export.py`, `src/fc27_capture.py`) are available for offline validation, but manual JSON/CSV files are **not** the primary long-term architecture.
- **PostgreSQL 16 Governance & Audit Foundation**: Role-separated PostgreSQL 16 storage (`migrations/0001_initial.sql`–`migrations/0012_fc27_performance_and_quarantine.sql`, Grants Policy Version `14`), LangGraph `StateGraph` orchestration with single `interrupt()` approval boundaries, deterministic pre-approval validation (`src/fc27_coaching.py`), and Phase 2 WebAuthn/HMAC identity and evidence binding (`src/identity/`, `src/approval_store.py`) are implemented and verified against local PostgreSQL 16.

## 3. East Coasters Identity

| Attribute | Value | Governance Status |
|---|---|---|
| **Canonical Club Name** | `East Coasters` (aliases: `EastCoasters FC`, `ECF`) | Target Club |
| **Club ID (`FC27_CLUB_ID`)** | `330323` | `CORROBORATED` / `PROVISIONAL` (not live-provider `VERIFIED`) |
| **Platform (`FC_PLATFORM`)** | `common-gen5` | Target Platform |
| **Game Version** | `FC27` (`fc_version: "27"`) | Target Edition |
| **Manager Authority Status** | `UNVERIFIED` | Club identity never self-grants human authority |

## 4. Current Architecture

- **`graph-api` (`src/graph_api.py`, `src/graph.py`)**: FastAPI HTTP boundary and LangGraph `StateGraph` (`search` -> `scout` -> `analyst` -> `coach` -> `tactics` -> `director` -> `prepare_approval` -> `approval` [`interrupt()`] -> `finalize_decision`).
- **PostgreSQL 16 (`migrations/0001_initial.sql`–`migrations/0012_fc27_performance_and_quarantine.sql`, `migrations/post/grants.sql`)**: Single authoritative audit of record across `graph_threads`, `recommendation_packages`, `approval_requests`, `approval_audit`, `fc27_decision_evidence`, `domain_events`, `outbox`, identity tables (`fc27_users`, `fc27_sessions`, `fc27_webauthn_credentials`, `fc27_challenges`), and normalized FC 27 performance tables (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`). LangGraph `PostgresSaver` tables (`checkpoints*`) store workflow state only and are strictly separated from normalized FC 27 tables and authorization state.
- **Redis (`redis:7.4.2-alpine`)**: Ephemeral pub/sub and watcher lease transport only. **Redis is never audit of record or authorization authority.**
- **Safe FC 27 Provider & Contract Layer (`src/fc27_authorized_provider.py`, `src/fc27_clubs_api_contracts.py`, `src/fc27_api_cache.py`, `src/fc27_api_rate_limit.py`, `config/fc27_safe_adapter_bundle_manifest.json`)**: Repository-native data-only adaptation (`fc27-safe-adapter-manifest-v1`) of fixed FC 27 endpoint contracts, strict Pydantic response schemas, 23-bit match event bitmap (`ev`) decoding, bounded normalized-object caching, token-bucket rate limiting, per-run request budgets (`max_attempts=1`, `max_retries=0`), and a 3-state circuit breaker.

## 5. Implemented Capabilities

1. **Least-Privilege PostgreSQL 16 Foundation**: Versioned migrations (`0001`–`0012`) and Grants Policy Version `14` with separated `fc27_owner` (`NOLOGIN`), `fc27_migrator`, `fc27_app` (DML-only, zero DDL, zero `DELETE` on FC 27 tables), `fc27_dispatch`, and `fc27_relay` roles.
2. **LangGraph Single-Interrupt Approval Workflow**: Persisted recommendation packages (`package_version` SHA-256), single `interrupt()` at node `approval`, and mandatory PostgreSQL decision re-read on resume (`ApprovalNotCommitted` if missing or mismatched).
3. **Human Identity, WebAuthn Step-Up, & CLI HMAC Verification**: Phase 2 user/session/passkey/TOTP/delegation store (`src/identity/`), 14-field bound `fc27_decision_evidence` with mandatory WebAuthn User Verification (`require_user_verification=True`), and constant-time symmetric HMAC-SHA256 verification (`src/approval.py`, `scripts/commissioner_decide.py`).
4. **FC 27 Safe Provider Adapter & Contracts**: 11 fixed endpoint operations, 13 strict response schemas, match event bitmap (`ev`) decoder, `plr_ecf_<12-hex>` pseudonymization with collision detection (`PSEUDONYM_COLLISION`), `FC27SourceProvenance`, bounded normalized cache, token-bucket rate limiter, request budget, circuit breaker, and pure retry classifier.
5. **Normalized Performance Storage, Read-Only Analytics, Search/Scout Foundation, & Analyst Evidence Synthesis**: Repository persistence (`src/fc27_repository.py`), pure deterministic analytics (`src/fc27_analytics.py`, `FORMULA_VERSION = "fc27-v1.0"`), deterministic Search context assembly and Scout evidence/hypothesis synthesis (`src/fc27_search_scout.py`, `FC27AnalyticsReader`, `search_node`, `scout_node`), deterministic Analyst evidence validation, clustering, counter-evidence, confounder/uncertainty analysis, and bounded observational finding synthesis (`src/fc27_analyst.py`, `analyst_evidence_node`), read-only PostgreSQL dashboard queries (`src/fc27_dashboard_queries.py`, `READ ONLY`, `5000ms` statement timeout), and deterministic pre-approval coaching validation (`src/fc27_coaching.py`).
6. **Local FC 27 One-Shot Ingestion & Analytics Demo**: Loopback synthetic FC 27 fixture server (`scripts/fc27_demo_fixture_server.py`), one-shot ingestion CLI (`scripts/fc27_demo_ingest.py`), vertical demo pipeline (`src/fc27_demo.py`), and loopback-only Demo API & self-contained HTML inspection view (`src/fc27_demo_api.py`, canonical ASGI factory `src.fc27_demo_api:create_demo_app`, gated by `FC27_DEMO_MODE=false` by default; documented in [`docs/FC27_DEMO.md`](docs/FC27_DEMO.md)).
7. **Disabled-by-Default FC 27 Community Polling Worker (`fc27-community-worker`)**: Production-shaped background polling worker (`src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`, `Dockerfile.community-worker`, `docker-compose.community-worker.yml`; documented in [`docs/FC27_POLLING_WORKER.md`](docs/FC27_POLLING_WORKER.md) and [`docs/FC27_DAILY_POLLING_OPERATIONS.md`](docs/FC27_DAILY_POLLING_OPERATIONS.md)) with single-flight PostgreSQL advisory locking, 12-hour intervals (`POLL_INTERVAL_SECONDS=43200`, `ONCE_EVERY_12_HOURS`, max 2 requests/24h), 1-request/0-retry budgets, transactional persistence before analytics, provider window continuity tracking, and full advisory analytics (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`) on new matches only (`FC27_COMMUNITY_POLLING_ENABLED=false` by default).
8. **Local Database Backup & Restore Verification (`src/fc27_backup_restore.py`)**: Custom-format `pg_dump -Fc` domain backups (`scripts/fc27_backup.py`), 7-backup retention, SHA-256 manifest verification, and disposable temporary-database restore verification (`scripts/fc27_restore_verify.py`; documented in [`docs/FC27_BACKUP_AND_RECOVERY.md`](docs/FC27_BACKUP_AND_RECOVERY.md)).
9. **Read-Only East Coasters Discord Analytics Bot (`src/fc27_discord_bot.py`)**: Disabled-by-default (`FC27_DISCORD_BOT_ENABLED=false`), slash-command-only read-only bot (`scripts/fc27_discord_bot.py`, `Dockerfile.discord`, `docker-compose.discord.yml`; documented in [`docs/FC27_DISCORD_BOT.md`](docs/FC27_DISCORD_BOT.md)) with zero provider authority (`DISCORD_PROVIDER_REQUESTS=0`), zero database write access, and guild/channel allowlists.
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
2. Copy `config.example.env` to `.env` for local configuration templates (never put database URIs or plaintext Commissioner tokens in `.env`; use `./secrets/` as documented in [`MIGRATION_GUIDE.md`](MIGRATION_GUIDE.md) and [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)).
3. Run static bytecode and boundary verification:
   ```powershell
   .\.venv\Scripts\python.exe -m compileall -q src scripts tests
   .\.venv\Scripts\python.exe scripts/check_matchpulse_boundary.py
   .\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py
   ```

## 10. Testing

- **Focused FC 27 Adapter, Manifest, Cache, Rate-Limit, Provider, Search/Scout, Analyst, & Demo Tests (No DB required)**:
  ```powershell
  .\.venv\Scripts\pytest.exe tests/test_fc27_safe_adapter_bundle_manifest.py tests/test_fc27_clubs_api_contracts.py tests/test_fc27_api_cache.py tests/test_fc27_api_rate_limit.py tests/test_fc27_authorized_provider.py tests/test_fc27_authorized_api.py tests/test_fc27_search.py tests/test_fc27_scout.py tests/test_fc27_analyst.py tests/test_fc27_demo.py -q
  ```
- **Documentation, Packaging, Workflow, & Archive Integrity Tests**:
  ```powershell
  .\.venv\Scripts\pytest.exe tests/test_documentation_consistency.py tests/test_packaging.py tests/test_workflow.py tests/test_archive_integrity.py -q
  ```
- **Full Suite with Ephemeral PostgreSQL 16**: See [`docs/TESTING.md`](docs/TESTING.md) and dated verification results in [`TEST_RESULTS.md`](TEST_RESULTS.md).

## 11. PostgreSQL Requirements

- **Engine**: PostgreSQL 16 (`postgres:16-alpine` pinned by digest in `docker-compose.yml`).
- **Migrations**: `migrations/0001_initial.sql` through `migrations/0012_fc27_performance_and_quarantine.sql` + post-migration `migrations/post/grants.sql` (Grants Policy Version `14`).
- **Role Separation**: `python -m src.db_roles bootstrap --confirm` initializes `fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`, and `fc27_relay`. `python -m src.db_migrate` applies all migrations and `PostgresSaver` tables under `fc27_owner`.

## 12. Docker Compose Status

- **Compose Files**:
  - `docker-compose.yml` — Base topology (`postgres`, `redis`, `db-bootstrap`, `db-migrate`, `ollama`, `ollama-init`, `fc27-mcp`, `graph-api`, `live-watcher`, `post-match-rundown`, `ws-server`, plus optional `--profile rtsp` and `--profile voice` services documented in `docs/VOICE_CAPABILITIES.md`).
  - `docker-compose.gpu.yml` — NVIDIA GPU reservation overlay for `ollama`.
  - `docker-compose.cpu.yml` — CPU-only overlay for `ollama`.
  - `docker-compose.langfuse.yml` — Optional self-hosted Langfuse observability overlay (`--profile langfuse`).
  - `docker-compose.voice-gpu.yml` — Optional GPU overlay for Faster Whisper STT (`--profile voice`).
- **Deployment Validation Status**: Static Compose model resolution and secret/network isolation are verified via `tests/test_compose_isolation.py`, and database migrations/stores are verified against live PostgreSQL 16 containers. Full multi-container target-host deployment (`ollama`, `fc27-mcp`, `live-watcher`, `ws-server`, `voice`) is `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED`.

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
| `LANGFUSE_CAPTURE_CONTENT` | `false` | Controls whether Langfuse traces capture prompt/candidate text |

## 14. Human Approval Model

1. **Deterministic Pre-Approval Validation (`src/fc27_coaching.py`)**: Candidate packages are checked against sample-size gates (`>= 10` completed matches required for `PROVISIONAL_TRENDS` / `HIGH_SAMPLE_READINESS`), evidence-reference bindings, and the tactical hypothesis blocklist before reaching human review.
2. **Single Graph Interrupt (`src/graph.py`)**: The workflow pauses at `interrupt()` in the `approval` node with an immutable `package_version` hash.
3. **Cryptographic Human Proof (`src/identity/`, `src/approval_store.py`)**:
   - **WebAuthn Step-Up**: Requires hardware/biometric User Verification (`require_user_verification=True`) and binds 14 fields in `public.fc27_decision_evidence` over RFC-8785 canonical JSON.
   - **CLI HMAC-SHA256**: Binds a single-use nonce in `public.approval_nonces` signed with the shared secret `APPROVAL_SIGNING_SECRET` (`scripts/commissioner_decide.py`).
4. **Atomic Commit Before Resume**: The decision, request status update, thread status update, `approval_audit` row, and `domain_events` row commit in one PostgreSQL transaction before the graph resumes.

## 15. Documentation Map

- **Index**: [`docs/README.md`](docs/README.md)
- **Architecture & Status**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/FC27_COMPONENT_STATUS.md`](docs/FC27_COMPONENT_STATUS.md), [`docs/ADR_FC27_EAST_COASTERS_PLATFORM.md`](docs/ADR_FC27_EAST_COASTERS_PLATFORM.md)
- **Development, Testing, & Operations**: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md), [`docs/TESTING.md`](docs/TESTING.md), [`docs/OPERATIONS.md`](docs/OPERATIONS.md), [`MIGRATION_GUIDE.md`](MIGRATION_GUIDE.md), [`TEST_RESULTS.md`](TEST_RESULTS.md)
- **Security & Feature Flags**: [`docs/SECURITY_BOUNDARIES.md`](docs/SECURITY_BOUNDARIES.md), [`docs/FEATURE_FLAGS.md`](docs/FEATURE_FLAGS.md), [`docs/APPROVAL_SECURITY.md`](docs/APPROVAL_SECURITY.md), [`docs/DEPLOYMENT_AND_SECURITY.md`](docs/DEPLOYMENT_AND_SECURITY.md), [`SECURITY.md`](SECURITY.md)
- **Provider, Data Model, Analytics, Demo, Approval, & Hypotheses**: [`docs/FC27_PROVIDER_INTEGRATION.md`](docs/FC27_PROVIDER_INTEGRATION.md), [`docs/FC27_COMMUNITY_PROVIDER_EVALUATION.md`](docs/FC27_COMMUNITY_PROVIDER_EVALUATION.md), [`docs/FC27_DATA_MODEL.md`](docs/FC27_DATA_MODEL.md), [`docs/FC27_ANALYTICS.md`](docs/FC27_ANALYTICS.md), [`docs/FC27_DEMO.md`](docs/FC27_DEMO.md), [`docs/FC27_APPROVAL_FLOW.md`](docs/FC27_APPROVAL_FLOW.md), [`docs/FC27_TACTICAL_HYPOTHESES.md`](docs/FC27_TACTICAL_HYPOTHESES.md)
- **Dispatch, Offline Export, Voice, & Archive**: [`docs/DISPATCH_ACTIVATION.md`](docs/DISPATCH_ACTIVATION.md), [`docs/EA_PLAYER_IDENTITY_VERIFICATION.md`](docs/EA_PLAYER_IDENTITY_VERIFICATION.md), [`docs/FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md`](docs/FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md), [`docs/VOICE_CAPABILITIES.md`](docs/VOICE_CAPABILITIES.md), [`docs/archive/README.md`](docs/archive/README.md)

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
