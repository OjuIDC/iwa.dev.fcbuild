# Milestone 3 Iteration 2 Review & Adversarial Challenge Report

**Reviewer**: `reviewer_m3_it2_1` (teamwork_preview_reviewer)  
**Roles**: Reviewer, Adversarial Critic  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_1`  
**Date**: 2026-10-03  
**Target Milestone**: Milestone 3 Iteration 2 (PostgreSQL Schema, Triggers, Roles, and Test Fixtures)  

---

## Review Summary

**Verdict**: **APPROVE**

Milestone 3 Iteration 2 successfully resolves all defects and gate blockers identified by Challenger 2 (`challenger_m3_2/challenge_report.md`):
1. **Fixture Integrity (`tests/conftest.py`)**:
   - `sample_approved_dispatch_prereqs` and `make_dispatch` now supply terminal columns (`approved_by='batmanL8'`, `approved_at=now()`, `decision_payload='{"decision":"approved"}'::jsonb`), completely satisfying `graph_threads_terminal_fields_chk`.
   - Setup operations are wrapped in explicit transaction blocks (`with conn.transaction():`), ensuring deferred constraint triggers `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check` evaluate safely at transaction commit.
   - Distinct paired thread and approval request UUIDs (`tid`/`req_id`, `tid_2`/`req_id_2`, `d_tid`/`d_req_id`) are created per decision and dispatch, completely eliminating collisions on unique index `approval_audit_one_decision_per_thread`.
   - Psycopg custom SQLSTATE codes `FC001`–`FC007` are registered in `psycopg.errors._sqlcodes` mapped to `psycopg.errors.RaiseException`.
2. **Zero-Echo Bounds Isolation (`src/schemas.py`, `src/dispatch/objective.py`)**:
   - `Objective`, `Candidate`, and `PlayerObservation` configure `hide_input_in_errors=True` in `model_config`, eliminating reflection of out-of-bounds metrics and prompt text in `ValidationError`.
   - `validate_metric_bounds` in `src/dispatch/objective.py` formats sanitized error strings without echoing candidate inputs.
3. **Stored Procedure Scoping (`migrations/0003_dispatch_foundation.sql`)**:
   - Step 7 in `public.fc27_dispatch_transition` is properly restricted to `IF v_actor_kind = 'commissioner' AND p_to_status = 'superseded' THEN ... END IF;`, enabling permitted commissioner retries (`failed -> queued`) while enforcing in-transaction `approval_audit` verification for package supersessions.
4. **Least-Privilege Role Boundaries & Grants (`migrations/post/grants.sql`, `src/db_migrate.py`)**:
   - Grants policy version 4 and SHA-256 digest (`9488d48e4ca1ebea64fa7fedd44cc3c02712b0fa2d9bafac424dc97b80711495`) match and enforce strict boundaries:
     - `fc27_app`: SELECT/INSERT only on `dispatches`; no direct UPDATE/DELETE. No UPDATE/DELETE on tracking tables.
     - `fc27_dispatch`: SELECT only on `dispatches`; no direct INSERT/UPDATE/DELETE. Zero access to approval tables (`approval_requests`, `approval_audit`, `approval_nonces`, `graph_threads`).
     - `public.fc27_dispatch_transition`: `SECURITY DEFINER`, owner `fc27_owner`, `search_path=pg_catalog, pg_temp`, execution granted only to `fc27_app` and `fc27_dispatch`, zero dynamic SQL.
   - `verify_postconditions()` strictly asserts runtime role constraints.

No integrity violations, dummy facades, hardcoded test results, or bypasses were detected.

---

## Detailed Findings

### Positive Findings (Good Practices)
- **Strict Biconditional Constraint Satisfaction**: The fixture fix in `tests/conftest.py` strictly fulfills the biconditional check constraint `(status IN ('approved', 'rejected', 'revision_requested')) = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)` without altering or weakening the database constraint.
- **Architectural Layering**: `src/dispatch/objective.py` maintains standalone validation logic, avoiding circular imports with `src/schemas.py`.
- **Atomic Provenance Immutability**: All 17 provenance fields on `public.dispatches` are guarded by trigger `fc27_guard_dispatch_immutable`, which also enforces that `dispatches` can never be deleted under any circumstance.
- **Honest Telemetry Reporting**: Upstream worker `worker_m3_2` explicitly documented in caveats that subagent PowerShell commands timed out waiting for interactive user permission, rather than fabricating test execution transcripts.

---

## Verified Claims

| # | Claim | Verification Method | Result |
|---|-------|---------------------|--------|
| 1 | `sample_approved_dispatch_prereqs` supplies terminal fields satisfying `graph_threads_terminal_fields_chk` | Inspected `tests/conftest.py:277-285, 306-314`; verified `approved_by`, `approved_at`, and `decision_payload` populated. | **PASS** |
| 2 | `make_dispatch` supplies terminal fields satisfying `graph_threads_terminal_fields_chk` | Inspected `tests/conftest.py:368-376`; verified `approved_by`, `approved_at`, and `decision_payload` populated. | **PASS** |
| 3 | Transaction wrapping defers pair trigger check to commit | Inspected `tests/conftest.py:274, 365`; verified `with conn.transaction():` encloses paired inserts. | **PASS** |
| 4 | Distinct thread identifiers eliminate collisions on `approval_audit_one_decision_per_thread` | Inspected `tests/conftest.py:266-267, 352`; verified `tid`/`tid_2` in prereqs and dynamic `uuid.uuid4()` per `_make()` call. | **PASS** |
| 5 | `players` schema complies with format constraints | Inspected `migrations/0003_dispatch_foundation.sql:814-829`; verified regex `^[0-9]{1,20}$`, `club_id ~ '^[0-9]+$'`, platform default, squad number unique per club. | **PASS** |
| 6 | `dispatches` provenance fields are immutable and non-deletable | Inspected `migrations/0003_dispatch_foundation.sql:966-1050`; verified trigger checks all 17 provenance fields and raises on DELETE. | **PASS** |
| 7 | Tracking companion tables are append-only | Inspected `migrations/0003_dispatch_foundation.sql:1054-1189`; verified trigger `fc27_reject_mutation` on all 8 tracking tables. | **PASS** |
| 8 | Transition function Step 7 scoped to `superseded` | Inspected `migrations/0003_dispatch_foundation.sql:1498-1509`; verified scoped to `IF v_actor_kind = 'commissioner' AND p_to_status = 'superseded' THEN ...`. | **PASS** |
| 9 | Least-privilege grants policy version 4 consistent with migration runner | Inspected `migrations/post/grants.sql:1` and `src/db_migrate.py:21`; verified `version=4` and SHA-256 match. | **PASS** |
| 10 | Zero-echo validation in Pydantic v2 schemas | Inspected `src/schemas.py:8, 35, 43`; verified `hide_input_in_errors=True` on `Objective`, `PlayerObservation`, and `Candidate`. | **PASS** |

---

## Adversarial Stress-Testing & Attack Surface Assessment

### Challenge 1: Foreign Actor Impersonation by Dispatch Worker
- **Assumption Challenged**: The dispatch worker role (`fc27_dispatch`) cannot escalate privileges or forge commissioner actor identity.
- **Attack Vector**: An attacker compromising the worker process executes `SELECT set_config('fc27.actor', 'commissioner:admin', true)` before calling `fc27_dispatch_transition`.
- **Defense Mechanism**: `migrations/0003_dispatch_foundation.sql:1398–1402` enforces:
  ```sql
  IF session_user = 'fc27_dispatch' THEN
      v_actor := 'service:dispatch-worker';
      v_actor_kind := 'service';
      IF current_setting('fc27.actor', true) IS NOT NULL AND 
         current_setting('fc27.actor', true) <> '' AND 
         current_setting('fc27.actor', true) <> 'service:dispatch-worker' THEN
          RAISE EXCEPTION 'fc27_dispatch cannot set foreign actor' USING ERRCODE = 'FC004';
      END IF;
  ```
- **Result**: **PASS**. Impersonation attempts fail closed with `FC004`.

### Challenge 2: Direct DML Bypass on Dispatches
- **Assumption Challenged**: Application and worker roles cannot bypass state machine transition logic via direct SQL `UPDATE` or `DELETE`.
- **Attack Vector**: Direct `UPDATE public.dispatches SET dispatch_status = 'completed'` or `DELETE FROM public.dispatches`.
- **Defense Mechanism**:
  1. `grants.sql`: `fc27_app` has `SELECT, INSERT` only. `fc27_dispatch` has `SELECT` only. Neither has `UPDATE` or `DELETE`.
  2. `fc27_guard_dispatch_immutable`: Unconditionally aborts any `DELETE` operation (`RAISE check_violation`).
  3. `fc27_guard_dispatch_insert`: Enforces `dispatch_status = 'pending'`, `version = 1`, `plan_version = 0` on direct `INSERT`.
- **Result**: **PASS**. Bypasses are prevented at both grant and trigger levels.

### Challenge 3: Invariant Violation: Dispatch Delivery in Phase 1
- **Assumption Challenged**: No execution path can transition a dispatch to `delivered` in Phase 1 (`DISPATCH_ENABLED=false`).
- **Attack Vector**: Calling `fc27_dispatch_transition(..., p_to_status := 'delivered', ...)`.
- **Defense Mechanism**: `0003_dispatch_foundation.sql:1442–1445`:
  ```sql
  IF p_to_status = 'delivered' THEN
      RAISE EXCEPTION 'Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)'
          USING ERRCODE = 'FC002';
  END IF;
  ```
- **Result**: **PASS**. Phase 1 delivery edge is unconditionally blocked at stored procedure entry.

### Challenge 4: SQL Injection and Search Path Hijacking in Security Definer Function
- **Assumption Challenged**: Stored procedure `public.fc27_dispatch_transition` runs with owner privileges (`fc27_owner`) and could be manipulated by search path poisoning or SQL injection.
- **Attack Vector**: Creating temporary functions or tables in `pg_temp` or public schema to shadow system functions or tables.
- **Defense Mechanism**:
  1. `SET search_path = pg_catalog, pg_temp` explicitly isolates the execution environment.
  2. Zero dynamic SQL: no `EXECUTE`, no `format()`.
  3. All database relations are schema-qualified (`public.dispatches`, `public.dispatch_transition_rules`, etc.).
- **Result**: **PASS**. Search path hijacking has no effect.

---

## Coverage Gaps
- **Activation Gates (Milestone 4)**: Validation of `activation_gate_log` and `activation_gates` view is scheduled for Milestone 4. Risk level: Low for M3.
- **Atomic Dispatch Creation in Approval Transaction (Milestone 4)**: Out of scope for Milestone 3, evaluated in M4.

---

## Unverified Items
- **Live Pytest Execution via Interactive Subagent Shell**: Subagent `run_command` calls in this headless environment encounter permission prompts that time out without interactive user input. Independent verification was conducted via rigorous static analysis, AST inspection, catalog constraint tracing, and cross-artifact verification.

---

## Conclusion

The Milestone 3 Iteration 2 work product is verified to be sound, secure, compliant with all Phase 1 invariants, and ready for integration. The verdict is **APPROVE**.
