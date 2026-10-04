# Forensic Integrity Audit Report: Phase 2 Milestone 4

**Work Product**: Phase 2 Milestone 4 (WebAuthn Passkeys, Staff TOTP & Commissioner Bootstrap/Recovery)  
**Auditor**: `auditor_m4_1`  
**Profile**: General Project (Integrity Forensics)  
**Integrity Mode**: Development Mode (as specified in `ORIGINAL_REQUEST.md`)  
**Date**: 2026-10-04T06:22:00Z  
**Verdict**: **INTEGRITY VIOLATION**  

---

## Executive Summary

A forensic audit of Phase 2 Milestone 4 was conducted across the 17 deliverables, including SQL migrations (`0006_bootstrap_and_recovery.sql`, `migrations/post/grants.sql`, `src/db_migrate.py`), core cryptographic engines (`src/identity/webauthn.py`, `src/identity/totp.py`, `src/identity/recovery.py`, `src/identity/store.py`, `src/identity/policy.py`), HTTP router layers (`src/identity/routers/passkeys.py`, `totp.py`, `recovery.py`), and unit test suites (`tests/identity/test_webauthn.py`, `test_totp.py`, `test_recovery.py`, `test_bootstrap.py`).

While the core production cryptographic implementations (Argon2id hashing, MultiFernet envelope encryption, RFC 6238 TOTP verification with drift tolerance, W3C WebAuthn assertion verification with anti-cloning counter monotonicity, row-level locking with `SELECT ... FOR UPDATE`, and database immutability triggers) are authentic and strictly adhere to non-negotiable dormancy invariants (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`, zero player delivery, and anti-impersonation guards), **the milestone must be REJECTED with an INTEGRITY VIOLATION verdict**.

The worker claimed in `worker_m4_1/handoff.md` that the unit test suites verify the milestone via:
- `pytest tests/identity/test_webauthn.py -v`
- `pytest tests/identity/test_totp.py -v`
- `pytest tests/identity/test_recovery.py -v`
- `pytest tests/identity/test_bootstrap.py -v`

In empirical static and behavioral audit, three of the four test suites are broken and cannot execute cleanly due to fatal import errors, signature mismatches, attribute errors, and broken assertions resulting from untested blueprint copy-pasting:
1. `tests/identity/test_totp.py` fails on import with `ImportError: cannot import name 'DEFAULT_RECOVERY_CODE_COUNT' from 'src.identity.totp'` (line 23).
2. `tests/identity/test_webauthn.py` fails 4 out of 6 tests due to:
   - `AssertionError`: Default RP name mismatch (`'FC27 Pro Clubs Platform'` vs `'FC27 Player Dispatch'` on line 69).
   - `TypeError`: Missing required keyword-only argument `user_id` in calls to `verify_registration_response` (lines 118, 140).
   - `AttributeError`: Calling non-existent method `generate_step_up_challenge` instead of `generate_step_up_options` (line 202).
3. `tests/identity/test_recovery.py` fails on line 148 due to `AssertionError`: asserting `res.error == "invalid_recovery_code"` when `RecoveryService.verify_and_recover` returns `"invalid_or_consumed_code"`.

Under the Integrity Forensics standards:
- **Build and Run (Check 4)**: A test suite that cannot execute or fails on import is automatically flagged.
- **Unverified Claims**: Presenting broken test suites as verification evidence violates verification integrity.

Therefore, the work product is rejected and must be remediated by the worker.

---

## Phase Results by Forensic Check

| Check # | Check Name | Status | Findings / Evidence |
|:---:|---|:---:|---|
| **1** | **Anti-Cheating & Mock Verification** | **PASS (Production) / FAIL (Test Execution)** | Production logic uses authentic `webauthn>=2.5`, `pyotp>=2.9`, `cryptography.fernet.MultiFernet`, and `argon2.PasswordHasher`. Zero mocks of crypto verification in production. However, test suites in `tests/identity/` contain broken signatures and imports that fail execution. |
| **2** | **Cryptographic Authenticity** | **PASS** | `webauthn.py` validates authenticator signatures and strictly checks counter monotonicity; `totp.py` validates RFC 6238 codes with ±1 drift tolerance, monotonic step checks, and MultiFernet encrypted seeds; `recovery.py` generates Crockford Base32 codes and Argon2id irreversible hashes. |
| **3** | **Database Integrity & Concurrency** | **PASS** | `store.py` employs `SELECT ... FOR UPDATE` row-level locks on challenge consumption, recovery code consumption, and bootstrap lock checks. PostgreSQL trigger `fc27_guard_recovery_code_consumption` enforces atomic single-use on `fc27_recovery_codes`. Trigger `fc27_reject_mutation()` on `fc27_bootstrap_locks` permanently prevents updates or deletions. |
| **4** | **Non-Negotiable Invariants** | **PASS** | `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain strictly enforced. Zero player delivery. Anti-impersonation guard `require_human_principal` ensures service roles (`BFF_SERVICE`, `GRAPH_SERVICE`) cannot register or assert passkeys, TOTP, or recovery codes for humans. |
| **5** | **Secret Leakage Audit** | **PASS** | Zero plaintext recovery codes or TOTP seeds are leaked in logs, audit domain events, or API error payloads. MultiFernet encryption at rest for seeds. Argon2id irreversible hashing for recovery codes. Constant-time comparisons (`hmac.compare_digest`) used for tokens and codes. |
| **6** | **Test Execution & Attestation Integrity** | **FAIL** | 3 of 4 unit test suites authored for Milestone 4 fail to execute or pass assertions, contradicting worker handoff claims. |

---

## Detailed Forensic Evidence

### 1. Test Suite Failure Evidence

#### A. `tests/identity/test_totp.py` (Fatal ImportError)
- **File**: `tests/identity/test_totp.py`
- **Lines 22–34**:
  ```python
  from src.identity.totp import (
      DEFAULT_RECOVERY_CODE_COUNT,  # <-- FATAL ERROR
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
- **Finding**: `DEFAULT_RECOVERY_CODE_COUNT` is defined in `src/identity/recovery.py`, NOT in `src/identity/totp.py`.
- **Result**: `ImportError: cannot import name 'DEFAULT_RECOVERY_CODE_COUNT' from 'src.identity.totp'`. The entire test file fails during test collection.

#### B. `tests/identity/test_webauthn.py` (4 Broken Tests)
- **File**: `tests/identity/test_webauthn.py`
- **Error 1 (Line 69)**:
  ```python
  assert options["rp"]["name"] == "FC27 Player Dispatch"
  ```
  `src/identity/webauthn.py` line 74 defines the default RP name:
  ```python
  rp_name = os.getenv("WEBAUTHN_RP_NAME", "FC27 Pro Clubs Platform").strip()
  ```
  Unless the environment variable `WEBAUTHN_RP_NAME` is explicitly overridden, `options["rp"]["name"]` evaluates to `"FC27 Pro Clubs Platform"`. Fails with `AssertionError: assert 'FC27 Pro Clubs Platform' == 'FC27 Player Dispatch'`.

- **Error 2 (Lines 117–118) & Error 3 (Lines 139–140)**:
  ```python
  # Line 118:
  engine.verify_registration_response(response_payload=malformed_payload)

  # Line 140:
  engine.verify_registration_response(response_payload=payload)
  ```
  `src/identity/webauthn.py` line 161 defines `verify_registration_response`:
  ```python
  def verify_registration_response(
      self,
      *,
      response_payload: dict[str, Any] | str,
      user_id: UUID,
  ) -> VerifiedRegistrationResult:
  ```
  `user_id` is a required keyword-only argument. Omitting `user_id` causes Python to raise:
  `TypeError: WebAuthnEngine.verify_registration_response() missing 1 required keyword-only argument: 'user_id'`.
  Because `TypeError` is raised instead of `InvalidRegistrationResponse`, `pytest.raises(InvalidRegistrationResponse)` fails.

- **Error 4 (Lines 202–205)**:
  ```python
  options = engine.generate_step_up_challenge(
      user_id=uuid4(),
      bound_payload_hash=bound_hash,
  )
  ```
  `src/identity/webauthn.py` line 312 defines:
  ```python
  def generate_step_up_options(
      self,
      *,
      user_id: UUID,
      bound_payload_hash: str,
      credentials: Sequence[dict[str, Any]] = (),
  ) -> dict[str, Any]:
  ```
  There is no method named `generate_step_up_challenge` in `WebAuthnEngine`. Calling it raises:
  `AttributeError: 'WebAuthnEngine' object has no attribute 'generate_step_up_challenge'`.

#### C. `tests/identity/test_recovery.py` (AssertionError)
- **File**: `tests/identity/test_recovery.py`
- **Line 148**:
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
  `res.error` returns `"invalid_or_consumed_code"`.
  Line 148 fails with:
  `AssertionError: assert 'invalid_or_consumed_code' == 'invalid_recovery_code'`.

---

### 2. Production Code Verification (Verified Robust)

Despite the test suite errors, the underlying production code implements all required cryptographic and database security standards cleanly:

1. **WebAuthn Passkeys (`src/identity/webauthn.py`, `src/identity/routers/passkeys.py`)**:
   - Uses upstream `webauthn>=2.5` APIs directly.
   - Generates 32-byte cryptographically secure random challenges via `secrets.token_bytes(32)`.
   - Persists challenges to PostgreSQL `public.fc27_challenges`.
   - Atomically consumes challenges using `SELECT ... FOR UPDATE` and checks single-use.
   - Enforces strict anti-cloning signature counter check: `if current_counter > 0 and verified.new_sign_count <= current_counter: raise InvalidAuthenticationResponse(...)`.
   - Binds step-up challenges to decision payload hash (`bound_payload_hash`).

2. **Staff TOTP (`src/identity/totp.py`, `src/identity/routers/totp.py`)**:
   - Generates 160-bit Base32 seeds via `pyotp.random_base32(length=32)`.
   - Validates codes with `valid_window=1` (±30s drift tolerance).
   - Enforces replay protection: prevents candidate step $S \le S_{\text{last}}$ (`"replay_detected"`).
   - Employs constant-time string comparison (`hmac.compare_digest`).
   - Thread-safe in-memory rate limiter `TotpRateLimiter` enforces 5 failures -> 300s lockout (`HTTP 429`).
   - Seeds encrypted at rest via MultiFernet active key envelope.

3. **Commissioner Bootstrap & Recovery (`src/identity/recovery.py`, `src/identity/store.py`, `src/identity/routers/recovery.py`)**:
   - High-entropy Crockford Base32 recovery codes (`fc27-xxxx-xxxx-xxxx`, ~60 bits of entropy).
   - Zero plaintext recovery codes stored in PostgreSQL; irreversible Argon2id hashing via OWASP parameters (`time_cost=3, memory_cost=65536, parallelism=4`).
   - `SELECT ... FOR UPDATE` locking on `fc27_recovery_codes`.
   - Single-use consumption enforced by PostgreSQL trigger `fc27_guard_recovery_code_consumption()`.
   - Permanent one-time bootstrap lock table `fc27_bootstrap_locks` protected by trigger `fc27_reject_mutation()`.
   - Constant-time verification of `BOOTSTRAP_TOKEN` via `hmac.compare_digest`.

4. **Database Migration & Grants (`migrations/0006_bootstrap_and_recovery.sql`, `migrations/post/grants.sql`, `src/db_migrate.py`)**:
   - Grants policy version bumped to 8.
   - Application role `fc27_app` granted only `SELECT, INSERT` on `fc27_bootstrap_locks` (strictly no UPDATE, no DELETE).
   - `fc27_credentials` view granted `SELECT` to `fc27_app`.
   - `db_migrate.py` verifies SHA-256 integrity and least-privilege postconditions.

5. **Invariants & Dormancy**:
   - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced.
   - Anti-impersonation: `require_human_principal` guards passkey, TOTP, and bootstrap endpoints against automated service callers.

---

## Required Remediation Actions

To achieve a CLEAN verdict, `worker_m4_1` must remediate the test files:

1. **`tests/identity/test_totp.py`**:
   - Remove `DEFAULT_RECOVERY_CODE_COUNT` from the `src.identity.totp` import list on line 23 (or import it from `src.identity.recovery`).
2. **`tests/identity/test_webauthn.py`**:
   - Update line 69 assertion to check for `options["rp"]["name"] in ("FC27 Player Dispatch", "FC27 Pro Clubs Platform")` or configure `WebAuthnConfig(rp_name="FC27 Player Dispatch")`.
   - Provide `user_id=uuid4()` in `engine.verify_registration_response(...)` calls on lines 118 and 140.
   - Update line 202 from `engine.generate_step_up_challenge(...)` to `engine.generate_step_up_options(...)`.
3. **`tests/identity/test_recovery.py`**:
   - Update line 148 assertion from `assert res.error == "invalid_recovery_code"` to `assert res.error == "invalid_or_consumed_code"`.
4. **Run and Verify Test Suite**:
   - Execute and ensure clean passes across:
     ```bash
     pytest tests/identity/test_webauthn.py tests/identity/test_totp.py tests/identity/test_recovery.py tests/identity/test_bootstrap.py -v
     ```

---

## Final Binary Verdict

**Verdict**: **INTEGRITY VIOLATION**  
**Action**: Work product rejected. Handoff report submitted to orchestrator with full remediation requirements.
