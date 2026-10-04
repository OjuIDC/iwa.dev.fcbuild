# DISPATCH: challenger_m5_i2_1 (Scoped Tokens & Step-Up Replay Challenger)

**Role**: `teamwork_preview_challenger`
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_1`
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
**Worker Handoff**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2\handoff.md`
**Previous Challenger Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1\challenge_report.md`

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
Empirically stress-test Scoped Action Tokens & Decision Step-Up Replay / Scope Lockout for Phase 2 Milestone 5:
1. **Scoped Action Tokens**:
   - Test token replay: consuming already-consumed token fails closed with HTTP 409 (`ActionTokenAlreadyConsumedError`).
   - Test expired token: consuming expired token fails closed with HTTP 410 (`ActionTokenExpiredError`).
   - Test revoked token: consuming revoked token fails closed with HTTP 410 (`ActionTokenRevokedError`).
   - Test scope confusion: using `invitation` or `session_refresh` token on player decision fails closed with HTTP 403 (`ActionTokenScopeMismatchError`).
   - Test assignment mismatch: token minted for assignment A used on assignment B fails closed with HTTP 403 (`ActionTokenAssignmentMismatchError`).
   - Verify SHA-256 storage: verify tokens are stored exclusively as `token_hash` in `fc27_action_tokens`, never plaintext.
2. **Decision Step-Up Replay & Scope Lockout**:
   - Test evidence replay: reusing `authentication_evidence_id` fails closed with HTTP 409 (`evidence_already_consumed`).
   - Test evidence scope lockout: evidence minted for package X, version V1 cannot be consumed for package Y or version V2 (fails closed with HTTP 400 `evidence_scope_mismatch` or HTTP 404).
   - Test no `"unspecified"` bypasses exist in `consume_decision_evidence`.
3. **Scope Dormancy**:
   - Verify `DISPATCH_ENABLED=false` and zero player delivery.
4. **Execute Empirical Tests**:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/identity/test_tokens.py tests/identity/test_step_up.py tests/test_packaging.py tests/test_phase1_scope.py -v
   ```
   (Set `WaitMsBeforeAsync: 10000`). If permission prompt times out, document honestly and verify via AST / static analysis.
5. Provide an explicit verdict in your handoff report: **APPROVE** or **REQUEST_CHANGES**.
6. Deliver report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_1\handoff.md`.
7. Notify orchestrator via `send_message` when complete.


## 2026-10-04T10:12:12Z
[Message] timestamp=2026-10-04T10:12:12Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are challenger_m5_i2_1, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_i2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_2\handoff.md
Previous challenger report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1\challenge_report.md
