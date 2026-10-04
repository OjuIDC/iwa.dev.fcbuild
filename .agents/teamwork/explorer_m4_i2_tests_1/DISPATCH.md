# DISPATCH — explorer_m4_i2_tests_1

**Timestamp**: 2026-10-04T06:24:00Z  
**Role**: Test Suite Import, Signature & Assertion Remediation Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_tests_1`  
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`  
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`  
**Full Forensic Audit Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1\handoff.md`  
**Reviewer Reports**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\handoff.md`  
**Challenger Reports**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2\handoff.md`  

---

## Mission
Analyze and formulate exact fix proposals for the broken unit test suites identified in the Milestone 4 audit and review:
1. In `tests/identity/test_totp.py:23`: remove `DEFAULT_RECOVERY_CODE_COUNT` from `src.identity.totp` (it is defined in `src.identity.recovery` if needed, or remove the unused import so pytest collection passes).
2. In `tests/identity/test_webauthn.py`:
   - Line 69: align the RP name assertion with `WebAuthnConfig.from_env()` default (`"FC27 Pro Clubs Platform"`) or set `WEBAUTHN_RP_NAME="FC27 Player Dispatch"` in the test fixture / monkeypatch.
   - Lines 118 & 140: pass the required keyword-only argument `user_id: UUID` (e.g. `user_id=uuid4()`) to `engine.verify_registration_response()`.
   - Line 202: call `engine.generate_step_up_options()` instead of non-existent `generate_step_up_challenge()`.
3. In `tests/identity/test_recovery.py:148`:
   - Align assertion from `assert res.error == "invalid_recovery_code"` to `assert res.error == "invalid_or_consumed_code"`.
4. Formulate the comprehensive verification command matrix so the Worker can execute and verify all 4 unit test suites (`test_totp.py`, `test_webauthn.py`, `test_recovery.py`, `test_bootstrap.py`) and packaging/scope guards.
5. Provide exact code diffs and recommendations in `handoff.md`. Do NOT edit source files directly.
