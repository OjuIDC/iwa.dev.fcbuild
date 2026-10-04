# Handoff Report: Milestone 3 Test Architecture

**Agent:** `explorer_m3_tests_1`  
**Role:** M3 Test Architecture Explorer  
**Date:** 2026-10-03  
**Working Directory:** `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_tests_1`  
**Handoff Type:** Hard (Exploration task complete)  

---

## 1. Observation

1. **Mandatory Documents Inspected**:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`: Lines 35–65 define the non-negotiable Phase 1 invariants: `DISPATCH_ENABLED=false` at all times, no player delivery, and approved/rejected as terminal outcomes.
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`: Lines 83–86, 101, and 131–137 define Milestone 3 features #30–#33: `players` and `dispatches` schema with immutable provenance triggers, tracking schema (`dispatch_plans`, `player_observations`, `dispatch_assessments`), `public.fc27_dispatch_transition` `SECURITY DEFINER` function with SQLSTATEs `FC001`–`FC007`, and `dispatch_transition_rules` matrix.
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md`:
     - Task 6 (lines 657–748): Migration 0003 Part C (`players`, `dispatches`, partial unique index on objective metric, insert guard requiring `pending` and `version=1`, immutable triggers, forward-compatible tracking tables, and grants). Defines 18 required test assertions in `tests/test_dispatch_schema.py`.
     - Task 7 (lines 750–917): Migration 0003 Part D (`public.fc27_dispatch_transition` `SECURITY DEFINER`, `dispatch_transition_rules` matrix, SQLSTATE error taxonomy `FC001`–`FC007`, C-10 event source verification, and `src/dispatch/transition.py` wrapper). Defines 22 required test assertions in `tests/test_dispatch_transition.py`.
   - `migrations/0003_dispatch_foundation.sql`: Lines 1–810 contain Section A (events/outbox) and Section B (approval requests, consistency triggers, and Section 1A compatibility tables). Sections C (dispatch schema) and D (transition function) are planned for implementation in Milestone 3.
   - `migrations/post/grants.sql`: Lines 1–82 specify versioned grants (`-- grants-policy-version: 3`). `fc27_app` holds DML on approval tables, but currently lacks grants on dispatches. `fc27_dispatch` holds SELECT on `domain_events`.
   - `src/db_migrate.py`: Lines 16–22 define `GRANTS_POLICY = GrantsPolicy(version=3, sha256="9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1")`. Any change to `grants.sql` requires bumping this policy version and hash.
   - `tests/conftest.py`: Lines 179–181 provide the `pg_dispatch_uri` fixture connected as `fc27_dispatch`. `pg_uri` connects as `fc27_app`. `pg_admin_db_uri` connects as superuser.
   - `tests/test_events_outbox.py`: Lines 140–154 test trigger `fc27_check_event_source()` (C-10), demonstrating that direct inserts of transition-only event types (`dispatch.delivered`) by `fc27_app` raise `check_violation` ("transition-only event type not allowed outside transition function").

2. **Peer Explorer Coordination**:
   - `explorer_m3_schema_1/DISPATCH.md`: Assigned to design DDL and triggers for `players`, `dispatches`, tracking schema, and least-privilege grants (`fc27_app` has SELECT, INSERT; no UPDATE/DELETE on `dispatches`).
   - `explorer_m3_transition_1/DISPATCH.md`: Assigned to design `public.dispatch_transition_rules`, `public.fc27_dispatch_transition`, and Python wrapper `src/dispatch/transition.py`.

---

## 2. Logic Chain

1. **Premise 1**: From `PROJECT.md` (§Milestone 3) and `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Tasks 6 & 7), Milestone 3 introduces two new test modules: `tests/test_dispatch_schema.py` and `tests/test_dispatch_transition.py`.
2. **Premise 2**: Direct updates to `public.dispatches` by runtime roles must be prevented at two distinct levels:
   - Database grant level: `migrations/post/grants.sql` grants `fc27_app` only `SELECT, INSERT` on `dispatches` (and `fc27_dispatch` only `SELECT`). Direct `UPDATE` or `DELETE` statements executed by `fc27_app` must be rejected with `psycopg.errors.InsufficientPrivilege` (Observation 1, Task 6).
   - Trigger level: Trigger `fc27_dispatch_immutable` prevents mutation of provenance columns (`dispatch_id`, `source_thread_id`, `approval_id`, `approval_request_id`, `player_id`, etc.) and freezes terminal rows, so that even privileged/superuser mutations raise `psycopg.errors.CheckViolation` (Observation 1, Task 6).
3. **Premise 3**: State changes must occur exclusively via `public.fc27_dispatch_transition` (`SECURITY DEFINER`, search_path `pg_catalog, pg_temp`). The function derives caller identity (`session_user` and `fc27.actor`), validates optimistic locking against `expected_status` and `expected_version`, looks up `dispatch_transition_rules`, validates patch allowlists, updates row version, and inserts atomic `domain_events` and `outbox` records (Observation 1, Task 7).
4. **Premise 4**: In Phase 1, `DISPATCH_ENABLED=false` is a non-negotiable invariant (Observation 1, `ORIGINAL_REQUEST.md`). Therefore, transition to `delivered` must fail closed.
5. **Premise 5**: To test these interactions without state leakage, tests require test fixtures in `tests/conftest.py` that seed active players (`sample_player`), set up valid parent approval foreign keys (`sample_approved_dispatch_prereqs`), and construct test dispatches in designated initial states (`make_dispatch`).
6. **Conclusion**: The test architecture detailed in `tests_analysis.md` provides complete, non-overlapping coverage of all schema constraints, immutability triggers, transition rules, role boundaries, and safety invariants.

---

## 3. Caveats

1. **Phase 2 Identity & User Actors**: User actors (`user:<uuid>`) and player acknowledgments (`delivered -> acknowledged`) require Phase 2 user tables (`public.users`) and passkeys. In Milestone 3 (Phase 1), transition attempts with `user:` actors must be rejected with SQLSTATE `FC004` ("user actors require phase 2").
2. **Dynamic SQL Absence**: Static analysis in `test_function_source_has_no_dynamic_sql` inspects the text of `migrations/0003_dispatch_foundation.sql`. The test assumes standard formatting without obfuscated dynamic execution calls.
3. **Reconciliation of Transition Matrices**: The orchestrator dispatch prompt used illustrative lifecycle statuses (`running`, `paused`), whereas the approved specification (§11.3 & §14.3) defines the authoritative lifecycle domain (`pending`, `queued`, `delivered`, `requires_reapproval`, `failed`, `acknowledged`, `tracking`, `completed`, `expired`, `cancelled`, `superseded`). `tests_analysis.md` addresses both models and provides exact test assertions for the authoritative specification domain.

---

## 4. Conclusion

1. **Architecture Complete**: Detailed technical test architecture has been authored in `tests_analysis.md`, covering 18 schema tests, 22 transition tests, and 3 conftest fixtures.
2. **Strict Read-Only Adherence**: No production source code in `src/` or `migrations/` has been modified.
3. **Downstream Ready**: The implementation agents (`worker_m3_*` or test writers) have exact specifications, SQLSTATE assertions, and fixture definitions to execute test-driven implementation for Milestone 3.

---

## 5. Verification Method

1. **Verify Analysis Artifacts**:
   - Inspect `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_tests_1\tests_analysis.md`.
   - Inspect `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_tests_1\handoff.md`.
2. **Verify Repository Cleanliness**:
   - Run `git status` to ensure zero modifications in `src/`, `migrations/`, or other production directories.
3. **Test Execution Command for Implementers**:
   Once Milestone 3 schema and function implementations land, run:
   ```bash
   python -m pytest -o addopts="" -q -rs tests/test_dispatch_schema.py tests/test_dispatch_transition.py
   ```
