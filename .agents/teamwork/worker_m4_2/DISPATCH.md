# DISPATCH — worker_m4_2

**Timestamp**: 2026-10-04T06:35:00Z  
**Role**: Milestone 4 Iteration 2 Remediation Worker  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2`  
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`  
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`  
**Full Forensic Audit Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1\handoff.md`  
**Explorer Handoffs**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_tests_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_webauthn_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_recovery_1\handoff.md`
- Patch: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_recovery_1\proposed_recovery_fixes.patch`  

---

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

---

## File Ownership
You exclusively own and may edit:
- `src/identity/webauthn.py`
- `src/identity/store.py`
- `src/identity/recovery.py`
- `tests/identity/test_totp.py`
- `tests/identity/test_webauthn.py`
- `tests/identity/test_recovery.py`
- `tests/identity/test_bootstrap.py`
- `tests/identity/test_adversarial_m4_1.py`

---

## Remediation Tasks

### 1. Test Suite Remediation (per `explorer_m4_i2_tests_1/handoff.md`)
- In `tests/identity/test_totp.py`:
  - Line 23: remove `DEFAULT_RECOVERY_CODE_COUNT` from imports.
- In `tests/identity/test_webauthn.py`:
  - Line 69: align the RP name assertion with `WebAuthnConfig.from_env()` default (`"FC27 Pro Clubs Platform"`) or set env var in the test fixture.
  - Lines 118 & 140: pass required keyword-only argument `user_id=uuid4()` to `engine.verify_registration_response()`.
  - Line 202: call `engine.generate_step_up_options()` instead of non-existent `generate_step_up_challenge()`.
- In `tests/identity/test_recovery.py`:
  - Line 148: change `assert res.error == "invalid_recovery_code"` to `assert res.error == "invalid_or_consumed_code"`.

### 2. WebAuthn Cross-User Challenge Isolation & Counter Check (per `explorer_m4_i2_webauthn_1/handoff.md`)
- In `src/identity/webauthn.py`:
  - In `verify_authentication_response()`: after consuming the challenge, validate:
    ```python
    if challenge_row.get("user_id") and challenge_row["user_id"] != stored_credential.get("user_id"):
        raise InvalidAuthenticationResponse("authentication challenge not issued to this user")
    ```
  - In `verify_step_up_response()`: after consuming the challenge, validate:
    ```python
    if challenge_row.get("user_id") and challenge_row["user_id"] != stored_credential.get("user_id"):
        raise InvalidAuthenticationResponse("step-up challenge not issued to this user")
    ```
  - In `verify_step_up_response()`: add explicit second-layer counter monotonicity defense:
    ```python
    if current_counter > 0 and verified.new_sign_count <= current_counter:
        raise InvalidAuthenticationResponse("authenticator sign counter did not increase")
    ```

### 3. Recovery Code Revocation & Store DML Filtering (per `explorer_m4_i2_recovery_1/handoff.md` and patch)
- In `src/identity/store.py` (`save_recovery_codes`):
  - In `with self.connect() as conn, conn.transaction():`, execute the atomic revocation query before inserting new codes:
    ```python
    conn.execute(
        """
        UPDATE public.fc27_recovery_codes
        SET consumed_at = now()
        WHERE user_id = %s AND consumed_at IS NULL
        """,
        (user_id,),
    )
    ```
- In `src/identity/store.py` (`verify_and_consume_recovery_code`):
  - Add DML filter `AND consumed_at IS NULL`:
    ```python
    conn.execute(
        """
        UPDATE public.fc27_recovery_codes
        SET consumed_at = now()
        WHERE code_id = %s AND consumed_at IS NULL
        """,
        (r["code_id"],),
    )
    ```

---

## Verification Commands
Execute and document clean passes for:
1. `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
2. `pytest tests/identity/test_totp.py -v`
3. `pytest tests/identity/test_webauthn.py -v`
4. `pytest tests/identity/test_recovery.py -v`
5. `pytest tests/identity/test_bootstrap.py -v`
6. `pytest tests/identity/test_adversarial_m4_1.py -v`
7. `pytest tests/test_m4_phase2_adversarial.py -v`

Deliver complete hard handoff report to:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2\handoff.md`


## 2026-10-04T06:30:50Z
You are worker_m4_2, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Full Forensic Audit Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1\handoff.md
Explorer handoffs:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_tests_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_webauthn_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_recovery_1\handoff.md
- Patch: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_recovery_1\proposed_recovery_fixes.patch

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2\DISPATCH.md before starting work.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

File ownership:
You exclusively own and may edit:
- src/identity/webauthn.py
- src/identity/store.py
- src/identity/recovery.py
- tests/identity/test_totp.py
- tests/identity/test_webauthn.py
- tests/identity/test_recovery.py
- tests/identity/test_bootstrap.py
- tests/identity/test_adversarial_m4_1.py

Implement Milestone 4 Iteration 2 Remediation:
1. Test suite fixes:
   - tests/identity/test_totp.py: remove DEFAULT_RECOVERY_CODE_COUNT from imports on line 23.
   - tests/identity/test_webauthn.py: line 69 align RP name assertion with WebAuthnConfig.from_env() ("FC27 Pro Clubs Platform") or fixture; lines 118 & 140 pass user_id=uuid4() to engine.verify_registration_response(); line 202 call engine.generate_step_up_options().
   - tests/identity/test_recovery.py: line 148 change assert res.error == "invalid_recovery_code" to "invalid_or_consumed_code".
   - tests/identity/test_adversarial_m4_1.py: ensure test fixtures align with isolation checks.
2. WebAuthn cross-user challenge isolation & counter check:
   - src/identity/webauthn.py: in verify_authentication_response() and verify_step_up_response(), validate if challenge_row.get("user_id") and challenge_row["user_id"] != stored_credential.get("user_id"), raise InvalidAuthenticationResponse("... challenge not issued to this user").
   - src/identity/webauthn.py: in verify_step_up_response(), add explicit second-layer counter monotonicity defense if current_counter > 0 and verified.new_sign_count <= current_counter: raise InvalidAuthenticationResponse("authenticator sign counter did not increase").
3. Recovery code revocation & store DML:
   - src/identity/store.py (save_recovery_codes): in conn.transaction(), execute UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL before inserting new codes.
   - src/identity/store.py (verify_and_consume_recovery_code): add AND consumed_at IS NULL filter on UPDATE statement.
4. Execute verification commands and record verbatim output:
   - .\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v
   - .\.venv\Scripts\pytest.exe tests/identity/test_totp.py tests/identity/test_webauthn.py tests/identity/test_recovery.py tests/identity/test_bootstrap.py -v
   - .\.venv\Scripts\pytest.exe tests/identity/test_adversarial_m4_1.py tests/test_m4_phase2_adversarial.py -v
5. Deliver complete hard handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2\handoff.md
When finished, notify me with your report path and verification results via send_message.
