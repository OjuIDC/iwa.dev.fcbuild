# FC 27 Component Status Classification

- **Status**: Current
- **Audience**: Developers, Operators, Security Reviewers
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `src/`, `migrations/`, `docker-compose.yml`, `tests/`
- **Supersedes**: `NONE`

---

## 1. Allowed Status Classifications (DC11)

Every component in `iwa.dev.fcbuild` is classified using one of the nine canonical architecture status values:

- `IMPLEMENTED` — Implemented in active repository source code and verified by automated tests.
- `PARTIAL` — Partially implemented; requires additional planned integration before end-to-end use.
- `PROPOSED` — Planned for a future phase; not yet implemented.
- `DEFERRED` — Intentionally postponed or held behind multi-gate activation criteria.
- `DISABLED` — Implemented with fail-closed gates that default to disabled (`false`).
- `REJECTED` — Evaluated and permanently prohibited for security, licensing, or architectural reasons.
- `SUPERSEDED` — Replaced by a newer active module or architecture.
- `ARCHIVED` — Historical provenance no longer part of active runtime.
- `NOT_FOUND` — Referenced externally but absent from this repository.

> **Deployment Validation Note**: Where code and container definitions exist and pass static/local tests but full target-host deployment has not been executed, the Deployment Note column records `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED`.

---

## 2. Authoritative Component Status Table

| Component | Status | Deployment Note | Source Evidence | Test Evidence | Default Runtime Gate / Profile |
|---|---|---|---|---|---|
| `graph-api` | `SUPERSEDED` | Removed in Stage B3; superseded by `fc27-discord-bot` and `fc27-community-worker` | `Dockerfile.graph`, `docker-compose.yml` | `tests/test_compose_isolation.py` | Removed (`CANDIDATE_DELETED_IN_B3`) |
| `LangGraph StateGraph` | `SUPERSEDED` | Removed in Stage B3; superseded by deterministic `src/fc27_analyst.py`, `src/fc27_coach.py`, `src/fc27_tactics.py`, `src/fc27_director.py` | `src/fc27_analyst.py`, `src/fc27_coach.py`, `src/fc27_tactics.py`, `src/fc27_director.py` | `tests/test_fc27_advisory_pipeline_e2e.py` | Removed (`CANDIDATE_DELETED_IN_B3`) |
| `PostgresSaver` | `SUPERSEDED` | Removed in Stage B3; `db_migrate.py` defaults to `checkpoint_setup=False` | `src/db_migrate.py` | `tests/test_db_migrate_roles.py` | Separated from normalized `fc27_*` tables |
| `normalized FC27 PostgreSQL storage` | `IMPLEMENTED` | Verified against PostgreSQL 16 | `migrations/0012_fc27_performance_and_quarantine.sql`, `src/fc27_repository.py` | `tests/test_fc27_persistence_and_queries.py` | `FC27_PERSISTENCE_ENABLED=false` |
| `read-only analytics` | `IMPLEMENTED` | Verified against PostgreSQL 16 | `src/fc27_analytics.py`, `src/fc27_dashboard_queries.py`, `src/fc27_search_scout.py`, `src/fc27_analyst.py` | `tests/test_fc27_dashboard_queries.py`, `tests/test_fc27_persistence_and_queries.py`, `tests/test_fc27_search.py`, `tests/test_fc27_scout.py`, `tests/test_fc27_analyst.py` | `FC27_ANALYTICS_ENABLED=false` |
| `Search` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (pure deterministic context + reader adapter) | `src/fc27_search_scout.py` | `tests/test_fc27_search.py`, `tests/test_fc27_search_scout_graph.py` | Pure / Read-only (`FC27_ANALYTICS_ENABLED=false` for PG reader) |
| `Scout` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (pure deterministic evidence + hypotheses) | `src/fc27_search_scout.py` | `tests/test_fc27_scout.py`, `tests/test_fc27_search_scout_graph.py` | Pure / Observational only (`OBSERVATIONAL_ONLY`) |
| `Analyst` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (pure deterministic Analyst evidence synthesis + graph adapter) | `src/fc27_analyst.py` | `tests/test_fc27_analyst.py`, `tests/test_fc27_analyst_graph.py` | Pure / Observational only (`OBSERVATIONAL_ONLY`, `NOT_A_RECOMMENDATION`) |
| `FC27 Demo Fixture Server` | `SUPERSEDED` | Removed in Stage B4 | `src/fc27_discord_bot.py` | `tests/test_fc27_discord_bot.py` | Removed (`CANDIDATE_DELETED_IN_B4`) |
| `FC27 One-Shot Demo CLI` | `SUPERSEDED` | Removed in Stage B4; superseded by `scripts/fc27_polling_worker.py` | `scripts/fc27_polling_worker.py` | `tests/test_fc27_polling_worker.py` | Removed (`CANDIDATE_DELETED_IN_B4`) |
| `FC27 Demo API` | `SUPERSEDED` | Removed in Stage B4; polling and repository status exposed via `/fc27 status` in `src/fc27_discord_bot.py` | `src/fc27_discord_bot.py` | `tests/test_fc27_discord_bot.py` | Removed (`CANDIDATE_DELETED_IN_B4`) |
| `FC27 Live Probe` | `SUPERSEDED` | Removed in Stage B4 (`src/fc27_demo.py` removed) | `src/fc27_community_provider.py` | `tests/test_fc27_community_provider.py` | Removed (`CANDIDATE_DELETED_IN_B4`) |
| `Coach` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (deterministic observational Coach hypothesis engine; `recommendation=false`) | `src/fc27_coach.py` | `tests/test_fc27_coach.py`, `tests/test_fc27_advisory_pipeline_e2e.py` | Pure / Observational only (`NOT_A_RECOMMENDATION`) |
| `Coach recommendation activation` | `DISABLED` | Gated fail-closed by default (`src/fc27_coaching.py` removed in Stage B3) | `src/settings.py` | `tests/test_fc27_coach.py` | `FC27_RECOMMENDATIONS_ENABLED=false` |
| `Tactics` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (deterministic experimental Tactics candidate engine; `recommendation=false`) | `src/fc27_tactics.py` | `tests/test_fc27_tactics.py`, `tests/test_fc27_advisory_pipeline_e2e.py` | Pure / Experimental only (`EXPERIMENTAL_ONLY`) |
| `Director` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (deterministic Director evidence reconciliation engine; `recommendation=false`) | `src/fc27_director.py` | `tests/test_fc27_director.py`, `tests/test_fc27_advisory_pipeline_e2e.py` | Pure / Reconciliation only (`RECONCILIATION_ONLY_HUMAN_REVIEW_REQUIRED`) |
| `pre-approval validator` | `SUPERSEDED` | Removed in Stage B3 (`src/fc27_coaching.py` removed) | `src/settings.py`, `src/schemas.py` | `tests/test_fc27_coach.py` | `FC27_RECOMMENDATIONS_ENABLED=false` |
| `provider-neutral boundary` | `SUPERSEDED` | Superseded by `src/fc27_community_provider.py` and `src/fc27_clubs_api_contracts.py` | `src/fc27_provider.py`, `src/fc27_community_provider.py`, `src/fc27_clubs_api_contracts.py` | `tests/test_fc27_community_provider.py`, `tests/test_fc27_clubs_api_contracts.py` | `FC27_PROVIDER_ENABLED=false` |
| `endpoint contracts` | `IMPLEMENTED` | Verified in unit suites | `src/fc27_clubs_api_contracts.py` | `tests/test_fc27_clubs_api_contracts.py` | `FC27_UPSTREAM_COMPATIBILITY_ENABLED=false` |
| `safe adapter bundle manifest` | `SUPERSEDED` | Superseded by `src/fc27_clubs_api_contracts.py` and `THIRD_PARTY_NOTICES.md` | `src/fc27_clubs_api_contracts.py`, `THIRD_PARTY_NOTICES.md` | `tests/test_fc27_clubs_api_contracts.py` | `FC27_PROVIDER_ENABLED=false` |
| `cache` | `DISABLED` | Verified in unit suites | `src/fc27_api_cache.py` | `tests/test_fc27_api_cache.py` | `FC27_API_CACHE_ENABLED=false` |
| `rate limiter` | `DISABLED` | Verified in unit suites | `src/fc27_api_rate_limit.py` | `tests/test_fc27_api_rate_limit.py` | `FC27_API_RATE_LIMIT_ENABLED=false` |
| `request budget` | `IMPLEMENTED` | Verified in unit suites | `src/fc27_api_rate_limit.py` | `tests/test_fc27_api_rate_limit.py` | Enforces `max_attempts=1`, `max_retries=0` |
| `circuit breaker` | `IMPLEMENTED` | Verified in unit suites | `src/fc27_api_rate_limit.py` | `tests/test_fc27_api_rate_limit.py` | `FC27_API_CIRCUIT_BREAKER_ENABLED=true` (starts `OPEN`) |
| `operator capture` | `SUPERSEDED` | Removed in Stage B4; privacy helpers retained in `src/fc27_privacy.py` and neutral persistence structures retained in `src/fc27_repository.py` | `src/fc27_privacy.py`, `src/fc27_repository.py` | `tests/test_fc27_privacy.py`, `tests/test_fc27_persistence_and_queries.py` | Removed (`CANDIDATE_DELETED_IN_B4`) |
| `browser-profile research sidecar` | `REJECTED` | Excluded from `main` | `src/fc27_clubs_api_contracts.py` (`PROHIBITED_HEADER_NAMES`) | `tests/test_fc27_clubs_api_contracts.py` | Prohibited (`allow_browser_impersonation=False`) |
| `fc27-mcp` | `SUPERSEDED` | Removed in Stage B2; superseded by `src/fc27_community_provider.py` | `src/fc27_community_provider.py` | `tests/test_fc27_community_provider.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `live-watcher` | `SUPERSEDED` | Removed in Stage B2; superseded by `src/fc27_polling_worker.py` | `src/fc27_polling_worker.py` | `tests/test_fc27_polling_worker.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `post-match-rundown` | `SUPERSEDED` | Removed in Stage B2; superseded by `src/fc27_match_results.py` and `src/fc27_discord_bot.py` | `src/fc27_match_results.py`, `src/fc27_discord_bot.py` | `tests/test_fc27_match_results.py`, `tests/test_fc27_discord_bot.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `Redis event bus` | `IMPLEMENTED` | Non-authoritative cache/bus | `src/events.py`, `docker-compose.yml` | `tests/test_compose_isolation.py` | `127.0.0.1:6379` (`fc27_net` only) |
| `ws-server` | `SUPERSEDED` | Removed in Stage B2 | `docker-compose.yml` | `tests/test_compose_isolation.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `Ollama` | `SUPERSEDED` | Removed in Stage B2; deterministic role execution in `src/fc27_analyst.py`, `src/fc27_coach.py`, `src/fc27_tactics.py`, `src/fc27_director.py` | `src/fc27_analyst.py`, `docs/FC27_LOCAL_INFERENCE_BOUNDARY.md` | `tests/test_fc27_analyst.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `LM Studio` | `SUPERSEDED` | Removed in Stage B2 | `src/fc27_analyst.py`, `docs/FC27_LOCAL_INFERENCE_BOUNDARY.md` | `tests/test_fc27_analyst.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `Swarms backend` | `SUPERSEDED` | Removed in Stage B2; deterministic role execution in `src/fc27_analyst.py`, `src/fc27_coach.py`, `src/fc27_tactics.py`, `src/fc27_director.py` | `src/fc27_analyst.py` | `tests/test_fc27_analyst.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `Windows Capture Agent` | `SUPERSEDED` | Removed in Stage B2 | `src/fc27_privacy.py` | `tests/test_fc27_privacy.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `RTSP OCR` | `SUPERSEDED` | Removed in Stage B2 | `src/fc27_privacy.py` | `tests/test_fc27_privacy.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `Voice Profile` | `SUPERSEDED` | Removed in Stage B2 | `docker-compose.yml` | `tests/test_compose_isolation.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `Langfuse` | `SUPERSEDED` | Removed in Stage B2 | `docker-compose.yml` | `tests/test_compose_isolation.py` | Removed (`CANDIDATE_DELETED_IN_B2`) |
| `Commissioner CLI` | `SUPERSEDED` | Removed in Stage B3 (`scripts/commissioner_decide.py` removed) | `src/settings.py` | `tests/test_fc27_platform.py` | `APPROVAL_CLI_HMAC_ENABLED=false` in Python defaults |
| `HMAC verification` | `SUPERSEDED` | Removed in Stage B3 (`src/approval.py` removed) | `src/settings.py` | `tests/test_fc27_platform.py` | Requires `APPROVAL_SIGNING_SECRET` (`>= 32` chars) |
| `WebAuthn` | `SUPERSEDED` | Removed in Stage B3 (`src/identity/webauthn.py` removed; schema tables retained in `migrations/0007_decision_evidence.sql`, `migrations/0009_r3_evidence_binding.sql`) | `migrations/0007_decision_evidence.sql`, `migrations/0009_r3_evidence_binding.sql` | `tests/test_db_migrate_roles.py` | Removed (`CANDIDATE_DELETED_IN_B3`) |
| `identity` | `SUPERSEDED` | Removed in Stage B3 (`src/identity/` removed; schema tables retained in `migrations/0004_identity_foundation.sql`..`migrations/0009_r3_evidence_binding.sql`) | `migrations/0004_identity_foundation.sql`, `migrations/0009_r3_evidence_binding.sql` | `tests/test_db_migrate_roles.py` | Removed (`CANDIDATE_DELETED_IN_B3`) |
| `recommendations` | `DISABLED` | Gated by `FC27_RECOMMENDATIONS_ENABLED=false` (`src/fc27_coaching.py` removed in Stage B3) | `src/settings.py` | `tests/test_fc27_coach.py` | `FC27_RECOMMENDATIONS_ENABLED=false` |
| `dispatch` | `DISABLED` | Removed runtime in Stage B3 (`src/dispatch/` removed; schema tables retained in `migrations/0003_dispatch_foundation.sql`, `migrations/0010_outbox_claim_ownership.sql`, `migrations/0011_dispatch_worker_claims.sql`) | `migrations/0003_dispatch_foundation.sql`, `migrations/0010_outbox_claim_ownership.sql`, `migrations/0011_dispatch_worker_claims.sql`, `docs/DISPATCH_ACTIVATION.md` | `tests/test_db_migrate_roles.py` | `DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false` |
| `continuous polling` | `REJECTED` | Prohibited by request budget | `src/fc27_api_rate_limit.py` (`FC27RequestBudget`, `max_attempts=1`) | `tests/test_fc27_api_rate_limit.py` | Prohibited (`max_attempts=1`) |
| `live EA ingestion` | `DEFERRED` | Blocked by default (`FC27_LIVE_INGESTION_ENABLED=false`) | `src/fc27_community_provider.py`, `src/fc27_polling_worker.py` | `tests/test_fc27_community_provider.py`, `tests/test_fc27_polling_worker.py` | `FC27_LIVE_INGESTION_ENABLED=false` |
| `FC27 Community Client Evaluation` | `SUPERSEDED` | Standalone evaluation CLI/container removed in Stage B4; community provider retained in `src/fc27_community_provider.py` and `src/fc27_polling_worker.py` | `src/fc27_community_provider.py`, `src/fc27_polling_worker.py` | `tests/test_fc27_community_provider.py`, `tests/test_fc27_polling_worker.py` | `FC27_COMMUNITY_PROVIDER_ENABLED=false` (`LIVE_EA_CLUBS_COMMUNITY_CLIENT`) |
| `FC27 Community Polling Worker` | `DISABLED` | `IMPLEMENTED_AND_TESTED` (`IMPLEMENTED_AND_TESTED_DISABLED_BY_DEFAULT`: disabled-by-default single-flight 12-hour polling worker + opt-in `fc27-polling` container and digest-pinned `docker-compose.ghcr.yml` overlay) | `src/fc27_polling_worker.py`, `scripts/fc27_polling_worker.py`, `Dockerfile.community-worker`, `docker-compose.community-worker.yml`, `docker-compose.ghcr.yml` | `tests/test_fc27_polling_worker.py`, `tests/test_compose_isolation.py` | `FC27_COMMUNITY_POLLING_ENABLED=false` (`--profile fc27-polling`, `43200s`) |
| `FC27 Database Backup & Restore` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (local `.local-data/backups/` custom-format backup covering provider domain tables, migration ledger, and manual Match Results tables, 7-backup retention, and disposable restore verification) | `src/fc27_backup_restore.py`, `scripts/fc27_backup.py`, `scripts/fc27_restore_verify.py` | `tests/test_fc27_backup_restore.py` | Local operator CLI |
| `FC27 Discord Analytics Bot` | `DISABLED` | `IMPLEMENTED_AND_TESTED` (disabled-by-default slash-command Discord bot + manual Match Results commands via dedicated `fc27_match_results_writer` role + digest-pinned `docker-compose.ghcr.yml` overlay; `DISCORD_PROVIDER_REQUESTS=0`, `DISCORD_DATABASE_WRITE_ACCESS=false` for analytics path) | `src/fc27_discord_bot.py`, `src/fc27_match_results.py`, `scripts/fc27_discord_bot.py`, `migrations/0013_fc27_manual_match_results.sql`, `Dockerfile.discord`, `docker-compose.discord.yml`, `docker-compose.ghcr.yml` | `tests/test_fc27_discord_bot.py`, `tests/test_fc27_match_results.py`, `tests/test_compose_isolation.py` | `FC27_DISCORD_BOT_ENABLED=false` (`--profile fc27-discord`) |
| `FC27 Pandas Analytical View` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (optional downstream tabular view from normalized records) | `src/fc27_pandas_view.py` | `tests/test_fc27_pandas_view.py` | Optional downstream consumer only (not required for ingestion) |
| `FC27 Performance Telemetry & Baseline` | `DISABLED` | `IMPLEMENTED_AND_TESTED` (disabled-by-default privacy-safe telemetry, multi-scale benchmarks, PostgreSQL EXPLAIN, Python profiling, Polars comparison, and language decision matrix) | `src/fc27_performance.py`, `docs/FC27_PERFORMANCE_BASELINE.md`, `docs/FC27_LANGUAGE_DECISION_MATRIX.md` | `tests/test_fc27_performance.py` | `FC27_PERFORMANCE_TELEMETRY_ENABLED=false` |
| `FC27 Advisory Report Cache` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (thread-safe single-flight immutable 6-report advisory cache with hash verification, defensive copy-on-read, and batch-key invalidation) | `src/fc27_advisory_cache.py`, `docs/FC27_ADVISORY_CACHE.md` | `tests/test_fc27_advisory_cache.py` | Read-only in-memory cache (`ttl_seconds=43200`, `max_entries=16`) |
| `FC27 CI Test Strategy & Browser Reports` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (two-tier `test-fast` + sharded `ci` + `test-nightly` workflows, 128-module isolation matrix, and sanitized self-contained HTML + Job Summary reports) | `src/fc27_ci_test_groups.py`, `scripts/classify_ci_changes.py`, `scripts/build_test_report.py`, `scripts/aggregate_test_reports.py`, `scripts/sanitize_test_report.py` | `tests/test_github_workflows.py`, `tests/test_ci_report_sanitization.py` | `BROWSER_PREVIEW=JOB_SUMMARY_ONLY` (`PAGES_SECURITY_DECISION=NOT_APPROVED_USE_JOB_SUMMARY_ONLY`) |
| `FC27 Container Build, Scan & GHCR Publishing` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (digest-pinned `python:3.11.18-slim` container builds, fail-closed Trivy scan with zero waivers, SPDX-2.3 SBOM, SLSA v1 provenance, Cosign keyless signing, and private GHCR publication) | `Dockerfile.community-worker`, `Dockerfile.discord`, `.github/workflows/container-images.yml`, `scripts/build_container_report.py`, `docs/FC27_CONTAINER_PUBLISHING.md` | `tests/test_github_workflows.py` | `PULL_REQUEST_IMAGE_PUBLICATION=NO`, `GHCR_VISIBILITY=PRIVATE` |
| `FC27 Local Inference Sidecar Boundary` | `PROPOSED` | Design-only read-only sidecar boundary (`NON_AUTHORITATIVE_SIDECAR_SUMMARY`; no active model runtime) | `docs/FC27_LOCAL_INFERENCE_BOUNDARY.md` | `tests/test_fc27_performance.py` | `FC27_LOCAL_INFERENCE_ENABLED=false` |
