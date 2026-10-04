# Dispatch Instructions for explorer_m3_i2_policy_1

**Agent ID**: `explorer_m3_i2_policy_1`
**Role**: Policy Ownership & Session Security Explorer
**Milestone**: Phase 2 Milestone 3 (Iteration 2 Remediation)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_policy_1`

## Full Forensic Audit & Review Evidence (MANDATORY TO READ)
1. Full Audit Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
2. Reviewer Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md`
3. Challenger Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md`
4. Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
5. Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`

## Problem Statement from Audit & Review
1. **Authorization Bypass on Session Revocation**:
   - `src/identity/policy.py` grants `ActionType.SESSION_REVOKE` and `SESSION_VIEW` to `COACH`, `ANALYST`, and `STAFF`.
   - However, Step 5 object-level isolation only checks `if principal.is_player:`. Non-player staff can revoke or view sessions of ANY user in the club (including the Commissioner).
   - Expected invariant: Only Commissioner or Commissioner Delegate may view or revoke another user's sessions. Regular club members (`coach`, `analyst`, `staff`, `player`) can ONLY view or revoke their OWN sessions (`session.user_id == principal.user_id`).
2. **Arbitrary Client Elevation on Rotation**:
   - In `src/identity/routers/sessions.py` (`POST /v1/auth/sessions/rotate`), the endpoint accepts `new_auth_level` from client body and applies it without cryptographic step-up verification.
   - At Milestone 3, rotation must NOT allow arbitrary elevation to `step_up_passkey` without proof. It should preserve current auth level, or if elevation is requested, fail closed with HTTP 403 / 422 until WebAuthn step-up is supplied.
3. **Anti-Impersonation Gaps on Session Endpoints**:
   - `GET /v1/auth/sessions` and `DELETE /v1/auth/sessions` must strictly require human principals (`require_human_principal`) to prevent service principals from snooping or clearing human sessions.

## Your Task
1. Inspect `src/identity/policy.py` lines 650–700 and `src/identity/routers/sessions.py`.
2. Formulate the exact fix recommendations:
   - In `policy.py`, enforce that for `SESSION_VIEW`, `SESSION_REVOKE`, and `SESSION_REVOKE_ALL`, if `res.owner_user_id` is present and `res.owner_user_id != principal.user_id`, the action is DENIED (`raise RoleNotPermittedError("only commissioner or delegate can view/revoke another user's session")`) unless `principal.is_commissioner` or `principal.is_delegate`.
   - In `sessions.py`, guard `rotate_session` against unverified privilege escalation.
   - In `sessions.py`, ensure all human session endpoints use `require_human_principal`.
3. Provide line-by-line patch proposals and rationale. Do NOT modify source code directly.
4. Deliver your complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_policy_1\handoff.md`.
When finished, notify me with your report path via `send_message`.


## 2026-10-04T05:10:17Z
You are explorer_m3_i2_policy_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_policy_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_policy_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Full Forensic Audit Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md
Reviewer Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md
Challenger Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and the full audit report before starting work.

Your task:
Analyze and formulate exact fix proposals for policy ownership and router session security:
1. In src/identity/policy.py (Step 5), enforce that for SESSION_VIEW, SESSION_REVOKE, and SESSION_REVOKE_ALL, if res.owner_user_id is present and != principal.user_id, the action is forbidden unless principal is commissioner or delegate.
2. In src/identity/routers/sessions.py, guard rotate_session against unproven step_up_passkey elevation.
3. In src/identity/routers/sessions.py, ensure all human session endpoints enforce require_human_principal.
4. Provide exact code diffs and rationale. Do NOT edit source files directly.
5. Deliver handoff report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_policy_1\handoff.md
When finished, notify me with your report path via send_message.
