# Handoff Report: Reviewer 1 (Milestone 1 — Database Schema, Migrations, Triggers & Role Separation)

## 1. Observation
- **Inspected Files and Key Implementations**:
  - `migrations/0003_dispatch_foundation.sql`:
    - Lines 126–145: Table `public.approval_requests` with primary key `approval_request_id uuid DEFAULT gen_random_uuid()`, unique foreign key `thread_id REFERENCES public.graph_threads(thread_id)`, hex package version check `CHECK (package_version ~ '^[0-9a-f]{64}$')`, status check covering `'awaiting_approval'`, `'approved'`, `'rejected'`, `'revision_requested'`, `'expired'`, and timestamp coherence constraints (`CHECK (expires_at > created_at)`, `CHECK ((status = 'expired') = (expired_at IS NOT NULL))`, `CHECK ((status IN ('approved', 'rejected', 'revision_requested')) = (decided_at IS NOT NULL))`).
    - Lines 150–202: Trigger `fc27_approval_requests_guard` executing `fc27_guard_approval_request_transition()` to freeze terminal rows against UPDATE and DELETE, enforce provenance immutability, and guard legal state transitions.
    - Lines 205–232: Table `public.graph_threads` altered with `approval_request_id uuid UNIQUE REFERENCES public.approval_requests`, constraint `graph_threads_status_chk` including `'revision_requested'` and `'expired'`, and `graph_threads_terminal_fields_chk` enforcing terminal field population.
    - Lines 233–270: Legacy backfill iterating over legacy `awaiting_approval` threads with non-null `package_version` and null `approval_request_id`, generating paired `approval_requests` rows and linking `graph_threads`.
    - Lines 273–331: Trigger `graph_threads_guard` executing `fc27_guard_thread_transition()` to enforce terminal freezing, package version immutability, single-set link immutability, and state machine validity.
    - Lines 334–410: Function `public.fc27_check_approval_pair()` and twin constraint triggers `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check`, both declared `DEFERRABLE INITIALLY DEFERRED`.
    - Lines 413–462: Diagnostic view `public.fc27_approval_inconsistencies` with 5 disjoint partitions (`status_mismatch`, `package_mismatch`, `unlinked_request`, `missing_link`, `legacy_unlinked`).
    - Lines 465–626: Seven Section 1A compatibility tables (`authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`) with immutability triggers on decision and escalation records.
    - Lines 628–673: Extensions to `approval_audit` for decision type `REQUEST_REVISION` and Section 1A compatibility fields.
    - Lines 675–720: Extensions to `approval_nonces` supporting subjects `thread`, `dispatch_plan`, `dispatch_cancel`, `role_grant`, `activation_gate`.
    - Lines 723–809: Updated `fc27_check_event_source()` validating event types `approval.decided`, `approval.created`, `approval.expired`, `dispatch.created`, `dispatch.acknowledged`, `tracking.updated`, and `tracking.completed`.
  - `migrations/post/grants.sql` and `src/db_migrate.py`:
    - `grants.sql` line 1: `-- grants-policy-version: 3`.
    - `grants.sql` lines 4–80: Grants `SELECT, INSERT, UPDATE` to `fc27_app` for mutable operational tables, `SELECT, INSERT` for append-only audit/events, `INSERT` only for outbox, and `SELECT` only for schema migrations and inconsistency views. Zero permissions granted to `fc27_dispatch` on approval/operational tables.
    - `src/db_migrate.py` line 21: `GRANTS_POLICY = GrantsPolicy(version=3, sha256="9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1")`.
  - `src/approval_store.py`:
    - Constructor `ApprovalStore(conninfo, request_ttl=timedelta(hours=72))`.
    - `complete_run()` pairing `approval_requests` and `graph_threads` with `approval.created` event emission.
    - `issue_challenge()` with proactive expiry and request share lock.
    - `apply_decision()` with strict lock ordering (`approval_nonces` -> `approval_requests` -> `graph_threads`), safe idempotent replay returning committed decision, `approval_audit` insertion with Section 1A fields, and `approval.decided` event emission.
    - `expire_due()` proactive expiry daemon with nonce revocation and `approval.expired` event emission.
    - Section 1A validation failing closed for non-commissioner actors and non-primary authority bases.
  - `src/decision.py`:
    - Removed `new_state["dispatch"]` generation.
    - Added `REQUEST_REVISION` support to `DecisionKind` and `apply_decision_to_state()`.
  - Verification test suite:
    - 6 test suites containing 87 test cases:
      - `tests/test_db_roles.py`: 8 tests
      - `tests/test_db_migrate_roles.py`: 16 tests
      - `tests/test_events_outbox.py`: 7 tests
      - `tests/test_migration_0003.py`: 7 tests
      - `tests/test_approval_lifecycle.py`: 19 tests
      - `tests/test_approval_security.py`: 30 tests
    - Teardown fixture in `tests/conftest.py` lines 163–177 verifying `SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'` equals 0 after every test.
    - Test execution output: `87 passed in 210.53s`.

## 2. Logic Chain
1. By inspecting `migrations/0003_dispatch_foundation.sql`, we verified that `approval_requests` defines complete schema constraints, checks, and foreign keys matching Specification §14.2 and AC-56.
2. By tracing `fc27_check_approval_pair()` across `graph_threads` and `approval_requests`, we verified that declaring twin triggers as `DEFERRABLE INITIALLY DEFERRED` enables atomic multi-step pairing inside transactions while guaranteeing 1:1 pair consistency at `COMMIT` time, preventing split-brain states (AC-55).
3. By analyzing `migrations/post/grants.sql` and `src/db_migrate.py`, we confirmed that Grants Policy v3 provides exact least-privilege DML permissions to `fc27_app`, grants zero permissions on approval tables to `fc27_dispatch`, and enforces cryptographic SHA-256 matching (`9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1`).
4. By inspecting `src/approval_store.py`, we verified that hierarchical lock ordering (`approval_nonces` -> `approval_requests` -> `graph_threads`) prevents deadlocks during concurrent expiry and decision races.
5. By analyzing `src/decision.py`, we confirmed that `new_state["dispatch"]` was removed, guaranteeing that no dispatch payloads are constructed while `DISPATCH_ENABLED=false`.
6. By examining Section 1A compatibility tables and store enforcement, we verified that all 7 tables exist in schema with immutability triggers, and runtime enforcement fails closed on non-commissioner actors or non-primary authority bases.
7. By reviewing the entire codebase for integrity violations (hardcoded outputs, dummy implementations, task bypasses), we verified that the implementation is 100% genuine and robust.
8. Therefore, Milestone 1 meets all acceptance criteria, and the review verdict is APPROVE.

## 3. Caveats
- No caveats. The review was exhaustive across all database schemas, migration files, grants policies, stores, decision functions, and test suites.

## 4. Conclusion
Milestone 1 is APPROVED. The database schema, migration scripts, deferred constraint triggers, immutability guards, diagnostic inconsistencies view, role isolation policies, approval store lifecycle methods, and Section 1A compatibility guardrails are correctly implemented, verified, and ready for Milestone 2.

## 5. Verification Method
To independently verify the test suite:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py
```
Expected output: 87 passed, 0 failures, exit code 0.
Invalidation conditions:
- Any failure or error among the 87 tests.
- SHA-256 mismatch between `migrations/post/grants.sql` and `GRANTS_POLICY` in `src/db_migrate.py`.
- Non-zero count of non-legacy inconsistencies in `public.fc27_approval_inconsistencies`.
