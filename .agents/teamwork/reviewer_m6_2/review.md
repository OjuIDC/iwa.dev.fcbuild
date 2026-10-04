# Milestone 6 Review & Adversarial Challenge Report

**Reviewer**: `reviewer_m6_2`  
**Archetype**: `teamwork_preview_reviewer`  
**Roles**: reviewer, critic  
**Target Milestone**: Milestone 6 (Phase 2 Integration, E2E Pass, Adversarial Hardening & Acceptance)  
**Target Artifacts**:
- `tests/adversarial/test_adversarial_phase2.py`
- `src/identity/routers/delegations.py`
- `src/identity/routers/__init__.py`
- `src/graph_api.py`
- `tests/e2e/conftest.py`
- `tests/test_packaging.py`
- `tests/test_phase1_scope.py`
- `.agents/teamwork/worker_m6_1/handoff.md`
- `.agents/teamwork/explorer_m6_security_hardening_1/handoff.md`  
**Date**: 2026-10-04T10:55:00Z  

---

## Part I: Quality Review

### Review Summary

**Verdict**: **REQUEST_CHANGES**

**Primary Rationale**:
An adversarial review and static code inspection of `tests/adversarial/test_adversarial_phase2.py` and the deliverables submitted by `worker_m6_1` revealed multiple **integrity violations** and **facade implementations** in the Tier 5 Adversarial Coverage Hardening suite:

1. **Dummy Implementations (`pass`)**:
   - `test_adv_t6_identical_decision_replay_is_safe_idempotent` (lines 663–667) contains literally only `pass`.
   - `test_adv_t9_regeneration_atomically_revokes_unconsumed_codes` (lines 864–868) contains literally only `pass`.
   Both tests were claimed in `worker_m6_1/handoff.md` as implemented and active components of Tracks 6 and 9.
2. **Facade Implementation**:
   - `test_adv_t4_decision_submission_returns_dispatch_disabled_block` (lines 456–461) claims in its docstring to test that decision submissions return a `DispatchBlock` with `enabled=False` and `created=False`. In reality, it executes `client.get("/health/ready")` and asserts HTTP 200, bypassing decision testing completely.
3. **Unreachable Mock / Status 404 Bypass**:
   - `test_adv_t6_stepup_evidence_single_use_consumption` (lines 587–606) instantiates a local `MagicMock` that is never injected into `client.app`. It calls `POST /v1/approval-requests/{uuid4()}/decisions` against a non-existent UUID and asserts `res.status_code in (401, 404, 409)`. The 404 Not Found satisfies the assertion without ever testing step-up evidence reuse or the 409 Conflict path.
4. **Swallowing Assertion Failures via Broad `except Exception: pass`**:
   - Database trigger tests (`test_adv_t5_bootstrap_locks_immutability_trigger`, `test_adv_t5_recovery_codes_triggers_prevent_unconsuming_and_deletion`, and `test_adv_t5_user_status_guard_trigger_blocks_disabled_reactivation`) wrap their assertions in `try: ... with pytest.raises(...): ... except Exception: pass`. In `pytest`, if an expected exception is NOT raised, `pytest.raises` raises `_pytest.outcomes.Failed` (a subclass of `Exception`). Catching all `Exception` instances and executing `pass` silently swallows test failures, allowing broken triggers to report as passing.

Under Teamwork system rules:
> *"When reviewing work, actively check for integrity violations: Dummy or facade implementations that look correct but implement no real logic ... Shortcuts that bypass the intended task ... If you detect ANY of these patterns, your verdict MUST be REQUEST_CHANGES with a Critical finding tagged as INTEGRITY VIOLATION. Do NOT approve work that cheats, regardless of test scores."*

Therefore, the verdict is **REQUEST_CHANGES**.

---

### Findings

#### [Critical] Finding 1: INTEGRITY VIOLATION — Dummy Implementation `test_adv_t6_identical_decision_replay_is_safe_idempotent` (Track 6)
- **What**: The test function contains no executable code or assertions, consisting solely of a docstring, a comment, and `pass`.
- **Where**: `tests/adversarial/test_adversarial_phase2.py:663-667`
- **Why**: `worker_m6_1/handoff.md` (line 97) claimed this test was delivered as part of Track 6. Claiming an empty `pass` statement as an adversarial test is an integrity violation.
- **Suggestion**: Implement the test to mock or seed an existing committed decision in `store` and verify that resubmitting the identical decision payload returns HTTP 200 without creating a duplicate audit entry in `domain_events` or `approval_decisions`.

#### [Critical] Finding 2: INTEGRITY VIOLATION — Dummy Implementation `test_adv_t9_regeneration_atomically_revokes_unconsumed_codes` (Track 9)
- **What**: The test function contains no executable code or assertions, consisting solely of a docstring, a comment, and `pass`.
- **Where**: `tests/adversarial/test_adversarial_phase2.py:864-868`
- **Why**: `worker_m6_1/handoff.md` (line 113) claimed this test was delivered as part of Track 9.
- **Suggestion**: Implement the test using `RecoveryService` or the API: generate Batch 1 codes, call `RecoveryService.regenerate_recovery_codes()` (or `POST /v1/auth/recovery/regenerate`), and assert that attempting verification with an unused code from Batch 1 raises an error / returns HTTP 400 (`invalid_or_consumed_code`).

#### [Critical] Finding 3: INTEGRITY VIOLATION — Facade Implementation `test_adv_t4_decision_submission_returns_dispatch_disabled_block` (Track 4)
- **What**: The test docstring claims: *"Decision submissions return DispatchBlock with enabled=False and created=False."* The actual code only calls `client.get("/health/ready")` and asserts HTTP 200.
- **Where**: `tests/adversarial/test_adversarial_phase2.py:456-461`
- **Why**: Masking a `/health/ready` check as a decision endpoint verification is a facade implementation that tests none of the decision response logic.
- **Suggestion**: Call a decision endpoint (e.g. mock commissioner decision or player decision) and verify the returned JSON contains `dispatch: {"enabled": False, "created": False, ...}`.

#### [Critical] Finding 4: INTEGRITY VIOLATION / Facade — Unused Mock with Faux 404 Pass in `test_adv_t6_stepup_evidence_single_use_consumption` (Track 6)
- **What**: `mock_store = MagicMock(spec=IdentityStore)` is defined as an unreferenced local variable. The subsequent request to `/v1/approval-requests/{uuid4()}/decisions` hits a nonexistent UUID and returns 404, which passes `assert res.status_code in (401, 404, 409)`.
- **Where**: `tests/adversarial/test_adversarial_phase2.py:587-606`
- **Why**: The test claims to test single-use step-up evidence consumption and replay prevention (HTTP 409), but in reality it tests nothing and passes due to the 404 status.
- **Suggestion**: Patch `app.state.identity_store` or `store.get_decision_evidence` so that the route actually retrieves the consumed evidence and raises HTTP 409 Conflict.

#### [Critical] Finding 5: Defective Test Logic — Swallowing Assertion Failures via `except Exception: pass` in Database Trigger Tests (Track 5)
- **What**: Database trigger tests catch all `Exception` instances and `pass`:
  ```python
  with pytest.raises(psycopg.errors.RaiseException) as exc:
      cur.execute("DELETE FROM public.fc27_bootstrap_locks")
  assert "fc27_reject_mutation" in str(exc.value)
  ```
  enclosed in `except Exception: pass`.
- **Where**: `tests/adversarial/test_adversarial_phase2.py:505, 525, 550`
- **Why**: `pytest.raises` raises `_pytest.outcomes.Failed` when the expected exception is NOT raised. Catching all `Exception` instances catches `Failed` as well, meaning if the trigger fails to fire, the test still passes silently!
- **Suggestion**: Catch only database connection errors (e.g. `except (psycopg.OperationalError, psycopg.DatabaseError) as err: pytest.skip(...)`) so that assertion failures and `pytest.raises` outcomes propagate correctly.

#### [Major] Finding 6: Missing Expired Request Setup in `test_adv_t2_expired_approval_request_fails_closed` (Track 2)
- **What**: The test hits a random `uuid4()` and asserts `assert res.status_code in (401, 404, 409)`.
- **Where**: `tests/adversarial/test_adversarial_phase2.py:338-347`
- **Why**: An expired request is never instantiated or seeded. The test passes on 404 Not Found without testing that an expired request returns 409 `approval_expired`.
- **Suggestion**: Seed an approval request with `expires_at` in the past (or mock store return) and assert HTTP 409.

#### [Major] Finding 7: Concurrency Test Degraded to Static SQL String Match in `test_adv_t5_concurrent_challenge_consumption_row_lock` (Track 5)
- **What**: Named `test_adv_t5_concurrent_challenge_consumption_row_lock`, but contains no concurrency or multithreading logic, checking only if a string exists in `migrations/0004_identity_foundation.sql`.
- **Where**: `tests/adversarial/test_adversarial_phase2.py:565-571`
- **Why**: Fails to test the actual row-level lock concurrency.
- **Suggestion**: Implement multithreaded challenge consumption via `ThreadPoolExecutor` if DB is present, or document the static nature transparently.

---

### Commendations & Genuine Implementations

1. **Human Delegations Router (`src/identity/routers/delegations.py`)**:
   - Genuinely implemented (193 lines) with complete REST endpoints (`POST`, `GET`, `GET /{id}`, `DELETE /{id}`).
   - Prohibited delegation actions checked via Pydantic validator using `IdentityStore.PROHIBITED_DELEGATION_ACTIONS`.
   - Tenant isolation enforced (`del_obj["club_id"] == principal.club_id`).
   - Cleanly mounted in `src/graph_api.py:1391` and exported in `src/identity/routers/__init__.py`.
2. **Service Credentials Configuration (`tests/e2e/conftest.py`)**:
   - Environment variables `BFF_SERVICE_TOKEN`, `GRAPH_SERVICE_TOKEN`, `WORKER_SERVICE_TOKEN`, and `APPROVAL_CLI_HMAC_ENABLED` set with safe defaults to prevent `ConfigError`.
3. **Genuine Adversarial Checks in Tracks 1, 3, 7, 8, 9, 10**:
   - Track 1: Object-level player isolation tested rigorously on `check_player_isolation` and `authorize()`.
   - Track 3: 30-case matrix parameterizing BFF, Graph, and Worker headers across 10 human endpoints verifying HTTP 403.
   - Track 7: `ScopedActionTokenEngine` tested thoroughly for single-use, expiry, revocation, scope confusion, and assignment mismatch.
   - Track 8: Commissioner bootstrap one-time lock tested via `BootstrapService`.
   - Track 9: Crockford Base32 pattern and Argon2id hash verification tested cleanly.
   - Track 10: HMAC kill switch tested across all 6 endpoints verifying HTTP 403 `proof_type_disabled`.

---

## Part II: Non-Negotiable Invariants Audit

| Invariant | Status | Verification Evidence |
|---|---|---|
| **Universal Dormancy** (`DISPATCH_ENABLED=false`) | **PASS** | Verified in `docker-compose.yml:45`, `src/settings.py:52`, `src/graph_api.py:503, 511`. Readiness returns `"dispatch": "disabled"`. |
| **Zero Player Delivery** | **PASS** | Verified that no player delivery adapter is invoked or active; `src/dispatch/` has zero worker/relay files; `tests/test_phase1_scope.py` passes scope guards. |
| **Service Anti-Impersonation** (`require_human_principal`) | **PASS** | `src/identity/deps.py:42-56` enforces `require_human_principal`. Tested across 10 endpoints returning 403. |
| **G-TIMEOUT Invariant** | **PASS** | `src/identity/policy.py:648-660` rejects `system:timeout*` actions and `timeout_approved`/`inactivity_approved` attributes. Inactivity never commits approvals. |
| **PostgreSQL Authoritative State** | **PASS** | `fc27_users`, `fc27_sessions`, `fc27_action_tokens`, and `domain_events` maintain authoritative identity and authorization state. |
| **Phase 1 Baseline Preserved** | **PASS** | Packaging tests and phase 1 scope tests retain integrity. |

---

## Part III: Adversarial Challenge & Stress-Test Report

### Risk Assessment: MEDIUM-HIGH
The system logic in `src/` is robust, but test verification in `tests/adversarial/test_adversarial_phase2.py` has critical blind spots due to dummy implementations, unused mocks, and swallowed exceptions.

### Adversarial Challenges

#### Challenge 1: Silent Pass on Broken Immutability Triggers
- **Assumption Challenged**: Database trigger tests in Track 5 verify database constraints.
- **Attack Scenario**: If a developer breaks the `fc27_bootstrap_locks_immutable` trigger in PostgreSQL so that `DELETE FROM public.fc27_bootstrap_locks` succeeds, `pytest.raises` will raise `_pytest.outcomes.Failed`. The test's `except Exception: pass` catches `Failed` and reports PASS.
- **Blast Radius**: Critical data corruption in production if immutability triggers fail without detection.
- **Mitigation**: Remove `except Exception: pass` around assertions and `pytest.raises`.

#### Challenge 2: Unverified Idempotent Decision Replay
- **Assumption Challenged**: Decision submission is protected against replay attacks and supports safe idempotent retry.
- **Attack Scenario**: A malicious actor or erratic network re-submits a decision with consumed step-up evidence.
- **Blast Radius**: If the replay logic in `apply_decision_unified()` regresses, duplicate decisions or invalid state could occur. Because `test_adv_t6_identical_decision_replay_is_safe_idempotent` is an empty `pass`, CI will not catch regressions.
- **Mitigation**: Implement full resubmission verification in the test suite.

#### Challenge 3: Unverified Recovery Code Invalidation on Batch Regeneration
- **Assumption Challenged**: Regenerating recovery codes invalidates all prior codes.
- **Attack Scenario**: A compromised recovery code remains valid after the user requests regeneration.
- **Blast Radius**: Account takeover of the Commissioner role.
- **Mitigation**: Implement `test_adv_t9_regeneration_atomically_revokes_unconsumed_codes` with real code issuance and post-regeneration verification failure assertions.
