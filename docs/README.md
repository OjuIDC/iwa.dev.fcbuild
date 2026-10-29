# `iwa.dev.fcbuild` Documentation Index

Welcome to the authoritative documentation directory for **`iwa.dev.fcbuild`**. Use this index to navigate current architecture, development workflows, security boundaries, FC 27 provider integration, deterministic analytics, Commissioner approval governance, and archived historical records.

---

## 1. Start Here

- **[`../README.md`](../README.md)** — Repository overview, quickstart, target club scope (`East Coasters`, `club_id=330323`, `common-gen5`, `FC27`), component status summary, and safety boundaries.
- **[`FC27_COMPONENT_STATUS.md`](FC27_COMPONENT_STATUS.md)** — Authoritative status classification (`IMPLEMENTED_AND_TESTED`, `DISABLED_BY_DEFAULT`, `BLOCKED_EXTERNAL_AUTHORIZATION`, etc.) for all 37 repository subsystems.
- **[`FC27_DOCUMENTATION_TRUTH_TABLE.md`](FC27_DOCUMENTATION_TRUTH_TABLE.md)** — Complete reconciliation truth table mapping every Markdown document to its current status and code evidence.
- **[`repository_inventory.json`](repository_inventory.json)** — Machine-readable inventory of all tracked files in the repository.

---

## 2. Architecture

- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — System architecture, runtime topology (`mcp-server`, `bff-server`, `postgres`, `redis`, `vllm`, `dispatch-worker`, `audit-relay`), trust zones, data-flow diagrams, and `PostgresSaver` vs. domain table separation.
- **[`ADR_FC27_EAST_COASTERS_PLATFORM.md`](ADR_FC27_EAST_COASTERS_PLATFORM.md)** — Architecture Decision Record for the East Coasters FC 27 Pro Clubs analytics, safe provider adapter, and coaching validation platform.
- **[`FC27_DATA_MODEL.md`](FC27_DATA_MODEL.md)** — Normalized PostgreSQL 16 schema (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`), `plr_ecf_<12-hex>` pseudonymization, idempotency, and Grants Policy Version 14.

---

## 3. Development

- **[`DEVELOPMENT.md`](DEVELOPMENT.md)** — Local environment setup, Python virtual environment, offline unit testing, PostgreSQL 16 test container commands, and boundary verification scripts.

---

## 4. Testing

- **[`TESTING.md`](TESTING.md)** — Test architecture, suite inventory, focused FC 27 adapter/analytics/coaching tests, full PostgreSQL 16 integration suite execution, and zero-network invariants.
- **[`../TEST_RESULTS.md`](../TEST_RESULTS.md)** — Current commit-bound test execution report (`2026-10-29`, PostgreSQL 16 verified).

---

## 5. Operations

- **[`OPERATIONS.md`](OPERATIONS.md)** — Secrets provisioning (`./secrets/`), database role bootstrap (`db-bootstrap`), migrations (`src.db_migrate`), Commissioner WebAuthn bootstrap ceremony, `MultiFernet` & HMAC key rotation, health probes, and rollback runbooks.
- **[`DEPLOYMENT_AND_SECURITY.md`](DEPLOYMENT_AND_SECURITY.md)** — Docker Compose deployment profiles (`gpu`, `cpu`, `ops`, `dispatch`), network isolation, and operational hardening checklist.
- **[`FEATURE_FLAGS.md`](FEATURE_FLAGS.md)** — Complete specification and dependency chain for all fail-closed feature flags in `src/settings.py` and `config.example.env`.
- **[`VOICE_CAPABILITIES.md`](VOICE_CAPABILITIES.md)** — Scope and non-inclusion boundary for voice/audio capabilities in `iwa.dev.fcbuild`.
- **[`notes/mcp_ws.md`](notes/mcp_ws.md)** — Technical note on MCP streamable HTTP (`src/mcp_http_server.py`) and stdio (`src/mcp_server.py`) transport behavior.

---

## 6. Security

- **[`SECURITY_BOUNDARIES.md`](SECURITY_BOUNDARIES.md)** — Authoritative security invariants: service token vs. Commissioner authority, WebAuthn primary vs. symmetric HMAC-SHA256 shared-secret fallback, 14-field `fc27_decision_evidence` lock hierarchy, resume payload non-authority, Redis non-authority, least-privilege DB roles, and anti-impersonation controls.
- **[`../SECURITY.md`](../SECURITY.md)** — Root security policy, vulnerability reporting, and secret management summary.
- **[`APPROVAL_SECURITY.md`](APPROVAL_SECURITY.md)** — Detailed Commissioner step-up cryptographic verification specification.

---

## 7. Provider Integration

- **[`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md)** — Provider-neutral API boundary (`src/fc27_authorized_api.py`, `src/fc27_authorized_provider.py`, `src/fc27_authorized_transport.py`), 11 `fc27-clubs-api` endpoint contracts (`src/fc27_clubs_api_contracts.py`), 23-bit `ev` bitmap decoder, bounded normalized cache (`FC27BoundedNormalizedCache`), token-bucket rate limiter (`FC27ProviderTokenBucketLimiter`), 3-state circuit breaker, and preflight/lookup CLIs (`scripts/fc27_capture_preflight.py`, `scripts/fc27_club_lookup.py`).
- **[`FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md`](FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md)** — Secondary offline JSON/CSV validation fallback and schema pilot guide (`scripts/fc27_user_export_import.py`).
- **[`EA_PLAYER_IDENTITY_VERIFICATION.md`](EA_PLAYER_IDENTITY_VERIFICATION.md)** — Player identity verification and deterministic pseudonymization rules.
- **[`MATCHPULSE_INTEGRATION_BOUNDARY.md`](MATCHPULSE_INTEGRATION_BOUNDARY.md)** — Sibling repository boundary specification (`scripts/check_matchpulse_boundary.py`).
- **[`MODEL_SELECTION.md`](MODEL_SELECTION.md)** & **[`ROLE_MODEL_BENCHMARK_MATRIX.md`](ROLE_MODEL_BENCHMARK_MATRIX.md)** — Local LLM model catalog and role benchmark matrix.
- **[`WINDHOVER_INTEGRATION_GAP.md`](WINDHOVER_INTEGRATION_GAP.md)** — Windhover integration gap analysis.

---

## 8. Analytics

- **[`FC27_ANALYTICS.md`](FC27_ANALYTICS.md)** — Deterministic pure analytics engine (`FORMULA_VERSION = "1.0.0"`, `src/fc27_analytics.py`), `COMPLETED`-only match eligibility, sample-size confidence tiers, last-20 baseline, last-5 recent form, 730-day rolling horizon, and PostgreSQL `READ ONLY` dashboard queries (`src/fc27_dashboard_queries.py`).

---

## 9. Approval Flow

- **[`FC27_APPROVAL_FLOW.md`](FC27_APPROVAL_FLOW.md)** — End-to-end lead ranking (`fc27-rank-v1`), pre-approval coaching validation (`src/fc27_coaching.py`), canonical `package_version` hashing, single LangGraph `interrupt()`, and `APPROVED, NOT DISPATCHED` termination.

---

## 10. Tactical Hypotheses

- **[`FC27_TACTICAL_HYPOTHESES.md`](FC27_TACTICAL_HYPOTHESES.md)** — Registry of 10 external community/meta tactical hypothesis families (`TH-FC27-001` through `TH-FC27-010`), all classified as `EXTERNAL_TACTICAL_HYPOTHESIS` (`UNVERIFIED`, `NOT_APPROVABLE`).

---

## 11. Migrations

- **[`../MIGRATION_GUIDE.md`](../MIGRATION_GUIDE.md)** — Numbered PostgreSQL 16 migrations (`0001`–`0012`), advisory locking, SHA-256 checksum verification, and Grants Policy Version 14 (`migrations/post/grants.sql`).

---

## 12. Dispatch Activation

- **[`DISPATCH_ACTIVATION.md`](DISPATCH_ACTIVATION.md)** — Six mandatory activation gates (`G-MIGRATION`, `G-SECRETS`, `G-CHANNEL`, `G-CONSENT`, `G-MODERATION`, `G-OUTBOX`) keeping `DISPATCH_ENABLED=false`.

---

## 13. Identity

- **Identity & PII Encryption (`src/identity/`)**: Documented in [`SECURITY_BOUNDARIES.md`](SECURITY_BOUNDARIES.md) and [`OPERATIONS.md`](OPERATIONS.md) (`MultiFernet` encryption in `src/identity/crypto.py`, consent & suppression ledger in `src/identity/repository.py`, and WebAuthn passkeys in `src/identity/webauthn.py`).

---

## 14. Archive

- **[`archive/README.md`](archive/README.md)** — Policy and directory layout for non-authoritative historical documents.
- **[`archive/ARCHIVE_MANIFEST.md`](archive/ARCHIVE_MANIFEST.md)** — Complete manifest mapping all 18 archived Phase 1/Phase 2 prompts, audit reports, design specs, and historical test traces to their current authoritative replacements.
