# DISPATCH — 2026-10-04T05:50:00Z

## Mission
Adversarial Stress Testing of Object-Level Player Isolation, 5 Decision Actions, HMAC Kill Switch & G-TIMEOUT Invariant for Phase 2 Milestone 5.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (MANDATORY: You MUST read this before starting work)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1\handoff.md`

## Adversarial Tracks
1. Object-Level Player Isolation (`src/identity/routers/player.py`):
   - Attack 1: Cross-Player Viewing — Player A attempts to query Player B's assignments. Verify fails closed with HTTP 403 `PlayerIsolationError`.
   - Attack 2: Cross-Player Challenge — Player A attempts to request a decision challenge for Player B's assignment. Verify fails closed with HTTP 403 `PlayerIsolationError`.
   - Attack 3: Cross-Player Decision — Player A attempts to submit a decision for Player B's assignment. Verify fails closed with HTTP 403 `PlayerIsolationError`.
   - Attack 4: Staff Impersonation — Commissioner or Delegate attempts to decide a player assignment as player. Verify fails closed with HTTP 403.
2. 5 Distinct Player Decision Actions:
   - Attack 5: Conflation of `acknowledge` with `accept` — verify that `acknowledge` records informational review receipt but NEVER activates developmental plans or transitions assignment to completed.
   - Attack 6: Incomplete `change_request` — verify submitting `change_request` without notes fails closed with HTTP 422.
   - Attack 7: Invalid `pause` — verify submitting `pause` without positive duration fails closed with HTTP 422.
3. Graph API HMAC Kill Switch (`src/graph_api.py`):
   - Attack 8: HMAC Submission with Kill Switch Active — verify submitting `proof_type="hmac"` when `APPROVAL_CLI_HMAC_ENABLED=false` is rejected immediately with HTTP 403 `proof_type_disabled`.
   - Attack 9: Pre-flight Unauthenticated Probe — verify that non-WebAuthn probe to decision endpoints returns HTTP 403 before authentication or schema parsing.
4. G-TIMEOUT Invariant (Feature 46):
   - Attack 10: Inactivity / Timeout Approval Injection — verify any attempt to submit an approval with action `"timeout"` or attribute `timeout_approved: true` is rejected with HTTP 403 `AgentImpersonationError`.
   - Attack 11: Expired Request Decision — verify submitting a decision for an expired request returns HTTP 409 `approval_expired`.
5. Universal Invariants:
   - Verify `DISPATCH_ENABLED=false` and zero player delivery.

## Verification Instructions
Execute test verification:
```pwsh
.\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
```
If an interactive terminal prompt times out waiting for user confirmation, document the attempted command and timeout honestly, and verify via AST and static analysis.
Provide an explicit verdict in your handoff report: **APPROVE** or **REQUEST_CHANGES**.

Deliver report to:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\handoff.md`
When finished, notify orchestrator with your verdict and report path via send_message.

## 2026-10-04T09:47:23Z
You are challenger_m5_2, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\DISPATCH.md before starting work.

Your task:
Empirically stress-test Object-Level Player Isolation, 5 Decision Actions, HMAC Kill Switch & G-TIMEOUT Invariant for Phase 2 Milestone 5:
1. Object-Level Player Isolation: test cross-player viewing (403), cross-player challenge (403), cross-player decision (403), staff impersonation (403).
2. 5 Decision Actions: test acknowledge does not activate or accept, test change_request without notes fails (422), test pause without positive duration fails (422).
3. Graph API HMAC Kill Switch: test proof_type=hmac rejected when APPROVAL_CLI_HMAC_ENABLED=false (403 proof_type_disabled), pre-flight probe blocked before auth.
4. G-TIMEOUT Invariant: test timeout approval injection rejected (403 AgentImpersonationError), expired request decision rejected (409 approval_expired).
5. Scope dormancy: verify DISPATCH_ENABLED=false and zero player delivery.
6. Execute empirical tests:
   .\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
   (Set WaitMsBeforeAsync=10000). If permission prompt times out, document honestly and verify via static analysis / AST checks.
7. Provide an explicit verdict in your handoff report: APPROVE or REQUEST_CHANGES.
Deliver complete report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\handoff.md
When finished, notify orchestrator with your verdict and report path via send_message.
