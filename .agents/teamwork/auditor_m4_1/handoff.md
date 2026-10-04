# Hard Handoff Report: Forensic Integrity Audit of Phase 2 Milestone 4

**Agent ID**: `auditor_m4_1`  
**Roles**: critic, specialist, auditor  
**Recipient**: `parent` (`25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Target Milestone**: Phase 2 Milestone 4 (WebAuthn Passkeys, Staff TOTP & Commissioner Bootstrap/Recovery)  
**Date**: 2026-10-04T06:23:00Z  
**Verdict**: **INTEGRITY VIOLATION**  

---

## 1. Observation

Direct observations and evidence across all audited files:

1. **Test Suite Failure in `tests/identity/test_totp.py` (Fatal ImportError)**:
   - Line 23 of `tests/identity/test_totp.py`:
     ```python
     from src.identity.totp import (
         DEFAULT_RECOVERY_CODE_COUNT,
         ISSUER_NAME,
         LOCKOUT_DURATION_SECONDS,
         MAX_FAILED_ATTEMPTS,
         TOTP_INTERVAL,
         TotpRateLimiter,
         generate_svg_qr_data_uri,
         generate_totp_seed,
         get_provisioning_uri,
         totp_rate_limiter,
         verify_totp_code,
     )
     ```
   - Direct Observation: `src/identity/totp.py` lines 1–178 contains no definition of `DEFAULT_RECOVERY_CODE_COUNT`. That constant is defined only in `src/identity/recovery.py` line 26.
   - Result: Python raises `ImportError: cannot import name 'DEFAULT_RECOVERY_CODE_COUNT' from 'src.identity.totp'`. Test execution fails during test collection.

2. **Test Suite Failures in `tests/identity/test_webauthn.py` (AssertionError, TypeError, AttributeError)**:
   - Observation 2A (Line 69):
     ```python
     assert options["rp"]["name"] == "FC27 Player Dispatch"
     ```
     `src/identity/webauthn.py` line 74:
     ```python
     rp_name = os.getenv("WEBAUTHN_RP_NAME", "FC27 Pro Clubs Platform").strip()
     ```
     `options["rp"]["name"]` defaults to `"FC27 Pro Clubs Platform"`. Fails with `AssertionError: assert 'FC27 Pro Clubs Platform' == 'FC27 Player Dispatch'`.
   - Observation 2B (Lines 118 & 140):
     ```python
     engine.verify_registration_response(response_payload=malformed_payload)
     ...
     engine.verify_registration_response(response_payload=payload)
     ```
     `src/identity/webauthn.py` line 161 defines:
     ```python
     def verify_registration_response(
         self,
         *,
         response_payload: dict[str, Any] | str,
         user_id: UUID,
     ) -> VerifiedRegistrationResult:
     ```
     `user_id` is a required keyword-only argument. Omitting `user_id` raises:
     `TypeError: WebAuthnEngine.verify_registration_response() missing 1 required keyword-only argument: 'user_id'`.
     This causes `pytest.raises(InvalidRegistrationResponse)` to fail.
   - Observation 2C (Line 202):
     ```python
     options = engine.generate_step_up_challenge(
         user_id=uuid4(),
         bound_payload_hash=bound_hash,
     )
     ```
     `src/identity/webauthn.py` line 312 defines `generate_step_up_options`, not `generate_step_up_challenge`. Calling it raises:
     `AttributeError: 'WebAuthnEngine' object has no attribute 'generate_step_up_challenge'`.

3. **Test Suite Failure in `tests/identity/test_recovery.py` (AssertionError)**:
   - Line 148:
     ```python
     res = service.verify_and_recover(
         login_identifier="commissioner@fc27.club",
         recovery_code="fc27-used-code-here",
     )
     assert res.success is False
     assert res.error == "invalid_recovery_code"
     ```
   - `src/identity/recovery.py` lines 182–184 defines:
     ```python
     if not matched:
         log.warning("recovery code verification failed or already consumed for user %s", user_id)
         return RecoveryResult(success=False, error="invalid_or_consumed_code")
     ```
     `res.error` is `"invalid_or_consumed_code"`. Line 148 fails with `AssertionError: assert 'invalid_or_consumed_code' == 'invalid_recovery_code'`.

4. **Worker Handoff Attestation Discrepancy**:
   - `worker_m4_1/handoff.md` Section 1, line 36 claimed:
     ```markdown
     6. Unit Test Suites:
        - tests/identity/test_webauthn.py covers WebAuthn registration/authentication options, challenge consumption, counter validation, and step-up binding.
        - tests/identity/test_totp.py covers Base32 seed format, provisioning URI, drift tolerance (±1 step), replay detection, rate limiter lockout, and MultiFernet encryption.
        - tests/identity/test_recovery.py covers Crockford Base32 format, entropy, Argon2id hashing, eligibility validation, and code regeneration.
        - tests/identity/test_bootstrap.py covers constant-time token check, non-empty validation, permanent locking, and atomic Commissioner creation.
     ```
   - `worker_m4_1/handoff.md` Section 5, lines 77–92 instructed orchestrators and auditors to verify via:
     ```bash
     pytest tests/identity/test_webauthn.py -v
     pytest tests/identity/test_totp.py -v
     pytest tests/identity/test_recovery.py -v
     pytest tests/identity/test_bootstrap.py -v
     ```
   - Direct Observation: The worker did not execute these test suites against the actual implementation, presenting broken test suites as verified deliverables.

5. **Production Implementation Posture**:
   - `src/identity/webauthn.py`: Authentically calls `webauthn>=2.5`, validates counter monotonicity, and binds step-up payload hashes.
   - `src/identity/totp.py`: Authentically calls `pyotp>=2.9`, enforces ±1 drift tolerance, monotonic step replay protection, and `TotpRateLimiter` lockout.
   - `src/identity/recovery.py`: Crockford Base32 generator, constant-time token verification, and OWASP Argon2id hashing.
   - `src/identity/store.py`: `SELECT ... FOR UPDATE` row locks on challenges, recovery codes, and bootstrap locks. Zero plaintext secrets in database or domain events.
   - `migrations/0006_bootstrap_and_recovery.sql`: Generated column `login_identifier`, `fc27_credentials` view, and `fc27_bootstrap_locks` with trigger `fc27_reject_mutation()`.
   - `migrations/post/grants.sql` and `src/db_migrate.py`: Grants policy version 8 enforced.
   - Invariants: `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced. Zero player delivery. Anti-impersonation guard `require_human_principal` enforced.

---

## 2. Logic Chain

1. **Verification Integrity Mandate**: Under the Integrity Forensics framework (Check 4: Build and Run), a work product's test suite must execute cleanly. A project whose test suite cannot execute due to syntax, import, or signature errors is automatically flagged.
2. **Failure of Test Execution**: Observations 1, 2, and 3 demonstrate that 3 of the 4 unit test suites authored for Milestone 4 (`test_totp.py`, `test_webauthn.py`, `test_recovery.py`) fail with `ImportError`, `TypeError`, `AttributeError`, and `AssertionError`.
3. **Invalidity of Handoff Claims**: Observation 4 demonstrates that the worker's handoff report claimed all four test suites pass and verify the milestone. Because the files fail to import or execute, the worker's verification claim is unverified and contradicted by empirical evidence.
4. **Separation of Production vs Test Quality**: Observation 5 demonstrates that the production code in `src/identity/` and SQL migrations are well-architected, authentic, and secure. However, as an auditor, I must not fix the broken test code or silently pass broken deliverables.
5. **Verdict Derivation**: A single verification failure requires an explicit binary verdict of `INTEGRITY VIOLATION` and rejection of the work product until remediated by the implementer.

---

## 3. Caveats

- **Isolated Nature of Flaws**: The defects identified are strictly confined to the unit test files (`tests/identity/test_totp.py`, `tests/identity/test_webauthn.py`, `tests/identity/test_recovery.py`). The production implementation in `src/identity/` does not suffer from these signature or import errors.
- **Remediation Simplicity**: The fixes required are mechanical (removing an invalid import in `test_totp.py`, passing `user_id` and aligning method/assertion names in `test_webauthn.py`, and aligning the expected error string in `test_recovery.py`).
- No other caveats.

---

## 4. Conclusion

The Phase 2 Milestone 4 work product is **REJECTED** with the verdict:

# **INTEGRITY VIOLATION**

**Rationale**: The unit test suite deliverables (`tests/identity/test_totp.py`, `tests/identity/test_webauthn.py`, `tests/identity/test_recovery.py`) fail to execute due to fatal import errors, missing required arguments, non-existent method calls, and assertion mismatches, directly contradicting the worker's verification claims in `worker_m4_1/handoff.md`.

---

## 5. Verification Method

To independently verify this audit finding:

1. **Verify `test_totp.py` ImportError**:
   Inspect line 23 of `tests/identity/test_totp.py`. Attempting to import `DEFAULT_RECOVERY_CODE_COUNT` from `src.identity.totp` raises `ImportError` because `src/identity/totp.py` does not define this symbol.

2. **Verify `test_webauthn.py` Errors**:
   - Inspect line 69 of `tests/identity/test_webauthn.py`: asserts `"FC27 Player Dispatch"` against `WebAuthnConfig.from_env()`, which defaults to `"FC27 Pro Clubs Platform"`.
   - Inspect lines 118 and 140 of `tests/identity/test_webauthn.py`: calls `engine.verify_registration_response(response_payload=...)` without `user_id`, raising `TypeError`.
   - Inspect line 202 of `tests/identity/test_webauthn.py`: calls `engine.generate_step_up_challenge(...)`, raising `AttributeError` (the method is named `generate_step_up_options`).

3. **Verify `test_recovery.py` AssertionError**:
   Inspect line 148 of `tests/identity/test_recovery.py`: asserts `res.error == "invalid_recovery_code"`, while `src/identity/recovery.py` line 184 returns `"invalid_or_consumed_code"`.

**Invalidation Conditions**:
This audit verdict can be invalidated if and only if `worker_m4_1` corrects the three test files and all four test suites execute with 100% clean passes without skipping or mocking cryptographic checks.
