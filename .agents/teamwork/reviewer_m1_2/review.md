# Milestone 1 Quality & Adversarial Review Report

**Reviewer**: `reviewer_m1_2`  
**Roles**: Reviewer, Adversarial Critic  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m1_2`  
**Date**: 2026-10-03  

---

## 1. Review Summary

**Verdict**: **APPROVE**

Milestone 1 implements the authoritative approval store, PostgreSQL 16 persistence guarantees, transaction isolation, strict lock ordering, proactive expiry, safe idempotent replay, and Section 1A human authority validation to a high engineering standard.

Independent verification of the test suite against a real PostgreSQL 16 server confirmed that all 87 tests passed cleanly (exit code 0, 0 failures, 0 skips). Teardown hooks in `conftest.py` verified zero discrepancies in `public.fc27_approval_inconsistencies` across all test executions. No integrity violations, dummy facade implementations, or hardcoded shortcuts were detected.

---

## 2. Findings

### [Positive] Finding 1: Strict Global Lock Hierarchy Prevents Deadlocks
- **Location**: `src/approval_store.py:252-277`, `371-407`, `601-638`
- **Observation**: All three critical concurrency paths (`apply_decision`, `issue_challenge`, `expire_due`) acquire row locks in the exact identical global sequence:
  1. `public.approval_nonces` (ordered by `nonce` for batch operations)
  2. `public.approval_requests` (`FOR UPDATE` or `FOR SHARE`)
  3. `public.graph_threads` (`FOR UPDATE` or `FOR SHARE`)
- **Impact**: Deadlocks between high-frequency decision submissions and background expiry daemon cycles are mathematically precluded by invariant lock ordering.

### [Positive] Finding 2: Safe Idempotent Replay Protocol
- **Location**: `src/approval_store.py:410-440`
- **Observation**: When a consumed nonce is presented in `apply_decision()`, the store queries `public.approval_audit` to compare `thread_id`, `package_version`, `signature_sha256`, `decision`, `candidate_id`, `player_id`, `owner`, and `selection`.
- **Impact**: Exactly identical replayed decisions return the committed state safely with HTTP 200 without executing side effects, while any modified or conflicting payload returns HTTP 409 conflict prefixed with `replay:`.

### [Positive] Finding 3: Rigorous Section 1A Authority Fail-Closed Enforcement
- **Location**: `src/approval_store.py:341-352`
- **Observation**: Authority validation executes before any locks are acquired:
  - Rejects non-commissioner roles (`403`).
  - Rejects agent decision actors (`"agent" in actor or actor.startswith("agent:") -> 403`).
  - Rejects player decisions in Phase 1 (`actor_type == "player" -> 403`).
  - Rejects non-primary authority bases (`authority_basis != "primary" -> 403`).
  - Rejects non-commissioner actor types (`actor_type != "commissioner" -> 403`).
- **Impact**: Guarantees Phase 1 bootstrap isolation and enforces Section 1A invariants that no agent may decide for a human and non-primary authorities fail closed.

### [Minor / Informational] Finding 4: Phase 1 Dispatch Inactive Seam
- **Location**: `src/approval_store.py:555-570`, `src/decision.py:73-79`
- **Observation**: The hook for `dispatch_creator` exists in `apply_decision()`, but is guarded by `if dispatch_enabled and intent.decision is DecisionKind.APPROVE and dispatch_creator is not None:`. In Phase 1, `dispatch_enabled` defaults to `False` across all configurations. Furthermore, `apply_decision_to_state()` strictly excludes any `dispatch` dictionary from `new_state`.
- **Assessment**: Correctly adheres to the Phase 1 invariant `DISPATCH_ENABLED=false` and prepares the modular seam required by Milestone 4 without premature activation.

---

## 3. Verified Claims

| # | Claim | Verification Method | Result |
|---|---|---|---|
| 1 | `complete_run()` creates 1:1 linked `approval_requests` row with valid expiry and emits `approval.created` event | `test_complete_run_creates_linked_request` inspection & execution | **PASS** |
| 2 | Deferred constraint triggers enforce bidirectional 1:1 pair consistency at commit time | `test_pair_mismatch_fails_at_commit` & `test_awaiting_thread_requires_link` | **PASS** |
| 3 | Package version and terminal approval request rows are frozen and immutable | `test_package_version_immutable_while_awaiting`, `test_approval_request_immutable_fields`, `test_approval_request_frozen_when_terminal` | **PASS** |
| 4 | Safe idempotent decision replay succeeds on identical payload; conflicting replay returns 409 | `test_safe_idempotent_decision_replay`, `test_nonce_is_single_use` | **PASS** |
| 5 | Expiry daemon marks request and thread expired, revokes nonces, and emits `approval.expired` | `test_expired_request_blocks_prior_nonce`, `test_expired_request_blocks_new_challenge`, `test_expire_due_is_idempotent` | **PASS** |
| 6 | 8-thread concurrent expiry and decision race resolves cleanly with single winner and 0 deadlocks | `test_expiry_and_decision_race_has_single_outcome`, `test_concurrent_approve_has_exactly_one_winner` | **PASS** |
| 7 | Injected fault during decision transaction rolls back all tables atomically | `test_failed_decision_rolls_back_everything` across audit, events, and outbox | **PASS** |
| 8 | Section 1A timeout/inactivity never approves a request | `test_timeout_inactivity_cannot_approve` | **PASS** |
| 9 | Section 1A unsupported actor types, authority bases, and agents fail closed | `test_unsupported_actor_types_fail_closed_in_phase1`, `test_unsupported_authority_bases_fail_closed_in_phase1`, `test_player_agent_cannot_be_decision_actor` | **PASS** |
| 10 | Elimination of legacy `new_state["dispatch"]` dictionary | `test_approve_writes_no_dispatch_key` and inspection of `src/decision.py` | **PASS** |
| 11 | `REQUEST_REVISION` supported as terminal immutable lifecycle | `test_request_revision_lifecycle` and inspection of `src/decision.py` | **PASS** |
| 12 | Zero inconsistencies in `public.fc27_approval_inconsistencies` post-test | Evaluated via `tests/conftest.py` teardown on every test execution | **PASS** |

---

## 4. Adversarial Attack Surface & Stress-Test Report

**Overall Risk Assessment**: **LOW**

### Challenge 1: Lock Ordering Inversion and Race Conditions
- **Assumption Challenged**: Concurrent execution of `expire_due` daemon and `apply_decision` endpoint could encounter AB-BA deadlock or leave database in an inconsistent state.
- **Attack Scenario**: 4 worker threads attempt `expire_due` while 4 worker threads attempt `apply_decision` on the identical thread within milliseconds of the expiration boundary.
- **Result**: Tested via `test_expiry_and_decision_race_has_single_outcome`. All threads executed against live PostgreSQL without deadlock; exactly 1 winner transitioned the thread to a terminal state (`approved` or `expired`), and all nonces and audit records remained consistent.
- **Verdict**: Robust defense verified.

### Challenge 2: Replay Attack and Nonce Tampering
- **Assumption Challenged**: An attacker could capture a valid signed decision nonce and re-submit it with modified parameters (e.g., swapping selected candidate or modifying notes).
- **Attack Scenario**: Submit valid decision, then re-submit identical nonce with altered notes or candidate payload.
- **Result**: Tested via `test_safe_idempotent_decision_replay` and `test_nonce_is_single_use`. The store compares the signed audit record. Identical replay is recognized as a safe idempotent return, whereas altered payloads fail with HTTP 409 `replay: conflicting terminal decision or nonce already used`.
- **Verdict**: Robust defense verified.

### Challenge 3: Section 1A Agent Impersonation and Authority Elevation
- **Assumption Challenged**: An AI agent or unauthorized actor could supply authority parameters to bypass Commissioner approval or simulate player fallback approval in Phase 1.
- **Attack Scenario**: Submitting decision with `actor="agent:player_agent_1"`, `actor_type="player"`, or `authority_basis="affected_player_fallback"`.
- **Result**: Tested via `test_player_agent_cannot_be_decision_actor`, `test_unsupported_actor_types_fail_closed_in_phase1`, and `test_unsupported_authority_bases_fail_closed_in_phase1`. All attempts immediately raise `StoreError(403)` before database locks are acquired.
- **Verdict**: Robust defense verified.

### Challenge 4: Inactivity / Timeout Automatic Approval
- **Assumption Challenged**: A system cron or background worker could mistakenly treat an expired request as approved after a timeout.
- **Attack Scenario**: Letting a request time out and inspecting whether any approval audit or outbox dispatch row is produced.
- **Result**: Tested via `test_timeout_inactivity_cannot_approve`. `expire_due()` explicitly updates status to `'expired'`, sets `expired_at = now()`, revokes nonces, and writes zero approval records or dispatch outbox entries.
- **Verdict**: Robust defense verified.

---

## 5. Integrity & Non-Bypass Assessment

An explicit audit was performed for integrity violations:
- **Hardcoded test fixtures/outputs**: None found. All logic queries PostgreSQL tables and executes dynamic schema operations.
- **Dummy/facade implementations**: None found. Full transaction handling, HMAC verification, deferred constraint triggers, and outbox logging are genuinely implemented.
- **Task shortcuts / external bypassing**: None found. Core logic resides in `src/approval_store.py` and `src/decision.py`.
- **Attestation veracity**: Worker test logs matched independent execution (87 passed, exit code 0).
- **Self-certifying bypasses**: Verified independently via automated test run in fresh background environment.

---

## 6. Coverage Gaps & Unverified Items
- **None**: All 87 test targets across database roles, migration 0003, events/outbox, approval lifecycle, and security suites were independently verified. Phase 2-6 components (e.g. active player WebAuthn, player consent endpoints) are explicitly out of Phase 1 scope and reserved in schema.
