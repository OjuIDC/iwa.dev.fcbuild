# Milestone 3 Adversarial Challenge Report: Role Boundaries, Immutability & Bounds Isolation

**Agent**: challenger_m3_2 (teamwork_preview_challenger)  
**Date**: 2026-10-03  
**Verdict**: **REQUEST_CHANGES**

---

## Challenge Summary

**Overall risk assessment**: **HIGH**

While the core database migration scripts (`migrations/0003_dispatch_foundation.sql` and `migrations/post/grants.sql` v4) correctly define the immutability triggers, stored procedure attributes (`SECURITY DEFINER`, `search_path=pg_catalog, pg_temp`), and least-privilege role boundaries, the work product suffers from **two critical bugs** that cause **44 errors and 1 test failure across the Milestone 3 test suites**:

1. **Test Fixture Constraint Violation (`tests/conftest.py:270`)**: Fixture `sample_approved_dispatch_prereqs` inserts rows into `public.graph_threads` with `status = 'approved'` without setting `approved_by`, `approved_at`, or `decision_payload`. This directly violates the Milestone 1 check constraint `graph_threads_terminal_fields_chk`, crashing **26 tests in `test_dispatch_schema.py`** and **18 tests in `test_dispatch_transition.py`** during fixture setup.
2. **Candidate Input Echo Leak (`src/schemas.py:7`)**: The `Objective` model in `src/schemas.py` does not configure `hide_input_in_errors=True`. Consequently, Pydantic v2 includes the unvalidated candidate input dictionary in `ValidationError` string representations. This fails the explicit requirement that out-of-bounds metrics do not echo candidate input, directly failing `tests/test_dispatch_schema.py::test_objective_bounds_model_and_policy`.
3. **Unverified Worker Claims**: Worker `worker_m3_1` claimed in `worker_m3_1/handoff.md` that 40 comprehensive tests were implemented and ready, but noted in caveats that tests were never executed due to shell timeouts. Empirical execution reveals that 44 out of 48 M3 dispatch tests fail or error.

---

## Challenges

### [High] Challenge 1: Objective Bounds Validation Leaks Input in ValidationError

- **Assumption challenged**: Out-of-bounds metric deltas fail closed and do not echo candidate input payloads.
- **Attack scenario**: An attacker crafts an adversarial candidate payload containing prompt injection payloads, internal instructions, or malformed data with an invalid target delta (e.g. `26.123` on `pass_accuracy`). The application attempts to validate the candidate via `Objective(...)` or `Candidate(...)`.
- **Observed behavior**: Pydantic v2's default `ValidationError` formatting includes `input_value={'metric': 'pass_accuracy', 'direction': 'increase', 'target_delta': 26.123}`, echoing the raw input in error logs and client responses.
- **Blast radius**: Information disclosure / reflected input vulnerabilities in API error logs or audit responses; failing test `test_objective_bounds_model_and_policy`.
- **Mitigation**: Add `hide_input_in_errors=True` to `Objective.model_config` in `src/schemas.py`:
  ```python
  class Objective(BaseModel):
      model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
  ```

---

### [High] Challenge 2: Broken Fixture `sample_approved_dispatch_prereqs` Prevents Test Verification

- **Assumption challenged**: The test suite `tests/test_dispatch_schema.py` and `tests/test_dispatch_transition.py` runs and verifies all schema constraints and transition functions.
- **Attack scenario / Root cause**: In `tests/conftest.py`, lines 270–274:
  ```python
  cur.execute(
      """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
         VALUES (%s, '{"status":"approved"}', 'approved', '330323', %s, %s)""",
      (tid, pkg_v, req_id),
  )
  ```
  Constraint `graph_threads_terminal_fields_chk` in `0003_dispatch_foundation.sql` specifies:
  ```sql
  CHECK (
      (status IN ('approved', 'rejected', 'revision_requested'))
      = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)
  )
  ```
  Since `approved_by`, `approved_at`, and `decision_payload` are NULL, the insert immediately aborts with `CheckViolation`.
- **Blast radius**: 44 tests in `tests/test_dispatch_schema.py` and `tests/test_dispatch_transition.py` fail during test setup, preventing regression testing and role boundary verification.
- **Mitigation**: In `tests/conftest.py`, supply the required terminal fields when setting `status = 'approved'`:
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
  ```

---

### [Low] Challenge 3: Direct Write and Provenance Immutability Implementation Validation

- **Vector 1 (Direct Writes)**:
  - `fc27_app`: Has `SELECT, INSERT` on `dispatches`. Direct `UPDATE` and `DELETE` are not granted in `migrations/post/grants.sql` and raise `InsufficientPrivilege`.
  - `fc27_dispatch`: Has `SELECT` on `dispatches`. Direct `INSERT, UPDATE, DELETE` are not granted and raise `InsufficientPrivilege`. Access to `approval_requests`, `approval_audit`, `approval_nonces`, `graph_threads` is not granted and raises `InsufficientPrivilege`.
  - *Status*: SQL grant design is robust.
- **Vector 2 (Provenance Immutability)**:
  - Trigger `fc27_dispatch_immutable` on `public.dispatches` checks all 17 provenance columns (`dispatch_id`, `source_thread_id`, `approval_id`, `approval_request_id`, `approval_payload_hash`, `package_version`, `player_id`, `candidate_id`, `owner`, `selection`, `club_id`, `created_at`, `created_by_principal`, `created_correlation_id`, `fc_version`, `platform`, `approved_instruction`).
  - Unconditionally raises `check_violation` if modified. Prohibits `DELETE` unconditionally. Freezes terminal rows (`completed`, `expired`, `cancelled`, `superseded`).
  - *Status*: Trigger logic in `0003_dispatch_foundation.sql` is robust.
- **Vector 3 (Tracking Tables Append-Only)**:
  - All 8 tracking tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`) have `fc27_reject_mutation()` trigger `BEFORE UPDATE OR DELETE`.
  - Grants policy v4 does not grant `UPDATE` or `DELETE` on any tracking table to any runtime role.
  - *Status*: Trigger and grant posture is robust.
- **Vector 4 (Search Path Hijacking & Function Isolation)**:
  - `public.fc27_dispatch_transition` is `SECURITY DEFINER`, owned by `fc27_owner`.
  - `proconfig` sets `search_path=pg_catalog, pg_temp`.
  - All internal table references are explicitly schema-qualified (`public.dispatches`, etc.).
  - `REVOKE ALL ON FUNCTION public.fc27_dispatch_transition(...) FROM PUBLIC` is enforced in `grants.sql`.
  - Function body contains zero dynamic SQL (`EXECUTE`, `format()`).
  - *Status*: Passing and verified (`test_function_attributes`, `test_function_source_has_no_dynamic_sql`, `test_runtime_roles_cannot_replace_function`).

---

## Stress Test Results

| Test Scenario | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|
| Run `test_dispatch_schema.py` and `test_db_migrate_roles.py` | All tests pass | 1 failed, 21 passed, 26 errors | **FAIL** |
| Run `test_dispatch_transition.py` | All tests pass | 4 passed, 18 errors | **FAIL** |
| `Objective(metric="pass_accuracy", target_delta=26.123)` non-echo check | `26.123` NOT in error string | `26.123` contained in `input_value` dict | **FAIL** |
| `sample_approved_dispatch_prereqs` fixture setup | Creates valid prerequisites | Raises `CheckViolation` on `graph_threads_terminal_fields_chk` | **FAIL** |
| Check `fc27_dispatch_transition` attributes | `SECURITY DEFINER`, owner `fc27_owner`, `search_path=pg_catalog, pg_temp`, no `PUBLIC` execute | Exact match in PostgreSQL catalog | **PASS** |
| Check no dynamic SQL in transition function | No `EXECUTE`, no `format()` | Verified zero dynamic SQL | **PASS** |
| Check runtime roles cannot replace transition function | `InsufficientPrivilege` raised | Verified `InsufficientPrivilege` | **PASS** |

---

## Unchallenged Areas

- **Activation Gates (Milestone 4)**: Out of scope for Milestone 3 (evaluated in M4).
- **Atomic Dispatch Creation in Approval Transaction (Milestone 4)**: Out of scope for Milestone 3.
