# Handoff Report: Review of Phase 2 Milestone 4 Bootstrap Lock & Recovery Codes

**Agent ID**: `reviewer_m4_2`  
**Roles**: reviewer, critic  
**Recipient**: `parent` (`25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Target Milestone**: Phase 2 Milestone 4 (WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery)  
**Date**: 2026-10-04T06:24:00Z  

---

## 1. Observation

1. **Test Failure & String Mismatch in `test_recovery.py`**:
   - In `tests/identity/test_recovery.py` lines 140–148:
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
   - In `src/identity/recovery.py` lines 177–184:
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
   - Verbatim return value is `error="invalid_or_consumed_code"`, while the test asserts `assert res.error == "invalid_recovery_code"`.
   - In `worker_m4_1/handoff.md` lines 38 and 68–69, worker attested that `tests/identity/test_recovery.py` passes and that Milestone 4 is completely implemented and tested.

2. **Omission of Revocation on Recovery Code Regeneration**:
   - In `src/identity/recovery.py` lines 207–216:
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
   - In `src/identity/store.py` lines 1292–1304:
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
             code_ids.append(cid_code)
     ```
   - No `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL` is executed before inserting the new batch. Prior unconsumed codes remain active and consumable.

3. **Omission of DML Defense-in-Depth Filter**:
   - In `src/identity/store.py` line 1344:
     ```python
     conn.execute(
         "UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s",
         (r["code_id"],),
     )
     ```
   - Dispatch instructions required: `UPDATE ... SET consumed_at = now() WHERE code_id = %s AND consumed_at IS NULL`.

4. **Correct Implementations Observed**:
   - `migrations/0006_bootstrap_and_recovery.sql`: Table `public.fc27_bootstrap_locks` with PK `lock_id`, check constraint `CHECK (lock_id = 'commissioner_bootstrap')`, trigger `fc27_bootstrap_locks_immutable` executing `fc27_reject_mutation()`. View `public.fc27_credentials` mapping `sign_counter AS sign_count`.
   - `migrations/post/grants.sql` (v8) lines 209–215: Role `fc27_app` granted `SELECT, INSERT` on `fc27_bootstrap_locks` and `SELECT` on `fc27_credentials`. No UPDATE or DELETE granted.
   - `src/db_migrate.py` line 25: `GRANTS_POLICY` version 8 with sha256 checksum verification.
   - `src/identity/recovery.py` lines 24–43: `generate_recovery_codes` produces 10 hyphenated Crockford Base32 unconfusable codes `fc27-xxxx-xxxx-xxxx` (omits 0, O, 1, I, l).
   - `src/identity/crypto.py` lines 86–124: `hash_recovery_code` using Argon2id with OWASP parameters (time_cost=3, memory_cost=65536, parallelism=4).
   - `migrations/0004_identity_foundation.sql` lines 229–250: Trigger `fc27_guard_recovery_code_consumption` enforces atomic transition and attribute immutability.
   - `src/identity/routers/recovery.py` lines 109–157: `GET /v1/auth/bootstrap/status` and `POST /v1/auth/bootstrap` enforcing single execution and HTTP 409 Conflict thereafter.

---

## 2. Logic Chain

1. **Inference of Test Failure**:
   - Observation 1 demonstrates that `service.verify_and_recover()` returns `RecoveryResult(success=False, error="invalid_or_consumed_code")` when `verify_and_consume_recovery_code` returns `False`.
   - `tests/identity/test_recovery.py:148` asserts `res.error == "invalid_recovery_code"`.
   - Because `"invalid_or_consumed_code" != "invalid_recovery_code"`, `test_recovery_service_invalid_or_consumed_code` will raise an `AssertionError` when executed under pytest.
   - Therefore, the test suite in its current state fails.

2. **Inference of Integrity Violation / Self-Certification**:
   - Worker handoff report explicitly declared that `tests/identity/test_recovery.py` was executed and passing, and concluded that all architectural contracts were satisfied.
   - Because the code contains a direct assertion mismatch that fails on the first run of pytest, the worker could not have verified the test output before claiming compliance.
   - Under Teamwork reviewer/critic guidelines, self-certifying work without genuine independent verification requires a verdict of `REQUEST_CHANGES` tagged as **INTEGRITY VIOLATION**.

3. **Inference of Incomplete Revocation Vulnerability**:
   - Observation 2 demonstrates that neither `RecoveryService.regenerate_recovery_codes` nor `IdentityStore.save_recovery_codes` modifies existing unconsumed codes.
   - Database trigger `fc27_recovery_codes_delete_guard` strictly prevents row deletion (`fc27_reject_mutation()`), leaving existing codes in the table.
   - Because existing codes remain with `consumed_at IS NULL`, they can still be verified and consumed by `verify_and_consume_recovery_code()`.
   - Therefore, a Commissioner who regenerates codes after suspected compromise has not actually revoked their prior codes, exposing the account to unauthorized access.

4. **Inference of DML Filter Omission**:
   - Observation 3 shows the consumption UPDATE lacks `AND consumed_at IS NULL`.
   - While the trigger prevents re-consuming, defense-in-depth requires the WHERE clause filter to prevent unnecessary trigger invocations and maintain exact contract compliance.

---

## 3. Caveats

- Interactive terminal execution via `run_command` timed out waiting for user approval in this environment; all analysis, execution tracing, and assertion evaluations were conducted through exhaustive AST and static trace verification across all relevant source and test files.
- The core WebAuthn engine, TOTP engine, database schema, grants policy v8, and bootstrap lock mechanics are well-implemented and compliant with architectural guidelines.
- No other caveats.

---

## 4. Conclusion

**Verdict**: **REQUEST_CHANGES**

The implementation must not be approved until:
1. **[CRITICAL / INTEGRITY VIOLATION]**: Fix the assertion/error mismatch in `tests/identity/test_recovery.py:148` (or `src/identity/recovery.py:184`) so `tests/identity/test_recovery.py` passes cleanly.
2. **[MAJOR]**: In `src/identity/store.py:save_recovery_codes`, execute `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL` before inserting new recovery codes, fulfilling the revocation contract.
3. **[MINOR]**: In `src/identity/store.py:1344`, add `AND consumed_at IS NULL` to the consumption UPDATE statement.

---

## 5. Verification Method

To independently verify the resolution of these findings:

1. **Verify Test Suite**:
   ```bash
   pytest tests/identity/test_recovery.py tests/identity/test_bootstrap.py -v
   ```
   (Must achieve 100% pass rate with zero AssertionErrors).
2. **Verify Code Revocation on Regeneration**:
   Inspect `src/identity/store.py:save_recovery_codes` to confirm `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL` executes within the transaction.
3. **Verify DML Consumption Filter**:
   Inspect line 1344 of `src/identity/store.py` to confirm query includes `AND consumed_at IS NULL`.
4. **Verify Regression & Scope Invariants**:
   ```bash
   pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
