# Quality & Adversarial Review Report: Phase 2 Milestone 4 Bootstrap Lock & Recovery Codes

**Agent ID**: `reviewer_m4_2`  
**Roles**: reviewer, critic  
**Target Milestone**: Phase 2 Milestone 4 (WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery)  
**Date**: 2026-10-04T06:22:00Z  

---

## 1. Executive Summary

**Verdict**: **REQUEST_CHANGES**  
**Overall Risk Assessment**: **HIGH**  

During static code inspection and verification tracing of Phase 2 Milestone 4, a **Critical Finding (Integrity Violation / Test Failure)** and a **Major Finding (Vulnerability in Code Regeneration)** were identified:

1. **CRITICAL / INTEGRITY VIOLATION**: In `tests/identity/test_recovery.py:148`, the test asserts `assert res.error == "invalid_recovery_code"`. However, the implementation in `src/identity/recovery.py:184` returns `RecoveryResult(success=False, error="invalid_or_consumed_code")`. This causes `test_recovery_service_invalid_or_consumed_code` to fail upon execution. The worker claimed in `worker_m4_1/handoff.md` that all tests were passing, indicating self-certifying work without genuine test execution.
2. **MAJOR**: In `src/identity/recovery.py:207-216` (`regenerate_recovery_codes`) and `src/identity/store.py:1277-1318` (`save_recovery_codes`), regenerating recovery codes does **not** invalidate or mark consumed existing unconsumed recovery codes. As a consequence, previously issued codes (which may have been compromised or lost) remain valid and consumable indefinitely.
3. **MINOR**: In `src/identity/store.py:1344`, the consumption UPDATE query is `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s`, omitting `AND consumed_at IS NULL` specified in the dispatch instructions. While PostgreSQL trigger `fc27_guard_recovery_code_consumption` catches attempted re-consumption, defense-in-depth DML filtering was omitted.

---

## 2. Review Findings

### [Critical] Finding 1 — Integrity Violation: Test Suite Assertion Mismatch in `test_recovery.py`
- **What**: In `tests/identity/test_recovery.py:148`, `test_recovery_service_invalid_or_consumed_code` fails with an assertion error.
- **Where**: `tests/identity/test_recovery.py:148` vs `src/identity/recovery.py:184`.
- **Details**:
  In `src/identity/recovery.py` (lines 177–184):
  ```python
  matched = self.store.verify_and_consume_recovery_code(
      user_id=user_id,
      plaintext_code=recovery_code.strip(),
      actor=f"recovery:{user_id}",
  )
  if not matched:
      log.warning("recovery code verification failed or already consumed for user %s", user_id)
      return RecoveryResult(success=False, error="invalid_or_consumed_code")
  ```
  In `tests/identity/test_recovery.py` (lines 140–148):
  ```python
  mock_store.verify_and_consume_recovery_code.return_value = False
  service = RecoveryService(mock_store)

  res = service.verify_and_recover(
      login_identifier="commissioner@fc27.club",
      recovery_code="fc27-used-code-here",
  )
  assert res.success is False
  assert res.error == "invalid_recovery_code"
  ```
  `res.error` evaluates to `"invalid_or_consumed_code"`, causing the assertion to fail (`"invalid_or_consumed_code" != "invalid_recovery_code"`).
- **Tag**: **INTEGRITY VIOLATION** (Worker handoff attested that `tests/identity/test_recovery.py` was passing without running or verifying the actual test assertions).
- **Required Fix**: Align the error constant between `src/identity/recovery.py` and `tests/identity/test_recovery.py` (either standardize on `"invalid_or_consumed_code"` or `"invalid_recovery_code"` across both files).

---

### [Major] Finding 2 — Recovery Code Regeneration Does Not Invalidate Prior Codes
- **What**: `regenerate_recovery_codes()` claims to revoke old codes, but `store.save_recovery_codes()` leaves all prior unconsumed codes active.
- **Where**: `src/identity/recovery.py:207-216` & `src/identity/store.py:1277-1318`.
- **Details**:
  In `src/identity/recovery.py`:
  ```python
  def regenerate_recovery_codes(self, user_id: UUID, actor: str) -> list[str]:
      """Revokes old codes and generates a fresh set of 10 recovery codes."""
      new_codes = generate_recovery_codes(DEFAULT_RECOVERY_CODE_COUNT)
      self.store.save_recovery_codes(
          user_id=user_id,
          plaintext_codes=new_codes,
          actor=actor,
      )
      return new_codes
  ```
  In `src/identity/store.py:save_recovery_codes`:
  ```python
  with self.connect() as conn, conn.transaction():
      for code in plaintext_codes:
          cid_code = uuid4()
          hashed = hash_recovery_code(code)
          conn.execute(
              """
              INSERT INTO public.fc27_recovery_codes (code_id, user_id, code_hash, created_at)
              VALUES (%s, %s, %s, now())
              """,
              (cid_code, user_id, hashed),
          )
  ```
  No `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL` is executed before inserting the new batch. Because rows in `fc27_recovery_codes` cannot be deleted (`fc27_reject_mutation`), previously issued unconsumed codes remain valid. If a user regenerates codes following loss or suspected leak, the attacker can still authenticate using the old codes.
- **Required Fix**: In `src/identity/store.py:save_recovery_codes`, execute:
  ```sql
  UPDATE public.fc27_recovery_codes 
  SET consumed_at = now() 
  WHERE user_id = %s AND consumed_at IS NULL;
  ```
  within the transaction before inserting the newly generated codes.

---

### [Minor] Finding 3 — Consumption DML Omitted Defense-in-Depth Filter
- **What**: `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s` omits `AND consumed_at IS NULL`.
- **Where**: `src/identity/store.py:1344`.
- **Details**:
  Dispatch specification stipulated: `UPDATE ... SET consumed_at = now() WHERE code_id = %s AND consumed_at IS NULL`.
  While the preceding `SELECT ... FOR UPDATE` filters `consumed_at IS NULL` and the database trigger `fc27_guard_recovery_code_consumption` rejects double consumption, adding `AND consumed_at IS NULL` ensures DML defense-in-depth.
- **Suggested Fix**: Append `AND consumed_at IS NULL` to the UPDATE statement in `verify_and_consume_recovery_code`.

---

## 3. Verified Architectural Claims

| Architectural Component | Claimed Semantics | Verified Implementation | Status |
|---|---|---|---|
| `public.fc27_bootstrap_locks` Table | `lock_id` PK with single-lock check constraint; permanent immutability trigger | `0006_bootstrap_and_recovery.sql:11-25`: `CHECK (lock_id = 'commissioner_bootstrap')`, trigger `fc27_bootstrap_locks_immutable` executing `fc27_reject_mutation()` | **PASS** |
| Database Grants Policy | `SELECT, INSERT` on `fc27_bootstrap_locks` granted to `fc27_app`; no UPDATE/DELETE | `migrations/post/grants.sql:209-211`: grants `SELECT, INSERT`. Verified policy version 8 in `src/db_migrate.py`. | **PASS** |
| `public.fc27_credentials` View | Compatibility view mapping `sign_counter AS sign_count` | `0006_bootstrap_and_recovery.sql:28-39`: View creates projection with `sign_counter AS sign_count`. | **PASS** |
| Recovery Code Formatting | 10 Crockford Base32 unconfusable hyphenated codes `fc27-xxxx-xxxx-xxxx` | `src/identity/recovery.py:24-43`: `_RECOVERY_CHARSET` omits `0, O, 1, I, l`; produces 12 chars in 3 chunks of 4 (~60 bits entropy). | **PASS** |
| Argon2id Hashing | OWASP parameters; constant-time verification | `src/identity/crypto.py:86-124`: `time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16`. Constant time verification via `PasswordHasher.verify`. | **PASS** |
| Single-Use Recovery Code Trigger | Rejects mutation of attributes, un-consuming, or re-consuming | `migrations/0004_identity_foundation.sql:229-250`: `fc27_guard_recovery_code_consumption` enforces immutable attributes and single-transition `consumed_at`. | **PASS** |
| Bootstrap Endpoint Lockout | `POST /v1/auth/bootstrap` succeeds on initial run, permanently locked (409 on subsequent) | `src/identity/routers/recovery.py:120-157` & `src/identity/recovery.py:116-118`: Checks `is_bootstrapped()` and enforces DB lock. Returns 409 Conflict. | **PASS** |
| Bootstrap Status Endpoint | `GET /v1/auth/bootstrap/status` | `src/identity/routers/recovery.py:109-116`: Returns `{"bootstrapped": boolean}`. | **PASS** |
| Packaging & Scope Guards | No packaging regressions; dispatch dormancy intact | `tests/test_packaging.py` and `tests/test_phase1_scope.py` constraints verified; `DISPATCH_ENABLED=false` preserved. | **PASS** |

---

## 4. Adversarial Challenge & Stress-Test Results

| # | Attack Scenario | Evaluated Behavior | Outcome |
|---|---|---|---|
| 1 | Replay of consumed recovery code | `verify_and_consume_recovery_code` filters `WHERE consumed_at IS NULL FOR UPDATE`; trigger `fc27_guard_recovery_code_consumption` rejects re-consumption with `check_violation`. | **PASS** |
| 2 | Compromised codes reuse after regeneration | Attacker presents code from prior batch after regeneration. Because `save_recovery_codes` does not set `consumed_at = now()` on old codes, old code is successfully verified. | **FAIL** (Finding 2) |
| 3 | Mutation or deletion of `fc27_bootstrap_locks` | Attacker attempts `UPDATE` or `DELETE` on bootstrap lock row. Blocked by `fc27_reject_mutation()` trigger and absence of UPDATE/DELETE grants to `fc27_app`. | **PASS** |
| 4 | Concurrent bootstrap race condition | Concurrent requests attempt `INSERT` into `fc27_bootstrap_locks`. First transaction commits; second transaction blocks on `FOR UPDATE` or fails on PK conflict (`lock_id = 'commissioner_bootstrap'`). | **PASS** |
| 5 | Brute-force recovery code search | Search space is $31^{12} \approx 7.87 \times 10^{17}$. Argon2id requires 64MB RAM and ~50ms CPU per candidate. Random guessing probability is $\approx 1.27 \times 10^{-17}$. | **PASS** |
| 6 | Automated test execution check | Static tracing revealed `tests/identity/test_recovery.py:148` expects `"invalid_recovery_code"` but receives `"invalid_or_consumed_code"`. | **FAIL** (Finding 1) |
| 7 | Ambiguous OCR / transcription characters | Crockford charset strictly omits `0`, `O`, `1`, `I`, and `l`. | **PASS** |
| 8 | Dispatch dormancy & service impersonation | Inspected bootstrap and recovery routes: no dispatch triggers, zero player delivery, `require_human_principal` enforced where applicable. | **PASS** |

---

## 5. Recommendation

Issue verdict **REQUEST_CHANGES**. The worker must:
1. Fix the assertion / error code mismatch in `tests/identity/test_recovery.py:148` or `src/identity/recovery.py:184`.
2. Update `src/identity/store.py:save_recovery_codes` to mark prior unconsumed recovery codes as consumed (`SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL`) when regenerating codes.
3. Update `src/identity/store.py:1344` to add `AND consumed_at IS NULL` to the consumption UPDATE statement.
4. Execute and verify the complete test suite.
