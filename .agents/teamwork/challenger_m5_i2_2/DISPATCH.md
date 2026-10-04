# DISPATCH: challenger_m5_i2_2 (Player Isolation, Kill Switch & G-TIMEOUT Challenger)

**Role**: `teamwork_preview_challenger`
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_2`
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
**Worker Handoff**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2\handoff.md`
**Previous Challenger Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\handoff.md`

---

## Non-Negotiable Invariants
1. Universal dormancy: `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced across all environments, settings, and endpoints.
2. Zero player delivery; Phase 2 must NEVER activate player dispatch, delivery adapters, or outbox relay.
3. No Phase 3 dispatch worker, outbox relay, or player-agent execution pipeline.
4. Preserve Phase 1 baseline commit `3339d6d672bbc59f251e1e2a6977bc9ea65edffe`.
5. PostgreSQL is authoritative for all identity and authorization state.
6. No agent-human impersonation (automated services/LLM nodes cannot authenticate or decide for humans; `require_human_principal` enforced).
7. Gamertags and display names are presentation data only; use immutable local user UUIDs and numeric player IDs as primary identity.
8. Inactivity and timeouts alter escalation state but NEVER create approvals or decisions (G-TIMEOUT invariant).

---

## Adversarial Challenge Mandate
Empirically stress-test Object-Level Player Isolation, 5 Decision Actions, HMAC Kill Switch & G-TIMEOUT Invariant for Phase 2 Milestone 5:
1. **Object-Level Player Isolation**:
   - Test cross-player assignment access: Player A querying Player B's assignment fails closed with HTTP 403 (`PlayerIsolationError`).
   - Test cross-player decision execution: Player A submitting decision on Player B's assignment fails closed with HTTP 403 (`PlayerIsolationError`).
   - Test staff impersonation: Staff attempting to decide on player assignment fails closed with HTTP 403 (`PlayerIsolationError`).
2. **5 Distinct Decision Actions**:
   - `accept`: Requires valid proof (passkey evidence or action token).
   - `reject`: Rejects assignment.
   - `change_request`: Requires non-empty reason/notes (empty notes fail with HTTP 422).
   - `pause`: Requires duration >= 1 (invalid duration fails with HTTP 422).
   - `acknowledge`: Commits informational receipt without activating or accepting the assignment.
3. **Graph API HMAC Kill Switch**:
   - Verify `APPROVAL_CLI_HMAC_ENABLED` defaults to `False`.
   - When False, submissions with `proof_type="hmac"` fail closed immediately with HTTP 403 `proof_type_disabled`.
4. **G-TIMEOUT Invariant**:
   - Test timeout approval injection rejected (HTTP 403 `AgentImpersonationError`). Inactivity or timeouts never create approvals or decisions.
5. **Scope Dormancy**:
   - Verify `DISPATCH_ENABLED=false` and zero player delivery.
6. **Execute Empirical Tests**:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
   (Set `WaitMsBeforeAsync: 10000`). If permission prompt times out, document honestly and verify via AST / static analysis.
7. Provide an explicit verdict in your handoff report: **APPROVE** or **REQUEST_CHANGES**.
8. Deliver report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_2\handoff.md`.
9. Notify orchestrator via `send_message` when complete.


## 2026-10-04T10:12:12Z
Received dispatch from parent (25210876-a9ad-4f78-a87c-dec88e86edb9):
You are challenger_m5_i2_2, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2\handoff.md
Previous challenger report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_2\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_2\DISPATCH.md before starting work.

Your task:
Empirically stress-test Object-Level Player Isolation, 5 Decision Actions, HMAC Kill Switch & G-TIMEOUT Invariant for Phase 2 Milestone 5 Iteration 2:
1. Object-Level Player Isolation: test cross-player viewing (403), cross-player challenge (403), cross-player decision (403), staff impersonation (403).
2. 5 Decision Actions: test acknowledge does not activate or accept, test change_request without notes fails (422), test pause without positive duration fails (422).
3. Graph API HMAC Kill Switch: test proof_type=hmac rejected when APPROVAL_CLI_HMAC_ENABLED=false (403 proof_type_disabled).
4. G-TIMEOUT Invariant: test timeout approval injection rejected (403 AgentImpersonationError). Inactivity or timeouts never create approvals or decisions.
5. Scope dormancy: verify DISPATCH_ENABLED=false and zero player delivery.
6. Execute empirical tests:
   .\.venv\Scripts\pytest.exe tests/identity/test_player.py tests/test_packaging.py tests/test_phase1_scope.py -v
   (Set WaitMsBeforeAsync=10000). If permission prompt times out, document honestly and verify via AST / static analysis.
7. Provide an explicit verdict in your handoff report: APPROVE or REQUEST_CHANGES.
Deliver complete report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_2\handoff.md
When finished, notify orchestrator with your verdict and report path via send_message.
