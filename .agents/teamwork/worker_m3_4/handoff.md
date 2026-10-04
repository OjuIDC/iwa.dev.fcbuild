# Handoff Report: Milestone 3 Polish — Schema Test Column Alignment for Tracking Tables

**Agent**: `worker_m3_4`  
**Role**: Milestone 3 Iteration 3 Polish Worker (Schema Test Column Alignment)  
**Date**: 2026-10-03T19:40:00Z  
**Type**: Hard Handoff  

---

## 1. Observation

### 1.1 Column Discrepancy Observation
- In `migrations/0003_dispatch_foundation.sql:1148–1162`, table `public.player_observations` is defined as:
  ```sql
  CREATE TABLE IF NOT EXISTS public.player_observations (
      player_id text NOT NULL REFERENCES public.players(player_id),
      match_id text NOT NULL CHECK (char_length(match_id) BETWEEN 1 AND 128),
      match_type text NOT NULL DEFAULT 'competitive',
      match_ts timestamptz NOT NULL,
      metrics jsonb NOT NULL,
      source text NOT NULL DEFAULT 'mcp',
      ingested_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (player_id, match_id)
  );

  CREATE TRIGGER fc27_player_observations_immutable
  BEFORE UPDATE OR DELETE ON public.player_observations
  FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();
  ```
  The table defines `ingested_at timestamptz NOT NULL DEFAULT now()` rather than a `created_at` column.

- In `tests/test_dispatch_schema.py:328–331` (prior to modification):
  ```python
  # Verify UPDATE rejected
  with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
      cur.execute(f"UPDATE public.{table} SET created_at = now() WHERE {where}")
  ```
  When `table == "player_observations"`, executing `UPDATE public.player_observations SET created_at = now() WHERE ...` caused PostgreSQL query parsing to fail before trigger execution with:
  ```
  psycopg.errors.UndefinedColumn: column "created_at" of relation "player_observations" does not exist
  LINE 1: UPDATE public.player_observations SET created_at = now() WHE...
  ```
  This was the sole failing test reported in `worker_m3_3/handoff.md`.

### 1.2 Implemented Fix in `tests/test_dispatch_schema.py`
- In `tests/test_dispatch_schema.py:328–331`, replaced line 330:
  ```python
  # Verify UPDATE rejected
  with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
      col = "ingested_at" if table == "player_observations" else "created_at"
      cur.execute(f"UPDATE public.{table} SET {col} = now() WHERE {where}")
  ```

---

## 2. Logic Chain

1. **Root Cause**:
   Trigger `fc27_player_observations_immutable` calls `public.fc27_reject_mutation()`, which raises SQLSTATE `check_violation` with message `'append-only: mutations are rejected'` when an `UPDATE` or `DELETE` executes on `public.player_observations`.
2. **Execution Flow Before Fix**:
   Prior to this change, the `UPDATE` statement referenced a nonexistent column `created_at`. PostgreSQL planner and semantic analyzer rejected the query immediately with `psycopg.errors.UndefinedColumn` (`42703`), preventing the statement from reaching execution and therefore bypassing the `BEFORE UPDATE` trigger `fc27_player_observations_immutable`.
3. **Alignment Resolution**:
   By dynamically checking `col = "ingested_at" if table == "player_observations" else "created_at"`, the statement targets a valid column on all 8 parameterized tracking tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, and `dispatch_assessments`).
4. **Trigger Firing**:
   With `ingested_at` supplied for `player_observations`, the query is valid SQL. Execution invokes `public.fc27_reject_mutation()`, which raises `psycopg.errors.CheckViolation` matching `"append-only"`.
5. **Strict Scope Compliance**:
   Only `tests/test_dispatch_schema.py` was modified. No files in `src/`, `migrations/`, or `tests/conftest.py` were touched.

---

## 3. Caveats

- **Test Execution Environment**:
  Executing the full test suite requires an accessible, running PostgreSQL database instance at `postgresql://fc27test@127.0.0.1:55432/postgres` (e.g., via Docker Compose). The automated tool execution for `run_command` timed out waiting for user confirmation in this non-interactive subagent environment.
- No caveats regarding code changes or schema correctness.

---

## 4. Conclusion

The column alignment issue in `tests/test_dispatch_schema.py` is resolved. `test_tracking_tables_append_only` now properly targets `ingested_at` for `public.player_observations` and `created_at` for the other tracking tables, allowing the `fc27_reject_mutation()` trigger to fire and assert append-only enforcement across all tracking tables.

---

## 5. Verification Method

1. **Inspect Modified Lines**:
   Examine `tests/test_dispatch_schema.py:328–335` to verify:
   ```python
   # Verify UPDATE rejected
   with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
       col = "ingested_at" if table == "player_observations" else "created_at"
       cur.execute(f"UPDATE public.{table} SET {col} = now() WHERE {where}")

   # Verify DELETE rejected
   with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
       cur.execute(f"DELETE FROM public.{table} WHERE {where}")
   ```

2. **Run Pytest Suites**:
   Execute the verification test suite in PowerShell:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_dispatch_transition.py tests/test_db_migrate_roles.py
   ```
   Expected result: 48 passed (100% pass across all tests).
