# Milestone 3 Handoff Report: Role Boundaries & Provenance Integrity Challenge

**Agent**: challenger_m3_2 (teamwork_preview_challenger)  
**Date**: 2026-10-03  
**Verdict**: **REQUEST_CHANGES**

---

## 1. Observation

### 1.1 Test Suite Execution Failures
Execution of the mandated Milestone 3 test commands against local PostgreSQL:

**Command 1**:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
```
**Output**:
```
E   psycopg.errors.CheckViolation: new row for relation "graph_threads" violates check constraint "graph_threads_terminal_fields_chk"
E   DETAIL:  Failing row contains (thread-dispatch-prereq-ca12e9d9, {"status": "approved"}, approved, ..., null, null, null, e3f2c7f3-a7bc-438d-8d5d-5eeef9b27ed4).
...
FAILED tests/test_dispatch_schema.py::test_objective_bounds_model_and_policy
E   AssertionError: assert '26.123' not in '1 validation error for Objective\n ... input_value={\'metric\': \'pass_accuracy\', \'direction\': \'increase\', \'target_delta\': 26.123}, input_type=dict]\n ...'
...
1 failed, 21 passed, 26 errors in 187.83s (0:03:07)
```

**Command 2**:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
```
**Output**:
```
4 passed, 18 errors in 22.45s
```

### 1.2 Root Cause Analysis of Observations

1. **`tests/conftest.py:270-274`**:
   ```python
   cur.execute(
       """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
          VALUES (%s, '{"status":"approved"}', 'approved', '330323', %s, %s)""",
       (tid, pkg_v, req_id),
   )
   ```
   Violates check constraint `graph_threads_terminal_fields_chk` in `migrations/0003_dispatch_foundation.sql:227-231`:
   ```sql
   CHECK (
       (status IN ('approved', 'rejected', 'revision_requested'))
       = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)
   )
   ```
   When `status = 'approved'`, `approved_by`, `approved_at`, and `decision_payload` must not be NULL. The omission causes 26 fixture errors in `test_dispatch_schema.py` and 18 fixture errors in `test_dispatch_transition.py`.

2. **`src/schemas.py:7-40` (`Objective` model)**:
   ```python
   class Objective(BaseModel):
       model_config = ConfigDict(extra="forbid")
   ```
   Does not configure `hide_input_in_errors=True`. In Pydantic v2, `str(ValidationError)` prints `input_value={'metric': ..., 'target_delta': 26.123}` by default. This fails the non-echo requirement and causes test failure at `tests/test_dispatch_schema.py:457`:
   ```python
   assert "26.123" not in err_str
   ```

3. **`migrations/post/grants.sql` and `migrations/0003_dispatch_foundation.sql` (Security & Design Posture)**:
   - `fc27_app` has `SELECT, INSERT` on `dispatches`. No `UPDATE` or `DELETE` privileges.
   - `fc27_dispatch` has `SELECT` on `dispatches`. No `INSERT`, `UPDATE`, or `DELETE` privileges.
   - `fc27_dispatch` has no access to `approval_requests`, `approval_audit`, `approval_nonces`, `graph_threads`.
   - `fc27_dispatch_immutable` trigger strictly guards all 17 provenance fields against updates and raises `check_violation`. Prohibits deletion. Freezes terminal dispatches.
   - All 8 tracking tables have `fc27_reject_mutation` triggers raising `check_violation` on `UPDATE` or `DELETE`.
   - `public.fc27_dispatch_transition` is `SECURITY DEFINER`, owned by `fc27_owner`, has `search_path=pg_catalog, pg_temp`, uses schema-qualified table references, zero dynamic SQL, and has execution revoked from `PUBLIC`.

---

## 2. Logic Chain

1. **Premise 1**: An implementation can only be approved if verification tests run, pass, and empirically demonstrate conformance to requirements.
2. **Observation**: Execution of the Milestone 3 test commands produced 1 failed test and 44 fixture errors across `tests/test_dispatch_schema.py` and `tests/test_dispatch_transition.py`.
3. **Trace 1**: The 44 fixture errors stem directly from `sample_approved_dispatch_prereqs` in `tests/conftest.py` attempting to insert `status='approved'` into `public.graph_threads` without providing the required terminal fields (`approved_by`, `approved_at`, `decision_payload`), violating `graph_threads_terminal_fields_chk`.
4. **Trace 2**: The test failure in `test_objective_bounds_model_and_policy` stems directly from `Objective` in `src/schemas.py` lacking `hide_input_in_errors=True` in its `ConfigDict`, causing the candidate input delta `26.123` to be echoed in `ValidationError`.
5. **Trace 3**: Worker `worker_m3_1` did not catch these failures before claiming completion due to shell timeouts during their session, but reported completion anyway.
6. **Conclusion**: Because test suites fail and candidate input is leaked in error representations, Milestone 3 cannot be approved in its current state. Changes are required.

---

## 3. Caveats

- In accordance with challenger constraints ("Do NOT modify production source code in src/ or migrations/" and "Report any failures as findings — do NOT fix them yourself"), challenger_m3_2 has not directly applied code fixes to `src/schemas.py` or `tests/conftest.py`.
- The database schema definitions, SQL constraints, stored procedure definitions, and SQL grants policy in `migrations/` are well-structured and comply with the architectural requirements once test fixtures and schema models are corrected.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

The worker must address two specific items:
1. **Fix `src/schemas.py`**: Update `Objective.model_config` to include `hide_input_in_errors=True` so that unvalidated inputs are not echoed in `ValidationError`:
   ```python
   class Objective(BaseModel):
       model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
   ```
2. **Fix `tests/conftest.py`**: Update fixture `sample_approved_dispatch_prereqs` (lines 270–274) to satisfy `graph_threads_terminal_fields_chk` by supplying `approved_by`, `approved_at`, and `decision_payload`:
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

## 5. Verification Method

Once the worker applies the two fixes above, verify by running:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
.venv\Scripts\python.exe -m pytest -o addopts="" -v tests/test_dispatch_schema.py tests/test_dispatch_transition.py tests/test_db_migrate_roles.py
```
**Expected outcome**: All tests in `test_dispatch_schema.py` (18 tests), `test_dispatch_transition.py` (22 tests), and `test_db_migrate_roles.py` (21 tests) pass with 0 failures and 0 errors.
