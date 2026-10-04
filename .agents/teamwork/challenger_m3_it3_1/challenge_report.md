# Milestone 3 Iteration 3 Adversarial Challenge Report: State Machine Transitions, Concurrency & Delivery Invariants

**Agent**: `challenger_m3_it3_1` (teamwork_preview_challenger / critic & specialist)  
**Parent**: `7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b` (orchestrator_phase1)  
**Date**: 2026-10-03T19:55:00Z  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it3_1`  
**Verdict**: **APPROVE**

---

## Challenge Summary

**Overall risk assessment**: **LOW**

Empirical verification of the transition test suite (`tests/test_dispatch_transition.py`) confirms that the circular foreign key fixture bug identified during Iteration 2 is completely resolved:
```pwsh
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
# Result: 22 passed in 26.03s (100% PASS)
```

Adversarial stress-testing of `public.fc27_dispatch_transition` and its surrounding security constraints confirms:
1. **Phase 1 Delivery Block Invariant**: Unconditionally enforced. Any attempt to transition to `delivered` fails immediately with SQLSTATE `FC002` across all database roles, actors, and source statuses before table locking or transition allowlist matching.
2. **Transition Rules & Actor Validation**:
   - Disallowed edge jumps, terminal state mutations (completed, expired, cancelled, superseded), and self-transitions are strictly rejected with `FC002`.
   - Cross-role privilege escalation is blocked (`fc27_app` cannot execute worker edges; `fc27_dispatch` cannot execute commissioner edges).
   - Actor validation enforces strict regex matching, preventing foreign actor spoofing by worker (`FC004`), missing/malformed actors on app connections (`FC004`), and Phase 1 user actor attempts (`FC004`).
   - Administrative cancellation enforces non-empty reason (`FC007`) and patch payload (`FC003`). Transitions to `paused` are rejected as unpermitted edges (`FC002`).
   - Null correlation IDs fail closed with `FC005`.
3. **Commissioner Retry Continuity**: Commissioner retry (`failed -> queued`) cleanly succeeds without requiring an in-transaction `approval_audit` record, whereas `superseded` correctly enforces in-transaction proof (`FC004`).
4. **Concurrency & Optimistic Locking**: Target rows are locked via `SELECT ... FOR UPDATE`, guaranteeing serial execution, while optimistic version and status assertions prevent lost updates (`FC001`).

---

## Challenges

### [Low] Challenge 1: Custom Error Code Mapping Distinction (FC003 vs FC004 and FC005 vs FC007)

- **Assumption challenged**: The dispatch prompt suggested verifying that "invalid actors fail with FC003" and "pause/cancel without reason fail with FC005".
- **Attack Scenario**: If an implementation conflated patch validation with actor validation, or correlation ID validation with reason validation, error semantics would violate `PROJECT.md` interface contracts (§Interface Contracts, line 136).
- **Empirical Observation**:
  - In `migrations/0003_dispatch_foundation.sql:1395–1429`, invalid actors raise SQLSTATE **`FC004`** (`'fc27_dispatch cannot set foreign actor'`, `'missing fc27.actor setting'`, `'malformed actor %'`, `'user actors require Phase 2'`).
  - In `migrations/0003_dispatch_foundation.sql:1512–1625`, **`FC003`** is strictly reserved for patch validation errors (unexpected patch key, missing required patch field, wrong patch JSON type).
  - In `migrations/0003_dispatch_foundation.sql:1432–1434`, **`FC005`** is raised for `null correlation ID`.
  - In `migrations/0003_dispatch_foundation.sql:1437–1439` and `lines 1493–1496`, **`FC007`** is raised for missing required transition reason or reason exceeding 500 characters.
  - In `src/dispatch/transition.py:159–196`, the Python wrapper cleanly maps `FC004` -> `InvalidActorError`, `FC003` -> `InvalidPatchError`, `FC005` -> `NullCorrelationIdError`, and `FC007` -> `ReasonInvalidError` / `MissingReasonError`.
- **Blast Radius**: None. The implementation and Python service strictly adhere to the authoritative architectural specification in `PROJECT.md`.
- **Mitigation / Assessment**: The code correctly preserves orthogonal error domains. This empirical check validates that the implementation is not masking distinct failure conditions.

### [Low] Challenge 2: In-Transaction Proof Scoping for Commissioner Operations

- **Assumption challenged**: In-transaction `approval_audit` validation might inadvertently block valid commissioner operational retries (`failed -> queued`), or conversely allow unproven supersessions.
- **Attack Scenario**:
  - If step 7 (`migrations/0003_dispatch_foundation.sql:1498–1509`) enforced `approval_audit` for all commissioner actions, retrying a transiently failed dispatch from the UI/API without generating a new approval audit would be impossible.
  - If step 7 failed to enforce `approval_audit` for `superseded`, an operator could supersede an approved dispatch without audit trail linkage.
- **Empirical Observation**:
  - Step 7 explicitly scopes the in-transaction proof:
    ```sql
    IF v_actor_kind = 'commissioner' AND p_to_status = 'superseded' THEN
        SELECT EXISTS (
            SELECT 1 FROM public.approval_audit
             WHERE xmin = pg_catalog.pg_current_xact_id()::xid
               AND approved_by = substring(v_actor from 14)
        ) INTO v_has_audit;
        IF NOT v_has_audit THEN
            RAISE EXCEPTION 'supersede requires approval_audit committed in current transaction'
                USING ERRCODE = 'FC004';
        END IF;
    END IF;
    ```
  - `failed -> queued` succeeds cleanly with zero `approval_audit` rows created in the transaction (`test_allowed_phase1_transitions`, lines 253–265).
  - Any call to `superseded` without an active in-transaction `approval_audit` fails with `FC004`.
- **Blast Radius**: Operational continuity for retry is preserved while audit integrity for supersession is guaranteed.
- **Mitigation**: Verified working as designed.

### [Low] Challenge 3: Paused Status Edge Absence

- **Assumption challenged**: The status domain check on table `public.dispatches` includes `'paused'` (`migrations/0003_dispatch_foundation.sql:860`), suggesting a pause transition might exist.
- **Attack Scenario**: A caller attempts to pause a running or queued dispatch in Phase 1.
- **Empirical Observation**:
  - Table `public.dispatch_transition_rules` (`migrations/0003_dispatch_foundation.sql:1327–1361`) contains zero rules targeting or originating from `paused`.
  - Any attempt to transition to `paused` fails with `FC002` (`'transition from ... to paused not permitted for role ... and actor_kind ...'`).
  - This complies with Phase 1 Non-negotiable Invariants §1A.3 / §1A.9, where player pause and operational pause are deferred to Phase 3/4.
- **Blast Radius**: None. The schema preserves forward-compatibility while the transition matrix prevents premature activation.
- **Mitigation**: Verified safe and closed.

---

## Stress Test Results

| # | Stress Scenario | Expected Behavior | Actual Behavior | Result |
|---|-----------------|-------------------|-----------------|--------|
| 1 | Transition to `delivered` (worker role, `queued` status) | Fail with `FC002` ("Phase 1 invariant violation") | Raised `FC002` | **PASS** |
| 2 | Transition to `delivered` across all 13 dispatch statuses | Fail with `FC002` on all statuses | Raised `FC002` on all 13 | **PASS** |
| 3 | Transition to `delivered` via `fc27_app` with commissioner actor | Fail with `FC002` ("Phase 1 invariant violation") | Raised `FC002` | **PASS** |
| 4 | Transition to `delivered` via `fc27_owner` / superuser | Fail with `FC002` ("Phase 1 invariant violation") | Raised `FC002` | **PASS** |
| 5 | Disallowed edge jump (`pending -> completed`) | Fail with `FC002` (not in rule matrix) | Raised `FC002` | **PASS** |
| 6 | Cross-role invocation (`fc27_app` calling `pending -> queued`) | Fail with `FC002` (role not permitted) | Raised `FC002` | **PASS** |
| 7 | Cross-role invocation (`fc27_dispatch` calling `pending -> cancelled`) | Fail with `FC002` (role not permitted) | Raised `FC002` | **PASS** |
| 8 | Transition on frozen terminal row (`completed -> queued`) | Fail with `FC002` (terminal state frozen) | Raised `FC002` | **PASS** |
| 9 | Self-transition (`pending -> pending`) | Fail with `FC002` (already in status) | Raised `FC002` | **PASS** |
| 10 | Worker sets foreign actor (`commissioner:batmanL8`) | Fail with `FC004` (foreign actor denied) | Raised `FC004` | **PASS** |
| 11 | App calls without setting `fc27.actor` | Fail with `FC004` (missing actor setting) | Raised `FC004` | **PASS** |
| 12 | App calls with malformed actor (`root`) | Fail with `FC004` (malformed actor) | Raised `FC004` | **PASS** |
| 13 | App calls with `user:...` actor in Phase 1 | Fail with `FC004` ("user actors require Phase 2") | Raised `FC004` | **PASS** |
| 14 | Null correlation ID argument | Fail with `FC005` (null correlation ID) | Raised `FC005` | **PASS** |
| 15 | Reason > 500 characters | Fail with `FC007` (reason length limit) | Raised `FC007` | **PASS** |
| 16 | Cancellation without reason argument | Fail with `FC007` (reason required) | Raised `FC007` | **PASS** |
| 17 | Cancellation without patch `cancel_reason` | Fail with `FC003` (cancel_reason required) | Raised `FC003` | **PASS** |
| 18 | Transition to `paused` | Fail with `FC002` (edge not permitted) | Raised `FC002` | **PASS** |
| 19 | Commissioner retry (`failed -> queued`) without `approval_audit` | Succeed, increment version, emit domain event | Version 2 returned, event emitted | **PASS** |
| 20 | Supersede without `approval_audit` | Fail with `FC004` (requires audit) | Raised `FC004` | **PASS** |
| 21 | Stale version optimistic conflict (`expected_version = 99`) | Fail with `FC001` (version conflict) | Raised `FC001` | **PASS** |
| 22 | Stale status optimistic conflict (`expected_status = 'running'`) | Fail with `FC001` (status conflict) | Raised `FC001` | **PASS** |
| 23 | Search path shadowing with temp tables / functions | Zero impact on `SECURITY DEFINER` execution | Shadowing ignored, secure execution | **PASS** |
| 24 | Direct insert of transition domain event outside function | Fail with trigger check violation | Trigger rejected direct insert | **PASS** |
| 25 | Execution of full test suite (`test_dispatch_transition.py`) | 22/22 tests pass (100% PASS) | 22 passed in 26.03s | **PASS** |

---

## Unchallenged Areas

- **Activation Gate Logic (`G-MIGRATION` .. `G-OUTBOX`)**: Scheduled for Milestone 4 (Milestone 3 scope was strictly schema, fixtures foreign key sequencing, and single-write-path transition function).
- **LangGraph Persistent Graph API Integration with Dispatches**: Scheduled for Milestone 4 / Milestone 5.
- **Phase 2 Identity & WebAuthn Actors**: Intentionally blocked closed in Phase 1 as designed.

---

## Final Verdict

**APPROVE**

Milestone 3 Iteration 3 transition security and delivery invariants are completely verified and sound. All 22 tests in `tests/test_dispatch_transition.py` pass without regression or failure.
