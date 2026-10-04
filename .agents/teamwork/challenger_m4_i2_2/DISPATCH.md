# DISPATCH — challenger_m4_i2_2

**Timestamp**: 2026-10-04T06:42:00Z  
**Role**: Recovery Immutability & Replay Challenger  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_i2_2`  
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`  
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`  
**Worker Handoff**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_2\handoff.md`  
**Previous Challenger Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2\handoff.md`  

---

## Mission
Empirically stress-test recovery code revocation, single-use immutability, and bootstrap locks for Phase 2 Milestone 4 Iteration 2:
1. Recovery Code Revocation on Regeneration:
   - Verify that generating a new set of recovery codes revokes all prior unconsumed codes (`consumed_at` set to non-null).
   - Verify that old codes can no longer be consumed after regeneration.
2. Recovery Code Single-Use & Immutability:
   - Verify that an already-consumed code cannot be re-consumed.
   - Verify that un-consuming (`consumed_at = NULL`), attribute modification, or row deletion triggers `fc27_guard_recovery_code_consumption` / `fc27_reject_mutation` and fails closed.
3. Bootstrap Lock Immutability:
   - Verify that once bootstrapped, subsequent calls to `POST /v1/auth/bootstrap` return HTTP 409 Conflict.
   - Verify that UPDATE or DELETE rows on `fc27_bootstrap_locks` fails closed via trigger `fc27_reject_mutation`.
4. Execute empirical tests:
   - `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`
   - `pytest tests/identity/test_recovery.py tests/identity/test_bootstrap.py tests/test_m4_phase2_adversarial.py -v`
5. Document all commands, results, and explicit verdict (APPROVE or REQUEST_CHANGES) in:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_i2_2\handoff.md`
When finished, notify orchestrator with your verdict and report path via `send_message`.
