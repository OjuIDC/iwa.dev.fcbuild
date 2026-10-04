# Handoff Report: Milestone 4 Gates & Dispatch Creation Exploration

**Agent**: `explorer_m4_creation_1`  
**Role**: Milestone 4 Gates & Dispatch Creation Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_creation_1`  
**Timestamp**: 2026-10-03T20:15:00Z  
**Type**: Hard (Task Complete)  

---

## 1. Observation

1. **Plan & Specification Contracts**:
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md`, Task 8 (lines 919–1040):
     Specifies `src/dispatch/gates.py` (`GateResult`, `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})`, `GATE_SIGNED_FIELDS`, `DeploymentIdentity`, `evaluate_gates()`, `dispatch_effective()`).
     Specifies `src/dispatch/creation.py` (`NotDispatchable`, `create_dispatch_for_approval()`, `pg_advisory_xact_lock`, supersession via `transition()`, `dispatches` insert, `dispatch.created` event emission).
     Specifies `src/graph_api.py` settings (`FC27_CODE_REVISION`, `FC27_DEPLOYMENT_ID`), `reevaluate_gates(app)`, challenge and record routes for `/v1/activation-gates/{gate_id}`.
   - `docs/superpowers/specs/2026-10-02-player-dispatch-design.md`, §31.2:
     Defines the six activation gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`). Specifies that `G-OUTBOX` is manual but outside `RECORDABLE_GATES` in Phase 1 (returns 409 `not_recordable_before_phase_3`). Specifies that empty `code_revision` or `deployment_id` makes all manual gates fail.
   - `docs/superpowers/specs/2026-10-02-player-dispatch-design.md`, §7.2:
     Specifies atomic dispatch creation inside `apply_decision` when `dispatch_effective` is true.

2. **Database Schema & Constraints**:
   - `migrations/0003_dispatch_foundation.sql`, lines 674–720:
     Table `public.approval_nonces` already has columns `subject_type`, `gate_id`, `revoked_at` with check constraint:
     `subject_type = 'activation_gate' AND gate_id IS NOT NULL AND thread_id IS NULL AND dispatch_id IS NULL AND user_id IS NULL`.
   - `migrations/0003_dispatch_foundation.sql`, lines 841–904:
     Table `public.dispatches` contains 25 columns, including `superseded_by uuid REFERENCES public.dispatches(dispatch_id) DEFERRABLE INITIALLY DEFERRED`, `created_correlation_id uuid NOT NULL`, `approved_instruction jsonb NOT NULL`, and `status text GENERATED ALWAYS AS (dispatch_status) STORED`.
   - `migrations/0003_dispatch_foundation.sql`, lines 920–964:
     Trigger `fc27_dispatch_insert_guard` requires `dispatch_status = 'pending'`, `version = 1`, `plan_version = 0`, and null terminal timestamps on insert.
   - `migrations/0003_dispatch_foundation.sql`, lines 1365–1774:
     `public.fc27_dispatch_transition` enforces:
     - Line 1499–1509: `commissioner` actor performing `superseded` requires `xmin = pg_current_xact_id()::xid` in `approval_audit`.
     - Line 1610–1620: `p_patch ? 'superseded_by'`, and asserts `NOT EXISTS (SELECT 1 FROM dispatches WHERE dispatch_id = superseded_by)`.
     - Lines 1756–1771: atomically inserts into `domain_events` (`dispatch.superseded`) and `outbox`.

3. **Current Codebase Seams**:
   - `src/approval_store.py`, lines 590–612:
     Currently invokes `dispatch_creator` with legacy placeholder kwargs (`conn, thread_id=thread_id, thread=thread, audit_id=audit_id, req_id=req["approval_request_id"], correlation_id=corr_id`).
     Candidate is already resolved at line 512 via `resolve_candidate(thread["state"], intent)`.
     Lines 445–474 handle idempotent replay but currently set `dispatch_id=None` instead of querying `SELECT dispatch_id FROM dispatches WHERE approval_id = %s`.
   - `src/graph_api.py`, lines 568–576, 582–598:
     `apply_decision_internal` passes `dispatch_enabled=settings.dispatch_enabled` without passing `dispatch_creator`.
     `dispatch_block` is currently static (`enabled=False, created=False`).
   - `src/dispatch/transition.py`:
     Wraps `public.fc27_dispatch_transition` cleanly, accepting `conn, dispatch_id, expected_status, expected_version, to_status, patch, actor, correlation_id`.
   - `src/dispatch/objective.py` & `src/schemas.py`:
     `Objective` model already validates bounds against `METRIC_BOUNDS` for policy `fc27-objective-bounds-v1`.

---

## 2. Logic Chain

1. **Gate Evaluation & Deployment Identity Binding**:
   - *Observation 1* establishes that manual gates (`G-ID`, `G-ATOMIC`, `G-OUTBOX`) must match the running deployment's `code_revision`, `deployment_id`, `fc_version`, and `platform`.
   - Records with foreign deployment identifiers or missing identity fields must fail closed without throwing unhandled exceptions.
   - Therefore, `DeploymentIdentity` must encapsulate validation (`is_valid()`), and `evaluate_gates()` must filter `public.activation_gate_log` by all four identity attributes.
   - `dispatch_effective()` must compute `configured AND all(r.passed for r in results)` so that any failing gate blocks dispatch even when `DISPATCH_ENABLED=true`.

2. **Concurrency Control & Partition Locking**:
   - Multiple Commissioner approvals for recommendations targeting the same player and metric partition must not conflict or generate primary key / unique index violations on `dispatches_one_active_per_objective`.
   - *Observation 2* shows that `dispatches_one_active_per_objective` prohibits two active dispatches for the same `(player_id, owner, metric)`.
   - Therefore, `create_dispatch_for_approval` must acquire `SELECT pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))` before querying or superseding active dispatches.

3. **Atomic Supersession Before Dispatch Insertion**:
   - *Observation 2* notes that `fc27_dispatch_transition` checks `IF EXISTS (SELECT 1 FROM dispatches WHERE dispatch_id = superseded_by) RAISE EXCEPTION 'superseded_by id already exists'`.
   - Therefore, the supersede transition for existing active dispatches MUST be executed **before** the new dispatch row is inserted into `public.dispatches`.
   - Because `dispatches.superseded_by` has a foreign key constraint marked `DEFERRABLE INITIALLY DEFERRED`, PostgreSQL defers validation until transaction commit. When the new dispatch is inserted in the next statement, the foreign key constraint is satisfied at commit time.
   - Furthermore, `fc27_dispatch_transition` requires that `approval_audit` was written in the same transaction (`xmin = pg_current_xact_id()::xid`), which holds because `ApprovalStore.apply_decision` inserts `approval_audit` immediately before calling `dispatch_creator`.

4. **Single Transaction Boundary & Rollback Invariant**:
   - *Observation 3* shows that `ApprovalStore.apply_decision` encloses all decision writes in `with conn.transaction():`.
   - If `dispatch_creator` raises `NotDispatchable` or encounters an unhandled database error (e.g., an invalid player, unverified identity, or trigger error on `dispatches`), the exception forces a transaction rollback.
   - Consequently, the nonce consumption, thread/request status updates, audit record, and `approval.decided` event all roll back cleanly, preserving thread resumability and request validity.

---

## 3. Caveats

1. **Production Dispatch Disabled Invariant**:
   In Phase 1, `DISPATCH_ENABLED=false` must remain enforced in all production environments. Automated gate satisfaction for `G-WORKER` (which requires a live dispatch worker heartbeat) cannot legitimately occur in production Phase 1.
2. **Synthetic Test Fixtures**:
   Tests verifying `dispatch_effective=True` (e.g., `enabled_client`) must synthesize a fresh `service_heartbeats` row for `dispatch-worker` and directly insert a passed `G-OUTBOX` row, because `G-OUTBOX` is explicitly unrecordable via the API in Phase 1 (`RECORDABLE_GATES` = `{"G-ID", "G-ATOMIC"}`).
3. **Correlation ID Isolation**:
   `X-Correlation-Id` is strictly a distributed tracing and observability attribute. It is passed into `fc27_dispatch_transition`, stored in `dispatches.created_correlation_id`, and attached to domain events, but MUST NOT be included in HMAC-SHA256 signature payloads.

---

## 4. Conclusion

The technical architecture for Milestone 4 is fully resolved and documented in `creation_analysis.md`. The design guarantees:
- Zero data drift: gate evaluations and manual gate recordings are cryptographically bound to deployment identity via single-use nonces.
- Complete atomicity: approval decisions and dispatch creations execute within a single transaction boundary.
- Concurrency serialization: advisory xact partitioning ensures deterministic supersession without unique constraint collisions.
- Strict isolation: failure at any step of dispatch creation safely rolls back Commissioner decisions.

---

## 5. Verification Method

1. **Static Analysis & Inspection**:
   - Inspect `creation_analysis.md` in `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_creation_1\creation_analysis.md`.
   - Confirm function signatures and constants match Task 8 lines 940–976.
2. **Test Implementation Verification**:
   When implementing Milestone 4, run the dedicated test suite:
   ```bash
   python -m pytest -o addopts="" -q tests/test_dispatch_creation.py
   ```
3. **Regression Test Verification**:
   Verify regression compatibility across existing milestones:
   ```bash
   python -m pytest -o addopts="" -q tests/test_dispatch_transition.py tests/test_dispatch_schema.py tests/test_approval_lifecycle.py
   ```
4. **Invalidation Conditions**:
   - Modifying `GATE_SIGNED_FIELDS` to include `X-Correlation-Id` invalidates C-4 and C-5.
   - Inserting `dispatches` before executing the supersede `transition()` calls causes SQLSTATE `FC003` (`superseded_by id already exists`).
   - Calling `dispatch_creator` outside `conn.transaction()` violates atomic approval-to-dispatch consistency (AC-1/AC-2).
