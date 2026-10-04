# Handoff Report: Milestone 1 Approval Store & Decision Engine Exploration

**Agent**: `explorer_m1_store_1`  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_store_1`  
**Date**: 2026-10-03T14:23:00Z  
**Type**: Hard Handoff (Investigation & Design Complete)  

---

## 1. Observation

1. **`src/approval_store.py:48-52` (`__init__`)**:
   ```python
   class ApprovalStore:
       def __init__(self, conninfo: str):
           self.conninfo = conninfo
   ```
   No `request_ttl` parameter is accepted or validated.

2. **`src/approval_store.py:88-107` (`complete_run`)**:
   `complete_run` updates only `graph_threads` table (lines 99–103). It does not insert into `public.approval_requests`, does not compute `expires_at`, does not call `record_event('approval.created')`, and returns state without `approval_request_id` or `approval_expires_at`.

3. **`src/approval_store.py:126-160` (`issue_challenge`)**:
   Locks only `graph_threads` with `FOR SHARE` (line 132). Does not call `expire_due()`, does not query `public.fc27_approval_inconsistencies`, does not lock `approval_requests`, and does not bound nonce `expires_at` by request deadline.

4. **`src/approval_store.py:188-189` (`apply_decision` replay handling)**:
   ```python
   if nonce["consumed_at"] is not None:
       raise StoreError(409, "nonce already used (replay rejected)")
   ```
   Any replay of an already consumed nonce immediately throws `StoreError(409)`, directly conflicting with the requirement that identical idempotent replays must safely succeed and return the committed decision.

5. **`src/approval_store.py:175-206` (`apply_decision` lock order & consistency)**:
   Locks `approval_nonces FOR UPDATE` (line 181), then `graph_threads FOR UPDATE` (line 197). It never locks `approval_requests`. It does not check `fc27_approval_inconsistencies`. It does not validate Section 1A actor types or authority bases. It does not write `proof_type`, `correlation_id`, or `approval_request_id` to `approval_audit`. It does not emit `approval.decided` via `record_event`.

6. **`src/approval_store.py` (missing methods)**:
   `expire_due` and `pending_approvals` do not exist in `ApprovalStore`.

7. **`src/decision.py:49-54` (`apply_decision_to_state`)**:
   ```python
   if approved:
       new_state["dispatch"] = {
           "owner": intent.owner.value,
           "selection": intent.selection.value,
           "candidate": candidate,
           "dispatched_to_player_agent": intent.player_id,
       }
   ```
   The legacy `new_state["dispatch"]` dictionary is still populated on approval.

8. **`docs/superpowers/specs/2026-10-02-player-dispatch-design.md:413`**:
   "Global lock order. Every writer locks in this order: open `approval_nonces` rows for the thread (in nonce order), then `approval_requests`, then `graph_threads`."

9. **`ORIGINAL_REQUEST.md:967-1413` (Section 1A Patch)**:
   Requires authority model separating `actor_type`, `authority_basis` (`primary`), `decision_scope`. In Phase 1, only the bootstrap actor (Commissioner, primary authority) may submit active decisions; player decisions and fallback must be rejected.

---

## 2. Logic Chain

1. **Schema Trigger Synchronization**:
   - From Observation 2 and the deferred constraint trigger `fc27_check_approval_pair()` defined in migration 0003 Part B, updating `graph_threads` to `awaiting_approval` without inserting a corresponding `approval_requests` row will fail at commit time with `approval pair:`.
   - Therefore, `complete_run()` must atomically insert `approval_requests` (`status='awaiting_approval'`, `expires_at = now() + request_ttl`) and link `approval_request_id` to `graph_threads`.
   - Furthermore, Spec §14.5 requires an immutable event record, so `record_event('approval.created', ...)` must be executed in that same transaction.

2. **Deadlock Freedom at Expiration Deadline**:
   - From Observation 8, the global lock order is `approval_nonces` $\to$ `approval_requests` $\to$ `graph_threads`.
   - In Observation 3 and 5, `issue_challenge` and `apply_decision` violated this sequence by skipping `approval_requests`.
   - Concurrent execution of `expire_due()` and `apply_decision()` at `expires_at` would cause deadlocks if lock orders diverged.
   - Enforcing identical lock acquisition order in both `apply_decision()` and `expire_due()` guarantees deadlock-free resolution (Review Focus 2).

3. **Safe Idempotent Replay Resolution**:
   - From Observation 4, the existing code throws 409 whenever `nonce["consumed_at"] is not None`.
   - In distributed API workflows, retries on committed decisions occur due to client timeouts, proxy drops, or reconnections.
   - In accordance with ORIGINAL_REQUEST.md §3.4 and PROJECT.md Feature 14, if `nonce["consumed_at"]` is set, `apply_decision()` must query `approval_audit` and verify whether the stored record matches the incoming decision identically (same thread, version, signature hash, decision, candidate, player).
   - If identical, it must return the committed decision state. Only if there is a mismatch or conflict should it raise 409.

4. **Section 1A Authority Validation & Isolation**:
   - From Observation 9, Phase 1 is strictly limited to bootstrap authorization.
   - Any input specifying `actor_type="player"`, `authority_basis="affected_player_fallback"`, or `authority_basis="explicit_delegate"` must be explicitly rejected with 403 `StoreError`.
   - The authoritative `approval_audit` row must persist `actor_type='commissioner'`, `authority_basis='primary'`, `authority_role='commissioner'`, and `decision_scope='club'`.

5. **Phase 1 Invariant: Elimination of Dispatch Dictionary**:
   - From Observation 7 and Non-negotiable Invariant 1 (`DISPATCH_ENABLED=false`) and Invariant 4 ("No Graph node may transition to dispatched..."), writing `new_state["dispatch"]` violates the Phase 1 boundary.
   - Removing lines 49–54 in `src/decision.py` ensures no phantom dispatch record exists in state.

---

## 3. Caveats

1. **Read-Only Explorer Scope**: In accordance with the Explorer archetype rules, no changes were directly written to `src/approval_store.py` or `src/decision.py`. The implementer agent must apply the provided code blocks.
2. **Migration 0003 Part B Dependency**: The new store methods depend on the SQL migration 0003 Part B (`approval_requests` table, column additions on `approval_audit`, `approval_nonces`, and triggers). The schema migration must be applied by `fc27_owner` via `db_migrate` before the store tests will pass.
3. **Dispatch Creator Hook**: In Milestone 1, `dispatch_enabled` remains `False`, so the `dispatch_creator` hook is plumbed as a callable seam for Milestone 4, failing closed if called unexpectedly.

---

## 4. Conclusion

The technical exploration is complete. The exact Python implementation designs for `src/approval_store.py` and `src/decision.py` are documented in `store_analysis.md`. The design satisfies all requirements:
1. `complete_run()` creates `approval_requests` with TTL, links `graph_threads`, emits `approval.created`, and returns enriched state.
2. `issue_challenge()` calls `expire_due()`, checks inconsistencies, respects lock order, and bounds nonce TTL.
3. `apply_decision()` enforces strict lock order (nonces $\to$ request $\to$ thread), checks `fc27_approval_inconsistencies`, implements safe idempotent replay (returning committed decision without 409), writes audit with correlation/request IDs, emits `approval.decided`, and enforces Section 1A primary authority validation.
4. `expire_due()` handles due requests in independent transactions, revoking open nonces and emitting `approval.expired`.
5. `pending_approvals()` provides the dual-status filtered inbox query.
6. `src/decision.py` completely eliminates the legacy `dispatch` dictionary and adds `REQUEST_REVISION` support.

---

## 5. Verification Method

To independently verify the implementation once applied by the implementer:
1. **Target test files**:
   - `tests/test_migration_0003.py`: Validates schema backfill, `fc27_check_approval_pair` trigger failure on status mismatch, and `package_version` immutability.
   - `tests/test_approval_lifecycle.py`: Validates `complete_run`, `apply_decision`, `expire_due`, `pending_approvals`, race resolution, and safe idempotent replay.
   - `tests/test_approval_security.py`: Validates security invariants and least privilege.
2. **Commands**:
   ```bash
   python -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_lifecycle.py
   python -m pytest -o addopts="" -q tests/test_approval_security.py
   ```
3. **Invalidation Conditions**:
   - Any test run where `fc27_approval_inconsistencies` contains non-legacy unlinked rows invalidates the implementation.
   - Any occurrence of deadlock under the 4x `expire_due` + 4x `apply_decision` race invalidates the lock order.
   - Any 409 response on an identical replay of a signed decision invalidates the replay protocol.
   - Any presence of a `"dispatch"` key in `new_state` invalidates the Phase 1 invariant.
