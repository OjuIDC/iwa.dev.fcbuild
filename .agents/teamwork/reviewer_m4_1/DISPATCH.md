# Dispatch: Milestone 4 Reviewer 1 (Schema, Grants & Migrations)

**Agent**: `reviewer_m4_1`
**Role**: Milestone 4 Reviewer 1 (Schema, Grants & Migrations)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1`
**Timestamp**: 2026-10-03T20:36:00Z

---

## 1. Objective
Objectively review and challenge Milestone 4 database schema, triggers, roles, and migration alignment.

## 2. Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`
4. `migrations/0003_dispatch_foundation.sql` (Section E: lines 1780–1955)
5. `migrations/post/grants.sql` (Policy version 5)
6. `src/db_migrate.py`

## 3. Scope of Review
1. **Section E Schema & Immutability**:
   - Verify `public.activation_gate_log` columns, constraints (`gate_id IN ('G-ID','G-ATOMIC','G-OUTBOX')`, `status IN ('passed','failed')`, `fc_version = '27'`, `platform = 'common-gen5'`).
   - Verify trigger `fc27_activation_gate_log_immutable` invokes `fc27_reject_mutation()` for UPDATE and DELETE.
   - Verify `public.activation_gates` view uses `DISTINCT ON (gate_id) ORDER BY gate_id, recorded_at DESC, id DESC`.
   - Verify `public.service_heartbeats` table definition and primary key.
   - Verify `fc27_check_event_source()` validates `activation.gate_recorded` domain events against `activation_gate_log`.
2. **Grants Policy Version 5 & Least Privilege**:
   - Verify `migrations/post/grants.sql` header is `-- grants-policy-version: 5`.
   - Verify `fc27_app` has `SELECT, INSERT` on `activation_gate_log`, `USAGE` on sequence, `SELECT` on `activation_gates`, `SELECT` on `service_heartbeats`.
   - Verify `fc27_dispatch` has `SELECT, INSERT, UPDATE` on `service_heartbeats` and NO access to `activation_gate_log` or `activation_gates`.
   - Verify `src/db_migrate.py` GRANTS_POLICY matches policy version 5 and sha256.
3. **Execution Verification**:
   Run tests:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_migrate_roles.py tests/test_dispatch_schema.py tests/test_migration_0003.py
   ```
4. Formulate explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
5. Write `review.md` and `handoff.md` in your working directory.
6. Notify orchestrator via `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.
