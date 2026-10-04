# Adversarial Challenge Report: Milestone 1 Phase 1

**Reviewer**: Challenger 2 (`challenger_m1_2`)  
**Role**: Empirical Challenger (critic, specialist)  
**Target**: Milestone 1 Phase 1 Authoritative Approval Lifecycle, Expiry & Data Model  
**Verdict**: **APPROVE**  
**Overall Risk Assessment**: **LOW** (All tested security invariants held under empirical attack)

---

## 1. Challenge Summary

We performed systematic adversarial stress-testing against the Milestone 1 Phase 1 data foundation, targeting:
1. **Section 1A Authority Invariants**: Probed timeout approvals, unauthorized actors, unsupported authority bases, and automated decision prevention.
2. **Schema & Trigger Tamper Resistance**: Attempted direct SQL mutation of immutable fields, terminal row modifications/deletions, and twin pair consistency trigger bypasses.
3. **Inconsistency Detection Integrity**: Validated diagnostic view `public.fc27_approval_inconsistencies` against deliberately corrupted fixtures.

All attacks failed closed as designed. Every invariant held without exception.

---

## 2. Empirical Challenge Matrix

### Dimension 1: Section 1A Authority Attacks

| Attack Scenario | Attack Method | Expected Defense | Observed Behavior | Status |
|---|---|---|---|---|
| **Timeout Approval via SQL** | Direct SQL `UPDATE approval_requests SET status = 'approved'` on expired row | Blocked by trigger `fc27_approval_requests_guard` | Raised `CheckViolation: approval_request is terminal and frozen` | **PASS** |
| **Timeout Approval via Thread SQL** | Direct SQL `UPDATE graph_threads SET status = 'approved'` on expired row | Blocked by trigger `graph_threads_guard` | Raised `CheckViolation: thread ... is terminal (expired) and cannot be modified` | **PASS** |
| **Expired Request Decision via Store/API** | Call `apply_decision` after `expires_at <= now()` | Blocked by proactive expiry & status check | Raised `StoreError(409, 'approval_expired')`; zero approval audit inserted | **PASS** |
| **Expired Request Challenge via Store/API** | Call `issue_challenge` after `expires_at <= now()` | Blocked by proactive expiry & request check | Raised `StoreError(409, 'approval_expired')` | **PASS** |
| **Automated Decision Inactivity Attack** | Allow request to expire and trigger `expire_due()` daemon | Request marked `expired`; zero approval records written | `approval_requests.status = 'expired'`, 0 rows in `approval_audit`, 0 rows in `outbox` channel `fc27:dispatch` | **PASS** |
| **Unauthorized Actor: `player`** | Submit signed decision with `actor_type='player'` | Fails closed (403) | Raised `StoreError(403, 'player decisions are unsupported in Phase 1')` | **PASS** |
| **Unauthorized Actor: `delegate`** | Submit signed decision with `actor_type='delegate'` | Fails closed (403) | Raised `StoreError(403, 'unsupported actor type in Phase 1: delegate')` | **PASS** |
| **Unauthorized Actor: `agent`** | Submit decision with `actor='agent:player_agent_1'` | Fails closed (403) | Raised `StoreError(403, 'agents cannot be decision actors')` | **PASS** |
| **Unauthorized Actor: `coach`/`system`/`bot`** | Submit decision with non-commissioner actor | Fails closed (403) | Raised `StoreError(403, 'unsupported actor type in Phase 1: ...')` | **PASS** |
| **Authority Basis: `affected_player_fallback`** | Submit decision with `authority_basis='affected_player_fallback'` | Fails closed (403) | Raised `StoreError(403, 'unsupported authority basis in Phase 1: affected_player_fallback')` | **PASS** |
| **Authority Basis: `explicit_delegate`** | Submit decision with `authority_basis='explicit_delegate'` | Fails closed (403) | Raised `StoreError(403, 'unsupported authority basis in Phase 1: explicit_delegate')` | **PASS** |
| **Authority Basis: `affected_player_consent`** | Submit decision with `authority_basis='affected_player_consent'` | Fails closed (403) | Raised `StoreError(403, 'unsupported authority basis in Phase 1: affected_player_consent')` | **PASS** |
| **Service Token Privilege Escalation** | Call `/decision-challenge` or `/decision` with `Role.service` token | Fails closed (403) | HTTP 403 Forbidden | **PASS** |
| **Future Player Endpoint Probing** | `POST` to `/v1/player-decisions`, `/v1/delegations`, `/v1/escalations` | Inactive routes return 404 | HTTP 404 Not Found on all endpoints | **PASS** |

### Dimension 2: Schema & Trigger Tamper Attacks

| Attack Scenario | Attack Method | Expected Defense | Observed Behavior | Status |
|---|---|---|---|---|
| **Thread Package Version Tampering** | Direct SQL `UPDATE graph_threads SET package_version = ...` while awaiting | Blocked by trigger `graph_threads_guard` | Raised `CheckViolation: package_version is immutable` | **PASS** |
| **Request Package Version Tampering** | Direct SQL `UPDATE approval_requests SET package_version = ...` while awaiting | Blocked by trigger `fc27_approval_requests_guard` | Raised `CheckViolation: package_version is immutable` | **PASS** |
| **Request Immutable Fields Tampering** | Direct SQL UPDATE on `thread_id`, `created_at`, `expires_at`, `approval_request_id` | Blocked by trigger `fc27_approval_requests_guard` | Raised `CheckViolation: <field> is immutable` | **PASS** |
| **Terminal Request UPDATE Tampering** | Direct SQL `UPDATE approval_requests` on terminal row | Blocked by trigger `fc27_approval_requests_guard` | Raised `CheckViolation: approval_request is terminal and frozen` | **PASS** |
| **Terminal Request DELETE Tampering** | Direct SQL `DELETE FROM approval_requests` on terminal row (as admin) | Blocked by trigger `fc27_approval_requests_guard` | Raised `CheckViolation: approval_request is terminal and frozen` | **PASS** |
| **Terminal Thread UPDATE Tampering** | Direct SQL `UPDATE graph_threads` on terminal row | Blocked by trigger `graph_threads_guard` | Raised `CheckViolation: thread ... is terminal and cannot be modified` | **PASS** |
| **Terminal Thread DELETE Tampering** | Direct SQL `DELETE FROM graph_threads` on terminal row (as admin) | Blocked by trigger `graph_threads_guard` | Raised `CheckViolation: thread ... is terminal and cannot be deleted` | **PASS** |
| **Pair Mismatch: Thread Status** | Update `graph_threads.status` to `approved` without updating request | Deferred constraint trigger blocks commit | Raised `CheckViolation: approval pair: status mismatch` at commit | **PASS** |
| **Pair Mismatch: Request Status** | Update `approval_requests.status` to `approved` without updating thread | Deferred constraint trigger blocks commit | Raised `CheckViolation: approval pair: status mismatch` at commit | **PASS** |
| **Unlinked Awaiting Thread** | Insert awaiting thread with package version and `approval_request_id = NULL` | Deferred constraint trigger blocks commit | Raised `CheckViolation: approval pair: awaiting thread requires link` at commit | **PASS** |
| **Unlinked Approval Request** | Insert request pointing to thread that does not point back | Deferred constraint trigger blocks commit | Raised `CheckViolation: approval pair: thread ... does not link to approval_request ...` at commit | **PASS** |

### Dimension 3: Inconsistency View Verification

| Inconsistency Kind | Test Fixture Setup | Expected Detection in `fc27_approval_inconsistencies` | Observed Result | Status |
|---|---|---|---|---|
| `status_mismatch` | Linked pair with differing statuses | Row with `kind = 'status_mismatch'` | Accurately classified | **PASS** |
| `package_mismatch` | Linked pair with differing `package_version` | Row with `kind = 'package_mismatch'` | Accurately classified | **PASS** |
| `unlinked_request` | `approval_requests` row where thread does not link back | Row with `kind = 'unlinked_request'` | Accurately classified | **PASS** |
| `missing_link` | Awaiting thread with `package_version` and `approval_request_id IS NULL` | Row with `kind = 'missing_link'` | Accurately classified | **PASS** |
| `legacy_unlinked` | Pre-0003 unlinked non-awaiting threads | Row with `kind = 'legacy_unlinked'` | Accurately classified | **PASS** |

---

## 3. Test Execution Verification

1. **Mandatory Test Suite Command**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_security.py
   ```
   - **Result**: `38 passed in 40.23s`, exit code `0`.

2. **Adversarial Challenge Test Suite**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_m1_adversarial_challenger.py
   ```
   - **Result**: `21 passed in 24.67s`, exit code `0`.

---

## 4. Final Verdict

**Verdict**: **APPROVE**  
Milestone 1 Phase 1 implementation demonstrates comprehensive, genuine defense-in-depth across database triggers, constraint triggers, application-layer authorization, and audit infrastructure.
