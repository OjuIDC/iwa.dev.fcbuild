# Dispatch Instructions for challenger_m2_2

**Agent ID**: `challenger_m2_2`
**Role**: Delegation Lifecycle & Invariant Challenger
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_2`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Challenger Tasks
Empirically stress-test the human delegation lifecycle and architectural invariants:
1. Delegation Lifecycle Stress:
   - Test delegation evaluation with: expired delegation, future delegation (`starts_at > now`), revoked delegation, delegate user mismatch, club mismatch, action type mismatch.
   - Test attempts to delegate prohibited club governance actions (formations, transfers, roster, discipline). Must fail.
   - Test immediate revocation: once revoked, all subsequent attempts fail immediately.
2. Inactivity and Timeout Invariants (G-TIMEOUT):
   - Verify that timeouts/inactivity alter escalation state or eligibility, but NEVER commit or insert an approval decision.
   - Verify that simulated timeouts cannot trigger automated approvals in `policy.authorize()`.
3. Service Token Boundaries:
   - Test key collision detection (BFF, Graph, Worker sharing same secret must raise `ConfigError`).
   - Test token rotation (old token remains valid during rotation window, invalid tokens fail closed).
   - Test gamertag display separation: verify mutating gamertag/display_name has 0 impact on authorization or session keys.
4. Packaging & Scope Guards:
   - Execute `pytest tests/test_packaging.py tests/test_phase1_scope.py -v`. Ensure `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain strictly enforced.
5. Document commands, findings, and explicit verdict (**APPROVE** or **REQUEST_CHANGES**) in:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_2\handoff.md`


## 2026-10-04T04:21:23Z
[Message from parent 25210876-a9ad-4f78-a87c-dec88e86edb9]
You are challenger_m2_2, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Empirically stress-test delegation lifecycle and architectural invariants in Phase 2 Milestone 2.
1. Delegation Lifecycle: verify expiration, future time bounds (starts_at), immediate revocation, user mismatch, club mismatch, and prohibited club governance action delegation all fail closed.
2. G-TIMEOUT Invariant: verify timeouts/inactivity alter escalation but NEVER commit or create approval decisions.
3. Service Token Boundaries: verify key collision detection (BFF, Graph, Worker sharing same key raises error), token rotation, and gamertag display separation.
4. Scope Guards: verify tests/test_phase1_scope.py passes with DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false strictly enforced.
5. Run stress tests, document commands and results, and provide an explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_2\handoff.md
When finished, notify me with your verdict and report path via send_message.
