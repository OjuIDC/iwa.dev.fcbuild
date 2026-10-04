# Dispatch Instructions for challenger_m3_1

**Agent ID**: `challenger_m3_1`
**Role**: Session Dual-Expiry & Lifecycle Challenger
**Milestone**: Phase 2 Milestone 3 — Server-Side Sessions & Invitations Lifecycle
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read §5, §7, §8, §11, §14)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md`

## Mandatory Non-Negotiable Invariants
- `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` strictly enforced at all times.
- Zero player delivery; no active worker or relay.
- Plaintext tokens are NEVER stored in PostgreSQL; only deterministic SHA-256 hashes (`hash_token()`).
- Dual session timeouts: 30-minute idle expiration strictly capped at 24-hour absolute expiration.
- Suspended or disabled accounts fail closed immediately on session validation (HTTP 401).
- Session rotation on privilege change revokes previous session and issues new session in a single transaction.

## Adversarial Verification Tasks
Empirically stress-test the session subsystem:
1. **Dual-Expiry Invariant**:
   - Verify that an idle session (>30 min) fails closed with HTTP 401 even if within the 24-hour absolute window.
   - Verify that an expired absolute session (>24 hrs) fails closed with HTTP 401 regardless of recent touches.
   - Verify that calling `/v1/auth/sessions/touch` extends `idle_expires_at` but NEVER extends beyond `absolute_expires_at`.
2. **Session Rotation & Invalidation**:
   - Verify that rotating a session atomically revokes the old session (`revoked_at = now()`).
   - Verify that the old session token fails closed with HTTP 401 immediately after rotation.
   - Verify that logging out (`POST /v1/auth/logout`) or deleting a session immediately revokes it.
3. **Account Suspension & User Status Invariants**:
   - Verify that suspending a user account immediately invalidates all active sessions across all devices (HTTP 401).
   - Verify that a disabled user account cannot be used to authenticate.
4. **Anti-Impersonation & Player Isolation**:
   - Verify that service tokens (BFF, Graph, Worker) cannot call human session management endpoints (`AgentImpersonationError` / 403).
   - Verify that Player A cannot inspect or revoke Player B's sessions (`PlayerIsolationError` / 403).
5. **Execution**:
   - Write and execute adversarial test scripts, run `pytest tests/identity/test_routers_sessions.py -v`.
   - Document commands, test execution logs, and empirical evidence in `handoff.md`.
6. **Verdict**:
   - Provide an explicit verdict: `APPROVE` or `REQUEST_CHANGES`.

Write your report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md`.
When finished, notify me with your verdict and report path via `send_message`.

## 2026-10-04T05:00:05Z
You are challenger_m3_1, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Empirically stress-test session dual-expiry, rotation, and lifecycle invariants for Phase 2 Milestone 3.
1. Dual-Expiry: verify idle expiration (30m) fails closed with 401 even within 24h absolute; verify absolute expiration (24h) fails closed with 401; verify touch strictly caps idle_expires_at <= absolute_expires_at.
2. Rotation & Invalidation: verify atomic rotation revokes old session and old token returns 401; verify logout/delete revokes session immediately.
3. User Status: verify account suspension immediately fails closed all active sessions across all devices (401); verify disabled account cannot authenticate.
4. Anti-Impersonation & Player Isolation: verify service tokens cannot call human session management endpoints (403); verify player isolation.
5. Write and execute stress tests, document in handoff.md.
6. Provide an explicit verdict: APPROVE or REQUEST_CHANGES.
Write report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
