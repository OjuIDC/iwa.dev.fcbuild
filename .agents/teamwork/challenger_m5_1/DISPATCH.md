# DISPATCH — 2026-10-04T05:50:00Z

## Mission
Adversarial Stress Testing of Scoped Action Tokens & Decision Step-Up Replay / Scope Lockout for Phase 2 Milestone 5.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY: You MUST read this before starting work)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1\handoff.md`

## Adversarial Tracks
1. Scoped Action Tokens (`src/identity/tokens.py`):
   - Attack 1: Token Replay — attempt to consume an already-consumed action token. Verify fails closed with HTTP 409 `ActionTokenAlreadyConsumedError`.
   - Attack 2: Expired Token — attempt to consume an expired action token. Verify fails closed with HTTP 410 `ActionTokenExpiredError`.
   - Attack 3: Revoked Token — attempt to consume a revoked action token. Verify fails closed with HTTP 410 `ActionTokenRevokedError`.
   - Attack 4: Scope Confusion — attempt to consume a token issued for scope A (e.g. `step_up`) on scope B (e.g. `player_decision`). Verify fails closed with HTTP 403 `ActionTokenScopeMismatchError`.
   - Attack 5: Assignment Mismatch — attempt to consume a token issued for Assignment X on Assignment Y. Verify fails closed with HTTP 403 `ActionTokenAssignmentMismatchError`.
   - Attack 6: Token Format & Plaintext Storage — verify token format `sat_` and verify database contains ONLY SHA-256 hex digest, zero plaintext tokens.
2. Decision Step-Up Replay & Scope Lockout (`src/identity/routers/step_up.py`):
   - Attack 7: Evidence Replay — attempt to reuse an already-consumed `authentication_evidence_id` for a second decision submission. Verify fails closed with HTTP 409 Conflict.
   - Attack 8: Evidence Scope Lockout — attempt to use evidence issued for Package A on Package B. Verify fails closed with HTTP 400 or 403.
   - Attack 9: Missing Binding Parameters — call `/v1/auth/step-up/challenge` with missing `package_hash` or `package_version`. Verify fails closed with HTTP 422.
3. Universal Invariants:
   - Verify `DISPATCH_ENABLED=false` and zero player delivery.

## Verification Instructions
Execute test verification:
```pwsh
.\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v
```
If an interactive terminal prompt times out waiting for user confirmation, document the attempted command and timeout honestly, and verify via AST and static analysis.
Provide an explicit verdict in your handoff report: **APPROVE** or **REQUEST_CHANGES**.

Deliver report to:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1\handoff.md`
When finished, notify orchestrator with your verdict and report path via send_message.


## 2026-10-04T09:47:23Z
You are challenger_m5_1, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1\DISPATCH.md before starting work.

Your task:
Empirically stress-test Scoped Action Tokens & Decision Step-Up Replay / Scope Lockout for Phase 2 Milestone 5:
1. Scoped Action Tokens: test token replay (409), expired token (410), revoked token (410), scope confusion (403), assignment mismatch (403), token format sat_ and verify DB stores only SHA-256 digest.
2. Decision Step-Up Replay & Scope Lockout: test evidence replay (409), evidence scope lockout (400/403), missing binding parameters on challenge (422).
3. Scope dormancy: verify DISPATCH_ENABLED=false and zero player delivery.
4. Execute empirical tests:
   .\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v
   (Set WaitMsBeforeAsync=10000). If permission prompt times out, document honestly and verify via static analysis / AST checks.
5. Provide an explicit verdict in your handoff report: APPROVE or REQUEST_CHANGES.
Deliver complete report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1\handoff.md
When finished, notify orchestrator with your verdict and report path via send_message.
