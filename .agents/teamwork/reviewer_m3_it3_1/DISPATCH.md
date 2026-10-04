# Dispatch: reviewer_m3_it3_1

## Identity
- Role: Milestone 3 Iteration 3 Reviewer 1 (Fixtures Lifecycle Sequencing & Conftest Review)
- Working Directory: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it3_1`
- Parent: Orchestrator Phase 1 (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)

## Mandatory Inputs to Read First
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Worker 3 Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_3\handoff.md`
4. Worker 4 Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_4\handoff.md`
5. `tests/conftest.py`
6. `tests/test_dispatch_schema.py`

## Review Task
Review Milestone 3 Iteration 3 changes in `tests/conftest.py` and `tests/test_dispatch_schema.py`:
1. Verify `tests/conftest.py`:
   - Inspect `_seed_approved_approval_prereqs`: verify the full 6-step lifecycle sequence (`running` -> `awaiting_approval` -> `SET CONSTRAINTS ALL IMMEDIATE; SET CONSTRAINTS ALL DEFERRED;` -> `approved` -> `approval_audit`).
   - Verify `sample_approved_dispatch_prereqs` and `make_dispatch` cleanly resolve mutual foreign keys without `ForeignKeyViolation` or trigger deadlock.
   - Verify custom `FCError` SQLSTATE registration in `psycopg.errors._sqlcodes` (`FC001`–`FC007`).
2. Verify `tests/test_dispatch_schema.py`:
   - Inspect line 330: verify `col = "ingested_at" if table == "player_observations" else "created_at"` correctly targets `ingested_at` for `player_observations`.
3. Execution Verification:
   Run tests:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py tests/test_db_migrate_roles.py
   ```
4. Explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
5. Write `review.md` and `handoff.md` in your working directory.
6. Notify orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.
