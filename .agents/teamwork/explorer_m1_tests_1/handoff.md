# Handoff Report: Milestone 1 Test Suite Exploration & Design

## 1. Observation

1. **Mandatory Documentation & Source Files**:
   - `ORIGINAL_REQUEST.md` (lines 961–1591, Section 1A patch): Requires strict separation of club authorization from player consent; explicit prohibition against timeout/inactivity creating approvals ("inactivity never constitutes approval", "Neither the Graph nor a scheduler may implement logic equivalent to: `if approval_request_timed_out: approved = True`"); mandates fail-closed handling in Phase 1 for unsupported actor types (`player`, `delegate`, `agent`, `coach`) and unsupported authority bases (`affected_player_fallback`, `explicit_delegate`, `affected_player_consent`).
   - `orchestrator_phase1/PROJECT.md` (lines 61–70, 99): Milestone 1 covers features 8–17: `approval_requests`, immutable transitions, pair consistency trigger, `approval_inconsistencies` view, store methods, human authority compatibility fields (Section 1A), grants update.
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 4, lines 395–545): Requires `tests/test_migration_0003.py` and `tests/test_approval_lifecycle.py`; requires `tests/helpers.py::legacy_install(admin_db_uri)`; requires `conftest.py` teardown check asserting `fc27_approval_inconsistencies` view is empty.
   - `docs/superpowers/specs/2026-10-02-player-dispatch-design.md`: Spec sections §7.2, §7.4, §7.6, §14.2, §18.6, §25 step 5, AM-4, AM-8, AM-14; Acceptance Criteria AC-1, AC-43, AC-44, AC-55, AC-56.

2. **Existing Implementation State**:
   - `migrations/0003_dispatch_foundation.sql` (lines 1–122): Contains only Section A (`domain_events`, `outbox`, append-only triggers, `fc27_check_event_source()`). Section B (approval requests, pair triggers, inconsistencies view) does not yet exist.
   - `src/approval_store.py` (lines 88–107, 126–235): `complete_run` currently updates `graph_threads` only, without creating `approval_requests` or emitting events; `apply_decision` does not lock in the specified order (nonces -> request -> thread), does not validate authority basis, and writes no correlation ID.
   - `src/decision.py` (lines 48–54): `apply_decision_to_state` currently writes `new_state["dispatch"] = {...}`, violating AC-44.
   - `tests/test_approval_security.py` (lines 203–210):
     ```python
     def test_stale_candidate_package_cannot_be_approved(client, pg_uri):
         tid = start(client)
         ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
         with psycopg.connect(pg_uri) as conn:
             conn.execute("UPDATE graph_threads SET package_version=%s WHERE thread_id=%s", ("f" * 64, tid))
         r = client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch))
         assert r.status_code == 409 and "stale" in r.json()["detail"]
     ```
     Under 0003 Part B, `fc27_guard_thread_transition` blocks updating `package_version` once status is `awaiting_approval` with `'package_version is immutable'`, which causes this test to fail with `psycopg.errors.CheckViolation` unless rewritten.
   - `tests/helpers.py`: Missing `legacy_install(admin_db_uri)`.
   - `tests/conftest.py`: Missing the teardown query checking `fc27_approval_inconsistencies`.

3. **Live Test Execution**:
   - Command: `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_events_outbox.py`
     Result: `7 passed in 6.38s` (Task 114)
   - Command: `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_approval_security.py`
     Result: `30 passed in 27.50s` (Task 119)

---

## 2. Logic Chain

1. **Step 1 — Baseline Integrity**:
   - From Observation 3, the test harness, virtual environment, and PostgreSQL 16 container (`fc27-test-postgres`) are verified operational, and existing Task 1–3 suites pass 100%.

2. **Step 2 — Migration 0003 Part B & Legacy Compatibility**:
   - From Observation 1 and 2, migration 0003 must apply cleanly over a database containing legacy 0002 data.
   - To test this deterministically, `legacy_install(admin_db_uri)` must install 0001, 0002, and LangGraph checkpoints under admin ownership.
   - When 0003 is applied, backfill must create `approval_requests` for eligible awaiting threads (those with `package_version IS NOT NULL`) and link them.
   - Ineligible awaiting threads (without `package_version`) and terminal threads (`approved`, `rejected`) must retain `approval_request_id = NULL` and remain byte-identical.
   - The view `fc27_approval_inconsistencies` must classify these unlinked legacy records specifically as `kind = 'legacy_unlinked'`, while asserting 0 mismatches for `status_mismatch`, `package_mismatch`, or `unlinked_request`.
   - The deferred constraint trigger `fc27_check_approval_pair()` evaluates at transaction commit on both tables; testing requires asserting that single-sided updates fail at commit with `approval pair:`.

3. **Step 3 — Approval Lifecycle & Expiry**:
   - From Observation 1, `ApprovalStore` methods must maintain strict invariants:
     - `complete_run` must insert `approval_requests` with `expires_at = now() + request_ttl`, link `graph_threads`, and record `approval.created` event + outbox row.
     - `apply_decision` must lock in strict order (`approval_nonces` -> `approval_requests` FOR UPDATE -> `graph_threads`), validate HMAC, consume nonce, atomically update both records, insert `approval_audit` with `proof_type='hmac'`, and emit `approval.decided` + outbox row.
     - Under AC-44 and Observation 2, `apply_decision_to_state` must be updated so it never populates a `"dispatch"` key in thread state.
     - `expire_due` must scan expired requests, atomically transition request and thread to `expired`, revoke nonces, and emit `approval.expired`. It must be idempotent.
     - The concurrency race between `expire_due` and `apply_decision` at deadline must yield a single winner (`approved` with 1 audit row OR `expired` with 0 audit rows) without deadlocks or timeouts.
     - Fault injection (injected failing trigger on `approval_audit`, `domain_events`, or `outbox`) must completely roll back all mutations, leaving thread and request `awaiting_approval` and nonce unconsumed.

4. **Step 4 — Section 1A Authority Separation**:
   - From Observation 1 (Section 1A), authority model invariants must be verified:
     - A timeout or inactivity event can never approve or accept.
     - In Phase 1 bootstrap, only actor type `commissioner` and authority basis `primary` can write an active decision. Attempts with `player`, `delegate`, `agent`, `coach` or `affected_player_fallback`, `explicit_delegate`, `affected_player_consent` must fail closed (403/422).
     - Schema must distinguish club authorization (`approval_requests` / `approval_audit`) from player consent (`player_assignments`).
     - Agents can never act as human decision makers.
     - Future player decision endpoints must return 404 in Phase 1.

5. **Step 5 — Test Infrastructure Health**:
   - From Observation 1 and 2, adding the `conftest.py` teardown check on `fc27_approval_inconsistencies` ensures no test leaks inconsistencies, with `@pytest.mark.allow_inconsistency` provided for tests that intentionally test view detection.
   - `tests/test_approval_security.py:203-210` must be rewritten to assert that direct update of `package_version` on an awaiting thread raises `CheckViolation` with `'package_version is immutable'`, preserving suite green status.

---

## 3. Caveats

1. **Implementation Delegation**: This exploration did not write or modify repository code files (`src/` or `tests/`). All test suites, schema definitions, and helpers are specified in design blueprints ready for implementer agents.
2. **PostgreSQL Container Dependency**: Test execution requires `TEST_POSTGRES_URI` pointing to a PostgreSQL 16 server with trust auth and superuser capability (active on port 55432 in current dev container).
3. **Migration Sequence**: The test `test_0003_applies_on_legacy_0002_data` relies on `src.db_migrate.migrate` applying `0003_dispatch_foundation.sql`. It will fail until Section B is appended to `0003_dispatch_foundation.sql`.

---

## 4. Conclusion

1. **Architecture Complete**: Detailed technical design, schema definitions, trigger logic, and test blueprints have been fully developed and documented in `.agents/teamwork/explorer_m1_tests_1/tests_analysis.md`.
2. **Two New Test Files Designed**:
   - `tests/test_migration_0003.py`: 7 test cases covering legacy 0002 migration, pair constraint triggers, mandatory links, package version immutability, request immutability, frozen terminal rows, and inconsistency view diagnostics.
   - `tests/test_approval_lifecycle.py`: 11 test cases covering request creation, paired decision update, no dispatch key, request revision, deadline nonce rejection, new challenge rejection, idempotent expiry, concurrency race, inbox query filtering, atomic rollback fault injection, and safe idempotent replay.
3. **Section 1A Authority Tests Designed**: 7 dedicated test cases enforcing authority separation, strict timeout-cannot-approve invariant, fail-closed handling for unsupported actors/bases, and player agent prohibition.
4. **Framework Updates Designed**:
   - `tests/helpers.py::legacy_install` fully designed.
   - `tests/conftest.py` teardown check for `fc27_approval_inconsistencies` fully designed.
   - `tests/test_approval_security.py:203-210` rewrite fully specified.

---

## 5. Verification Method

1. **Independent Document Review**:
   - Review comprehensive test analysis and design blueprint:
     `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_tests_1\tests_analysis.md`
2. **Baseline Test Execution**:
   - Verify baseline passing suites:
     `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_events_outbox.py tests/test_approval_security.py`
3. **Post-Implementation Verification (by Implementer)**:
   - Once implementer applies migration 0003 Section B, store methods, and creates the test files:
     `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py`
     Expected: All tests PASS with zero regressions and zero inconsistencies reported.
