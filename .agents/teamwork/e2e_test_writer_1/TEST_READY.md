# E2E Test Suite Readiness Report (TEST_READY.md)
**Project**: Player-Dispatch Programme — Phase 1: Secure Data Foundation  
**Author**: `e2e_test_writer_1` (Teamwork E2E Test Writer)  
**Date**: 2026-10-03  
**Status**: Ready for Verification & Integration  

---

## 1. Executive Summary
The comprehensive, opaque-box, requirement-driven End-to-End (E2E) test suite for Phase 1: Secure Data Foundation has been designed and implemented in `tests/e2e/`.

The test suite strictly adheres to:
1. `ORIGINAL_REQUEST.md` (including Section 1A prompt patch)
2. `orchestrator_phase1/PROJECT.md`
3. 4-Tier Test Design Methodology (Tiers 1–4)
4. Non-negotiable security invariants (`DISPATCH_ENABLED=false`, zero player delivery, PostgreSQL authoritative approval, single interrupt, least privilege).

**Zero production code was modified**: `src/` and `migrations/` remain completely untouched.

---

## 2. Test File Inventory & Layout

| File Path | Description | Scope / Tier | Test Count |
|-----------|-------------|--------------|------------|
| `tests/e2e/__init__.py` | Package marker | Infrastructure | - |
| `tests/e2e/conftest.py` | Shared fixtures, token constants, DB inspection helpers (`table_exists`, `column_exists`, `view_exists`, `function_exists`), candidate generators | Harness | - |
| `tests/e2e/test_tier1_features.py` | Baseline feature coverage across all 41 Phase 1 features in `PROJECT.md` Feature Inventory | Tier 1 (Features 1–41) | 41 |
| `tests/e2e/test_tier2_boundaries.py` | Boundary, stress, and corner cases (empty inputs, max lengths, tampered hashes, expired nonces, DDL rejection, metric bounds) | Tier 2 (Boundaries) | 25 |
| `tests/e2e/test_tier3_combinations.py` | Pairwise and cross-feature interactions (decision vs expiry races, post-commit resume failure & reconciliation, concurrent migrations) | Tier 3 (Combinations) | 5 |
| `tests/e2e/test_tier4_scenarios.py` | Real-world application scenarios and Section 1A human authority & player consent invariants | Tier 4 & Section 1A | 9 |

**Total Test Cases**: 80 test cases across Tiers 1–4.

---

## 3. Tier Coverage Breakdown

### Tier 1: Feature Coverage (`test_tier1_features.py`)
Covers all 41 features from `PROJECT.md`:
- **Features 1–7 (Database Infrastructure & Migrations)**: Role bootstrap (`fc27_owner` NOLOGIN, `fc27_migrator`, `fc27_app`, `fc27_dispatch`), public CREATE revoked, migration runner order, grants policy checksum tracking, atomic event & outbox insertion, event source guard, transition event caller restriction, append-only table guards.
- **Features 8–17 (Approval Lifecycle & Nonces)**: `approval_requests` table schema, pair consistency constraint trigger, `fc27_approval_inconsistencies` view, 72h expiry daemon, single-use decision nonce generation (TTL <= 300s), nonce subject typing, authoritative decision commit to `approval_audit`, `REQUEST_REVISION` decision, Section 1A human authority schema compatibility, actor & authority basis separation.
- **Features 18–29 (Graph Core & Persistent API)**: Exactly one approval interrupt, PostgreSQL decision re-read upon resume, terminal state strings (`APPROVED, NOT DISPATCHED`, `REJECTED, NO ACTION TAKEN`), correlation ID propagation, `POST /v1/runs` durable creation, `GET /v1/runs/{id}` safe projection, approval request candidate summary, decision commit before resume, resume route refuses authorization in body, health liveness & readiness probes, HMAC kill switch, actor-neutral decision validation.
- **Features 30–33 (Dispatches & State Machine)**: `players` and `dispatches` schemas, tracking forward compatibility, SECURITY DEFINER `fc27_dispatch_transition` single write path, `dispatch_transition_rules` matrix.
- **Features 34–37 (Objectives & Activation Gates)**: Objective bounds validation (`fc27-objective-bounds-v1`), activation gate evaluation failing closed, gate challenge/record routes refusing `G-OUTBOX`, atomic dispatch creation in approval transaction.
- **Features 38–41 (Topology, Configuration & Invariants)**: Compose topology & secrets, standardized environment configuration, scope protection & phase isolation, runtime least-privilege verification.

### Tier 2: Boundary & Corner Cases (`test_tier2_boundaries.py`)
5+ tests across each of 5 critical boundary domains:
- **Input & Payload (5 tests)**: Empty bodies, 128-char max thread ID vs 129-char rejection, non-numeric club ID characters (SQL injection strings), Pydantic `extra="forbid"`, malformed JSON syntax.
- **Cryptographic & Nonces (5 tests)**: Tampered `payload_sha256`, invalid SHA-256 length (63, 65, non-hex), expired nonces (>300s), reused/consumed nonces, tampered HMAC signature.
- **State & Lifecycle (5 tests)**: Stale package version in decision, re-running against terminal thread, direct DB update of `package_version` while `awaiting_approval`, decisions referencing non-existent threads, illegal graph thread statuses (`dispatched`).
- **Role, Privilege & DDL (5 tests)**: `fc27_app` executing `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`, `CREATE FUNCTION`, `SET ROLE fc27_owner`.
- **Objective Bounds (5 tests)**: Delta > 25% for `pass_accuracy`, negative delta, `max_matches > 10`, `min_matches > max_matches`, non-numeric player IDs.

### Tier 3: Cross-Feature Combinations (`test_tier3_combinations.py`)
- Decision vs. Expiry race condition: Strict lock ordering prevents deadlocks and enforces exactly one winning terminal state.
- Post-commit Graph resume crash & reconciliation: DB decision preserved immutably; replayed resume is safe.
- Concurrent migration check & active run: Migrator running under `fc27_owner` does not corrupt or abort active `awaiting_approval` runs.
- Domain events and outbox atomicity: Aborted transaction rolls back both event and outbox row.
- Identical decision replay vs. conflicting decision: Replayed decision returns safely; conflicting decision fails with 409.

### Tier 4: Real-World Scenarios & Section 1A Invariants (`test_tier4_scenarios.py`)
- Full golden path approval journey: Run creation -> interrupt -> candidate review -> Commissioner challenge -> HMAC signing -> commit -> resume -> verified `APPROVED, NOT DISPATCHED` with `dispatch_enabled=false` and zero player delivery.
- Full rejection journey: Run creation -> interrupt -> Commissioner rejection -> commit -> resume -> verified `REJECTED, NO ACTION TAKEN`.
- Revision request workflow: `REQUEST_REVISION` with notes; original package remains immutable.
- Section 1A Authority Separation: Club authorization vs. Player consent distinct scopes.
- Section 1A Delegation Foundation: Time-bounded, revocable delegations; expired delegations fail closed.
- Section 1A Player Fallback Foundation: Single-player developmental assignment fallback only; multi-player and club-wide packages strictly prohibited from player fallback.
- Section 1A Inactivity & Timeout Invariant: Request expiration or inactivity NEVER creates an approval decision!
- Section 1A Late-Decision Handling: Late decision attempts cannot overwrite prior terminal decisions.
- Section 1A Actor Invariant: Player agents, LLMs, and background workers cannot act as decision actors.

---

## 4. How to Run the Tests

```bash
# Run the entire E2E test suite
pytest tests/e2e/ -v

# Run individual tiers
pytest tests/e2e/test_tier1_features.py -v
pytest tests/e2e/test_tier2_boundaries.py -v
pytest tests/e2e/test_tier3_combinations.py -v
pytest tests/e2e/test_tier4_scenarios.py -v

# Run with PostgreSQL admin connection
TEST_POSTGRES_URI=postgresql://user:pass@localhost:5432/postgres pytest tests/e2e/ -v
```

---

## 5. Escalation & Implementation Guidance for Milestone Workers

1. **Section 1A Authority Schema (Milestone 1)**: Ensure the database migration introduces `authority_policies`, `authority_delegations`, and `player_assignments`, and adds `authority_basis` to `approval_audit`.
2. **Approval Consistency View (Milestone 1)**: Ensure `fc27_approval_inconsistencies` view is created in `0003_dispatch_foundation.sql` Part B.
3. **Graph API Versioned Routes (Milestone 2)**: Ensure `/v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/approval-request`, and `/v1/runs/{id}/resume` are mounted in `src/graph_api.py`.
4. **Dispatches & Single Write Path (Milestone 3)**: Ensure `public.fc27_dispatch_transition` is created as `SECURITY DEFINER` owned by `fc27_owner` and executable by `fc27_app` and `fc27_dispatch`.
5. **No Dispatch Invariant**: All endpoints and workers must enforce `DISPATCH_ENABLED=false` at all times.
