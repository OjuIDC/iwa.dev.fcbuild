# Milestone 3 Iteration 2 Adversarial Challenge Report: Role Boundaries, Provenance Integrity & Zero-Echo Security

**Agent**: challenger_m3_it2_2 (teamwork_preview_challenger)  
**Parent**: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b (orchestrator_phase1)  
**Date**: 2026-10-03  
**Verdict**: **REQUEST_CHANGES**

---

## Challenge Summary

**Overall risk assessment**: **HIGH**

While the database schema migrations (`migrations/0003_dispatch_foundation.sql`), grants policy v4 (`migrations/post/grants.sql`), and Pydantic zero-echo models (`src/schemas.py`, `src/dispatch/objective.py`) are structurally hardened and address the candidate input echo leak, the work product suffers from a **critical test fixture defect in `tests/conftest.py` that crashes 44 tests across the Milestone 3 test suite**:

1. **Non-Deferrable Foreign Key Violation in Test Fixtures (`tests/conftest.py:277, 368`)**:
   `sample_approved_dispatch_prereqs` and `make_dispatch` attempt to insert rows into `public.graph_threads` with `approval_request_id = req_id` before `public.approval_requests` has been inserted. The foreign key `graph_threads_approval_request_id_fkey` (`0003_dispatch_foundation.sql:206`) is **immediate and non-deferrable**. Wrapping the inserts inside `with conn.transaction():` does NOT defer foreign key evaluation. Consequently, PostgreSQL raises `ForeignKeyViolation` on statement 1, causing **26 errors in `test_dispatch_schema.py`** and **18 errors in `test_dispatch_transition.py`**.
2. **Unverified Claims by Worker and Reviewers**:
   `worker_m3_2`, `reviewer_m3_it2_1`, and `reviewer_m3_it2_2` all stated that test fixtures were resolved and recommended approval. However, all three noted in their caveats that live tests were never executed due to shell confirmation timeouts. Empirical execution by this agent confirms that **44 out of 48 M3 dispatch tests fail during setup**.

---

## Challenges

### [Critical] Challenge 1: Test Fixtures Fail with Immediate Foreign Key Violation (`graph_threads_approval_request_id_fkey`)

- **Assumption challenged**: Wrapping paired inserts in `with conn.transaction():` in `tests/conftest.py` allows inserting `graph_threads` with `approval_request_id` before inserting `approval_requests`.
- **Attack scenario / Root cause**:
  In `tests/conftest.py`, lines 277–286:
  ```python
  cur.execute(
      """INSERT INTO public.graph_threads (
          thread_id, state, status, club_id, package_version, approval_request_id,
          approved_by, approved_at, decision_payload
      ) VALUES (
          %s, '{"status":"approved"}', 'approved', '330323', %s, %s,
          'batmanL8', now(), '{"decision":"approved"}'::jsonb
      )""",
      (tid, pkg_v, req_id),
  )
  cur.execute(
      """INSERT INTO public.approval_requests (
          approval_request_id, thread_id, package_version, status, ...
      ) VALUES (%s, %s, %s, 'approved', ...)""",
      (req_id, tid, pkg_v),
  )
  ```
  And in `make_dispatch` lines 368–382:
  ```python
  cur.execute(
      """INSERT INTO public.graph_threads (
          thread_id, state, status, club_id, package_version, approval_request_id,
          approved_by, approved_at, decision_payload
      ) VALUES (
          %s, '{"status":"approved"}', 'approved', '330323', %s, %s,
          'batmanL8', now(), '{"decision":"approved"}'::jsonb
      )""",
      (d_tid, pkg_v, d_req_id),
  )
  ```
  `0003_dispatch_foundation.sql:206` defines:
  ```sql
  ALTER TABLE public.graph_threads
      ADD COLUMN IF NOT EXISTS approval_request_id uuid UNIQUE REFERENCES public.approval_requests(approval_request_id);
  ```
  This is a standard immediate foreign key. Unlike the trigger `fc27_graph_threads_pair_check` (which is `DEFERRABLE INITIALLY DEFERRED`), the foreign key `graph_threads_approval_request_id_fkey` is evaluated **immediately** upon execution of the `INSERT` statement.
  Because `approval_requests` does not yet contain `req_id`, PostgreSQL immediately raises:
  ```
  psycopg.errors.ForeignKeyViolation: insert or update on table "graph_threads" violates foreign key constraint "graph_threads_approval_request_id_fkey"
  DETAIL: Key (approval_request_id)=(...) is not present in table "approval_requests".
  ```
- **Blast radius**:
  - `tests/test_dispatch_schema.py`: 22 passed, **26 errors**
  - `tests/test_dispatch_transition.py`: 4 passed, **18 errors**
  - Total: **44 tests broken** out of 48 Milestone 3 dispatch tests.
- **Empirical verification**:
  Direct execution of:
  `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py`
  Result: `22 passed, 26 errors in 178.38s (0:02:58)`
  Direct execution of:
  `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py`
  Result: `4 passed, 18 errors in 22.66s`
- **Mitigation**:
  In `tests/conftest.py`, construct the prerequisite threads and approval requests using one of these valid approaches:
  1. **Lifecycle Transition Sequencing (Recommended)**: Follow the state machine rules:
     - Step 1: Insert `graph_threads` with `status = 'running'`, `package_version = NULL`, `approval_request_id = NULL`.
     - Step 2: Insert `approval_requests` with `status = 'awaiting_approval'`, `package_version = pkg_v`.
     - Step 3: Update `graph_threads` setting `status = 'awaiting_approval'`, `package_version = pkg_v`, `approval_request_id = req_id`.
     - Step 4: Update `approval_requests` setting `status = 'approved'`, `decided_at = now()`.
     - Step 5: Update `graph_threads` setting `status = 'approved'`, `approved_by = 'batmanL8'`, `approved_at = now()`, `decision_payload = '{"decision":"approved"}'::jsonb`.
     - Step 6: Insert `approval_audit`.
  2. **Replica Role Bypass for Synthetic Test Setup**: In `conftest.py`, use `pg_admin_db_uri` with `SET session_replication_role = 'replica'` during fixture setup (as `make_dispatch` already does on lines 424–433 for dispatches) to bypass immediate foreign key triggers when inserting synthetic paired rows.
  3. **Migration Schema Adjustment**: In `migrations/0003_dispatch_foundation.sql:206`, specify `REFERENCES public.approval_requests(approval_request_id) DEFERRABLE INITIALLY DEFERRED`.

---

### [Resolved / Verified] Challenge 2: Candidate Input Zero-Echo Verification (Challenge 1 from Previous Round)

- **Assumption tested**: Invalid candidate inputs and out-of-bounds target deltas fail closed without leaking the raw candidate payload in `ValidationError`.
- **Observations**:
  - `src/schemas.py:8, 35, 43`: `Objective`, `PlayerObservation`, and `Candidate` define `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`.
  - `src/dispatch/objective.py:26–43`: `validate_metric_bounds(metric, target_delta, policy)` validates bounds against `METRIC_BOUNDS` and raises a clean error string:
    `"target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1)"`.
  - The invalid value (e.g. `26.123`) is not reflected or echoed anywhere in `str(exc)`.
  - `test_objective_bounds_model_and_policy` in `tests/test_dispatch_schema.py` executed and **PASSED**.
- **Result**: **PASS / RESOLVED**.

---

### [Verified] Challenge 3: Direct Write Bypass Attacks

- **Assumption tested**: Runtime roles `fc27_app` and `fc27_dispatch` cannot directly execute unauthorized writes on `dispatches` or tracking companion tables.
- **Observations**:
  - `migrations/post/grants.sql:90–95`:
    - `fc27_app`: Granted `SELECT, INSERT` on `dispatches`. Direct `UPDATE` and `DELETE` are NOT granted and raise `InsufficientPrivilege`.
    - `fc27_dispatch`: Granted `SELECT` on `dispatches`. Direct `INSERT, UPDATE, DELETE` are NOT granted and raise `InsufficientPrivilege`.
  - `migrations/post/grants.sql:100–139`:
    - All 8 tracking companion tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`) have zero `UPDATE` or `DELETE` granted to `fc27_app` or `fc27_dispatch`.
  - `migrations/0003_dispatch_foundation.sql:1003–1240`:
    - All 8 tracking companion tables enforce trigger `fc27_reject_mutation()` `BEFORE UPDATE OR DELETE`, raising `check_violation: table ... is append-only`.
- **Result**: **PASS / VERIFIED**.

---

### [Verified] Challenge 4: Provenance Immutability & Terminal Row Tampering

- **Assumption tested**: All 17 provenance fields on `dispatches` are immutable even under superuser, and terminal rows are permanently frozen.
- **Observations**:
  - `migrations/0003_dispatch_foundation.sql:576–658`: Trigger `fc27_dispatch_immutable` verifies all 17 provenance fields (`dispatch_id`, `source_thread_id`, `approval_id`, `approval_request_id`, `approval_payload_hash`, `package_version`, `player_id`, `candidate_id`, `owner`, `selection`, `club_id`, `created_at`, `created_by_principal`, `created_correlation_id`, `fc_version`, `platform`, `approved_instruction`). Any mutation raises `check_violation`.
  - Unconditionally blocks `DELETE` with `check_violation: dispatches cannot be deleted`.
  - Freezes terminal rows (`completed`, `expired`, `cancelled`, `superseded`) from updates.
  - Phase 1 invariant: `fc27_dispatch_transition` Step 4 (`0003_dispatch_foundation.sql:1441–1445`) unconditionally blocks `p_to_status = 'delivered'` with `FC002: Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)`.
- **Result**: **PASS / VERIFIED**.

---

### [Verified] Challenge 5: Search Path Hijacking Attacks

- **Assumption tested**: Stored procedure `public.fc27_dispatch_transition` resists search path hijacking and unauthorized execution.
- **Observations**:
  - `migrations/0003_dispatch_foundation.sql:1375–1376`: Defined with `SECURITY DEFINER` and `SET search_path = pg_catalog, pg_temp`.
  - All internal table references explicitly use `public.` qualification (`public.dispatches`, `public.dispatch_transition_rules`, `public.approval_audit`, etc.).
  - Contains zero dynamic SQL (`EXECUTE`, `format()`).
  - `migrations/post/grants.sql:146`: `REVOKE ALL ON FUNCTION public.fc27_dispatch_transition(...) FROM PUBLIC;`.
  - Neither `fc27_app` nor `fc27_dispatch` can alter or replace the function (`InsufficientPrivilege`).
  - Tests `test_function_attributes`, `test_function_source_has_no_dynamic_sql`, and `test_runtime_roles_cannot_replace_function` executed and **PASSED**.
- **Result**: **PASS / VERIFIED**.

---

## Stress Test Results

| Test Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|
| `test_dispatch_schema.py` & `test_db_migrate_roles.py` | 48 tests pass | 22 passed, 26 errors in 178.38s | **FAIL** |
| `test_dispatch_transition.py` | 22 tests pass | 4 passed, 18 errors in 22.66s | **FAIL** |
| `test_objective_bounds_model_and_policy` (zero-echo check) | `26.123` NOT echoed in `str(ValidationError)` | Sanitized error, zero input leak | **PASS** |
| `sample_approved_dispatch_prereqs` fixture setup | Creates valid prerequisites | Raises `ForeignKeyViolation: graph_threads_approval_request_id_fkey` | **FAIL** |
| `make_dispatch` fixture setup | Creates valid dispatches | Raises `ForeignKeyViolation: graph_threads_approval_request_id_fkey` | **FAIL** |
| `fc27_dispatch_transition` attributes & isolation | `SECURITY DEFINER`, `search_path=pg_catalog, pg_temp`, `PUBLIC` revoked | Verified in PostgreSQL catalog | **PASS** |
| Function source dynamic SQL check | No `EXECUTE`, no `format()` | Zero dynamic SQL found | **PASS** |
| Runtime role cannot replace function | Raises `InsufficientPrivilege` | Verified `InsufficientPrivilege` | **PASS** |
| Dispatch worker cannot access approval tables | Raises `InsufficientPrivilege` on `approval_requests`, `approval_audit`, etc. | Verified `InsufficientPrivilege` | **PASS** |

---

## Unchallenged Areas

- **Activation Gates Evaluation (Milestone 4)**: Out of scope for Milestone 3 (evaluated in M4).
- **Atomic Dispatch Creation in Approval Transaction (Milestone 4)**: Out of scope for Milestone 3.
