# Forensic Audit Report: Milestone 1 (Authoritative Approval Lifecycle, Expiry & Core Data Model)

**Work Product**: Milestone 1 Implementation across:
- `migrations/0003_dispatch_foundation.sql`
- `migrations/post/grants.sql`
- `src/db_migrate.py`
- `src/approval_store.py`
- `src/decision.py`
- `tests/helpers.py`
- `tests/conftest.py`
- `tests/test_approval_security.py`
- `tests/test_migration_0003.py`
- `tests/test_approval_lifecycle.py`

**Profile**: General Project  
**Integrity Mode**: Development (per `ORIGINAL_REQUEST.md` line 11)  
**Verdict**: **CLEAN**

---

## 1. Executive Summary
A forensic audit was performed on the Milestone 1 deliverables produced by `worker_m1_1`. The audit encompassed static code forensics, schema constraint and trigger analysis, role and privilege inspection, lock hierarchy validation, Section 1A human authority guardrail verification, and independent test execution against PostgreSQL 16.

Zero integrity violations, hardcoded test results, facade implementations, mock shortcuts, or unauthorized phase boundary crossings were detected. All 87 tests passed genuinely against a live PostgreSQL database in 234.59s.

---

## 2. Phase Results

| Forensic Check | Result | Details |
|---|:---:|---|
| **Hardcoded Output Detection** | **PASS** | Source code and test assertions contain zero hardcoded test outputs, return constants, or mock stub returns. Database state queries evaluate live PostgreSQL tables (`graph_threads`, `approval_requests`, `approval_audit`, `approval_nonces`, `domain_events`, `outbox`). |
| **Facade Implementation Detection** | **PASS** | `ApprovalStore`, `decision.py`, and `db_migrate.py` implement genuine business logic, transaction boundaries, cryptographic validations, and error paths. All database constraints and PL/pgSQL triggers (`fc27_check_approval_pair`, `fc27_guard_thread_transition`, `fc27_guard_approval_request_transition`, `fc27_reject_mutation`) are genuine stored procedures. |
| **Pre-populated Artifact Detection** | **PASS** | No pre-seeded logs, result files, or cached outputs exist in the repository. Tests spin up isolated PostgreSQL databases per test function and tear them down with full teardown consistency assertions. |
| **Phase Boundary & Dispatch Isolation** | **PASS** | `DISPATCH_ENABLED=false` is strictly preserved. `new_state["dispatch"]` is completely eliminated from `src/decision.py:apply_decision_to_state()`. Zero player delivery adapter calls exist. Outbox channel `fc27:dispatch` has 0 rows written. |
| **PostgreSQL Authoritative Enforcement** | **PASS** | Authoritative database state is enforced by dual deferred triggers (`fc27_graph_threads_pair_check`, `fc27_approval_requests_pair_check`). Strict lock ordering is enforced: `approval_nonces` -> `approval_requests` -> `graph_threads`. Least privilege grants in `grants.sql` isolate `fc27_app` (DML only, no DDL/CREATE) from `fc27_dispatch` (zero approval access). |
| **Section 1A Authority Guardrails** | **PASS** | Inactivity and timeout never approve (requests transition strictly to `expired`, revoking nonces with zero approval audit or dispatch events). Non-commissioner actors and non-primary authority bases fail closed with HTTP 403. Player agents are strictly barred from acting as decision actors. Future player decision routes return 404. |
| **Cryptographic Digest Integrity** | **PASS** | Grants policy version 3 and SHA-256 (`9ac85df90575adbc3938fb5ec68c459eb1c70de74d5191bebc050fdde215f4b1`) are authentically checked by migration runner. HMAC signatures and payload SHA-256 digests are genuinely validated. |
| **Execution Verification** | **PASS** | Full pytest verification suite executed independently against live PostgreSQL test database. All 87 tests passed with zero failures and zero skips. |

---

## 3. Detailed Forensic Findings

### 3.1 Zero Hardcoded Outputs & Genuine Implementation
- **Source Inspection**: In `src/approval_store.py`, methods `create_run()`, `complete_run()`, `issue_challenge()`, `apply_decision()`, `expire_due()`, and `pending_approvals()` perform authentic parameterized queries with explicit transaction control and row-level locking (`FOR SHARE` / `FOR UPDATE`).
- **Trigger Logic**: `migrations/0003_dispatch_foundation.sql` implements 810 lines of genuine SQL DDL, tables, constraints, indices, PL/pgSQL functions, and triggers:
  - `fc27_guard_approval_request_transition()`: enforces terminal freeze, provenance immutability (`approval_request_id`, `thread_id`, `package_version`, `created_at`, `expires_at`), and legal state machine transitions.
  - `fc27_guard_thread_transition()`: freezes terminal threads against update/delete, protects package version immutability, and enforces legal transition states.
  - `fc27_check_approval_pair()`: enforces 1:1 bidirectional pairing between `graph_threads` and `approval_requests` (matching status, package_version, and thread_id).
  - `fc27_approval_inconsistencies`: diagnostic view detecting `status_mismatch`, `package_mismatch`, `unlinked_request`, `missing_link`, and `legacy_unlinked`.
- **Fault Injection Verification**: `test_failed_decision_rolls_back_everything` injects failure triggers on `approval_audit`, `domain_events`, and `outbox`, verifying complete transactional rollback across all tables and nonces.

### 3.2 Phase Boundary & Dispatch Isolation
- **Dispatch Payload Elimination**: Lines 73-82 of `src/decision.py`:
  ```python
  # In Phase 1, dispatch is disabled. Under no condition is new_state["dispatch"] written!
  new_state.update(
      approved=approved,
      status=target_status,
      approved_by=approved_by,
      commissioner_input=intent.notes,
  )
  ```
  `new_state["dispatch"]` is entirely absent.
- **Test AC-44**: `test_approve_writes_no_dispatch_key` asserts `"dispatch" not in resp.json()` and `"dispatch" not in state`.
- **Dispatch Event Absence**: `test_timeout_inactivity_cannot_approve` asserts 0 rows in `public.outbox` where `channel = 'fc27:dispatch'`.
- `DISPATCH_ENABLED` defaults to `False` in `ApprovalStore.apply_decision`.

### 3.3 Lock Acquisition Order & Concurrency Verification
- **Global Lock Order**:
  1. `approval_nonces` (`FOR UPDATE` on `signed.nonce`)
  2. `approval_requests` (`FOR UPDATE` on `thread_id` / `approval_request_id`)
  3. `graph_threads` (`FOR UPDATE` on `thread_id`)
- **Challenge Lock Order**:
  1. `approval_requests` (`FOR SHARE` on `thread_id`)
  2. `graph_threads` (`FOR SHARE` on `thread_id`)
- **Expiry Lock Order**:
  1. `approval_nonces` (`FOR UPDATE` on open nonces for thread ordered by nonce)
  2. `approval_requests` (`FOR UPDATE` on `approval_request_id`)
  3. `graph_threads` (`FOR UPDATE` on `thread_id`)
- **Concurrency Stress Test**: `test_expiry_and_decision_race_has_single_outcome` launched 8 concurrent threads (4x `expire_due` + 4x `apply_decision`) right at deadline boundary. The system resolved to exactly one terminal state without deadlock or half-applied states.

### 3.4 Section 1A Authority Guardrails
- **Inactivity Never Approves**: When `expires_at <= now()`, `expire_due()` transitions status to `'expired'`. Zero approval audit records or dispatch events are generated.
- **Fail-Closed Role & Basis Enforcement**:
  ```python
  if principal.role is not Role.commissioner:
      raise StoreError(403, "only the commissioner can decide")
  if actor and ("agent" in actor or actor.startswith("agent:")):
      raise StoreError(403, "agents cannot be decision actors")
  if actor_type == "player":
      raise StoreError(403, "player decisions are unsupported in Phase 1")
  if authority_basis != "primary":
      raise StoreError(403, f"unsupported authority basis in Phase 1: {authority_basis}")
  if actor_type != "commissioner":
      raise StoreError(403, f"unsupported actor type in Phase 1: {actor_type}")
  ```
- **Tests Verified**:
  - `test_timeout_inactivity_cannot_approve`: confirms timeout produces expired, not approved.
  - `test_unsupported_actor_types_fail_closed_in_phase1`: confirms player, delegate, agent, coach, system fail closed.
  - `test_unsupported_authority_bases_fail_closed_in_phase1`: confirms fallback, delegate, consent fail closed.
  - `test_player_agent_cannot_be_decision_actor`: confirms agent strings fail closed.
  - `test_no_player_decision_endpoints_active_in_phase1`: confirms 404 for unexposed future routes.

---

## 4. Test Execution Evidence

**Test Execution Command**:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py
```

**Verbatim Pytest Output**:
```
........................................................................ [ 82%]
...............                                                          [100%]
87 passed in 234.59s (0:03:54)
```
**Exit Code**: `0`

**Test Breakdown by Module**:
- `tests/test_db_roles.py`: 12 passed
- `tests/test_db_migrate_roles.py`: 10 passed
- `tests/test_events_outbox.py`: 16 passed
- `tests/test_migration_0003.py`: 7 passed
- `tests/test_approval_lifecycle.py`: 17 passed
- `tests/test_approval_security.py`: 25 passed
**Total**: **87 passed, 0 failed, 0 skipped**.

**Inconsistency Teardown Verification**:
In `tests/conftest.py`, the `pg_uri` fixture automatically runs post-test assertions against `public.fc27_approval_inconsistencies` for every test function:
```python
cur.execute("SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'")
count = cur.fetchone()[0]
assert count == 0
```
Zero non-legacy inconsistencies were detected across the entire test suite.

---

## 5. Audit Verdict

### Final Verdict: **CLEAN**

The Milestone 1 work product meets all integrity standards, architectural specifications, security invariants, and Section 1A constraints without compromise or fabrication. Milestone 1 is verified ready for Milestone 2 integration.
