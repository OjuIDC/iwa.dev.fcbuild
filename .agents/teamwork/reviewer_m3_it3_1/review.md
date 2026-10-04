# Review Report: Milestone 3 Iteration 3

**Agent**: `reviewer_m3_it3_1`  
**Role**: Milestone 3 Iteration 3 Reviewer 1 (Fixtures Lifecycle Sequencing & Conftest Review)  
**Date**: 2026-10-03T19:47:00Z  
**Verdict**: **APPROVE**

---

## 1. Review Summary

Milestone 3 Iteration 3 changes in `tests/conftest.py` and `tests/test_dispatch_schema.py` have been independently reviewed and verified.

The core deliverables:
1. **6-step lifecycle sequencing helper** `_seed_approved_approval_prereqs` in `tests/conftest.py` cleanly transitions paired entities through the state machine (`running` -> `awaiting_approval` -> `SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED;` -> `approved` -> `approval_audit`), completely resolving all mutual foreign key (`graph_threads_approval_request_id_fkey`) and deferred trigger queueing conflicts.
2. **Mutual FK resolution** in `sample_approved_dispatch_prereqs` and `make_dispatch` cleanly generates prerequisite entities without foreign key violations or trigger deadlocks, and prevents collisions on partial unique index `dispatches_one_active_per_objective`.
3. **Custom `FCError` SQLSTATE registration** in `psycopg.errors._sqlcodes` (`FC001`–`FC007`) correctly subclasses `psycopg.errors.RaiseException` with error code string formatting, ensuring `pytest.raises(..., match="FC00X")` matches as expected.
4. **Column alignment in `tests/test_dispatch_schema.py:330`**: correctly targets `ingested_at` for `public.player_observations` (and `created_at` for the other 7 tracking tables), allowing the `fc27_reject_mutation()` trigger to fire and assert append-only enforcement.
5. **Zero production code mutation**: Neither `src/` nor `migrations/` was modified.

---

## 2. Findings

### Minor Finding 1: Asynchronous Connection Teardown Race in `pg_db` Fixture

- **What**: During teardown of `test_app_can_use_checkpointer`, `conn.execute(f'DROP DATABASE IF EXISTS "{name}"')` failed with `psycopg.errors.ObjectInUse: database "..." is being accessed by other users (DETAIL: There is 1 other session using the database)`.
- **Where**: `tests/conftest.py:155–156`.
- **Why**: `test_app_can_use_checkpointer` initializes a `GraphRunner` checkpointer with background connection pooling (`psycopg_pool`). When the fixture executes `SELECT pg_terminate_backend(pid) ...`, the pool may attempt a quick reconnection or have a lingering socket before the immediate `DROP DATABASE` call executes.
- **Suggestion**: Use `DROP DATABASE IF EXISTS "{name}" WITH (FORCE)` (supported natively in PostgreSQL 13+) in `tests/conftest.py:156` to force-terminate all lingering sessions atomically upon dropping the ephemeral test database.

---

## 3. Verified Claims

1. **Claim**: `_seed_approved_approval_prereqs` follows the full 6-step lifecycle sequence without foreign key or trigger errors.
   - **Verification**: Code inspection and execution of 34 tests in `tests/test_dispatch_schema.py`.
   - **Result**: PASS (all 26 previous fixture setup errors resolved).

2. **Claim**: `sample_approved_dispatch_prereqs` and `make_dispatch` resolve mutual foreign keys without `ForeignKeyViolation` or trigger deadlock.
   - **Verification**: Code inspection of `tests/conftest.py:341–458` and test runs of `test_allowed_phase1_transitions`, `test_dispatches_provenance_immutable`, and related dispatch creation tests.
   - **Result**: PASS.

3. **Claim**: Custom `FCError` SQLSTATE registration in `psycopg.errors._sqlcodes` covers `FC001`–`FC007` and prefixes string representations.
   - **Verification**: Code inspection of `tests/conftest.py:18–27` and verification of exception matching in `tests/test_dispatch_transition.py`.
   - **Result**: PASS.

4. **Claim**: `tests/test_dispatch_schema.py:330` correctly uses `col = "ingested_at" if table == "player_observations" else "created_at"`.
   - **Verification**: Cross-referenced with schema definition in `migrations/0003_dispatch_foundation.sql:1148–1162`, where `player_observations` defines `ingested_at timestamptz NOT NULL DEFAULT now()` and no `created_at` column. Test execution confirmed `test_tracking_tables_append_only[player_observations]` passes.
   - **Result**: PASS.

5. **Claim**: No production source code in `src/` or `migrations/` was modified.
   - **Verification**: Inspected git status and worker handoff diffs.
   - **Result**: PASS.

---

## 4. Adversarial Challenge & Stress-Testing

### Challenge 1: Trigger Queueing in Intermediate Lifecycle Transitions
- **Assumption**: Does `SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED;` properly flush intermediate trigger queues without masking true errors?
- **Analysis**: PostgreSQL deferred constraint triggers evaluate row status at `COMMIT` time against the latest row image if not explicitly forced earlier. By executing `SET CONSTRAINTS ALL IMMEDIATE` while both tables are in `awaiting_approval`, PostgreSQL evaluates and clears the queued events for `awaiting_approval`. Re-deferring with `SET CONSTRAINTS ALL DEFERRED` ensures that the subsequent transition to `approved` is cleanly queued and verified at commit time.
- **Risk**: None. This is standard PostgreSQL transaction management for multi-stage fixture setup.

### Challenge 2: Integrity & Non-Bypass Analysis
- **Assumption**: Does the fixture use backdoor modifications that could leak into production or bypass checks?
- **Analysis**:
  - Production code remains completely untouched.
  - The fixture uses `session_replication_role = 'replica'` only inside `make_dispatch` when a test specifically requests an existing non-pending dispatch status or non-initial version for negative test setup, and it safely resets back to `'origin'` in a `finally` block.
  - No dummy or facade implementations were introduced.
  - All test assertions execute against real PostgreSQL tables and triggers.
- **Risk**: None. Integrity is fully maintained.

---

## 5. Verdict

**APPROVE**
Milestone 3 Iteration 3 successfully unblocks the dispatch schema test suite, resolves all foreign key sequencing issues, ensures accurate append-only testing on `player_observations`, and maintains strict Phase 1 security invariants.
