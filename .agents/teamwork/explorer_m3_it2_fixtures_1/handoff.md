# Milestone 3 Iteration 2: Fixtures Investigation Handoff Report

**Agent**: explorer_m3_it2_fixtures_1 (teamwork_preview_explorer)  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_fixtures_1`  
**Handoff Type**: Hard (Investigation complete)

---

## 1. Observation

1. **`migrations/0003_dispatch_foundation.sql:226–231` (Check Constraint)**:
   ```sql
   ALTER TABLE public.graph_threads
       ADD CONSTRAINT graph_threads_terminal_fields_chk
       CHECK (
           (status IN ('approved', 'rejected', 'revision_requested'))
           = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)
       );
   ```
   Requires that any row in `public.graph_threads` with `status IN ('approved', 'rejected', 'revision_requested')` must have non-null values for all three terminal fields: `approved_by`, `approved_at`, and `decision_payload`.

2. **`tests/conftest.py:270–274` (Violating Fixture Setup)**:
   ```python
   cur.execute(
       """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
          VALUES (%s, '{"status":"approved"}', 'approved', '330323', %s, %s)""",
       (tid, pkg_v, req_id),
   )
   ```
   The insert supplies `status = 'approved'`, but omits `approved_by`, `approved_at`, and `decision_payload`, causing them to default to `NULL`.

3. **`challenger_m3_2/challenge_report.md:15` (Verbatim Challenger Report)**:
   > "1. Test Fixture Constraint Violation (tests/conftest.py:270): Fixture sample_approved_dispatch_prereqs inserts rows into public.graph_threads with status = 'approved' without setting approved_by, approved_at, or decision_payload. This directly violates the Milestone 1 check constraint graph_threads_terminal_fields_chk, crashing 26 tests in test_dispatch_schema.py and 18 tests in test_dispatch_transition.py during fixture setup."

4. **`migrations/0003_dispatch_foundation.sql:400–405` (Deferred Constraint Trigger)**:
   ```sql
   CREATE CONSTRAINT TRIGGER fc27_graph_threads_pair_check
   AFTER INSERT OR UPDATE ON public.graph_threads
   DEFERRABLE INITIALLY DEFERRED
   FOR EACH ROW EXECUTE FUNCTION public.fc27_check_approval_pair();
   ```
   Under `autocommit=True` (used in `tests/conftest.py:268`), deferred triggers evaluate at statement completion. Statement 1 (`graph_threads`) finishes and commits before Statement 2 (`approval_requests`), triggering `fc27_check_approval_pair` line 354:
   `RAISE EXCEPTION 'approval pair: linked approval_request % not found for thread %'`.

5. **`migrations/0002_approval_security.sql:96` (Unique Index on approval_audit)**:
   ```sql
   CREATE UNIQUE INDEX IF NOT EXISTS approval_audit_one_decision_per_thread ON approval_audit(thread_id);
   ```
   - In `tests/conftest.py:290–298`, a second insert into `public.approval_audit` is executed for `audit_id_2` passing the same `tid`.
   - In `tests/conftest.py:330–345` (`make_dispatch`), an insert into `public.approval_audit` is executed passing `prereqs["thread_id"]`.
   Reusing `thread_id` violates `approval_audit_one_decision_per_thread` and raises `psycopg.errors.UniqueViolation`.

6. **All other fixtures in `tests/conftest.py`**:
   - `sample_player`: Inserts into `players` with `player_id='10001'`, `club_id='330323'`, `active=true`, defaulting to `status='active'`. Fully compliant with `players` schema.
   - Session/Module fixtures (`pg_admin_uri`, `client`, `wired`, `pg_roles`, `pg_db`, `pg_db_migrated`, `pg_uri` teardown consistency check, etc.): Zero schema/table mutation defects.

---

## 2. Logic Chain

1. **Step 1 (Root Cause of Gate Failure)**:
   Observation 1 establishes that `graph_threads_terminal_fields_chk` mandates `approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL` whenever `status = 'approved'`.
   Observation 2 demonstrates that `tests/conftest.py:270` inserts `status='approved'` without supplying these three columns.
   Therefore, PostgreSQL throws `psycopg.errors.CheckViolation`, aborting the fixture execution and causing the 44 test errors recorded in Observation 3.

2. **Step 2 (Identification of Secondary Trigger Failure under Autocommit)**:
   Observation 4 shows that `fc27_graph_threads_pair_check` is a deferred constraint trigger. In `tests/conftest.py:268`, `autocommit=True` causes the first `INSERT INTO public.graph_threads` to commit immediately. Because the paired `approval_requests` row has not yet been inserted, the trigger fails with `linked approval_request not found`.
   Therefore, removing `autocommit=True` and grouping inserts inside an explicit transaction block (`with conn.transaction():`) is necessary so deferred triggers fire only at commit when both records exist.

3. **Step 3 (Identification of Unique Index Collisions)**:
   Observation 5 establishes that `approval_audit_one_decision_per_thread` permits only one decision per `thread_id`.
   - In `sample_approved_dispatch_prereqs`, `audit_id_2` reuses `tid`. Once check constraint #1 is resolved, this would immediately raise `UniqueViolation`.
   - In `make_dispatch`, `_make` repeatedly inserts into `approval_audit` with `prereqs["thread_id"]`. Calling `make_dispatch` multiple times in a single test (as in `test_allowed_phase1_transitions`) would raise `UniqueViolation`.
   Therefore, `sample_approved_dispatch_prereqs` must maintain a dedicated secondary thread `tid_2` for `audit_id_2`, and `make_dispatch` must generate a distinct thread `d_tid` and paired approval record per dispatch created.

4. **Step 4 (Completeness of Audit)**:
   Observation 6 confirms that `sample_player` and all other fixtures in `tests/conftest.py` are free of defects.

5. **Step 5 (Synthesis into Actionable Drop-In Replacement)**:
   Combining Steps 1–3 produces a drop-in replacement for lines 260–382 of `tests/conftest.py` that satisfies all database constraints, triggers, and test suite contracts.

---

## 3. Caveats

- **Scope boundary**: This investigation is strictly read-only per agent constraints. No source files (`tests/conftest.py`, `src/`, `migrations/`) were modified by this agent.
- **Other Iteration 2 Issues**: Challenger 2's finding regarding `Objective` in `src/schemas.py` (`hide_input_in_errors=True`) is assigned to peer agents and is not included in this fixture fix.

---

## 4. Conclusion

The exact fix for the 44 fixture-related test errors across `test_dispatch_schema.py` and `test_dispatch_transition.py` requires replacing lines 260–382 in `tests/conftest.py`. The replacement:
1. Supplies `approved_by = 'batmanL8'`, `approved_at = now()`, and `decision_payload = '{"decision":"approved"}'::jsonb` for all approved `graph_threads` inserts.
2. Uses explicit transaction blocks (`with conn.transaction():`) instead of `autocommit=True` so paired deferred constraint triggers evaluate at commit time.
3. Provides a dedicated secondary paired thread (`tid_2`, `req_id_2`) for `audit_id_2` in `sample_approved_dispatch_prereqs`.
4. Equips `make_dispatch` to generate unique thread and paired approval records per dispatch, allowing multiple dispatches to be generated within a single test without unique constraint violations.

The detailed analysis and verbatim replacement code are documented in `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_it2_fixtures_1\fixtures_fix_analysis.md`.

---

## 5. Verification Method

1. **Worker Code Replacement**:
   Inspect `tests/conftest.py:260–382` after worker applies the replacement code from `fixtures_fix_analysis.md`.
2. **Execute Full M3 Test Suite**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -v tests/test_dispatch_schema.py tests/test_dispatch_transition.py
   ```
   **Expected Outcome**: 0 errors, all 40 dispatch tests pass (assuming `src/schemas.py` candidate input echo fix is also applied).
3. **Invalidation Conditions**:
   - Any `CheckViolation` on `graph_threads_terminal_fields_chk`.
   - Any `UniqueViolation` on `approval_audit_one_decision_per_thread`.
   - Any `CheckViolation` on `fc27_check_approval_pair`.
   - Any inconsistency reported by `SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'`.
