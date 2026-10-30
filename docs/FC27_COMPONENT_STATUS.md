# FC 27 Component Status Classification

- **Status**: Current
- **Audience**: Developers, Operators, Security Reviewers
- **Last verified commit**: `db77a6f74540079b91ab03657642bbe30e3b9b18`
- **Source of truth**: `src/`, `config/`, `migrations/`, `docker-compose.yml`, `tests/`
- **Supersedes**: `docs/archive/reports/IMPLEMENTATION_REPORT.md`, `docs/archive/reports/HACKATHON_ALIGNMENT.md` (historical, non-authoritative)

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
- `ARCHIVED` — Moved to `docs/archive/` as non-authoritative historical provenance.
- `NOT_FOUND` — Referenced externally but absent from this repository.

> **Deployment Validation Note**: Where code and container definitions exist and pass static/local tests but full target-host deployment has not been executed, the Deployment Note column records `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED`.

---

## 2. Authoritative Component Status Table

| Component | Status | Deployment Note | Source Evidence | Test Evidence | Default Runtime Gate / Profile |
|---|---|---|---|---|---|
| `graph-api` | `IMPLEMENTED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/graph_api.py`, `Dockerfile.graph`, `docker-compose.yml` | `tests/test_graph_api_v1.py`, `tests/test_langgraph_flow.py` | Default service (`127.0.0.1:8001`) |
| `LangGraph StateGraph` | `IMPLEMENTED` | Verified against PostgreSQL 16 | `src/graph.py`, `src/hierarchy.py`, `src/lead_packages.py` | `tests/test_langgraph_flow.py`, `tests/test_graph_persistence.py` | Active inside `graph-api` |
| `PostgresSaver` | `IMPLEMENTED` | Verified against PostgreSQL 16 | `src/graph.py`, `src/db_migrate.py` | `tests/test_graph_persistence.py`, `tests/test_db_migrate_roles.py` | Separated from normalized `fc27_*` tables |
| `normalized FC27 PostgreSQL storage` | `IMPLEMENTED` | Verified against PostgreSQL 16 | `migrations/0012_fc27_performance_and_quarantine.sql`, `src/fc27_repository.py` | `tests/test_fc27_persistence_and_queries.py` | `FC27_PERSISTENCE_ENABLED=false` |
| `read-only analytics` | `IMPLEMENTED` | Verified against PostgreSQL 16 | `src/fc27_analytics.py`, `src/fc27_dashboard_queries.py`, `src/fc27_search_scout.py` | `tests/test_fc27_dashboard_queries.py`, `tests/test_fc27_persistence_and_queries.py`, `tests/test_fc27_search.py`, `tests/test_fc27_scout.py` | `FC27_ANALYTICS_ENABLED=false` |
| `Search` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (pure deterministic context + reader adapter) | `src/fc27_search_scout.py` | `tests/test_fc27_search.py`, `tests/test_fc27_search_scout_graph.py` | Pure / Read-only (`FC27_ANALYTICS_ENABLED=false` for PG reader) |
| `Scout` | `IMPLEMENTED` | `IMPLEMENTED_AND_TESTED` (pure deterministic evidence + hypotheses) | `src/fc27_search_scout.py` | `tests/test_fc27_scout.py`, `tests/test_fc27_search_scout_graph.py` | Pure / Observational only (`OBSERVATIONAL_ONLY`) |
| `Analyst` | `DEFERRED` | Next roadmap phase (Analyst evidence synthesis) | `src/graph.py`, `src/roles.py` | `tests/test_langgraph_flow.py` | Legacy role seam; normalized synthesis deferred |
| `Coach recommendation activation` | `DISABLED` | Gated fail-closed by default | `src/fc27_coaching.py`, `src/settings.py` | `tests/test_fc27_preapproval_validator.py` | `FC27_RECOMMENDATIONS_ENABLED=false` |
| `Tactics` | `DEFERRED` | Deferred pending Analyst and Coach phases | `src/graph.py`, `src/roles.py` | `tests/test_langgraph_flow.py` | Legacy role seam; normalized tactics deferred |
| `Director` | `DEFERRED` | Deferred pending Tactics phase | `src/graph.py`, `src/roles.py` | `tests/test_langgraph_flow.py` | Legacy role seam; normalized director deferred |
| `pre-approval validator` | `IMPLEMENTED` | Verified in unit suites | `src/fc27_coaching.py` | `tests/test_fc27_preapproval_validator.py` | `FC27_RECOMMENDATIONS_ENABLED=false` |
| `provider-neutral boundary` | `DISABLED` | Mock-validated only | `src/fc27_authorized_provider.py`, `src/fc27_authorized_transport.py`, `src/fc27_provider_contract.py`, `src/fc27_authorized_api.py` | `tests/test_fc27_authorized_provider.py`, `tests/test_fc27_authorized_api.py` | `FC27_PROVIDER_ENABLED=false` |
| `endpoint contracts` | `IMPLEMENTED` | Verified in unit suites | `src/fc27_clubs_api_contracts.py` | `tests/test_fc27_clubs_api_contracts.py` | `FC27_UPSTREAM_COMPATIBILITY_ENABLED=false` |
| `safe adapter bundle manifest` | `IMPLEMENTED` | Verified in unit suites (`fc27-safe-adapter-manifest-v1`) | `config/fc27_safe_adapter_bundle_manifest.json`, `THIRD_PARTY_NOTICES.md` | `tests/test_fc27_safe_adapter_bundle_manifest.py` | `FC27_PROVIDER_ENABLED=false` |
| `cache` | `DISABLED` | Verified in unit suites | `src/fc27_api_cache.py` | `tests/test_fc27_api_cache.py` | `FC27_API_CACHE_ENABLED=false` |
| `rate limiter` | `DISABLED` | Verified in unit suites | `src/fc27_api_rate_limit.py` | `tests/test_fc27_api_rate_limit.py` | `FC27_API_RATE_LIMIT_ENABLED=false` |
| `request budget` | `IMPLEMENTED` | Verified in unit suites | `src/fc27_api_rate_limit.py` | `tests/test_fc27_api_rate_limit.py` | Enforces `max_attempts=1`, `max_retries=0` |
| `circuit breaker` | `IMPLEMENTED` | Verified in unit suites | `src/fc27_api_rate_limit.py` | `tests/test_fc27_api_rate_limit.py` | `FC27_API_CIRCUIT_BREAKER_ENABLED=true` (starts `OPEN`) |
| `operator capture` | `IMPLEMENTED` | Secondary offline fallback | `src/fc27_capture.py`, `src/fc27_user_export.py`, `scripts/fc27_capture_preflight.py`, `scripts/fc27_user_export_import.py` | `tests/test_fc27_operator_capture.py`, `tests/test_fc27_user_export.py` | Offline CLI only (`--dry-run` default) |
| `browser-profile research sidecar` | `REJECTED` | Excluded from `main` | `src/fc27_provider_contract.py` (`FORBIDDEN_BROWSER_IMPERSONATION_TOKENS`) | `tests/test_fc27_authorized_provider.py`, `tests/test_fc27_clubs_api_contracts.py` | Prohibited (`allow_browser_impersonation=False`) |
| `fc27-mcp` | `IMPLEMENTED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/fc27_mcp_server.py`, `src/mcp_retry.py`, `src/mcp_healthcheck.py`, `Dockerfile.mcp` | `tests/test_mcp_http_server.py`, `tests/test_mcp_retry_and_tools.py` | Internal Compose service (`fc27_net`) |
| `live-watcher` | `IMPLEMENTED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/live_match_watcher.py`, `docker-compose.yml` | `tests/test_watcher_processing.py` | Internal Compose service (`fc27_net`) |
| `post-match-rundown` | `IMPLEMENTED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/post_match_rundown.py`, `docker-compose.yml` | `tests/test_rundown_match_and_prompt.py` | Internal Compose service (`fc27_net`) |
| `Redis event bus` | `IMPLEMENTED` | Non-authoritative cache/bus | `src/events.py`, `docker-compose.yml` | `tests/test_events_outbox.py`, `tests/test_compose_isolation.py` | `127.0.0.1:6379` (`fc27_net` only) |
| `ws-server` | `IMPLEMENTED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/live_ws_server.py`, `docker-compose.yml` | `tests/test_ws_server.py` | `127.0.0.1:8765` (`fc27_net`) |
| `Ollama` | `IMPLEMENTED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/roles.py`, `src/model_selection.py`, `scripts/ollama-init.sh`, `scripts/model_health.py`, `docker-compose.yml` | `tests/test_roles.py`, `tests/test_model_selection.py` | Internal Compose service (`fc27_net`) |
| `LM Studio` | `DISABLED` | Optional local fallback | `src/roles.py`, `src/model_selection.py`, `docker-compose.yml` | `tests/test_model_selection.py` | `LM_STUDIO_FALLBACK_ENABLED=false` |
| `Swarms backend` | `IMPLEMENTED` | Optional role runner backend | `src/swarms_backend.py`, `requirements-swarms.txt` | `tests/test_swarms_backend.py` | `ROLE_BACKEND=swarms` |
| `Windows Capture Agent` | `IMPLEMENTED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `host_agent/capture_agent.py`, `host_agent/install.ps1`, `src/ocr_parsing.py` | `tests/test_ocr_parsing.py`, `tests/test_installer_static.py` | Host-side optional utility |
| `RTSP OCR` | `DISABLED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/rtsp_frame_analyzer.py`, `src/ocr_parsing.py`, `docker-compose.yml` | `tests/test_ocr_parsing.py`, `tests/test_compose_isolation.py` | Compose profile `--profile rtsp` |
| `Voice Profile` | `DISABLED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/voice_gateway.py`, `Dockerfile.stt`, `Dockerfile.tts`, `docker-compose.yml` | `tests/test_voice_gateway_api.py`, `tests/test_typed_graph_voice.py` | Compose profile `--profile voice` |
| `Langfuse` | `DISABLED` | `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED` | `src/tracing.py`, `docker-compose.langfuse.yml`, `requirements-langfuse.txt` | `tests/test_tracing.py`, `tests/test_compose_isolation.py` | `LANGFUSE_ENABLED=false` (`--profile langfuse`) |
| `Commissioner CLI` | `IMPLEMENTED` | Verified against TestClient | `scripts/commissioner_decide.py`, `src/approval.py` | `tests/test_commissioner_cli.py`, `tests/test_approval_security.py` | `APPROVAL_CLI_HMAC_ENABLED=false` in Python defaults |
| `HMAC verification` | `IMPLEMENTED` | Symmetric shared-secret proof | `src/approval.py`, `src/approval_store.py` | `tests/test_approval_security.py`, `tests/test_workflow.py` | Requires `APPROVAL_SIGNING_SECRET` (`>= 32` chars) |
| `WebAuthn` | `IMPLEMENTED` | Primary human step-up proof | `src/identity/webauthn.py`, `src/identity/routers/passkeys.py`, `src/identity/routers/step_up.py`, `migrations/0007_decision_evidence.sql`, `migrations/0009_r3_evidence_binding.sql` | `tests/identity/test_webauthn.py`, `tests/identity/test_step_up.py` | Mandatory `require_user_verification=True` |
| `identity` | `IMPLEMENTED` | Verified against PostgreSQL 16 | `src/identity/`, `migrations/0004_identity_foundation.sql`..`migrations/0009_r3_evidence_binding.sql` | `tests/identity/test_store.py`, `tests/identity/test_crypto.py` | Active on `graph-api` |
| `recommendations` | `DISABLED` | Gated by pre-approval validator | `src/fc27_coaching.py`, `src/lead_packages.py` | `tests/test_fc27_preapproval_validator.py` | `FC27_RECOMMENDATIONS_ENABLED=false` |
| `dispatch` | `DISABLED` | Blocked by 6 activation gates | `src/dispatch/`, `migrations/0003_dispatch_foundation.sql`, `migrations/0010_outbox_claim_ownership.sql`, `migrations/0011_dispatch_worker_claims.sql`, `docs/DISPATCH_ACTIVATION.md` | `tests/test_dispatch_schema.py`, `tests/test_dispatch_transition.py`, `tests/test_phase1_scope.py` | `DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false` |
| `continuous polling` | `REJECTED` | Prohibited by request budget | `src/fc27_api_rate_limit.py` (`FC27RequestBudget`, `max_attempts=1`) | `tests/test_fc27_api_rate_limit.py` | Prohibited (`max_attempts=1`) |
| `live EA ingestion` | `DEFERRED` | Blocked pending written authorization | `src/fc27_authorized_provider.py`, `src/fc27_authorized_api.py` | `tests/test_fc27_authorized_provider.py`, `tests/test_fc27_authorized_api.py` | `FC27_LIVE_INGESTION_ENABLED=false` |
