# Task Assignment: worker_m3_2 (Milestone 3 Iteration 2 Worker)

## Mission
Implement the agreed fixes for Milestone 3 Iteration 2 to resolve the defects identified during adversarial challenge:
1. Fix test fixtures in `tests/conftest.py` (`sample_approved_dispatch_prereqs`, `make_dispatch`, and psycopg `_sqlcodes` registration).
2. Fix `src/schemas.py` and `src/dispatch/objective.py` (`hide_input_in_errors=True` on `Objective` and `Candidate`, and pure-Python `validate_metric_bounds`).
3. Fix Step 7 in `migrations/0003_dispatch_foundation.sql` (`fc27_dispatch_transition`) to allow commissioner retry (`failed -> queued`) while keeping in-transaction audit proof for `superseded`.
4. Re-run migration and verify that all 40 tests across `tests/test_dispatch_schema.py` and `tests/test_dispatch_transition.py` pass cleanly with zero errors and zero regressions.

## Working Directory
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2`

## Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Challenger 2 Failure Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md`
4. The 3 Iteration 2 Explorer Reports:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_fixtures_1\fixtures_fix_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_schemas_1\schemas_fix_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_tests_1\tests_fix_analysis.md`

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Write Ownership
You hold exclusive write ownership of the following files:
- `tests/conftest.py`
- `src/schemas.py`
- `src/dispatch/objective.py`
- `migrations/0003_dispatch_foundation.sql` (Step 7 fix in `fc27_dispatch_transition`)
- `tests/test_dispatch_schema.py`
- `tests/test_dispatch_transition.py`

## Implementation Steps

### 1. `src/schemas.py` and `src/dispatch/objective.py`
- In `src/schemas.py`:
  - On `Objective`: set `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`.
  - On `Candidate`: set `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`.
  - Delegate bounds check in `Objective` to `validate_metric_bounds`.
- In `src/dispatch/objective.py`:
  - Implement standalone `validate_metric_bounds(metric: str, target_delta: float | int, policy: str = "fc27-objective-bounds-v1")`.

### 2. `tests/conftest.py`
- Add dynamic mapping of custom SQLSTATEs to `psycopg.errors._sqlcodes`:
  ```python
  import psycopg.errors
  for _code in ("FC001", "FC002", "FC003", "FC004", "FC005", "FC006", "FC007"):
      psycopg.errors._sqlcodes[_code] = psycopg.errors.RaiseException
  ```
- Replace lines 260–382 in `tests/conftest.py` following the drop-in design in `fixtures_fix_analysis.md`:
  - `sample_approved_dispatch_prereqs`: wrap in `with conn.transaction():`, provide terminal fields (`approved_by='batmanL8'`, `approved_at=now()`, `decision_payload='{"decision":"approved"}'::jsonb`), and use distinct paired threads (`tid` for `audit_id_1`, `tid_2` for `audit_id_2`) so `approval_audit_one_decision_per_thread` is never violated.
  - `make_dispatch`: generate distinct `d_tid` and paired `d_req_id` on each call to prevent unique constraint collisions on `approval_audit(thread_id)`.

### 3. `migrations/0003_dispatch_foundation.sql`
- In Step 7 of `fc27_dispatch_transition` (around lines 1499–1514):
  Change:
  ```sql
  IF v_actor_kind = 'commissioner' THEN
      IF p_to_status = 'superseded' THEN
          ...
      ELSE
          RAISE EXCEPTION 'commissioner edge to % deferred to Phase 2', p_to_status USING ERRCODE = 'FC004';
      END IF;
  END IF;
  ```
  to:
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

## Verification Commands
Execute the full test verification suite against local PostgreSQL 16:
```pwsh
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py tests/test_dispatch_schema.py tests/test_dispatch_transition.py
```

## Completion Criteria
1. All 18 tests in `tests/test_dispatch_schema.py` pass.
2. All 22 tests in `tests/test_dispatch_transition.py` pass.
3. Zero regressions across baseline suites (`test_db_roles`, `test_approval_lifecycle`, `test_graph_persistence`, `test_graph_api_v1`).
4. Write detailed `handoff.md` in your working directory and notify the orchestrator via `send_message`.


## 2026-10-03T18:25:28Z
You are worker_m3_2, a teamwork_preview_worker agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. Challenger 2 Failure Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\challenge_report.md
4. The 3 Iteration 2 Explorer Reports:
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_fixtures_1\fixtures_fix_analysis.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_schemas_1\schemas_fix_analysis.md
   - c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_tests_1\tests_fix_analysis.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write ownership:
- tests/conftest.py
- src/schemas.py
- src/dispatch/objective.py
- migrations/0003_dispatch_foundation.sql (Step 7 in fc27_dispatch_transition)
- tests/test_dispatch_schema.py
- tests/test_dispatch_transition.py

Task:
Implement the agreed fixes across your owned files:
1. tests/conftest.py:
   - Register custom SQLSTATEs in psycopg.errors._sqlcodes:
     for _code in ("FC001", "FC002", "FC003", "FC004", "FC005", "FC006", "FC007"): psycopg.errors._sqlcodes[_code] = psycopg.errors.RaiseException
   - Replace sample_approved_dispatch_prereqs and make_dispatch per fixtures_fix_analysis.md: provide terminal fields (approved_by, approved_at, decision_payload), wrap in conn.transaction(), use dedicated paired thread per decision.
2. src/schemas.py & src/dispatch/objective.py:
   - Set model_config = ConfigDict(extra="forbid", hide_input_in_errors=True) on Objective and Candidate.
   - Implement validate_metric_bounds in src/dispatch/objective.py.
3. migrations/0003_dispatch_foundation.sql:
   - Change Step 7 condition to IF v_actor_kind = 'commissioner' AND p_to_status = 'superseded' THEN ... END IF; to permit commissioner retry (failed -> queued).

Execute verification tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py tests/test_dispatch_schema.py tests/test_dispatch_transition.py

Write handoff.md in your working directory and notify the orchestrator with send_message when done.
