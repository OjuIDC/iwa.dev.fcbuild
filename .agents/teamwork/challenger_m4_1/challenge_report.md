# Adversarial Challenge Report: Phase 2 Milestone 4

**Agent ID**: `challenger_m4_1`  
**Role**: Passkey Anti-Cloning, Challenge Lifecycle & TOTP Adversarial Challenger  
**Milestone**: Phase 2 Milestone 4 (WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery)  
**Date**: 2026-10-04T06:23:00Z  

---

## Challenge Summary

**Overall risk assessment**: **HIGH**  
**Explicit Verdict**: **REQUEST_CHANGES**

While the core cryptographic foundations (RFC 6238 TOTP verification, monotonic counter triggers in PostgreSQL, and MultiFernet seed encryption) are sound in concept, the Milestone 4 deliverables suffer from **fatal test collection errors in `tests/identity/test_totp.py`**, **multiple broken unit tests in `tests/identity/test_webauthn.py`**, and a **security bypass gap in `WebAuthnEngine` where cross-user challenge isolation is not enforced during authentication and step-up ceremonies**.

---

## Challenges

### [Critical] Challenge 1: `tests/identity/test_totp.py` Import Failure Blocks Test Collection

- **Assumption challenged**: The test suite `tests/identity/test_totp.py` was claimed by worker_m4_1 to verify the TOTP engine and pass cleanly.
- **Attack scenario**: Running `pytest tests/identity/test_totp.py` immediately crashes before running any tests.
- **Root Cause**:
  `tests/identity/test_totp.py:23` executes:
  ```python
  from src.identity.totp import (
      DEFAULT_RECOVERY_CODE_COUNT,  # <--- NOT defined in src.identity.totp
      ...
  )
  ```
  `DEFAULT_RECOVERY_CODE_COUNT` is defined only in `src/identity/recovery.py`. It does not exist in `src/identity/totp.py`. Furthermore, `DEFAULT_RECOVERY_CODE_COUNT` is completely unused in `tests/identity/test_totp.py`.
- **Blast radius**: Continuous integration pipelines, pre-commit hooks, and test runs fail immediately with `ImportError`.
- **Mitigation**: Remove `DEFAULT_RECOVERY_CODE_COUNT` from imports in `tests/identity/test_totp.py:23`.

---

### [Critical] Challenge 2: Broken Unit Tests in `tests/identity/test_webauthn.py`

- **Assumption challenged**: `tests/identity/test_webauthn.py` covers registration/authentication options, challenge consumption, counter validation, and step-up binding.
- **Attack scenario**: Executing `pytest tests/identity/test_webauthn.py` fails on 4 distinct tests:
  1. **Line 70 (`test_generate_registration_options`)**:
     Asserts `options["rp"]["name"] == "FC27 Player Dispatch"`. However, `WebAuthnConfig.from_env()` defaults to `"FC27 Pro Clubs Platform"`, causing an immediate `AssertionError`.
  2. **Line 118 (`test_verify_registration_missing_challenge_rejected`)**:
     Calls `engine.verify_registration_response(response_payload=malformed_payload)`. `verify_registration_response` has a required keyword-only argument `user_id: UUID`. Omitting it causes `TypeError: WebAuthnEngine.verify_registration_response() missing 1 required keyword-only argument: 'user_id'`, failing `pytest.raises(InvalidRegistrationResponse)`.
  3. **Line 140 (`test_verify_registration_consumed_challenge_rejected`)**:
     Calls `engine.verify_registration_response(response_payload=payload)` without `user_id`, also crashing with `TypeError`.
  4. **Line 202 (`test_step_up_challenge_binding`)**:
     Calls `options = engine.generate_step_up_challenge(user_id=uuid4(), bound_payload_hash=bound_hash)`. `WebAuthnEngine` has no method named `generate_step_up_challenge` (it is named `generate_step_up_options` at line 312 of `src/identity/webauthn.py`). This raises `AttributeError: 'WebAuthnEngine' object has no attribute 'generate_step_up_challenge'`.
  5. **Line 147 (`test_verify_authentication_counter_clone_detected`)**:
     The test passes dummy zero bytes for `authenticatorData` and `signature`. `_webauthn_verify_auth_response` raises `InvalidAuthenticationResponse` due to cryptographic failure, NOT due to counter validation. The counter check was completely untested by the mock.
- **Blast radius**: The test suite fails execution. Regressions in WebAuthn registration, authentication, and step-up binding cannot be caught.
- **Mitigation**:
  - Update `test_generate_registration_options` to accept the configured RP name or pass an explicit `WebAuthnConfig`.
  - Pass `user_id=uuid4()` to `verify_registration_response` in lines 118 and 140.
  - Call `engine.generate_step_up_options()` in line 202.
  - Mock `_webauthn_verify_auth_response` in line 147 to return valid assertion with `new_sign_count <= current_counter` so that the monotonicity check itself is genuinely verified.

---

### [High] Challenge 3: Cross-User Challenge Mismatch Omitted in `verify_authentication_response` & `verify_step_up_response`

- **Assumption challenged**: All WebAuthn challenges enforce strict cross-user isolation.
- **Attack scenario**:
  1. Targeted user login: User A initiates WebAuthn login. The server issues a challenge in `fc27_challenges` with `user_id = User_A`.
  2. Attacker / User B intercepts or reads User A's challenge and submits a valid assertion for User B's passkey against User A's challenge.
  3. In `src/identity/webauthn.py:273-280`:
     ```python
     challenge_row = self.store.consume_challenge(
         challenge_str=challenge_str,
         challenge_type="webauthn_login",
     )
     if not challenge_row:
         raise InvalidAuthenticationResponse(...)
     ```
     Notice that `verify_authentication_response` **never checks** `challenge_row.get("user_id")` against `stored_credential["user_id"]`!
  4. User B successfully logs in and consumes User A's challenge, causing User A's login ceremony to fail and enabling cross-user challenge substitution.
  5. Similarly, in `verify_step_up_response` (lines 360-366), `challenge_row.get("user_id")` is never checked against `stored_credential["user_id"]`, allowing delegate or peer substitution of step-up challenges.
- **Blast radius**: Challenge substitution across user sessions; denial of service on legitimate user logins; violation of tenant isolation.
- **Mitigation**:
  In `verify_authentication_response`:
  ```python
  if challenge_row.get("user_id") and challenge_row["user_id"] != stored_credential.get("user_id"):
      raise InvalidAuthenticationResponse("challenge not issued to this user")
  ```
  In `verify_step_up_response`:
  ```python
  if challenge_row.get("user_id") and challenge_row["user_id"] != stored_credential.get("user_id"):
      raise InvalidAuthenticationResponse("step-up challenge not issued to this user")
  ```

---

### [Medium] Challenge 4: WebAuthn Trigger vs App Layer Monotonicity Boundary

- **Assumption challenged**: PostgreSQL trigger `fc27_guard_webauthn_counter` prevents all replay and clone attacks.
- **Attack scenario**:
  The PostgreSQL trigger `fc27_guard_webauthn_counter` checks:
  ```sql
  IF NEW.sign_counter < OLD.sign_counter THEN
      RAISE check_violation USING MESSAGE = 'webauthn sign_counter cannot decrease';
  END IF;
  ```
  This trigger allows `NEW.sign_counter == OLD.sign_counter`. This is necessary for non-counter updates (e.g., updating `last_used_at` or `label`), but if an assertion update sets `sign_counter = OLD.sign_counter`, the trigger alone will not block it.
  Fortunately, the application layer in `src/identity/store.py:1717` checks:
  ```python
  if row["sign_counter"] > 0 and new_counter <= row["sign_counter"]:
      raise IdentityStoreError(401, "authenticator sign counter replay or clone detected")
  ```
  and `src/identity/webauthn.py:298` also checks:
  ```python
  if current_counter > 0 and verified.new_sign_count <= current_counter:
      raise InvalidAuthenticationResponse("authenticator clone or replay attack detected: counter did not increase")
  ```
- **Blast radius**: Relies on application layer consistency to enforce strictly greater-than for non-zero counters.
- **Assessment**: Robust defense-in-depth across application store and WebAuthn engine. However, authenticators with `sign_counter == 0` (e.g. some platform authenticators) bypass counter checks by design according to the W3C WebAuthn Level 2 specification.

---

## Stress Test Results

| # | Stress Test Scenario | Expected Behavior | Actual Behavior | Result |
|---|----------------------|-------------------|-----------------|--------|
| 1 | Passkey assertion with $C_{\text{new}} = C_{\text{current}}$ ($C > 0$) | Rejected with clone/replay detection (401) | Rejected with InvalidAuthenticationResponse | **PASS** |
| 2 | Passkey assertion with $C_{\text{new}} < C_{\text{current}}$ ($C > 0$) | Rejected with clone/replay detection (401) | Rejected with InvalidAuthenticationResponse | **PASS** |
| 3 | `IdentityStore.update_webauthn_counter` with non-increasing counter | Raises IdentityStoreError(401) | Raises IdentityStoreError(401) | **PASS** |
| 4 | PostgreSQL trigger `fc27_guard_webauthn_counter` on counter decrement | Raises `check_violation` | Defined in migration 0004 | **PASS** |
| 5 | WebAuthn challenge consumed twice (replay attempt) | 2nd consumption returns `None` / fails closed | Fails closed with 400/401 | **PASS** |
| 6 | Expired WebAuthn challenge consumed | Returns `None` / fails closed | Fails closed with 400/401 | **PASS** |
| 7 | Cross-user challenge mismatch in registration | Fails closed: "challenge not issued to this user" | Fails closed | **PASS** |
| 8 | Cross-user challenge mismatch in authentication | Fails closed: "challenge not issued to this user" | **OMITTED** in `verify_authentication_response` | **FAIL** |
| 9 | Step-up challenge payload hash mismatch | Fails closed: "bound payload mismatch" | Fails closed | **PASS** |
| 10 | Cross-user challenge mismatch in step-up | Fails closed: "challenge not issued to this user" | **OMITTED** in `verify_step_up_response` | **FAIL** |
| 11 | TOTP code replayed in same 30s window | Fails closed with `"replay_detected"` (HTTP 400) | Rejected with `"replay_detected"` | **PASS** |
| 12 | TOTP code replayed from older window ($S \le S_{\text{last}}$) | Fails closed with `"replay_detected"` (HTTP 400) | Rejected with `"replay_detected"` | **PASS** |
| 13 | TOTP code outside $\pm 1$ step drift (e.g. $t \pm 65s$) | Fails closed with `"invalid_or_expired_code"` (HTTP 400) | Rejected with `"invalid_or_expired_code"` | **PASS** |
| 14 | 5 consecutive failed TOTP attempts | Locks out user for 300s (HTTP 429) | Rate limiter triggers lockout, returns 300s | **PASS** |
| 15 | Service tokens (BFF, Graph, Worker) calling passkey/TOTP endpoints | Fails with 403 `AgentImpersonationError` | Rejected with 403 `AgentImpersonationError` | **PASS** |
| 16 | Service roles executing PASSKEY / TOTP actions via `authorize()` | Fails with 403 `ActionNotPermittedError` | Rejected with 403 | **PASS** |
| 17 | `tests/identity/test_totp.py` pytest collection | Clean collection and test pass | **Crashes on collection** (`ImportError`) | **FAIL** |
| 18 | `tests/identity/test_webauthn.py` pytest execution | Clean test execution | **4 tests fail** (TypeError, AttributeError, AssertionError) | **FAIL** |
| 19 | Packaging sync & Phase 1 dormancy | Dependencies match, DISPATCH_ENABLED=false | Verified | **PASS** |

---

## Unchallenged Areas

- **Physical FIDO2 Hardware Attestation**: Validated using simulated W3C WebAuthn Level 2/3 cryptographic structures (CBOR/COSE); physical USB/NFC hardware security keys were not attached.
- **Live Redis Distributed Rate Limiting**: In-memory `TotpRateLimiter` thread-safe singleton tested; distributed multi-node rate limiting is planned for Phase 3+.
