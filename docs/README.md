# East Coasters (`330323`) FC 27 Documentation Index

## Document Classification
- **Authority Tier**: Tier 1 — Authoritative Documentation Index
- **Truth Table Role**: Canonical entry point for all active `docs/` and root operational guides (`docs/FC27_DOCUMENTATION_TRUTH_TABLE.md`)
- **Scope**: East Coasters (`club_id = 330323`, `platform = common-gen5`, `game_version = FC27`)
- **Last Verified Against Repo State**: `2026-10-31`
- **Current Supported Acquisition Paths**:
  1. **Primary Real-Data Path**: Bounded one-shot community client provider (`fc-clubs-api-community-v1` / `LIVE_EA_CLUBS_COMMUNITY_CLIENT`, documented in `docs/FC27_COMMUNITY_PROVIDER_EVALUATION.md`)
  2. **Deterministic Local Loopback Demo**: Synthetic fixture source (`DEMO_FIXTURE_LOOPBACK`, documented in `docs/FC27_DEMO.md`)
  3. **Offline User-Export / Operator-Capture Import**: Validated local JSON envelopes (`docs/FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md`)
- **Non-Goals and Disabled Features**: Continuous live EA polling, browser scraping/WAF bypass, player alias/handle retention, raw provider payload storage, autonomous Coach/Tactics/Director recommendations (`FC27_RECOMMENDATIONS_ENABLED=false`), and autonomous player dispatch (`DISPATCH_ENABLED=false`).

---

## 1. Start Here

New developers and operators can identify and run the complete current system from these primary entry points:
- **[`../README.md`](../README.md)** — Repository quickstart, current real-data architecture diagram, 5-minute operator guide, and safety posture
- **[`FC27_COMPONENT_STATUS.md`](FC27_COMPONENT_STATUS.md)** — Component-by-component operational status matrix (`CURRENT`, `DISABLED`, `DEFERRED`, `REJECTED`)
- **[`FC27_DOCUMENTATION_TRUTH_TABLE.md`](FC27_DOCUMENTATION_TRUTH_TABLE.md)** — Canonical source-of-truth mapping across code, configuration, tests, and documentation
- **[`repository_inventory.json`](repository_inventory.json)** — Machine-readable inventory of all tracked files in the repository
- **[`../scripts/README.md`](../scripts/README.md)** — Complete index of current operator, provider, migration, demo, and CI scripts

---

## 2. Current Production Path

The current supported real-data pipeline for **East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`)** is:

```text
pinned FC27 community provider (fc-clubs-api-community-v1 / LIVE_EA_CLUBS_COMMUNITY_CLIENT)
  -> repository endpoint contracts & schema validation (src/fc27_clubs_api_contracts.py)
  -> normalization, sensitive-field rejection & plr_ecf_<12-hex> pseudonymization
  -> optional single-transaction PostgreSQL 16 persistence (src/fc27_repository.py)
  -> Search context assembly (src/fc27_search_scout.py)
  -> Scout evidence synthesis (src/fc27_search_scout.py)
  -> Analyst evidence synthesis (src/fc27_analyst.py)
  -> existing local dashboard & JSON API (src/fc27_demo_api.py:create_demo_app)
```

- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — System architecture, runtime topology, trust zones, and `PostgresSaver` vs. domain table separation
- **[`ADR_FC27_EAST_COASTERS_PLATFORM.md`](ADR_FC27_EAST_COASTERS_PLATFORM.md)** — Architecture Decision Record locking club `330323` ("East Coasters"), platform `common-gen5`, and game `FC27`

---

## 3. Provider and Ingestion

- **[`FC27_COMMUNITY_PROVIDER_EVALUATION.md`](FC27_COMMUNITY_PROVIDER_EVALUATION.md)** — Current operational community-client provider (`fc-clubs-api-community-v1`, `LIVE_EA_CLUBS_COMMUNITY_CLIENT`, `src/fc27_community_provider.py`, `scripts/fc27_community_eval.py`, `Dockerfile.community-eval`, `docker-compose.community-eval.yml`) and optional sanitized pandas view (`src/fc27_pandas_view.py`)
- **[`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md)** — Provider contract governance, historical transport findings (standards-client HTTP `403` and headless Chromium HTTP/2 diagnostic records), and rate/cache controls
- **[`FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md`](FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md)** — Secondary offline user-export and operator-capture ingestion specification (`scripts/fc27_user_export_import.py`)
- **[`EA_PLAYER_IDENTITY_VERIFICATION.md`](EA_PLAYER_IDENTITY_VERIFICATION.md)** — Pseudonymization (`plr_ecf_<12-hex>`) and strict prohibition on raw EA persona/blaze IDs and player handles

---

## 4. Storage

- **[`FC27_DATA_MODEL.md`](FC27_DATA_MODEL.md)** — Normalized PostgreSQL 16 schema (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`), canonical SHA-256 hashing, replay-conflict immutability, and Grants Policy Version 14
- **[`../MIGRATION_GUIDE.md`](../MIGRATION_GUIDE.md)** — Numbered PostgreSQL 16 migrations (`0001`–`0012`), role bootstrap (`python -m src.db_roles`), and migration runner (`python -m src.db_migrate`)

---

## 5. Analytics

- **[`FC27_ANALYTICS.md`](FC27_ANALYTICS.md)** — Deterministic `Search -> Scout -> Analyst` pipeline (`src/fc27_search_scout.py`, `src/fc27_analyst.py`, `src/fc27_analytics.py`), metric formulas, sample-size gates, and confidence tiers
- **[`FC27_TACTICAL_HYPOTHESES.md`](FC27_TACTICAL_HYPOTHESES.md)** — Registry of 10 external community/meta tactical hypothesis families (`TH-FC27-001` through `TH-FC27-010`), all classified as `EXTERNAL_TACTICAL_HYPOTHESIS` (`UNVERIFIED`, `NOT_APPROVABLE`)
- **[`MODEL_SELECTION.md`](MODEL_SELECTION.md)** — Local LLM model catalog and configuration for the optional LangGraph runtime
- **[`ROLE_MODEL_BENCHMARK_MATRIX.md`](ROLE_MODEL_BENCHMARK_MATRIX.md)** — Role model evaluation and benchmark matrix

---

## 6. Dashboard

- **[`FC27_DEMO.md`](FC27_DEMO.md)** — Local loopback dashboard (`src.fc27_demo_api:create_demo_app` on `http://127.0.0.1:8001/demo`), browser-readable JSON endpoints (`/demo/status`, `/demo/matches`, `/demo/analysis`), source-classification banners (`LIVE_EA_CLUBS_COMMUNITY_CLIENT` vs. `SYNTHETIC_FIXTURE`), and synthetic fixture demo CLI (`scripts/fc27_demo_ingest.py`)

---

## 7. Security

- **[`../SECURITY.md`](../SECURITY.md)** — Root security policy, vulnerability reporting, and non-negotiable safety boundaries
- **[`SECURITY_BOUNDARIES.md`](SECURITY_BOUNDARIES.md)** — Outbound network guards, PII scrubbing, service-token vs. Commissioner authority, fail-closed feature gates, and container hardening
- **[`APPROVAL_SECURITY.md`](APPROVAL_SECURITY.md)** — Commissioner approval gate cryptographic invariants and state machine
- **[`FC27_APPROVAL_FLOW.md`](FC27_APPROVAL_FLOW.md)** — WebAuthn step-up challenge binding, 14-field `fc27_decision_evidence`, and single LangGraph `interrupt()` specification
- **[`DEPLOYMENT_AND_SECURITY.md`](DEPLOYMENT_AND_SECURITY.md)** — Docker Compose deployment profiles, network isolation, and operational hardening checklist
- **[`DISPATCH_ACTIVATION.md`](DISPATCH_ACTIVATION.md)** — Fail-closed player dispatch gates (`DISPATCH_ENABLED=false`)
- **[`MATCHPULSE_INTEGRATION_BOUNDARY.md`](MATCHPULSE_INTEGRATION_BOUNDARY.md)** — Hard repository isolation boundary with `matchpulse-live-public` (`scripts/check_matchpulse_boundary.py`)
- **[`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)** — Clean-room provenance and third-party open-source attribution notices

---

## 8. Testing

- **[`TESTING.md`](TESTING.md)** — Complete test suite guide, disposable PostgreSQL 16 workflow, and focused validation gates
- **[`../TEST_RESULTS.md`](../TEST_RESULTS.md)** — Current reproducible test verification summary

---

## 9. Operations

- **[`FEATURE_FLAGS.md`](FEATURE_FLAGS.md)** — Authoritative reference for all environment flags (`src/settings.py` and `config.example.env`) and fail-closed defaults
- **[`OPERATIONS.md`](OPERATIONS.md)** — Day-2 operational runbooks, secret rotation, health probes, and rollback procedures
- **[`DEVELOPMENT.md`](DEVELOPMENT.md)** — Local development setup, virtual environment, and contributor workflow
- **[`VOICE_CAPABILITIES.md`](VOICE_CAPABILITIES.md)** — Optional local Discord/voice subsystem boundary (non-primary)

---

## 10. Historical Archive

- **[`archive/README.md`](archive/README.md)** — Non-authoritative historical archive index (`Operational status: DO NOT USE`)
- **[`archive/ARCHIVE_MANIFEST.md`](archive/ARCHIVE_MANIFEST.md)** — Human-readable archive manifest (`fc27-archive-manifest-v1`; machine-readable manifest at `config/fc27_archive_manifest.json`)
