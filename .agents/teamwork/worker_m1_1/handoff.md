# Handoff Report: Milestone 1 Implementation (Authoritative Approval Lifecycle, Expiry & Core Data Model)

## 1. Observation
- **Exclusively Owned Files Modified and Created**:
  - `migrations/0003_dispatch_foundation.sql`: Appended Section B defining `public.approval_requests`, trigger `fc27_approval_requests_guard`, `graph_threads` updates & constraint `graph_threads_status_chk`, `graph_threads_terminal_fields_chk`, trigger `graph_threads_guard`, deferred constraint triggers `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check` calling `fc27_check_approval_pair()`, diagnostic view `public.fc27_approval_inconsistencies`, `approval_audit` extensions, `approval_nonces` extensions, Section 1A compatibility tables (`authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`), updated `fc27_check_event_source()` trigger function for `approval_requests`, and legacy data backfill.
  - `migrations/post/grants.sql`: Upgraded to `-- grants-policy-version: 3` adding `SELECT, INSERT, UPDATE` grants for `fc27_app` on all newly introduced relations, maintaining zero access for `fc27_dispatch`.
  - `src/db_migrate.py`: Set `GRANTS_POLICY = GrantsPolicy(version=3, sha256="9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1")`.
  - `src/approval_store.py`: Implemented constructor `ApprovalStore(conninfo, request_ttl=timedelta(hours=72))`, `complete_run()` with atomic paired creation and `approval.created` event emission via `_safe_record_event`, `issue_challenge()` with proactive expiry and request share lock, `apply_decision()` with strict lock ordering (`approval_nonces` -> `approval_requests` -> `graph_threads`), safe idempotent replay returning committed state without 409, `approval_audit` insertion with correlation/request tracking and Section 1A compatibility fields, `approval.decided` event emission, `expire_due()` proactive expiry daemon with revocation and `approval.expired` event emission, `pending_approvals()` query, and Section 1A authority validation (rejecting non-primary/player decisions in Phase 1).
  - `src/decision.py`: Removed legacy `new_state["dispatch"]` dictionary creation from `apply_decision_to_state()`, added `REQUEST_REVISION` support to `DecisionKind` and `DecisionIntent`.
  - `tests/helpers.py`: Implemented `legacy_install()`, `start()`, `decide()`, and `db_status()` test helpers.
  - `tests/conftest.py`: Registered `allow_inconsistency` marker in `pytest_configure` and added teardown check asserting zero non-legacy inconsistencies in `fc27_approval_inconsistencies` in `pg_uri`.
  - `tests/test_approval_security.py`: Rewrote lines 203–210 to test `package_version` immutability trigger and stale payload rejection, updated `test_nonce_is_single_use` to verify single-use rejection on conflicting decision payloads.
  - `tests/test_migration_0003.py`: Created comprehensive test suite verifying forward migration AC-56 on legacy 0002 data with backfill, AC-55 deferred constraint triggers for pair mismatch and missing links, package version immutability while awaiting, immutable request fields, terminal row freezing, and `fc27_approval_inconsistencies` classification.
  - `tests/test_approval_lifecycle.py`: Created complete lifecycle test suite verifying `complete_run` pairing, atomic decision updates, absence of dispatch dictionary, `REQUEST_REVISION` terminal lifecycle, TTL expiry blocking prior nonces and new challenges, idempotent `expire_due`, 8-thread single-outcome race resolution, inbox query excluding terminal threads, transaction rollback on injected audit/event/outbox failures, safe idempotent replay, and Section 1A authority separation invariants.

- **Test Execution**:
  - Test command:
    `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py`
  - Verbatim output:
    `87 passed in 210.53s (0:03:30)`
    Exit code: `0`.

## 2. Logic Chain
1. In `migrations/0003_dispatch_foundation.sql`, defining `approval_requests` with a foreign key back to `graph_threads` and deferred twin constraint triggers `fc27_check_approval_pair()` guarantees bidirectional 1:1 pair consistency at transaction commit time.
2. In `migrations/post/grants.sql` and `src/db_migrate.py`, updating grants policy version to 3 and matching the SHA-256 digest (`9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1`) satisfies `test_grants_policy_constant_matches_file` and ensures schema policy governance is cryptographically tracked.
3. In `src/approval_store.py`, wrapping `record_event` with `_safe_record_event` to set `Connection.row_factory = tuple_row` during execution ensures that tuple indexing `row[0]` in `src/events.py` succeeds without `KeyError: 0`.
4. In `src/approval_store.py:expire_due()`, dynamically constructing the query without `(%s IS NULL OR ...)` resolves PostgreSQL `IndeterminateDatatype` exceptions on parameterized NULL comparisons.
5. In `src/approval_store.py:issue_challenge()`, checking `graph_threads` status when `approval_requests` row is absent ensures non-awaiting threads (such as `failed`) return 409 conflict rather than 404 not found.
6. In `src/approval_store.py:apply_decision()`, identifying identical prior audits for consumed nonces allows safe idempotent replay returning the committed decision, while conflicting payloads return 409 conflict prefixed with `replay:`.
7. In `src/decision.py`, omitting `new_state["dispatch"]` guarantees the Phase 1 invariant that no dispatch payloads are constructed while dispatch remains disabled.

## 3. Caveats
- No player delivery or dispatch execution was enabled or tested (`DISPATCH_ENABLED=false` was strictly preserved).
- Section 1A compatibility tables (`authority_policies`, `authority_delegations`, etc.) are established in schema with Phase 1 fail-closed enforcement; active multi-player consent flows are scheduled for later milestones.

## 4. Conclusion
Milestone 1 is fully implemented, verified, and passing all 87 tests without skips or workarounds. All constraints, data models, migration policies, atomicity guarantees, expiry mechanisms, and Section 1A guardrails are genuinely enforced.

## 5. Verification Method
Run the pre-approved test command against PostgreSQL:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py
```
Expected output: 87 passed, 0 failures, exit code 0.
