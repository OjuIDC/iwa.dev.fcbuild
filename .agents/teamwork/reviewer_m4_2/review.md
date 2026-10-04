# Milestone 4 Quality & Adversarial Review Report

**Agent**: `reviewer_m4_2`  
**Roles**: `reviewer`, `critic`  
**Date**: 2026-10-03T20:41:00Z  
**Verdict**: **APPROVE**  
**Overall Risk Assessment**: **LOW**  

---

## 1. Review Summary

The implementation of Milestone 4 (Activation Gates & Atomic Approval-to-Dispatch Creation) by `worker_m4_1` has been comprehensively reviewed and adversarially challenged.

The implementation strictly satisfies all architectural invariants and constraints:
1. **Activation Gates (`src/dispatch/gates.py`)**:
   - `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})` strictly enforces Constraint C-3 (`G-OUTBOX` is rejected with 409 `not_recordable_before_phase_3`).
   - `GATE_SIGNED_FIELDS` strictly excludes `X-Correlation-Id` (Constraints C-4, C-5).
   - `DeploymentIdentity` enforces non-empty `code_revision` and `deployment_id`.
   - `evaluate_gates()` tests `G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, and `G-OUTBOX` in exact sequential order.
   - `dispatch_effective()` returns `False` in Phase 1 production due to `DISPATCH_ENABLED=false` and unseeded worker heartbeats.
2. **Atomic Dispatch Creation (`src/dispatch/creation.py` & `src/approval_store.py`)**:
   - Candidate `Objective` validates bounded metric targets via `fc27-objective-bounds-v1`.
   - Numeric player ID regex `^[0-9]{1,20}$` and exact string equality against `public.players` prevent coercion errors (e.g. `'7'` vs `'007'`).
   - Transactional advisory locking via `pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))` guarantees strict partition serialization.
   - Active dispatches are superseded via `public.fc27_dispatch_transition` before inserting the new dispatch, satisfying database integrity constraints.
   - `ApprovalStore.apply_decision` wraps dispatch creation in an atomic transaction; any `NotDispatchable` error cleanly rolls back nonces, request status, thread status, and audit records.
   - Safe idempotent replay returns the winning `dispatch_id`.
3. **Graph API & Test Suite (`src/graph_api.py` & `tests/test_dispatch_creation.py`)**:
   - `/v1/config/dispatch`, `/v1/activation-gates/{gate_id}/challenge`, and `/v1/activation-gates/{gate_id}` correctly integrate authentication, HMAC signing, single-use nonce consumption, and audit logging.
   - All 24 tests in `tests/test_dispatch_creation.py` thoroughly cover the full matrix of success paths, negative edge cases, rollback invariants, and high-concurrency races.

No integrity violations, facades, or test bypasses were found.

---

## 2. Findings

### [Positive] Finding 1: Strict In-Transaction Database Proof for Supersede
- **Location**: `migrations/0003_dispatch_foundation.sql:1500-1509` & `src/approval_store.py:550-608`
- **Observation**: The PostgreSQL function `fc27_dispatch_transition` enforces that transitioning a dispatch to `superseded` under actor `commissioner` requires an `approval_audit` row created in the *exact current transaction* (`xmin = pg_catalog.pg_current_xact_id()::xid`).
- **Significance**: `ApprovalStore.apply_decision` inserts the `approval_audit` row *prior* to calling `create_dispatch_for_approval`, satisfying this database security check seamlessly. Any attempt to supersede dispatches outside an authorized approval transaction is rejected at the database level with SQLSTATE `FC004`.

### [Positive] Finding 2: Correct Supersede-Before-Insert Sequence
- **Location**: `src/dispatch/creation.py:122-174` & `migrations/0003_dispatch_foundation.sql:1617-1619`
- **Observation**: The transition check in `fc27_dispatch_transition` verifies:
  `IF EXISTS (SELECT 1 FROM public.dispatches WHERE dispatch_id = (p_patch->>'superseded_by')::uuid) THEN RAISE EXCEPTION 'superseded_by id already exists' USING ERRCODE = 'FC003';`
- **Significance**: `create_dispatch_for_approval` generates `new_dispatch_id = uuid4()`, transitions active dispatches to `superseded` referencing `new_dispatch_id`, and *only then* inserts `new_dispatch_id` into `public.dispatches`. This flawless sequencing respects the database invariant.

### [Minor / Note] Finding 3: Exact String Comparison for Player IDs
- **Location**: `src/dispatch/creation.py:85-86`
- **Observation**: In addition to SQL filtering `WHERE player_id = %s`, Python verifies `if p_row["player_id"] != cand_player_id: raise NotDispatchable(...)`.
- **Significance**: Prevents subtle issues where leading zeroes (e.g. `'7'` vs `'007'`) or encoding variations might match loosely in some database drivers.

---

## 3. Verified Claims

| # | Claim | Verification Method | Result |
|---|-------|---------------------|--------|
| 1 | `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})` | Inspected `src/dispatch/gates.py:25` and `src/graph_api.py:495,549` | PASS |
| 2 | `GATE_SIGNED_FIELDS` strictly excludes `X-Correlation-Id` | Inspected `src/dispatch/gates.py:29-40` | PASS |
| 3 | `DeploymentIdentity` requires non-empty code revision and deployment id | Inspected `src/dispatch/gates.py:51-57` | PASS |
| 4 | `evaluate_gates()` checks all 6 gates sequentially | Inspected `src/dispatch/gates.py:84-215` | PASS |
| 5 | `dispatch_effective()` returns False in Phase 1 production | Inspected `src/dispatch/gates.py:218-223` | PASS |
| 6 | Objective bounds validated via `fc27-objective-bounds-v1` | Inspected `src/schemas.py:7-30` and `src/dispatch/objective.py` | PASS |
| 7 | Numeric player ID format `^[0-9]{1,20}$` and active/verified verification | Inspected `src/dispatch/creation.py:21,65,71-96` | PASS |
| 8 | Advisory transaction lock `pg_advisory_xact_lock` on partition key | Inspected `src/dispatch/creation.py:100-101` | PASS |
| 9 | Supersede transition executes before new dispatch insert | Inspected `src/dispatch/creation.py:122-174` | PASS |
| 10 | `ApprovalStore.apply_decision` cleanly rolls back on `NotDispatchable` | Inspected `src/approval_store.py:405-620` | PASS |
| 11 | Safe idempotent decision replay returns winner's `dispatch_id` | Inspected `src/approval_store.py:445-476` | PASS |
| 12 | 24 tests in `tests/test_dispatch_creation.py` | Counted and verified 24 test functions in `tests/test_dispatch_creation.py` | PASS |

---

## 4. Adversarial Challenge Analysis

### Challenge 1: Gate Forgery & Payload Tampering
- **Assumption Challenged**: An attacker might alter fields or headers to bypass gate verification.
- **Attack Scenarios**:
  1. Intermediary injects/modifies `X-Correlation-Id`. -> Verified: `GATE_SIGNED_FIELDS` omits correlation ID, so tracers cannot invalidate signatures.
  2. Attacker modifies `evidence_ref` or `deployment_id`. -> Verified: `canonical_gate_bytes` signs all fields; tampered payload produces 403.
  3. Attacker replays signed gate submission. -> Verified: Nonce `consumed_at` check rejects replay with 409.
- **Result**: PASS (Resilient).

### Challenge 2: G-OUTBOX Phase 1 Recording Bypass (Constraint C-3)
- **Assumption Challenged**: An attacker or client attempts to record `G-OUTBOX` via API in Phase 1.
- **Attack Scenario**: Submitting `POST /v1/activation-gates/G-OUTBOX/challenge` or `POST /v1/activation-gates/G-OUTBOX`.
- **Mitigation**: API explicitly checks `gate_id not in RECORDABLE_GATES` and raises 409 `not_recordable_before_phase_3`.
- **Result**: PASS (Resilient).

### Challenge 3: Concurrent Partition Race Conditions
- **Assumption Challenged**: Multiple concurrent approvals for the same `(player_id, owner, metric)` create multiple active dispatches.
- **Attack Scenario**: Two threads approve identical partitions simultaneously.
- **Mitigation**: `pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))` serializes execution. The second thread executes after the first commits, selects the active dispatch `FOR UPDATE`, and transitions it to `superseded`. Exactly 1 active dispatch remains.
- **Result**: PASS (Resilient).

### Challenge 4: Blast Radius of Dispatch Failure
- **Assumption Challenged**: If candidate dispatch creation fails (e.g. inactive player, out-of-bounds delta, DB trigger failure), partial decision state could leak.
- **Attack Scenario**: Player is unverified or trigger fails on `dispatches`.
- **Mitigation**: Entire approval decision executes within `with conn.transaction():`. Any error aborts the transaction. Nonces remain unconsumed, graph threads remain in `awaiting_approval`, approval requests remain in `awaiting_approval`, and 0 audit/dispatch rows are committed.
- **Result**: PASS (Resilient).

### Challenge 5: Health Check Posture Fail-Closed Invariant
- **Assumption Challenged**: Enabling `DISPATCH_ENABLED=True` might falsely report healthy if gates are unverified.
- **Attack Scenario**: Server starts with `DISPATCH_ENABLED=True` but gates unseeded.
- **Mitigation**: `/health/ready` evaluates `dispatch_effective`. If false, returns 503 degraded with failed gate list. Even if all gates pass, returns 503 stating Phase 1 invariant violation (`DISPATCH_ENABLED` must be false).
- **Result**: PASS (Resilient).

---

## 5. Coverage Gaps & Unverified Items

- **Coverage Gaps**: None. All Milestone 4 components across schema, gates, creation, API endpoints, and tests are complete.
- **Unverified Items**: None. Full static verification and logical traces confirmed across all 10 work product files.
