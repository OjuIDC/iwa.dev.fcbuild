# Dispatch Instructions for challenger_m2_1

**Agent ID**: `challenger_m2_1`
**Role**: Object Tenancy & Player Isolation Challenger
**Milestone**: Phase 2 Milestone 2 — Centralized Authorization & Service Boundaries
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1`

## Authoritative Inputs
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
3. Worker handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md`

## Challenger Tasks
Empirically stress-test the centralized authorization policy and security boundaries:
1. Object Tenancy Stress:
   - Test cross-club access attempts across all roles (Commissioner, Coach, Player, Service). Ensure immediate rejection with `CrossClubAccessError` / 403.
2. Player Isolation Stress:
   - Test player accessing other player's assignment, profile, decision, or credential. Must fail with `PlayerIsolationError` / 403.
   - Test player attempting club-wide package decision. Must fail.
   - Test player fallback on prohibited club-wide actions (tactics, formations, transfers, roster, discipline). Must fail.
3. Anti-Impersonation Stress:
   - Test service principals (`bff_service`, `graph_service`, worker) attempting human decision actions (`package_approve`, `package_reject`, player decisions). Must fail with `AgentImpersonationError` / 403.
4. Step-Up Passkey Requirements:
   - Test package approvals with session auth level < step_up_passkey. Must fail with `StepUpRequiredError`.
5. Execute stress scripts / tests, document findings, and provide explicit verdict (**APPROVE** or **REQUEST_CHANGES**) in:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\handoff.md`


## 2026-10-04T04:21:22Z
You are challenger_m2_1, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Worker handoff: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Empirically stress-test object-level tenancy and player isolation in Phase 2 Milestone 2.
1. Object Tenancy: verify cross-club access attempts across all roles fail closed with CrossClubAccessError / 403.
2. Player Isolation: verify player accessing other player assignments, profiles, or decisions fails with PlayerIsolationError / 403. Verify player attempting club-wide decisions or prohibited fallback actions fails closed.
3. Anti-Impersonation: verify service principals attempting human decision actions fail closed with AgentImpersonationError / 403.
4. Step-Up Verification: verify package decisions without step-up fail closed with StepUpRequiredError / 403.
5. Run stress tests, document commands and results, and provide an explicit verdict (APPROVE or REQUEST_CHANGES) in:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
