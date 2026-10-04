# Hard Handoff Report: Phase 2 Milestone 4 Adversarial Stress Verification

**Agent ID**: `challenger_m4_1`  
**Role**: Passkey Anti-Cloning, Challenge Lifecycle & TOTP Adversarial Challenger  
**Milestone**: Phase 2 Milestone 4 (WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery)  
**Date**: 2026-10-04T06:24:00Z  
**Verdict**: **REQUEST_CHANGES**  

---

## 1. Observation

Direct observations and evidence across the repository:

1. **Test Suite Collection Failure in `tests/identity/test_totp.py`**:
   - `tests/identity/test_totp.py` lines 22–34:
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
   - In `src/identity/totp.py`, `DEFAULT_RECOVERY_CODE_COUNT` does not exist (it is defined in `src/identity/recovery.py:26`).
   - `DEFAULT_RECOVERY_CODE_COUNT` is never used in `tests/identity/test_totp.py`.
   - Result: Running `pytest tests/identity/test_totp.py` crashes on collection with `ImportError: cannot import name 'DEFAULT_RECOVERY_CODE_COUNT' from 'src.identity.totp'`.

2. **Broken Unit Tests in `tests/identity/test_webauthn.py`**:
   - Line 70 in `test_generate_registration_options`:
     ```python
     assert options["rp"]["name"] == "FC27 Player Dispatch"
     ```
     `WebAuthnConfig.from_env()` in `src/identity/webauthn.py:74` sets the default RP name to `"FC27 Pro Clubs Platform"`. Unless `WEBAUTHN_RP_NAME` is set in the environment, this test fails with `AssertionError`.
   - Lines 118 & 140 in `test_verify_registration_missing_challenge_rejected` and `test_verify_registration_consumed_challenge_rejected`:
     ```python
     engine.verify_registration_response(response_payload=malformed_payload)
     ```
     `WebAuthnEngine.verify_registration_response` in `src/identity/webauthn.py:161-166` defines `user_id: UUID` as a required keyword-only argument. Omitting `user_id` causes `TypeError: WebAuthnEngine.verify_registration_response() missing 1 required keyword-only argument: 'user_id'`. The test fails because `pytest.raises(InvalidRegistrationResponse)` expects an `InvalidRegistrationResponse`, not a `TypeError`.
   - Line 202 in `test_step_up_challenge_binding`:
     ```python
     options = engine.generate_step_up_challenge(user_id=uuid4(), bound_payload_hash=bound_hash)
     ```
     `WebAuthnEngine` has no method named `generate_step_up_challenge`. The actual method in `src/identity/webauthn.py:312` is `generate_step_up_options`. This test fails with `AttributeError: 'WebAuthnEngine' object has no attribute 'generate_step_up_challenge'`.
   - Line 147 in `test_verify_authentication_counter_clone_detected`:
     Passes dummy zero-byte payload to `_webauthn_verify_auth_response`. The test passes only because the crypto check fails on invalid binary data, not because counter clone detection was exercised.

3. **Cross-User Challenge Mismatch Omission in `WebAuthnEngine`**:
   - In `src/identity/webauthn.py:180-181` (`verify_registration_response`):
     ```python
     if challenge_row.get("user_id") and challenge_row["user_id"] != user_id:
         raise InvalidRegistrationResponse("challenge not issued to this user")
     ```
     Cross-user mismatch is correctly checked and fails closed.
   - In `src/identity/webauthn.py:273-280` (`verify_authentication_response`) and lines 360–366 (`verify_step_up_response`):
     ```python
     challenge_row = self.store.consume_challenge(...)
     if not challenge_row:
         raise InvalidAuthenticationResponse(...)
     ```
     Neither method verifies `challenge_row.get("user_id")` against the stored credential's `user_id`! A challenge issued targeted for User A can be consumed by User B presenting their own passkey, violating cross-user challenge isolation.

4. **Passkey Anti-Cloning & Database Counter Guard**:
   - In `src/identity/store.py:1717`:
     `if row["sign_counter"] > 0 and new_counter <= row["sign_counter"]: raise IdentityStoreError(401, "authenticator sign counter replay or clone detected")`
   - In `src/identity/webauthn.py:298`:
     `if current_counter > 0 and verified.new_sign_count <= current_counter: raise InvalidAuthenticationResponse("authenticator clone or replay attack detected: counter did not increase")`
   - In `migrations/0004_identity_foundation.sql:174-191`:
     PostgreSQL trigger function `public.fc27_guard_webauthn_counter()` enforces `IF NEW.sign_counter < OLD.sign_counter THEN RAISE check_violation ... END IF;` attached via `BEFORE UPDATE ON public.fc27_webauthn_credentials`.

5. **Staff TOTP Invariants**:
   - In `src/identity/totp.py:173`: `if last_step is not None and matched_step <= last_step: return False, None, "replay_detected"` rejects same-window and older-window reuse.
   - In `src/identity/routers/totp.py:223-231`: returns HTTP 400 with detail `"TOTP code already used in this time window"` or `"invalid or expired TOTP code"`.
   - `TotpRateLimiter` enforces 5 failed attempts lockout for 300 seconds; `totp_router` raises HTTP 429 Too Many Requests with `Retry-After: <seconds>` header.

6. **Anti-Impersonation Invariant**:
   - In `src/identity/deps.py:199-204`: `require_human_principal` raises `AgentImpersonationError` when `principal.is_service` is True.
   - `src/graph_api.py:371-376` registers `AuthorizationError` handler returning HTTP 403 Forbidden with `{"detail": exc.detail, "error_code": "AGENT_IMPERSONATION_FORBIDDEN"}`.
   - In `src/identity/policy.py:588-604`: `UserRole.BFF_SERVICE` and `UserRole.GRAPH_SERVICE` have no permissions for any `PASSKEY_*` or `TOTP_*` actions.

7. **Scope and Packaging Invariants**:
   - `pyproject.toml` dependencies match `requirements.txt`.
   - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain strictly enforced.

---

## 2. Logic Chain

1. **Step 1: Test Suite Operability**:
   - Verification tests supplied with a milestone must be syntactically valid, importable, and passing.
   - From Observation 1, `tests/identity/test_totp.py` imports a non-existent name from `src.identity.totp`, causing `pytest` to fail during collection.
   - From Observation 2, `tests/identity/test_webauthn.py` has 4 broken test functions with `TypeError`, `AttributeError`, and `AssertionError`.
   - Conclusion from Step 1: Milestone 4 test deliverables are broken and cannot pass the required gate:
     `pytest tests/identity/test_webauthn.py tests/identity/test_totp.py -v`.

2. **Step 2: Cross-User Challenge Isolation**:
   - The dispatch brief requires: *"Verify cross-user challenge mismatch fails closed."*
   - From Observation 3, `verify_authentication_response` and `verify_step_up_response` in `src/identity/webauthn.py` omit checking `challenge_row.get("user_id")` against `stored_credential["user_id"]`.
   - Conclusion from Step 2: Cross-user challenge mismatch fails open in authentication and step-up assertion paths.

3. **Step 3: Verification of Sound Subsystems**:
   - From Observations 4, 5, 6, and 7, the core mechanics for anti-cloning counter monotonicity, single-use challenge consumption, TOTP drift/replay/rate limiting, and service token anti-impersonation (HTTP 403) are correctly implemented.
   - I have written a dedicated, comprehensive adversarial test suite in `tests/identity/test_adversarial_m4_1.py` covering all these invariants.

4. **Step 4: Milestone Verdict**:
   - Because of the broken test suites and the cross-user challenge isolation gap, Phase 2 Milestone 4 cannot be approved as-is.
   - Explicit Verdict: **REQUEST_CHANGES**.

---

## 3. Caveats

- **Mocked WebAuthn Crypto**: End-to-end attestation responses require simulated CBOR/COSE bytes. Virtual authenticator simulations were used in unit tests rather than physical FIDO2 USB hardware dongles.
- **In-Memory Rate Limiting**: `TotpRateLimiter` operates in-memory (singleton per worker process). Distributed Redis rate limiting is deferred to Phase 3.
- No other caveats.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

Worker `worker_m4_1` must address the following 3 items before Phase 2 Milestone 4 can be approved:

1. **Fix `tests/identity/test_totp.py`**:
   - Remove unused import `DEFAULT_RECOVERY_CODE_COUNT` from `src.identity.totp` (line 23).
2. **Fix `tests/identity/test_webauthn.py`**:
   - In `test_generate_registration_options` (line 70): assert against `engine.config.rp_name` instead of hardcoded `"FC27 Player Dispatch"`.
   - In lines 118 & 140: pass required `user_id=uuid4()` to `engine.verify_registration_response`.
   - In line 202: change `engine.generate_step_up_challenge` to `engine.generate_step_up_options`.
   - In line 147: mock `_webauthn_verify_auth_response` to test genuine sign counter monotonicity.
3. **Fix Cross-User Challenge Isolation in `src/identity/webauthn.py`**:
   - In `verify_authentication_response`: check `if challenge_row.get("user_id") and challenge_row["user_id"] != stored_credential.get("user_id"): raise InvalidAuthenticationResponse("challenge not issued to this user")`.
   - In `verify_step_up_response`: check `if challenge_row.get("user_id") and challenge_row["user_id"] != stored_credential.get("user_id"): raise InvalidAuthenticationResponse("step-up challenge not issued to this user")`.

---

## 5. Verification Method

To independently verify all findings and test suites:

1. **Verify Broken Worker Test Suites (demonstrating the defects)**:
   ```bash
   pytest tests/identity/test_totp.py -v
   # (Observe collection failure: ImportError: cannot import name 'DEFAULT_RECOVERY_CODE_COUNT' from 'src.identity.totp')
   pytest tests/identity/test_webauthn.py -v
   # (Observe 4 test failures: TypeError, AttributeError, AssertionError)
   ```

2. **Run Challenger's Comprehensive Adversarial Suite**:
   ```bash
   pytest tests/identity/test_adversarial_m4_1.py -v
   ```
   (All 15 adversarial tests verify anti-cloning, single-use, TOTP replay/drift, lockout, anti-impersonation, and packaging invariants).

3. **Verify Scope and Packaging Invariants**:
   ```bash
   pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   ```

4. **Verify Dormancy Invariant**:
   ```bash
   git grep "DISPATCH_ENABLED" src/
   ```
   (Must confirm `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain intact).
