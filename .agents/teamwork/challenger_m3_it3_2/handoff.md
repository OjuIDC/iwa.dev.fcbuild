# Handoff Report: Milestone 3 Iteration 3 — Adversarial Challenge (Role Boundaries, Provenance Immutability & Schema Verification)

**Agent**: `challenger_m3_it3_2`  
**Role**: Milestone 3 Iteration 3 Challenger 2 (Adversarial Critic & Specialist)  
**Date**: 2026-10-03T19:58:00Z  
**Type**: Hard Handoff  
**Verdict**: **APPROVE**  

---

## 1. Observation

### 1.1 Direct Write Bypass & Role Isolation
- In `migrations/post/grants.sql:90–93`:
  ```sql
  IF to_regclass('public.dispatches') IS NOT NULL THEN
      -- CRITICAL: NO UPDATE, NO DELETE on dispatches for any runtime role!
      GRANT SELECT, INSERT ON public.dispatches TO fc27_app;
      GRANT SELECT ON public.dispatches TO fc27_dispatch;
  ```
- Direct execution observations:
  - In `tests/test_dispatch_schema.py:378–385`: `cur.execute("UPDATE public.dispatches SET dispatch_status = 'failed' WHERE dispatch_id = %s", (dispatch_id,))` under role `fc27_app` raises verbatim `psycopg.errors.InsufficientPrivilege` (`42501`).
  - In `tests/test_dispatch_schema.py:387–394`: `cur.execute("DELETE FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,))` under role `fc27_app` raises verbatim `psycopg.errors.InsufficientPrivilege`.
  - In `tests/test_dispatch_schema.py:396–416`: Under role `fc27_dispatch`, `SELECT` returns `'pending'`, while `INSERT`, `UPDATE`, and `DELETE` all raise verbatim `psycopg.errors.InsufficientPrivilege`.
  - In `tests/test_dispatch_schema.py:418–433`: Under role `fc27_dispatch`, queries on `approval_requests`, `approval_audit`, `approval_nonces`, and `graph_threads` all raise verbatim `psycopg.errors.InsufficientPrivilege`.

### 1.2 Append-Only Companion Tracking Tables
- In `migrations/0003_dispatch_foundation.sql:1054–1200`, all 8 companion tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`) attach triggers executing `public.fc27_reject_mutation()`:
  ```sql
  CREATE OR REPLACE FUNCTION public.fc27_reject_mutation()
  RETURNS trigger
  LANGUAGE plpgsql
  AS $$
  BEGIN
      RAISE EXCEPTION 'append-only: mutations are rejected'
          USING ERRCODE = 'check_violation';
  END;
  $$;
  ```
- In `tests/test_dispatch_schema.py:328–335`, Worker 4 corrected line 330:
  ```python
  col = "ingested_at" if table == "player_observations" else "created_at"
  cur.execute(f"UPDATE public.{table} SET {col} = now() WHERE {where}")
  ```
  targeting `ingested_at timestamptz NOT NULL DEFAULT now()` on `public.player_observations` (`migrations/0003_dispatch_foundation.sql:1155`).
  Both `UPDATE` and `DELETE` raise `psycopg.errors.CheckViolation` with message matching `"append-only"` across all 8 tables.

### 1.3 Provenance Immutability & Zero-Echo Verification
- In `migrations/0003_dispatch_foundation.sql:980–1030`, function `public.fc27_guard_dispatch_immutable()` verifies all 17 provenance fields:
  `dispatch_id`, `source_thread_id`, `approval_id`, `approval_request_id`, `approval_payload_hash`, `package_version`, `player_id`, `candidate_id`, `owner`, `selection`, `club_id`, `created_at`, `created_by_principal`, `created_correlation_id`, `fc_version`, `platform`, `approved_instruction`.
  Modifying any of these fields raises `CheckViolation` with `immutable provenance: <field> is immutable`.
- In `migrations/0003_dispatch_foundation.sql:1033–1041`, terminal rows in states `completed`, `expired`, `cancelled`, `superseded` are frozen from updates with `CheckViolation: dispatch % is terminal (%) and frozen from updates`.
- In `src/schemas.py`:
  - Line 8: `Objective`: `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`
  - Line 35: `PlayerObservation`: `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`
  - Line 43: `Candidate`: `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`
- In `src/dispatch/objective.py:40–42`: `validate_metric_bounds()` formats:
  `raise ValueError(f"target_delta for {metric} must be between {low_str} and {high_str} (policy {policy})")`,
  completely suppressing raw input deltas.
- In `tests/test_dispatch_schema.py:458`: `assert "26.123" not in err_str` passes.

### 1.4 Test Suite Execution Results
- **Command 1 (Dispatch Schema & Migration Roles)**:
  `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py`
  Result:
  ```
  ................................................                         [100%]
  48 passed in 180.54s (0:03:00)
  ```
  **100% PASS** (48 of 48 passed, 0 failures, 0 errors, 0 warnings).
  `test_tracking_tables_append_only[player_observations]` passed cleanly.

- **Command 2 (Dispatch Transition State Machine)**:
  `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py`
  Result:
  ```
  ......................                                                   [100%]
  22 passed in 23.68s
  ```
  **100% PASS** (22 of 22 passed).

---

## 2. Logic Chain

1. **Least-Privilege Enforcement**:
   Grant rules in `migrations/post/grants.sql` explicitly restrict `fc27_app` to `SELECT, INSERT` and `fc27_dispatch` to `SELECT` on `public.dispatches`. Because PostgreSQL enforces table-level permissions before query planning or execution, direct mutation attempts by these roles fail with `InsufficientPrivilege` without relying on application-level checks (Observation 1.1).
2. **Append-Only Tamper Resistance**:
   Every tracking companion table is equipped with a `BEFORE UPDATE OR DELETE` trigger invoking `fc27_reject_mutation()`. By aligning the test assertion in `tests/test_dispatch_schema.py:330` with `player_observations.ingested_at`, SQL statements parse cleanly and reach execution, confirming that mutations are rejected with `CheckViolation` across all 8 tables (Observation 1.2).
3. **Defense-in-Depth for Provenance**:
   Even if an attacker gains table-level update privileges (e.g. database owner or superuser), `BEFORE UPDATE` trigger `fc27_guard_dispatch_immutable()` inspects all 17 provenance fields and terminal statuses, raising `CheckViolation` on any attempt to tamper with approved packages, correlation IDs, or terminal state (Observation 1.3).
4. **Data Leakage Prevention**:
   Configuring `hide_input_in_errors=True` on `Objective`, `Candidate`, and `PlayerObservation` combined with sanitized messages in `validate_metric_bounds()` prevents reflection of unvalidated candidate metrics or payload data into logs, exception handlers, or client responses (Observation 1.3).
5. **Direct Verification Without Trust**:
   Independent, hands-on execution of the test suite via `.venv\Scripts\python.exe` confirmed that all 48 schema/role test cases and 22 transition test cases pass cleanly without errors, timeouts, or leaks (Observation 1.4).

---

## 3. Caveats

- **Database Connection Prerequisite**: Execution of database test suites requires a running PostgreSQL instance at `postgresql://fc27test@127.0.0.1:55432/postgres`.
- **Phase Boundary Scope**: Outbox worker delivery, player identity/authentication, and WebAuthn step-up remain out-of-scope and intentionally deferred to subsequent phases per programme invariants.
- No caveats regarding code correctness, security boundaries, or test results.

---

## 4. Conclusion

**Verdict: APPROVE**

Milestone 3 Iteration 3 successfully enforces all required security boundaries, role isolations, provenance immutability rules, and zero-echo error safeguards:
- `fc27_app` cannot update or delete dispatches.
- `fc27_dispatch` cannot insert, update, or delete dispatches, and cannot access approval tables.
- All 8 tracking tables strictly reject updates and deletes.
- All 17 provenance fields and terminal dispatch rows remain permanently immutable.
- Zero-echo security is verified across `Objective`, `Candidate`, and `PlayerObservation`.
- Full schema and role test suites pass with 100% success (48/48 passed).

---

## 5. Verification Method

To independently reproduce and verify:

1. **Execute Dispatch Schema and Migrate Roles Test Suite**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   ```
   Verify output: `48 passed in ~180s (100% pass)`.

2. **Execute Dispatch Transition Test Suite**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   ```
   Verify output: `22 passed in ~24s (100% pass)`.

3. **Inspect Verified Artifacts**:
   - `migrations/post/grants.sql` lines 90–93
   - `migrations/0003_dispatch_foundation.sql` lines 841–1050, 1054–1200
   - `src/schemas.py` lines 8, 35, 43
   - `src/dispatch/objective.py` lines 26–43
   - `tests/test_dispatch_schema.py` lines 328–335, 378–433, 435–483
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it3_2\challenge_report.md`
