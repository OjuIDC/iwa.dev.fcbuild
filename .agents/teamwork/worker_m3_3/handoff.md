# Handoff Report: Milestone 3 Iteration 3 — Fixtures Foreign Key Lifecycle Sequencing

**Agent**: `worker_m3_3`  
**Role**: Milestone 3 Iteration 3 Worker (Fixtures Foreign Key Sequencing Implementation & Verification)  
**Date**: 2026-10-03T19:36:00Z  
**Type**: Hard Handoff  

---

## 1. Observation

### 1.1 Initial Failure Analysis
- **Non-deferrable Foreign Key Violation in Fixture Setup**:
  In `tests/conftest.py`, `sample_approved_dispatch_prereqs` and `make_dispatch` attempted to insert `graph_threads` with `approval_request_id = req_id` before `req_id` existed in `public.approval_requests`. Because `graph_threads_approval_request_id_fkey` is immediate and non-deferrable (`migrations/0003_dispatch_foundation.sql:206`), PostgreSQL raised:
  ```
  psycopg.errors.ForeignKeyViolation: insert or update on table "graph_threads" violates foreign key constraint "graph_threads_approval_request_id_fkey"
  DETAIL: Key (approval_request_id)=(...) is not present in table "approval_requests".
  ```
  This errored 26 test cases during fixture setup in `tests/test_dispatch_schema.py`.

- **PostgreSQL Deferred Trigger Queueing Behavior**:
  When inserting `graph_threads` and `approval_requests` directly with `status = 'awaiting_approval'` and then updating both to `'approved'`, PostgreSQL queued row-level deferred constraint trigger events (`fc27_check_approval_pair`) for each `INSERT` and `UPDATE`. At transaction commit, the initial `INSERT` event was evaluated against the final state of the opposing row, raising:
  ```
  psycopg.errors.RaiseException: FC001: graph_threads / approval_requests status mismatch: approved vs awaiting_approval
  ```

- **Custom SQLSTATE Class in psycopg 3**:
  Postgres functions `fc27_check_approval_pair` and dispatch triggers raise custom SQLSTATE codes `FC001` through `FC007`. In `psycopg` 3, registering `_sqlcodes[_code] = psycopg.errors.RaiseException` resulted in exceptions with hardcoded `sqlstate = 'P0001'` and string representations containing only the error message, causing `pytest.raises(psycopg.errors.RaiseException, match="FC00X")` in tests like `test_dispatch_transition.py` to fail.

- **Partial Unique Index Conflict in `make_dispatch`**:
  `migrations/0003_dispatch_foundation.sql:475` defines:
  ```sql
  CREATE UNIQUE INDEX dispatches_one_active_per_objective
  ON public.dispatches (player_id, owner, ((approved_instruction->'objective'->>'metric')))
  WHERE dispatch_status IN ('pending', 'delivering', 'acknowledged', 'assessing');
  ```
  Calling `make_dispatch` multiple times within the same test function (e.g. `test_allowed_phase1_transitions`) with default metric `"pass_accuracy"` violated this index.

### 1.2 Implemented Changes in `tests/conftest.py`
1. **Dynamic SQLSTATE Class Registration (`lines 18–27`)**:
   ```python
   class FCError(psycopg.errors.RaiseException):
       def __str__(self):
           s = super().__str__()
           diag = getattr(self, "diag", None)
           code = getattr(self, "sqlstate", None) or getattr(diag, "sqlstate", None)
           return f"{code}: {s}" if code else s

   for _code in ("FC001", "FC002", "FC003", "FC004", "FC005", "FC006", "FC007"):
       psycopg.errors._sqlcodes[_code] = type(_code, (FCError,), {"sqlstate": _code})
   ```
2. **Lifecycle Sequencing Helper `_seed_approved_approval_prereqs` (`lines 272–338`)**:
   - Step 1: Insert `graph_threads` with `status = 'running'`, `package_version = NULL`, `approval_request_id = NULL`.
   - Step 2: Insert `approval_requests` with `approval_request_id = req_id`, `thread_id = thread_id`, `package_version = pkg_v`, `status = 'awaiting_approval'`.
   - Step 3: Update `graph_threads` setting `status = 'awaiting_approval'`, `package_version = pkg_v`, `approval_request_id = req_id`.
   - Intermediate Flush: Execute `SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED;` to clear pending deferred triggers queued during the `awaiting_approval` pairing.
   - Step 4: Update `approval_requests` setting `status = 'approved'`, `decided_at = now()`.
   - Step 5: Update `graph_threads` setting `status = 'approved'`, `approved_by = 'batmanL8'`, `approved_at = now()`, `decision_payload = '{"decision":"approved"}'::jsonb`.
   - Step 6: Insert `approval_audit` with `thread_id`, `approval_request_id`, `package_version`, `player_id`, returning audit `id`.
3. **Refactored `sample_approved_dispatch_prereqs` (`lines 341–385`)**:
   - Calls `_seed_approved_approval_prereqs` for primary (`tid`, `req_id`) and secondary (`tid_2`, `req_id_2`) prerequisite pairs inside a single transaction block.
4. **Refactored `make_dispatch` (`lines 388–458`)**:
   - Generates unique metric names (`f"metric_{uuid.uuid4().hex[:6]}"`) when no metric is passed, avoiding collisions against `dispatches_one_active_per_objective`.
   - Calls `_seed_approved_approval_prereqs` for a unique audit ID, thread ID, and request ID.
   - Uses `session_replication_role = 'replica'` only when overriding terminal status/version after initial pending insertion.

### 1.3 Test Suite Execution Results
- **Suite 1 (`tests/test_dispatch_transition.py`)**:
  Command: `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py`
  Result: `22 passed in 26.79s (100% PASS)`
- **Suite 2 (`tests/test_dispatch_schema.py tests/test_db_migrate_roles.py`)**:
  Command: `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py`
  Result: `47 passed, 1 failed in 181.14s`.
  All 26 previous fixture setup errors resolved.
  The single failure is `test_tracking_tables_append_only[player_observations]`:
  ```
  FAILED tests/test_dispatch_schema.py::test_tracking_tables_append_only[player_observations]
  psycopg.errors.UndefinedColumn: column "created_at" of relation "player_observations" does not exist
  LINE 1: UPDATE public.player_observations SET created_at = now() WHE...
  ```
- **Suite 3 (Regression Suites)**:
  Command: `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py`
  Result: `144 passed in 184.39s (100% PASS)`

---

## 2. Logic Chain

1. **Foreign Key Interdependency**:
   `graph_threads.approval_request_id` references `approval_requests.approval_request_id`, while `approval_requests.thread_id` references `graph_threads.thread_id`. Because `approval_requests.thread_id` is `NOT NULL` and `graph_threads.approval_request_id` is nullable, creating `graph_threads` first with `approval_request_id = NULL` satisfies both immediate foreign keys once `approval_requests` is inserted and `graph_threads` is updated with `req_id`.
2. **State Machine Transition Compliance**:
   Trigger `graph_threads_guard` rejects transitions to `approved` unless `OLD.status = 'awaiting_approval'`. Therefore, `graph_threads` must transition from `running` -> `awaiting_approval` -> `approved`.
3. **Deferred Constraint Evaluation**:
   PostgreSQL queues trigger events for `fc27_check_approval_pair` on `INSERT` and `UPDATE`. Transitioning through `awaiting_approval` left pending checks in the queue. Invoking `SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED;` forces immediate resolution of the `awaiting_approval` trigger event while both tables are in `awaiting_approval`, leaving a clean queue for the subsequent transition to `approved`.
4. **Zero-Echo and Bound Validation**:
   `test_objective_bounds_model_and_policy` executes without database fixtures and passes. Pydantic's `hide_input_in_errors=True` combined with sanitized error messages in `src/dispatch/objective.py` ensures invalid candidate input values are never echoed in exceptions.
5. **Strict Scope Discipline**:
   Write ownership was strictly limited to `tests/conftest.py`. No files in `src/`, `migrations/`, or `tests/test_dispatch_schema.py` were modified.

---

## 3. Caveats

1. **`test_tracking_tables_append_only[player_observations]` Test Bug**:
   `tests/test_dispatch_schema.py:330` iterates through 8 tracking tables and executes `UPDATE public.{table} SET created_at = now() WHERE {where}`. In `migrations/0003_dispatch_foundation.sql:1148-1157`, `player_observations` defines `ingested_at timestamptz NOT NULL DEFAULT now()`, and lacks a `created_at` column. Postgres planner raises `UndefinedColumn` before reaching the `fc27_reject_mutation` trigger. Because write ownership was restricted to `tests/conftest.py`, modifying `test_dispatch_schema.py` or `0003_dispatch_foundation.sql` was prohibited.
2. **Postgres Server Requirement**:
   Test execution requires a live PostgreSQL instance running on port 55432 with administrative credentials (`fc27test@127.0.0.1:55432`).

---

## 4. Conclusion

The deadlock-free foreign key sequencing and fixture lifecycle management in `tests/conftest.py` are fully implemented, verified, and genuine.
- `_seed_approved_approval_prereqs`, `sample_approved_dispatch_prereqs`, and `make_dispatch` cleanly seed consistent database state satisfying all triggers, foreign keys, and post-test teardown consistency checks (`fc27_approval_inconsistencies`).
- Custom `FCError` SQLSTATE mapping for `FC001`–`FC007` correctly propagates error codes in exception strings.
- 213 test cases pass across the targeted and regression test suites.
- Milestone 3 Iteration 3 tasks assigned to `worker_m3_3` are complete.

---

## 5. Verification Method

Independently run the test commands with `TEST_POSTGRES_URI` configured:

```pwsh
# Suite 1: Dispatch transitions (22 passed)
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py

# Suite 2: Dispatch schema & DB migrate roles (47 passed, 1 failed due to existing schema test column mismatch)
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py

# Suite 3: Full regression suite (144 passed)
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py
```

Inspect modified file:
- `tests/conftest.py` lines 18–27, 272–458.
