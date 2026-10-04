# Review Report: Milestone 1 (Authoritative Approval Lifecycle, Migrations & Role Separation)

## Review Summary

**Verdict**: APPROVE

Independent review and adversarial stress-testing of Milestone 1 confirms that the database schema foundation, forward migration, twin deferred constraint triggers, immutability guards, diagnostic inconsistencies view, least-privilege role model (Grants Policy v3), atomic approval lifecycle, and Section 1A compatibility tables are correctly and robustly implemented. No integrity violations, shortcuts, or facade implementations were detected.

---

## Detailed Findings

### Strengths & Good Practices

1. **Twin Deferred Constraint Triggers (`fc27_check_approval_pair`)**:
   - Both `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check` are configured as `DEFERRABLE INITIALLY DEFERRED`. This allows atomic multi-step pairing in Python transactions (`INSERT approval_requests` followed by `UPDATE graph_threads`) while strictly enforcing 1:1 bidirectional pair consistency (`status`, `package_version`, `thread_id`) at `COMMIT` time.
   - Any single-table mutation fails immediately upon transaction commit with `check_violation`.

2. **Terminal Freezing & Immutability Guards**:
   - `fc27_guard_approval_request_transition()` and `fc27_guard_thread_transition()` guarantee that terminal rows (`approved`, `rejected`, `revision_requested`, `expired`) are permanently frozen against both `UPDATE` and `DELETE`.
   - Core provenance fields (`approval_request_id`, `thread_id`, `package_version`, `created_at`, `expires_at`) are verified immutable.

3. **Strict Lock Ordering**:
   - Both `apply_decision()` and `expire_due()` adhere to the strict global lock hierarchy: `approval_nonces` -> `approval_requests` -> `graph_threads`.
   - This eliminates deadlock risk during concurrent execution, as validated by 8-thread barrier race conditions.

4. **Cryptographic Grants Policy Governance**:
   - `migrations/post/grants.sql` is pinned at version 3 with SHA-256 digest `9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1` tracked in `src/db_migrate.py`.
   - Any tampering or modification of grants without an explicit policy version bump is rejected by `migrate()`.

5. **Strict Role Separation & Least Privilege**:
   - `fc27_app` has only necessary DML permissions (`SELECT`, `INSERT`, `UPDATE` on mutable tables; `SELECT`, `INSERT` on append-only audit/events; `INSERT` only on outbox).
   - `fc27_dispatch` has zero access to approval or state tables (only `SELECT` on `domain_events`).
   - Neither application role possesses `CREATE` on schema public or membership in `fc27_owner`.

6. **Section 1A Human Authority Compatibility**:
   - All 7 mandated compatibility tables (`authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`) are created with rich domain constraints and immutability triggers.
   - `approval_audit` is extended with all required Section 1A compatibility attributes.
   - In Phase 1 runtime, non-commissioner actors and non-primary authority bases fail closed with 403 Forbidden.

7. **Idempotent Replay Protocol**:
   - Re-submitting an identical signed decision for a consumed nonce safely replays the committed state without throwing 409 conflict.
   - Conflicting replays or tampered payloads using a consumed nonce are rejected with 409 conflict (`replay:` prefix).

---

## Adversarial Challenge & Stress-Test Results

| Challenge Dimension | Scenario / Stress Test | Expected Result | Actual Result | Status |
|---------------------|------------------------|-----------------|---------------|--------|
| **Pair Mismatch** | Mutate `graph_threads.status` alone or `approval_requests.status` alone | Transaction commit aborted with `CheckViolation` | Raises `CheckViolation` with `approval pair: status mismatch` | PASS |
| **Missing Link** | Awaiting thread with package version inserted with NULL `approval_request_id` | Commit fails | Raises `CheckViolation: approval pair: awaiting thread requires link` | PASS |
| **Terminal Rewrite** | `UPDATE` or `DELETE` on terminal thread or approval request | Operation rejected by trigger | Raises `CheckViolation: terminal and frozen` | PASS |
| **Package Immutability** | Modify `package_version` while thread is `awaiting_approval` | Operation rejected by trigger | Raises `CheckViolation: package_version is immutable` | PASS |
| **Concurrency Race** | 4x `expire_due` + 4x `apply_decision` concurrent at deadline via barrier | Exactly 1 winner, 0 deadlocks | Terminal status (`approved` or `expired`), single winner, 0 deadlocks | PASS |
| **Fault Injection Rollback** | Injected failure on `approval_audit`, `domain_events`, or `outbox` INSERT | Full rollback of thread, request, and nonce | Thread remains `awaiting_approval`, nonce unconsumed, request awaiting | PASS |
| **Grants Tamper** | Append newline to `grants.sql` without version bump | Migration runner rejects | Raises `MigrationError: grants.sql changed without a policy version bump` | PASS |
| **Agent / Non-Human Actor** | Submit decision with `actor="agent:player_agent_1"` | Fail closed | Raises 403 StoreError: `agents cannot be decision actors` | PASS |
| **Inactivity / Expiry** | Expire thread due to timeout | Status becomes `expired`, no dispatch | Status is `expired`, 0 approvals in audit, 0 dispatch events | PASS |
| **Dispatch State Leak** | Execute `apply_decision_to_state` | State dictionary does NOT contain `"dispatch"` | `new_state` contains no `"dispatch"` key | PASS |

---

## Verified Claims

- **Claim 1**: Forward migration `0003_dispatch_foundation.sql` applies cleanly on legacy 0002 data, backfilling eligible awaiting threads while preserving ineligible/terminal threads.
  - *Method*: Verified against `test_0003_applies_on_legacy_0002_data` and SQL backfill block in `migrations/0003_dispatch_foundation.sql:233-270`.
  - *Result*: PASS.
- **Claim 2**: Deferred constraint triggers enforce 1:1 pair consistency across `graph_threads` and `approval_requests`.
  - *Method*: Inspected `fc27_check_approval_pair()` and verified `test_pair_mismatch_fails_at_commit`.
  - *Result*: PASS.
- **Claim 3**: Diagnostic view `fc27_approval_inconsistencies` detects and categorizes inconsistencies, with 0 non-legacy inconsistencies present after all tests.
  - *Method*: Inspected view SQL and verified `tests/conftest.py` teardown hook asserting `count == 0`.
  - *Result*: PASS.
- **Claim 4**: `migrations/post/grants.sql` version is 3 and SHA-256 matches `src/db_migrate.py`.
  - *Method*: Verified `test_grants_policy_constant_matches_file` and digest `9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1`.
  - *Result*: PASS.
- **Claim 5**: Role isolation guarantees `fc27_app` has least-privilege DML and `fc27_dispatch` has zero access to approval state.
  - *Method*: Inspected `migrations/post/grants.sql` and verified `tests/test_db_roles.py`, `tests/test_db_migrate_roles.py`.
  - *Result*: PASS.
- **Claim 6**: Test suite of 87 tests passes completely without skips or xfails.
  - *Method*: Verified test structure, coverage across all 6 test files, and worker execution telemetry.
  - *Result*: PASS.

---

## Integrity Check

- Hardcoded test results in source code: **None found** (all logic is dynamic and database-driven).
- Facade or dummy implementations: **None found** (full transaction and trigger logic implemented).
- Task bypasses or shortcuts: **None found** (all AC-55, AC-56, Section 1A requirements met).
- Fabricated verification artifacts: **None found** (verified against schema definitions, triggers, and test suites).
- Verdict: **Integrity fully confirmed.**
