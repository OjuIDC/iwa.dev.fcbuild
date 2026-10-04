# E2E Test Infrastructure Specification (TEST_INFRA.md)
**Project**: Player-Dispatch Programme — Phase 1: Secure Data Foundation  
**Author**: `e2e_test_writer_1` (Teamwork E2E Test Writer)  
**Date**: 2026-10-03  
**Status**: Authoritative Test Infrastructure Design  

---

## 1. Executive Overview & Mission
This document defines the architecture, design principles, test harness, and execution methodology for the comprehensive opaque-box End-to-End (E2E) test suite for Phase 1: Secure Data Foundation.

The E2E test suite validates all requirements, interface contracts, security invariants, and Section 1A human authority/player consent constraints defined in:
1. `ORIGINAL_REQUEST.md` (including Section 1A prompt patch)
2. `orchestrator_phase1/PROJECT.md`
3. `docs/superpowers/specs/2026-10-02-player-dispatch-design.md`

### Core Principles
- **Opaque-Box Requirement-Driven**: Tests are derived strictly from observable interface contracts, protocol specifications, and security invariants—never coupled to internal private implementation details.
- **4-Tier Test Design Methodology**:
  - **Tier 1 (Feature Coverage)**: Comprehensive baseline validation across all 41 Phase 1 features.
  - **Tier 2 (Boundary & Corner Cases)**: Stressing extreme inputs, maximum lengths, empty values, invalid cryptographic hashes, expired nonces, unauthorized roles, and DDL rejections.
  - **Tier 3 (Cross-Feature Combinations)**: Multi-component interactions, race conditions, concurrent migrations with active runs, decision vs expiry races, and post-commit resume failure with reconciliation.
  - **Tier 4 (Real-World Application Scenarios)**: Complete end-to-end user journeys from run creation to approval interrupt, decision signing, database commit, graph resume, and terminal verification—strictly verifying `DISPATCH_ENABLED=false` and zero player delivery.
- **Section 1A Authority Separation**: Enforcing distinct club authorization, delegated human authority, player consent, and single-player fallback; guaranteeing that timeout or inactivity NEVER creates approval; and ensuring late decisions cannot overwrite prior decisions.
- **Progressive Testability**: The test suite is organized such that completed milestones execute immediately, while tests for subsequent milestones cleanly detect prerequisite availability and progressively activate without breaking builds.

---

## 2. Test Architecture & Directory Layout

The E2E test suite resides exclusively under `tests/e2e/`:

```
tests/e2e/
├── __init__.py                     # Package marker
├── conftest.py                     # E2E test fixtures, DB roles, FastAPI TestClients, authentication
├── test_tier1_features.py          # Tier 1: Baseline feature coverage (Features 1-41)
├── test_tier2_boundaries.py        # Tier 2: Boundary, schema, and cryptographic edge cases
├── test_tier3_combinations.py      # Tier 3: Pairwise, race, and cross-feature interactions
└── test_tier4_scenarios.py         # Tier 4: Real-world workflows + Section 1A authority invariants
```

### Separation of Concerns
- **Production Code Isolation**: Test code is strictly confined to `tests/e2e/`. Production code under `src/` and `migrations/` is never modified by test writers.
- **Test-Only Synthetic Artifacts**: Test keys, synthetic players, and mock LLM pipelines are isolated to test fixtures.

---

## 3. 4-Tier Test Methodology & Coverage Map

### Tier 1: Feature Coverage (Features 1–41)
Covers every feature identified in the Phase 1 Feature Inventory in `PROJECT.md`:
- **DB & Migrations (Features 1–7)**: Role bootstrap, versioned migration runner, grants policy checksumming, atomic event/outbox recording, event source integrity guard, definer-only transition events, append-only triggers.
- **Approval Lifecycle & Store (Features 8–17)**: `approval_requests` lifecycle, pair consistency constraint trigger, `approval_inconsistencies` view, 72h expiry daemon, single-use decision nonces, nonce subject typing, atomic decision commit, request revision decision, human authority compatibility, actor separation.
- **Graph Core & API (Features 18–29)**: Single approval interrupt, PostgreSQL decision re-read, terminal state string formatting (`APPROVED, NOT DISPATCHED`, `REJECTED, NO ACTION TAKEN`), correlation ID middleware, `POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/approval-request`, decision endpoint, restricted resume endpoint, health live/ready probes, CLI HMAC kill switch, actor-neutral decision validation.
- **Dispatches & State Machine (Features 30–33)**: `players` and `dispatches` schemas, tracking forward-compatibility tables, SECURITY DEFINER `fc27_dispatch_transition` single-write-path, `dispatch_transition_rules` matrix.
- **Activation Gates & Objectives (Features 34–37)**: Objective bounds policy (`fc27-objective-bounds-v1`), activation gate evaluation, gate challenge/record routes, atomic dispatch creation in approval transaction.
- **Deployment & Security (Features 38–41)**: Compose network isolation, configuration standardization, scope protection, E2E invariant verification.

### Tier 2: Boundary & Corner Cases
Stresses boundaries across all domains (>=5 tests per domain):
- **Inputs & Payloads**: Empty request bodies, oversized candidate payloads, special characters in club IDs, non-ASCII characters, SQL injection strings.
- **Identifiers & Versions**: Non-conforming thread IDs, thread IDs mismatched with club ID, invalid SHA-256 hex lengths, malformed UUID correlation IDs.
- **Cryptographic & Nonce**: Altered payload SHA-256, expired nonces (>300s TTL), reused/consumed nonces, wrong HMAC secret, mismatched signature algorithm.
- **Database Posture & Security**: Runtime `fc27_app` attempting `CREATE TABLE`, `DROP TABLE`, `ALTER TABLE`; direct `UPDATE`/`DELETE` on `domain_events` or `approval_audit`; acquiring superuser status; direct mutation of terminal thread rows.
- **Objective Metric Bounds**: Deltas exceeding bounds (e.g. 26% for `pass_accuracy`), negative observation windows, non-numeric player IDs.

### Tier 3: Cross-Feature Combinations
Validates complex multi-feature interactions:
- **Decision vs. Expiry Race**: Concurrent submission of approval decision and execution of `expire_due()` at the exact expiry boundary; verifying strict lock order prevents deadlocks and enforces exactly one winning terminal state.
- **Post-Commit Resume Crash & Reconciliation**: Decision transaction commits to PostgreSQL but the process crashes before Graph resume can complete; verifying that the database decision remains intact and startup reconciliation successfully finalizes the thread.
- **Concurrent Migration & Active Runs**: Ensuring migration runner under `fc27_owner` does not corrupt or abort active `awaiting_approval` runs.
- **Outbox & Event Atomicity**: Verifying that every `domain_event` insertion produces exactly one corresponding `outbox` row in the same transaction, and transaction abort rolls back both.
- **Active Dispatch Supersession**: Creating a new approved dispatch for an existing active player/owner/metric; verifying advisory lock serializes supersession cleanly.

### Tier 4: Real-World Scenarios & Section 1A Authority
Validates full end-to-end lifecycles and human authority constraints:
- **Happy Path Approval**: Full run creation -> LangGraph execution -> interruption -> candidate retrieval -> Commissioner decision challenge -> HMAC signing -> commit -> resume -> verified `APPROVED, NOT DISPATCHED` with `dispatch_enabled=false` and zero player delivery.
- **Happy Path Rejection**: Full run creation -> interruption -> Commissioner rejection -> commit -> resume -> verified `REJECTED, NO ACTION TAKEN`.
- **Revision Request Flow**: Full run creation -> interruption -> Commissioner `REQUEST_REVISION` with notes -> verified `REVISION_REQUESTED`.
- **Section 1A Authority Separation**:
  - Club authorization vs Player consent: Commissioner authorization cannot masquerade as player consent, and player consent cannot masquerade as club authorization.
  - Delegated Authority: Time-bounded, revocable delegation; expired or revoked delegations fail closed; actions outside delegation scope are rejected.
  - Affected-Player Fallback: Single-player developmental assignment fallback only; multi-player and club-wide packages strictly prohibited from player fallback.
  - Timeout Inactivity Invariant: Request expiration or inactivity NEVER creates an approval decision.
  - Late-Decision Conflicts: Late decision attempts cannot overwrite prior decisions; conflicts are recorded immutably for resolution.

---

## 4. Test Harness & Fixtures (`tests/e2e/conftest.py`)

### Database Provisioning
- **Per-Session or Per-Test Isolation**: Tests execute against PostgreSQL 16 using either `TEST_POSTGRES_URI` (session-scoped test database dynamically allocated via `uuid.uuid4()`) or mock harnesses where appropriate.
- **Role Harness**:
  - `pg_migrator_uri`: Connects as `fc27_migrator` (member of `fc27_owner`) to execute migrations.
  - `pg_app_uri` (`pg_uri`): Connects as `fc27_app` (DML only, least-privileged runtime).
  - `pg_dispatch_uri`: Connects as `fc27_dispatch` (Phase 3 worker role).
  - `pg_admin_db_uri`: Connects as PostgreSQL superuser to inspect database posture and test permissions.

### Authentication & Token Fixtures
- **Service Role**: `X-API-Key: svc-test-token-0123456789abcdef0123456789abcdef` (used for `POST /v1/runs`, `GET /v1/runs/{id}`).
- **Commissioner Role**: `X-API-Key: commissioner-test-token-0123456789abcdef01234567` (used for decision challenge and decision submission).
- **HMAC Signatures**: Signed challenge fixtures using `APPROVAL_SIGNING_SECRET`.

### API & Runner Fixtures
- `e2e_client`: FastAPI `TestClient` initialized with full middleware stack, event publisher capture, and least-privileged database connection.
- `e2e_runner`: LangGraph `GraphRunner` wired with PostgreSQL checkpointer and decision reader.

---

## 5. Progressive Testability Strategy

To satisfy progressive testability across milestones:
1. **Direct Execution for M0**: All M0 features (roles, migrations, events, outbox, append-only triggers) are tested directly against existing tables and functions.
2. **Feature Detection & Safe Graceful Skipping**: For features in milestones M1–M5, tests inspect whether required tables (e.g. `approval_requests`, `dispatches`, `activation_gate_log`) or routes (e.g. `/v1/runs`, `/v1/approval-requests`) exist. If the milestone has not yet migrated or deployed, the test records an explicit informative skip:
   ```python
   if not table_exists(conn, "approval_requests"):
       pytest.skip("Milestone M1 required: table approval_requests not yet migrated")
   ```
   When the respective milestone completes, the exact same test activates automatically and verifies full compliance without modification.

---

## 6. Execution Commands

To execute the E2E test suite:

```bash
# Run the complete E2E test suite
pytest tests/e2e/ -v

# Run by Tier
pytest tests/e2e/test_tier1_features.py -v
pytest tests/e2e/test_tier2_boundaries.py -v
pytest tests/e2e/test_tier3_combinations.py -v
pytest tests/e2e/test_tier4_scenarios.py -v

# Run with PostgreSQL environment
TEST_POSTGRES_URI=postgresql://user:pass@localhost:5432/postgres pytest tests/e2e/ -v
```

---

## 7. Expected Outputs & Authoritative Oracles

| Operation | Expected Status | Observable Output / Verification |
|-----------|-----------------|----------------------------------|
| Run Creation | 201 Created | JSON contains `thread_id` matching `fc27-<club>-run-...`, `status="awaiting_approval"`, `dispatch_enabled=false` |
| Decision Challenge | 200 OK | JSON contains `nonce` (>=32 chars), `payload_sha256`, `expires_at` within 300s |
| Decision (Approve) | 200 OK | Database commit in `approval_audit`, `status="approved"`, terminal projection `"APPROVED, NOT DISPATCHED"` |
| Decision (Reject) | 200 OK | Database commit in `approval_audit`, `status="rejected"`, terminal projection `"REJECTED, NO ACTION TAKEN"` |
| Forged Resume | 403 / Exception | `ApprovalNotCommitted` raised; database status unchanged |
| DDL by `fc27_app` | Exception | PostgreSQL `insufficient_privilege` (SQLSTATE 42501) |
| Timeout Inactivity | 409 Conflict | Request marked `expired`; zero approval records; dispatch blocked |
