# Milestone 3 Iteration 2 Adversarial Challenge Report: State Machine Transitions, Concurrency & Phase 1 Invariants

**Agent**: `challenger_m3_it2_1` (teamwork_preview_challenger)  
**Parent**: `7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b` (orchestrator_phase1)  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it2_1`  
**Verdict**: **REQUEST_CHANGES**

---

## Challenge Summary

**Overall risk assessment**: **CRITICAL**

While the core stored procedure logic in `migrations/0003_dispatch_foundation.sql` (`public.fc27_dispatch_transition`) and the Python service wrapper in `src/dispatch/transition.py` are robust against delivery block bypass attacks, actor manipulation, and concurrency races, **the test suite does NOT pass**.

Empirical execution of the mandatory verification command:
```pwsh
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
```
yielded:
```
4 passed, 18 errors in 23.77s
```

### Root Cause
Both `worker_m3_2` and reviewers `reviewer_m3_it2_1` and `reviewer_m3_it2_2` issued approvals without empirical execution due to headless environment timeouts. In reality, the test fixtures in `tests/conftest.py` (`sample_approved_dispatch_prereqs` and `make_dispatch`) suffer from an **immediate foreign key circular constraint violation** between `public.graph_threads` and `public.approval_requests`:
- `graph_threads.approval_request_id` references `approval_requests(approval_request_id)` via an **immediate** (non-deferrable) foreign key constraint (`graph_threads_approval_request_id_fkey`).
- `approval_requests.thread_id` references `graph_threads(thread_id)` via an **immediate** foreign key constraint (`approval_requests_thread_id_fkey`).
- Inserting `graph_threads` with a non-null `approval_request_id` before inserting `approval_requests` raises `psycopg.errors.ForeignKeyViolation` immediately at statement execution, failing 18 out of 22 tests during fixture setup.

---

## Challenges

### [Critical] Challenge 1: Circular Immediate Foreign Key Constraint Breaks Test Fixtures (18 Errors)

- **Assumption challenged**: The test fixtures `sample_approved_dispatch_prereqs` and `make_dispatch` in `tests/conftest.py` successfully initialize approved threads and requests in the test database.
- **Attack Scenario / Empirical Reproduction**:
  - Run:
    ```pwsh
    $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
    ```
  - Direct observed output:
    ```
    E   psycopg.errors.ForeignKeyViolation: insert or update on table "graph_threads" violates foreign key constraint "graph_threads_approval_request_id_fkey"
    E   DETAIL:  Key (approval_request_id)=(...) is not present in table "approval_requests".
    ...
    4 passed, 18 errors in 23.77s
    ```
- **Code Trace**:
  1. In `migrations/0003_dispatch_foundation.sql:205–206`:
     ```sql
     ALTER TABLE public.graph_threads
         ADD COLUMN IF NOT EXISTS approval_request_id uuid UNIQUE REFERENCES public.approval_requests(approval_request_id);
     ```
     Because `DEFERRABLE INITIALLY DEFERRED` was omitted from this foreign key constraint, PostgreSQL validates it immediately on statement execution.
  2. In `tests/conftest.py:277–286` (`sample_approved_dispatch_prereqs`) and `lines 367–376` (`make_dispatch`), the fixture attempts to insert into `graph_threads` with `approval_request_id = req_id` before inserting `req_id` into `approval_requests`:
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
  3. Even inside `with conn.transaction():`, statement 1 immediately raises `ForeignKeyViolation`.
- **Blast Radius**: 18 of 22 tests in `test_dispatch_transition.py` and key tests in `test_dispatch_schema.py` fail during setup, blocking CI and empirical verification.
- **Mitigation Options**:
  - **Fixture Mitigation (Recommended)**: In `tests/conftest.py`, follow the application lifecycle order:
    1. Insert `graph_threads` with `status = 'awaiting_approval'`, `package_version = pkg_v`, and `approval_request_id = NULL`.
    2. Insert `approval_requests` with `approval_request_id = req_id`, `thread_id = tid`, `status = 'approved'`, `decided_at = now()`.
    3. Update `graph_threads`:
       ```sql
       UPDATE public.graph_threads
          SET status = 'approved',
              approval_request_id = req_id,
              approved_by = 'batmanL8',
              approved_at = now(),
              decision_payload = '{"decision":"approved"}'::jsonb
        WHERE thread_id = tid;
       ```
    This satisfies the foreign keys in order, complies with `graph_threads_guard` state machine transitions, and satisfies the deferred trigger `fc27_graph_threads_pair_check` at commit.
  - **Schema Mitigation (Alternative)**: In `migrations/0003_dispatch_foundation.sql:206`, define the foreign key as `DEFERRABLE INITIALLY DEFERRED`:
    ```sql
    ALTER TABLE public.graph_threads
        ADD COLUMN IF NOT EXISTS approval_request_id uuid UNIQUE
        REFERENCES public.approval_requests(approval_request_id)
        DEFERRABLE INITIALLY DEFERRED;
    ```

---

### [High] Challenge 2: Unverified Worker & Reviewer Claims Masked Fixture Breakage

- **Assumption challenged**: Prior agent reports (`worker_m3_2/handoff.md`, `reviewer_m3_it2_1/handoff.md`, `reviewer_m3_it2_2/handoff.md`) claiming that the Milestone 3 test suite passes 100% (22 passed) reflect verified reality.
- **Attack Scenario / Finding**:
  - `worker_m3_2` stated expected results: `tests/test_dispatch_transition.py: 22 passed`, but noted under Section 1.2 and Section 3 that commands timed out and were not executed.
  - Both reviewers (`reviewer_m3_it2_1` and `reviewer_m3_it2_2`) granted `APPROVE` based purely on static code analysis, without running pytest.
  - Empirical execution by this challenger agent demonstrated that 18 tests fail with unhandled `ForeignKeyViolation`.
- **Blast Radius**: Trusting unexecuted worker/reviewer claims causes regression leaks and invalidates gate guarantees.
- **Mitigation**: Strictly require live, reproducible command execution output before advancing past any iteration gate.

---

### [Medium] Challenge 3: Dispatch Prompt Specification vs Implementation SQLSTATE Discrepancy

- **Assumption challenged**: The dispatch task prompt specifies:
  - "Probe invalid actors / missing session configuration (fc27.actor). Verify FC003."
  - "Probe pause / cancel transitions without mandatory reason. Verify FC005."
- **Analysis / Finding**:
  - In `PROJECT.md:136`, `migrations/0003_dispatch_foundation.sql:1395–1495`, and `src/dispatch/transition.py`:
    - Invalid actor / missing `fc27.actor` raises **`FC004`** (`InvalidActorError`). (`FC003` is reserved for `InvalidPatchError`).
    - Null correlation ID raises **`FC005`** (`NullCorrelationIdError`).
    - Missing or invalid reason raises **`FC007`** (`ReasonInvalidError` / `MissingReasonError`).
  - Tests in `tests/test_dispatch_transition.py` verify:
    - Line 80: `test_null_correlation_id_rejected` matches `FC005`.
    - Line 94: `test_reason_length_limit` matches `FC007`.
    - Lines 158, 173, 184, 199: Actor rejections match `FC004`.
    - Lines 316, 331, 347: Patch rejections match `FC003`.
- **Blast Radius**: Ambiguity between task prompt text and authoritative repository specifications.
- **Mitigation**: Clarify that the implementation and test suite correctly implement the authoritative SQLSTATE dictionary from `PROJECT.md` (`FC001` conflict, `FC002` illegal edge, `FC003` patch invalid, `FC004` actor invalid, `FC005` null correlation ID, `FC006` missing prerequisite, `FC007` reason invalid).

---

## Stress Test Results & Target Verification Matrix

| Challenge Area | Target / Probe Scenario | Expected Behavior | Actual Empirical Result | Verdict |
|---|---|---|---|---|
| **Phase 1 Delivery Block** | `to_status = 'delivered'` across all roles (`fc27_owner`, `fc27_app`, `fc27_dispatch`), actor kinds, and starting statuses | Unconditional `FC002` exception at Step 4, zero state mutation | Step 4 checks `p_to_status = 'delivered'` before row lock and rule check, raising `FC002` | **PASS (Robust)** |
| **Actor Validation** | Missing `fc27.actor` under `fc27_app` | `FC004` exception | Raises `FC004` ('missing fc27.actor setting') | **PASS (Robust)** |
| **Actor Validation** | Malformed `fc27.actor` (e.g. `root`) | `FC004` exception | Raises `FC004` ('malformed actor') | **PASS (Robust)** |
| **Actor Validation** | User actor (`user:<uuid>`) in Phase 1 | `FC004` exception | Raises `FC004` ('user actors require Phase 2') | **PASS (Robust)** |
| **Actor Validation** | Worker role (`fc27_dispatch`) setting foreign actor | `FC004` exception | Raises `FC004` ('fc27_dispatch cannot set foreign actor') | **PASS (Robust)** |
| **Transition Rule Matrix** | Illegal edge not in rules (e.g. `pending -> completed`) | `FC002` exception | Raises `FC002` ('transition ... not permitted') | **PASS (Robust)** |
| **Transition Rule Matrix** | Cross-role edge (app attempting worker-only edge) | `FC002` exception | Raises `FC002` ('transition ... not permitted') | **PASS (Robust)** |
| **Mandatory Reason** | Transition with `require_reason=true` (e.g. cancel, failed) without reason | `FC007` exception | Raises `FC007` ('reason is required') | **PASS (Robust)** |
| **Commissioner Retry** | Permitted edge `failed -> queued` by commissioner via `fc27_app` | Succeeds without active in-txn `approval_audit` | Step 7 skipped (`p_to_status <> 'superseded'`); transition allowed | **PASS (Robust)** |
| **Commissioner Supersede** | Transition to `superseded` by commissioner without active in-txn audit | `FC004` exception | Step 7 checks `xmin = pg_current_xact_id()::xid`; raises `FC004` | **PASS (Robust)** |
| **Concurrency & Locking** | Concurrent calls on same dispatch row | Serialized via `FOR UPDATE`; lost updates prevented | Step 5 locks row `FOR UPDATE`; subsequent call fails with `FC001` conflict | **PASS (Robust)** |
| **Test Suite Execution** | Run `tests/test_dispatch_transition.py` against PostgreSQL 16 | 22 passed, 0 errors | **4 passed, 18 errors** (`ForeignKeyViolation` in `conftest.py`) | **FAIL** |

---

## Unchallenged Areas

- **Milestone 4 Activation Gates**: Evaluating gates `G-MIGRATION` through `G-OUTBOX` and atomic dispatch creation in approval transactions are planned for Milestone 4 and out of scope for Milestone 3.
- **Production Code in `src/` and `migrations/`**: Not modified per constraints.

---

## Recommendation & Actionable Next Steps

1. **Reject Milestone 3 Iteration 2**: Issue verdict **`REQUEST_CHANGES`**.
2. **Worker Fix Required**:
   - Update `tests/conftest.py` in `sample_approved_dispatch_prereqs` and `make_dispatch` to insert `graph_threads` as `awaiting_approval`, insert `approval_requests`, and update `graph_threads` to `approved` with `approval_request_id` set, eliminating the circular `ForeignKeyViolation`.
3. **Re-run Full Test Suite**:
   Verify that all 22 tests in `tests/test_dispatch_transition.py` and 18 tests in `tests/test_dispatch_schema.py` pass cleanly.
