# Handoff Report: Milestone 1 Independent Review & Adversarial Stress-Test

**Agent**: `reviewer_m1_2`  
**Role**: Reviewer, Adversarial Critic  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m1_2`  
**Verdict**: **APPROVE**  

---

## 1. Observation

- **Source Code Inspected**:
  - `src/approval_store.py`:
    - `ApprovalStore(conninfo, request_ttl=timedelta(hours=72))` configured.
    - `complete_run(thread_id, state)`: Implements atomic insertion into `public.approval_requests` and update of `public.graph_threads`, returning `approval_request_id` and `approval_expires_at`. Atomically records `approval.created` event via `_safe_record_event` with `source_table='approval_requests'`.
    - `issue_challenge(thread_id, intent, principal, ttl_seconds)`: Proactively invokes `expire_due(thread_id=thread_id)`, checks `public.fc27_approval_inconsistencies`, acquires `FOR SHARE` locks on `approval_requests` then `graph_threads`, validates candidate, clamps expiration to request TTL, and inserts single-use nonce with `subject_type='thread'`.
    - `apply_decision(thread_id, signed, principal, signing_secret, ...)`: Validates Section 1A constraints (`principal.role is Role.commissioner`, rejects `actor` containing "agent", rejects `actor_type == "player"`, rejects `authority_basis != "primary"`, rejects `actor_type != "commissioner"`). Verifies HMAC signature. Executes strict lock order: `approval_nonces FOR UPDATE` -> `approval_requests FOR UPDATE` -> `graph_threads FOR UPDATE`.
    - Safe idempotent replay protocol: checks consumed nonces against `public.approval_audit` for byte-level and semantic match; returns committed state without error on identical replay, or raises 409 conflict prefixed with `replay:` on divergence.
    - `approval_audit` insertion: populates `proof_type='hmac'`, `correlation_id`, `approval_request_id`, `actor_type='commissioner'`, `authority_basis='primary'`, `authority_role='commissioner'`, `decision_scope='club'`. Emits `approval.decided` event and outbox row.
    - `expire_due(*, thread_id=None, limit=100)`: Scans for expired requests, enforces strict lock order (`approval_nonces` -> `approval_requests` -> `graph_threads`), marks status `expired`, revokes nonces, and emits `approval.expired`.
    - `pending_approvals(limit=50)`: Queries unexpired `awaiting_approval` requests joined with `graph_threads`.
  - `src/decision.py`:
    - Elimination of `new_state["dispatch"]` dictionary verified (lines 73-79 strictly omit dispatch payloads).
    - `REQUEST_REVISION` supported in `DecisionKind`, handled in `resolve_candidate()` (returns None), and mapped to `status="revision_requested"` in `apply_decision_to_state()`.
  - `migrations/0003_dispatch_foundation.sql`:
    - Section B defines `approval_requests`, trigger `fc27_approval_requests_guard`, `graph_threads` constraints and trigger `graph_threads_guard`, deferred constraint triggers `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check` calling `fc27_check_approval_pair()`, view `public.fc27_approval_inconsistencies`, Section 1A compatibility tables (`authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`), and `approval_audit` / `approval_nonces` column extensions.
  - `tests/test_approval_lifecycle.py` and `tests/test_approval_security.py`:
    - Full coverage of lifecycle, race conditions, fault injection rollback, Section 1A invariants, and idempotent replay.

- **Independent Test Execution**:
  - Test command:
    ```powershell
    $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py
    ```
  - Verbatim output:
    ```
    ........................................................................ [ 82%]
    ...............                                                          [100%]
    87 passed in 237.62s (0:03:57)
    ```
  - Exit code: `0`.
  - Failures / Errors: `0`.
  - Skips: `0`.

---

## 2. Logic Chain

1. In `src/approval_store.py`, row locking in `apply_decision()`, `issue_challenge()`, and `expire_due()` follows the invariant hierarchy: `approval_nonces` -> `approval_requests` -> `graph_threads`. Because all queries follow this exact sequence, circular wait conditions (deadlocks) are impossible across concurrent decision commits and background expiry sweeps. This was confirmed empirically by `test_expiry_and_decision_race_has_single_outcome` (8 racing threads, 0 deadlocks, exactly 1 winner).
2. In `src/approval_store.py:apply_decision()`, checking `nonce["consumed_at"]` and comparing the signed payload against `public.approval_audit` guarantees idempotency: network retries submitting identical bytes receive the original committed state with HTTP 200, while attackers attempting to alter any attribute (candidate, player, selection, notes) are rejected with HTTP 409.
3. In `src/approval_store.py:apply_decision()`, Section 1A checks execute before acquiring locks or validating nonces. Rejecting `actor_type="player"`, non-primary authority bases, and agents ensures the system fails closed and prevents unauthorized elevation or simulated player consent during Phase 1.
4. In `migrations/0003_dispatch_foundation.sql`, deferred twin constraint triggers `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check` ensure that at transaction commit, both tables must agree on status, version, and mutual foreign keys, guaranteeing bidirectional 1:1 integrity.
5. In `src/decision.py`, the complete omission of `new_state["dispatch"]` guarantees the Phase 1 invariant `DISPATCH_ENABLED=false` and ensures no player delivery payloads are created.

---

## 3. Caveats

- Milestone 1 establishes the authoritative schema, triggers, and Section 1A compatibility tables (`authority_policies`, `player_assignments`, etc.) with fail-closed enforcement. Active multi-player consent workflows, WebAuthn authenticators, and player portals are intentionally deferred to Phases 2 through 6.
- Production dispatch remains disabled (`DISPATCH_ENABLED=false`); no dispatch execution was enabled or executed during tests.

---

## 4. Conclusion

Milestone 1 satisfies all technical, architectural, transaction isolation, concurrency, and Section 1A authority invariants defined in `ORIGINAL_REQUEST.md` and `PROJECT.md`. The implementation is genuine, complete, verified, and free of integrity violations.

**Final Verdict**: **APPROVE**

---

## 5. Verification Method

Independently reproduce the complete verification suite using:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py
```
Expected outcome: 87 passed, 0 failed, exit code 0.
