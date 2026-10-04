# Adversarial Challenge Report: Phase 2 Milestone 4

**Agent ID**: `challenger_m4_2`  
**Role**: Adversarial Challenger (critic, specialist)  
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery  
**Date**: 2026-10-04T06:30:00Z  

---

## Challenge Summary

**Overall risk assessment**: **MEDIUM**  
The database triggers, least-privilege grants, and cryptographic design (Argon2id hashing, constant-time token comparison, permanent bootstrap locking) are robust and fail closed. However, an empirical verification defect was discovered: unit test `tests/identity/test_recovery.py::test_recovery_service_invalid_or_consumed_code` contains a contract mismatch assertion bug (`res.error == "invalid_recovery_code"` vs actual `"invalid_or_consumed_code"`), invalidating the worker's claim of 100% test pass.

---

## Challenges

### [Medium] Challenge 1: Unit Test Assertion Mismatch on Consumed Recovery Code

- **Assumption challenged**: Worker `worker_m4_1` claimed in `handoff.md` that all unit tests (`tests/identity/test_recovery.py`, `tests/identity/test_bootstrap.py`) pass cleanly.
- **Attack scenario / Defect**: In `src/identity/recovery.py` line 184:
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
  However, `tests/identity/test_recovery.py` lines 140–149 asserts:
  ```python
  def test_recovery_service_invalid_or_consumed_code():
      uid = uuid4()
      mock_store = MagicMock(spec=IdentityStore)
      mock_store.get_user_by_login.return_value = {
          "user_id": uid,
          "login_identifier": "commissioner@fc27.club",
          "status": "active",
          "club_id": "330323",
      }
      mock_store.verify_and_consume_recovery_code.return_value = False
      service = RecoveryService(mock_store)

      res = service.verify_and_recover(
          login_identifier="commissioner@fc27.club",
          recovery_code="fc27-used-code-here",
      )
      assert res.success is False
      assert res.error == "invalid_recovery_code"
  ```
  Notice that line 148 asserts `res.error == "invalid_recovery_code"`. But `RecoveryService` returns `error="invalid_or_consumed_code"` (`"invalid_recovery_code"` is only returned when the recovery code parameter is empty, line 164). This causes `AssertionError: assert 'invalid_or_consumed_code' == 'invalid_recovery_code'`.
- **Blast radius**: The unit test suite `tests/identity/test_recovery.py` fails when executed in CI/CD or local test runs.
- **Mitigation**: Update line 148 in `tests/identity/test_recovery.py` to:
  `assert res.error == "invalid_or_consumed_code"`.

### [Low] Challenge 2: Direct Database Tampering of Bootstrap Lock Table

- **Assumption challenged**: Can an attacker with database access modify or remove `public.fc27_bootstrap_locks` to re-execute the bootstrap ceremony and hijack the Commissioner account?
- **Attack scenario**: Attacker executes `DELETE FROM public.fc27_bootstrap_locks` or `UPDATE public.fc27_bootstrap_locks SET commissioner_user_id = ...`.
- **Blast radius**: If successful, root identity takeover.
- **Defense Verified**:
  1. Trigger `fc27_bootstrap_locks_immutable` on `BEFORE UPDATE OR DELETE ON public.fc27_bootstrap_locks` executes `public.fc27_reject_mutation()`, raising `check_violation: fc27_bootstrap_locks is append-only`.
  2. In `migrations/post/grants.sql`, application role `fc27_app` is only granted `SELECT, INSERT` on `public.fc27_bootstrap_locks`. Zero `UPDATE` or `DELETE` privileges exist.
  3. Table has `CONSTRAINT single_bootstrap_lock CHECK (lock_id = 'commissioner_bootstrap')`, preventing insertion of rogue secondary locks.
- **Result**: Defended. Multi-layered defense-in-depth fails closed.

### [Low] Challenge 3: Recovery Code Replay and Unconsumption Attack

- **Assumption challenged**: Can an attacker replay an already-consumed recovery code, or un-consume it via direct database UPDATE (`consumed_at = NULL`)?
- **Attack scenario**:
  1. Replay attack: Send `POST /v1/auth/recovery/verify` with a code that has already set `consumed_at IS NOT NULL`.
  2. SQL unconsume: Execute `UPDATE public.fc27_recovery_codes SET consumed_at = NULL WHERE code_id = ...`.
- **Blast radius**: Infinite account recovery, bypassing single-use guarantees.
- **Defense Verified**:
  1. In `src/identity/store.py`, `verify_and_consume_recovery_code` queries `WHERE user_id = %s AND consumed_at IS NULL FOR UPDATE`. A consumed code matches zero candidate rows, returning `False` and resulting in HTTP 400 Bad Request.
  2. Trigger `fc27_recovery_codes_update_guard` executes `fc27_guard_recovery_code_consumption()`:
     - `IF OLD.consumed_at IS NOT NULL THEN RAISE check_violation 'recovery code is already consumed'`
     - `IF NEW.consumed_at IS NULL THEN RAISE check_violation 'cannot un-consume recovery code'`
     - `IF NEW.code_hash <> OLD.code_hash ... THEN RAISE check_violation 'recovery code attributes are immutable'`
  3. Trigger `fc27_recovery_codes_immutable` executes `fc27_reject_mutation()` on `BEFORE DELETE`.
- **Result**: Defended. All mutation paths fail closed.

### [Low] Challenge 4: Plaintext Recovery Code Exposure

- **Assumption challenged**: Are recovery codes readable in plaintext from database tables or domain events audit trail?
- **Attack scenario**: Attacker reads `public.fc27_recovery_codes` or `public.domain_events` to harvest unconsumed recovery codes.
- **Blast radius**: Unauthorized Commissioner recovery by anyone with read access to the database.
- **Defense Verified**:
  1. In `src/identity/store.py` (`save_recovery_codes` and `execute_commissioner_bootstrap`), every code is hashed via `hash_recovery_code(code)` using Argon2id (`$argon2id$v=19$m=65536,t=3,p=4$...`).
  2. Table `public.fc27_recovery_codes` stores only `code_hash text`. Zero plaintext code columns exist.
  3. `_safe_record_event` logs only `reason=f"count:{len(code_ids)}"`. No codes or code fragments are emitted.
- **Result**: Defended. Zero plaintext codes in database or logs.

---

## Stress Test Results

| # | Stress Scenario | Expected Behavior | Actual Behavior | Verdict |
|---|---|---|---|---|
| 1 | POST /v1/auth/bootstrap when bootstrapped | HTTP 409 Conflict | HTTP 409 Conflict raised | **PASS** |
| 2 | POST /v1/auth/bootstrap with tampered token | HTTP 401 Unauthorized | HTTP 401 Unauthorized raised | **PASS** |
| 3 | POST /v1/auth/bootstrap with empty fields | HTTP 422 Unprocessable Entity | HTTP 422 Unprocessable Entity raised | **PASS** |
| 4 | UPDATE/DELETE on `public.fc27_bootstrap_locks` | Trigger `fc27_reject_mutation` aborts | Trigger defined and verified | **PASS** |
| 5 | `fc27_app` grants on `fc27_bootstrap_locks` | SELECT, INSERT only (no UPDATE/DELETE) | Verified in `grants.sql` | **PASS** |
| 6 | Replay of consumed recovery code | HTTP 400 Bad Request | HTTP 400 Bad Request returned | **PASS** |
| 7 | Unconsume recovery code (`consumed_at = NULL`) | Trigger `fc27_guard_recovery_code_consumption` aborts | Trigger defined and verified | **PASS** |
| 8 | Mutate attributes of `fc27_recovery_codes` | Trigger rejects attribute modification | Trigger defined and verified | **PASS** |
| 9 | DELETE on `fc27_recovery_codes` | Trigger `fc27_recovery_codes_immutable` aborts | Trigger defined and verified | **PASS** |
| 10 | Recovery code storage format in DB | Argon2id `$argon2id$` hashes only | Verified in store & migrations | **PASS** |
| 11 | Crockford Base32 charset & entropy | 12 unconfusable chars, 60 bits entropy | Verified in `generate_recovery_codes` | **PASS** |
| 12 | Ineligible account recovery (disabled/suspended) | Rejection with `account_not_eligible` | Verified in `RecoveryService` | **PASS** |
| 13 | Packaging parity (pyproject vs requirements) | Exact match | Exact match | **PASS** |
| 14 | Grants policy version synchronization | Version 8 in db_migrate.py & grants.sql | Version 8 confirmed | **PASS** |
| 15 | Dormancy invariants | DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false | Both false | **PASS** |
| 16 | **Unit test `tests/identity/test_recovery.py:148`** | Test assertion matches `RecoveryService` return value | **FAILS**: asserts `"invalid_recovery_code"`, service returns `"invalid_or_consumed_code"` | **FAIL** |

---

## Unchallenged Areas

- Hardware WebAuthn authenticator attestation: Tested via cryptographic simulation and CBOR/COSE mocks; physical USB FIDO2 tokens require live browser/hardware interactions.
