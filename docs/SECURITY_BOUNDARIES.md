# Security Boundaries (`iwa.dev.fcbuild`)

- **Status**: Current
- **Audience**: Security Reviewers, Architects, Developers, Operators
- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`
- **Source of truth**: `src/security.py`, `src/settings.py`, `src/db_roles.py`, `src/fc27_community_provider.py`, `src/fc27_clubs_api_contracts.py`, `migrations/post/grants.sql`
- **Supersedes**: `docs/archive/architecture/PHASE2_SECURITY_INVARIANTS.md`, `docs/archive/architecture/PHASE2_WEBAUTHN_POLICY.md`, `docs/archive/reports/SECURITY_CHANGES.md`, `docs/archive/reports/PHASE2_REMEDIATION_REPORT.md`, `docs/archive/reports/BUNDLE_AUDIT.md`, `docs/archive/reports/FINAL_ARCHIVE_AUDIT.md`

This document is the authoritative security boundary specification for `iwa.dev.fcbuild`. Every boundary below is enforced in runtime code, PostgreSQL 16 role grants, and automated verification tests.

---

## 1. Canonical Security Invariants (DC12)

1. **`APPROVAL_IS_NOT_DISPATCH`**: Recording an `APPROVE` decision transitions the workflow to `APPROVED, NOT DISPATCHED` (historical `src/graph.py` superseded in Stage B3). Approval never triggers outbound player messaging or activates `DISPATCH_ENABLED`.
2. **`REDIS_IS_NOT_AUDIT`**: Redis is a non-authoritative pub/sub and ephemeral lease store (`src/events.py`). Redis events are never authoritative audit records or approval proofs.
3. **`RESUME_IS_NOT_AUTHORIZATION`**: Caller-supplied `Command(resume=...)` data and `POST /v1/runs/{id}/resume` requests are never authoritative (historical `src/graph.py`, `src/graph_api.py` superseded in Stage B3). The `approval` node re-reads the committed decision from PostgreSQL and fails closed (`ApprovalNotCommitted`) if none exists.
4. **`POSTGRES_IS_AUDIT_OF_RECORD`**: PostgreSQL 16 (`approval_requests`, `approval_audit`, `fc27_decision_evidence`, `domain_events`) is the sole authoritative audit of record.
5. **`PROVIDER_ACCESS_FAILS_CLOSED`**: `FC27_PROVIDER_ENABLED=false`, `FC27_STANDARD_HTTP_TRANSPORT_ENABLED=false`, `FC27_LIVE_INGESTION_ENABLED=false`, `FC27_COMMUNITY_PROVIDER_ENABLED=false`, and `FC27_COMMUNITY_POLLING_ENABLED=false` fail closed by default (`src/settings.py`, `src/fc27_community_provider.py`). Enabling cache or rate limiting never enables provider access.
6. **`PERSISTENCE_FAILS_CLOSED`**: `FC27_PERSISTENCE_ENABLED=false` by default. Provider results never automatically authorize PostgreSQL persistence (`src/fc27_repository.py`).
7. **`ANALYTICS_FAILS_CLOSED`**: `FC27_ANALYTICS_ENABLED=false` by default. Persistence never automatically authorizes analytics execution (`src/fc27_dashboard_queries.py`).
8. **`RECOMMENDATIONS_FAIL_CLOSED`**: `FC27_RECOMMENDATIONS_ENABLED=false` by default. Analytics never automatically authorize coaching recommendations (`src/settings.py`; historical `src/fc27_coaching.py` superseded in Stage B3).
9. **`DISPATCH_FAILS_CLOSED`**: `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` by default. Recommendations and approvals never automatically authorize dispatch (`src/settings.py`; historical `src/dispatch/gates.py` superseded in Stage B3).
10. **`RAW_PROVIDER_RESPONSES_NOT_RETAINED`**: Raw HTTP responses and raw export files are validated in memory only and discarded immediately (`raw_response_retained=False` in `src/fc27_community_provider.py` and `src/fc27_api_cache.py`).
11. **`PLAYER_IDENTIFIERS_SUPPRESSED`**: Raw personas, gamer tags, EA Account IDs, Nucleus IDs, and Persona IDs are never persisted (`raw_persona_persisted=False`); player keys are pseudonymized via SHA-256 into `plr_ecf_<12-hex>` (`src/fc27_clubs_api_contracts.py`).
12. **`ALIASES_HIDDEN_BY_DEFAULT`**: Display aliases default to hidden (`include_display_aliases=False` / `display_alias=None` in `src/fc27_analytics.py` and `src/fc27_dashboard_queries.py`).
13. **`POSTGRESSAVER_SEPARATE_FROM_DOMAIN_TABLES`**: LangGraph `PostgresSaver` checkpoint blobs (`checkpoints*`) store graph execution state only and are strictly separated from normalized FC 27 domain tables (`fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`).
14. **`MATCHPULSE_ISOLATED`**: The sibling repository `..\matchpulse-live-public` is completely isolated from `iwa.dev.fcbuild` (`scripts/check_matchpulse_boundary.py`).

---

## 2. Service Token vs. Commissioner Authority

- **Service Authentication (`GRAPH_SERVICE_TOKEN` / `BFF_SERVICE_TOKEN`)**: Authenticates machine-to-machine HTTP requests (`src/security.py`; historical `src/identity/deps.py` superseded in Stage B3).
- **Non-Equivalence to Commissioner Authority**: Holding `GRAPH_SERVICE_TOKEN` or `BFF_SERVICE_TOKEN` **never** grants authority to approve or reject a lead package.
- **Impersonation Rejection**: Any attempt to invoke human decision or passkey/TOTP routes with a machine principal (`is_service=True`) or unauthorized actor (`agent`, `system`, `coach`, `bot`) raises `AgentImpersonationError` (HTTP 403).

---

## 3. Commissioner Authentication: WebAuthn Primary & Symmetric HMAC Shared-Secret Fallback

### 3.1 Primary Authority: FIDO2 / WebAuthn Step-Up (Historical `src/identity/webauthn.py` Superseded in Stage B3)
- Enforces `require_user_verification=True` (`UserVerificationRequirement.REQUIRED`).
- Step-up challenges (`fc27_challenges`) are single-use (`consumed_at IS NULL`), short-lived, and cryptographically bound to the canonical payload hash (`bound_payload_hash`).
- Sign-counter regressions (`new_sign_count <= stored_sign_count` when `stored_sign_count > 0`) trigger immediate credential cloning detection and reject authentication.
- WebAuthn asymmetric passkey assertions are distinct from, and never conflated with, the symmetric CLI HMAC fallback.

### 3.2 Emergency Fallback: Symmetric HMAC-SHA256 (Historical `src/approval.py` Superseded in Stage B3)
- **Shared-Secret Authentication**: The CLI fallback uses **HMAC-SHA256 with a symmetric shared secret** (`APPROVAL_SIGNING_SECRET`, minimum 32 characters), verified in constant time via `hmac.compare_digest`. The HMAC key is a symmetric shared secret and must **never** be called a public key or asymmetric key.
- **Kill Switch**: Controlled by `APPROVAL_CLI_HMAC_ENABLED` (`False` by default in `src/settings.py`; historical `src/graph_api.py` superseded in Stage B3).
- **Canonical Payload**: Signs RFC-8785 canonical JSON over `SIGNED_FIELDS` (`thread_id`, `decision`, `owner`, `selection`, `candidate_id`, `player_id`, `notes`, `package_version`, `nonce`, `issued_at`, `expires_at`).

---

## 4. Single-Transaction Evidence Binding & Lock Hierarchy

Every human decision (historical `src/approval_store.py` superseded in Stage B3) executed inside a single PostgreSQL transaction enforcing a deterministic lock hierarchy (`migrations/0007_decision_evidence.sql`, `migrations/0009_r3_evidence_binding.sql`):

1. **Challenge / Nonce Consumption**: Atomically consumes the single-use `fc27_challenges` / `approval_nonces` row.
2. **14-Field `fc27_decision_evidence` Binding**: Locks and consumes the immutable 14-field `fc27_decision_evidence` row (`evidence_id`, `request_id`, `thread_id`, `actor_user_id`, `actor_role`, `authority_basis`, `auth_method`, `credential_id`, `challenge_id`, `package_version`, `decision_payload_sha256`, `verified_at`, `consumed_at`, `created_at`) before `approval_requests` can transition to `approved` or `rejected`.
3. **Lock Hierarchy**: Acquires row locks in strict order: `fc27_decision_evidence` -> `approval_requests` (`FOR UPDATE`) -> `graph_threads` (`FOR UPDATE`).
4. **Immutable Audit Append**: Appends to `approval_audit` and `domain_events` within the same transaction.

---

## 5. Least-Privilege Database Roles (Grants Policy Version 15)

Enforced by `migrations/post/grants.sql` (`-- grants-policy-version: 15`) and verified by `src/db_migrate.py` and `src/db_roles.py`:

| Role | Login | Capabilities | Restricted From |
|---|---|---|---|
| `fc27_owner` | `NOLOGIN` | Owns all schema objects | Direct runtime connection prohibited |
| `fc27_migrator` | `LOGIN` (`NOINHERIT`) | DDL migrations via `SET ROLE fc27_owner` | Runtime application queries |
| `fc27_app` | `LOGIN` | DML on app/FC27 provider tables; `SELECT`-only on `fc27_manual_*` tables; `SELECT, INSERT` only on `approval_audit` & `domain_events`; zero `DELETE` on `fc27_*` tables | DDL, `TRUNCATE`, `DELETE` on `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, `fc27_quarantine_records`, `fc27_manual_*`, `approval_audit`, `domain_events` |
| `fc27_match_results_writer` | `LOGIN` | Narrow DML on `fc27_manual_matches` (`SELECT, INSERT, UPDATE`), `fc27_manual_match_players` (`SELECT, INSERT, UPDATE, DELETE`), `fc27_manual_match_events` (`SELECT, INSERT, UPDATE, DELETE`), and `fc27_manual_match_audit` (`SELECT, INSERT`) | Zero privileges on `fc27_matches`, `fc27_player_match_stats`, poll state, approval, dispatch, outbox, identity, or migration ledger tables; zero `DELETE` on `fc27_manual_matches` and `fc27_manual_match_audit` |
| `fc27_dispatch` | `LOGIN` | Scoped to `outbox` and `dispatch_attempts` | Approval tables, graph threads, `fc27_*` analytics and manual tables |
| `fc27_relay` | `LOGIN` | Scoped to `outbox` relay operations | All approval, identity, and `fc27_*` analytics and manual tables |

---

## 6. Anti-Impersonation & Browser Bypass Rejection

`src/fc27_clubs_api_contracts.py` (`PROHIBITED_HEADER_NAMES`, `parse_json_strict_no_duplicates`) and `src/fc27_community_provider.py` permanently reject browser/TLS impersonation (`Impit` / `impit-node`, Chromium, Playwright, Puppeteer, Selenium, `curl_cffi`, `tls-client`, `Sec-CH-UA*`, `Sec-Fetch-*`, cookies, session tokens, and residential proxies).

---

## 9. CI Workflow and Browser Test Report Security Boundary

- **Fail-Closed Report Sanitization (`scripts/sanitize_test_report.py`)**: All GitHub Actions Job Summaries, self-contained HTML reports (`HTML_REPORT_SELF_CONTAINED=YES`, `HTML_EXTERNAL_RESOURCES=0`), JUnit XML outputs, and duration reports are scanned fail-closed before upload or publication (`FORBIDDEN_VALUES_PUBLISHED=0`), rejecting credentials, connection URIs, tokens, host paths, UNC paths, private IPs, Discord snowflakes, player pseudonyms, raw provider fields, and environment dumps without printing matched values.
- **GitHub Pages Security Posture**: Public GitHub Pages publication is not approved on private repositories without private Pages access control (`PAGES_SECURITY_DECISION=NOT_APPROVED_USE_JOB_SUMMARY_ONLY`, `BROWSER_PREVIEW=JOB_SUMMARY_ONLY`, `UNTRUSTED_FORK_PAGES_PUBLICATION=NO`). See [`FC27_CI_BROWSER_REPORTS.md`](FC27_CI_BROWSER_REPORTS.md).
- **Container Image Build & GHCR Publication Security Boundary**: `.dockerignore` excludes `.env*`, `secrets/`, `.local-data/`, `.local-artifacts/`, `*.7z`, `*recovery*`, `*.sql`, `*.sql.gz`, `.git/`, `.docker/`, `.venv/`, and local key material (`*.pem`, `*.key`, `*.pfx`, `*.kdbx`) from all Docker build contexts. `Dockerfile.community-worker`, `Dockerfile.discord`, and `Dockerfile.community-eval` pin `python:3.11.18-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545` and upgrade `setuptools>=84.2.1` and `wheel>=0.48.0` so `scripts/build_container_report.py` enforces `IMAGE_SECRET_SCAN=PASS`, `SECRETS_IN_IMAGE_HISTORY=0`, `SECRETS_IN_IMAGE_FILESYSTEM=0`, `CRITICAL=0`, and `HIGH_FIXED=0` with zero active vulnerability waivers (`waiver_id="NONE"`), non-root runtime user `10001:65534`, and `--read-only` root filesystem compatibility (see [`FC27_CONTAINER_PUBLISHING.md`](FC27_CONTAINER_PUBLISHING.md)). `.github/workflows/container-images.yml` blocks pull-request and fork publication (`PULL_REQUEST_IMAGE_PUBLICATION=NO`, `UNTRUSTED_FORK_IMAGE_PUBLICATION=NO`, `PUBLISH_WITH_FAILED_GATE=NO`) and keeps GHCR packages private (`GHCR_VISIBILITY=PRIVATE`, `GHCR_VISIBILITY_REVIEW=PASS`). Runtime GHCR promotion via `docker-compose.ghcr.yml` ([`FC27_GHCR_RUNTIME_PROMOTION.md`](FC27_GHCR_RUNTIME_PROMOTION.md)) pins exact verified immutable SHA-256 digests (`@sha256:...`), prohibits mutable tag references (`:main`, `:latest`), embeds zero registry credentials in Compose YAML or `.env`, and preserves single-replica, non-root (`10001:65534`), read-only rootfs, and 12-hour polling-interval (`43200s`) invariants.
