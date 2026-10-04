# Milestone 3 Iteration 2 Challenger 2 Handoff Report

**Agent**: challenger_m3_it2_2 (teamwork_preview_challenger)  
**Roles**: critic, specialist  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_it2_2`  
**Verdict**: **REQUEST_CHANGES**  
**Handoff Type**: Hard Handoff  

---

## 1. Observation

### 1.1 Empirical Test Suite Results
1. Command executed via PowerShell:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   ```
   Direct result:
   ```
   ....EEEEEEEEEEEEEEEEEEEEEEEEEE..................                         [100%]
   22 passed, 26 errors in 178.38s (0:02:58)
   ```
   Verbatim error from `task-38.log`:
   ```
   E   psycopg.errors.ForeignKeyViolation: insert or update on table "graph_threads" violates foreign key constraint "graph_threads_approval_request_id_fkey"
   E   DETAIL:  Key (approval_request_id)=(249b2fcc-f15c-41b0-b488-6d22bd318c8f) is not present in table "approval_requests".
   .venv\Lib\site-packages\psycopg\cursor.py:117: ForeignKeyViolation
   ```
   All 26 errors occurred at setup of tests in `tests/test_dispatch_schema.py` using fixtures `sample_approved_dispatch_prereqs` and `make_dispatch`.

2. Command executed via PowerShell:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   ```
   Direct result:
   ```
   ....EEEEEEEEEEEEEEEEEE                                                   [100%]
   4 passed, 18 errors in 22.66s
   ```
   All 18 errors occurred at setup of tests in `tests/test_dispatch_transition.py` using `make_dispatch`.

### 1.2 Database Schema Foreign Key Definition
In `migrations/0003_dispatch_foundation.sql:205–206`:
```sql
ALTER TABLE public.graph_threads
    ADD COLUMN IF NOT EXISTS approval_request_id uuid UNIQUE REFERENCES public.approval_requests(approval_request_id);
```
Direct observation: The constraint `graph_threads_approval_request_id_fkey` is an immediate foreign key. It is NOT declared `DEFERRABLE INITIALLY DEFERRED`.

### 1.3 Test Fixture Setup In `tests/conftest.py`
In `tests/conftest.py:277–286`:
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
        approval_request_id, thread_id, package_version, status,
        created_at, expires_at, decided_at
    ) VALUES (%s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now())""",
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
Direct observation: Statement 1 inserts `approval_request_id = req_id` into `public.graph_threads` before `public.approval_requests` contains `req_id`.

### 1.4 Zero-Echo Verification
In `src/schemas.py:8, 35, 43`:
```python
class Objective(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
```
In `tests/test_dispatch_schema.py:434–458`:
`test_objective_bounds_model_and_policy` verifies that `Objective(metric="pass_accuracy", target_delta=26.123)` raises a `ValidationError` containing `"target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1)"` and does NOT contain `"26.123"`. This test passed cleanly during execution.

---

## 2. Logic Chain

1. From Observation 1.2, constraint `graph_threads_approval_request_id_fkey` on table `public.graph_threads` references `public.approval_requests(approval_request_id)` with standard immediate evaluation.
2. From Observation 1.3, fixture `sample_approved_dispatch_prereqs` and helper `make_dispatch` execute `INSERT INTO public.graph_threads` with a non-null `approval_request_id` in statement 1, before inserting the corresponding row into `public.approval_requests` in statement 2.
3. Although statements 1 and 2 are enclosed in `with conn.transaction():`, PostgreSQL immediate foreign key constraints check referential integrity at the completion of each individual SQL statement, not at transaction commit.
4. Therefore, statement 1 immediately aborts with `psycopg.errors.ForeignKeyViolation: insert or update on table "graph_threads" violates foreign key constraint "graph_threads_approval_request_id_fkey"`.
5. From Observation 1.1, this immediate exception causes 26 test errors in `tests/test_dispatch_schema.py` and 18 test errors in `tests/test_dispatch_transition.py`, totaling 44 broken tests across Milestone 3.
6. Consequently, Milestone 3 cannot be approved until this test fixture blocker is resolved.

---

## 3. Caveats

1. **Production Schema & Application Code**:
   - `migrations/0003_dispatch_foundation.sql`, `migrations/post/grants.sql`, `src/schemas.py`, and `src/dispatch/objective.py` are structurally sound, least-privileged, and correctly enforce zero-echo and role boundaries. The primary defect resides in test fixture setup in `tests/conftest.py`.
2. **Phase 1 Invariants**:
   - `DISPATCH_ENABLED=false` remains strictly enforced. Transitions to `delivered` fail closed with `FC002`.
3. **Out-of-Scope Milestone Features**:
   - Milestone 4 activation gates and atomic approval-to-dispatch creation remain planned for M4.

---

## 4. Conclusion

The work product fails verification gate due to a critical test fixture bug in `tests/conftest.py` that violates immediate foreign key constraint `graph_threads_approval_request_id_fkey`, crashing 44 tests across the Milestone 3 suite.

**Verdict**: **REQUEST_CHANGES**

### Required Action Items for Worker:
1. In `tests/conftest.py`, update `sample_approved_dispatch_prereqs` and `make_dispatch` so that `public.graph_threads` and `public.approval_requests` are inserted without violating immediate foreign key constraints.
   - **Recommended Approach**: Follow the state machine transitions (`running` -> `awaiting_approval` -> `approved`):
     ```python
     # 1. Insert thread as running (approval_request_id is NULL)
     cur.execute(
         """INSERT INTO public.graph_threads (thread_id, state, status, club_id)
            VALUES (%s, '{"status":"running"}'::jsonb, 'running', '330323')""",
         (tid,),
     )
     # 2. Insert approval request as awaiting_approval
     cur.execute(
         """INSERT INTO public.approval_requests (
             approval_request_id, thread_id, package_version, status, created_at, expires_at
         ) VALUES (%s, %s, %s, 'awaiting_approval', now() - interval '10m', now() + interval '10h')""",
         (req_id, tid, pkg_v),
     )
     # 3. Update thread to awaiting_approval, linking approval_request_id
     cur.execute(
         """UPDATE public.graph_threads
               SET status = 'awaiting_approval', package_version = %s, approval_request_id = %s
             WHERE thread_id = %s""",
         (pkg_v, req_id, tid),
     )
     # 4. Update request to approved
     cur.execute(
         """UPDATE public.approval_requests
               SET status = 'approved', decided_at = now()
             WHERE approval_request_id = %s""",
         (req_id,),
     )
     # 5. Update thread to approved with terminal fields
     cur.execute(
         """UPDATE public.graph_threads
               SET status = 'approved', approved_by = 'batmanL8', approved_at = now(),
                   decision_payload = '{"decision":"approved"}'::jsonb
             WHERE thread_id = %s""",
         (tid,),
     )
     # 6. Insert approval_audit
     cur.execute(
         """INSERT INTO public.approval_audit (
             thread_id, commissioner, decision, notes, owner, selection,
             candidate_id, player_id, approved_by, approval_request_id, package_version
         ) VALUES (%s, 'batmanL8', 'APPROVE', 'ok', 'coach', 'best', 'cand-1', %s, 'batmanL8', %s, %s)
         RETURNING id""",
         (tid, sample_player["player_id"], req_id, pkg_v),
     )
     ```
   - Alternatively, perform synthetic test seeding using `pg_admin_db_uri` with `SET session_replication_role = 'replica'` (as `make_dispatch` already does on lines 424–433 for dispatches).
2. Re-run test commands to verify 100% pass rate:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py tests/test_dispatch_transition.py
   ```

---

## 5. Verification Method

1. Run the test command:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   ```
2. Verify test count:
   - Target: 48 passed, 0 failures, 0 errors.
   - Current: 22 passed, 26 errors (`ForeignKeyViolation`).
3. Inspect files:
   - `tests/conftest.py` (lines 264–342, 345–436)
   - `migrations/0003_dispatch_foundation.sql` (lines 205–206)
   - `tests/test_dispatch_schema.py`
   - `tests/test_dispatch_transition.py`
