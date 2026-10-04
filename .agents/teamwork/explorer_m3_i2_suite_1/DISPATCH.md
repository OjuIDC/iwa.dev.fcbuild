# Dispatch Instructions for explorer_m3_i2_suite_1

**Agent ID**: `explorer_m3_i2_suite_1`
**Role**: Verification Matrix & Test Remediation Explorer
**Milestone**: Phase 2 Milestone 3 (Iteration 2 Remediation)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_suite_1`

## Full Forensic Audit & Review Evidence (MANDATORY TO READ)
1. Full Audit Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
2. Reviewer Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md`
3. Challenger Reports:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\handoff.md`
4. Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
5. Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`

## Problem Statement from Audit & Review
1. In `tests/identity/test_routers_invitations.py`, `test_routers_sessions.py`, `test_routers_users.py`:
   - 11 call sites call `store.create_session(..., club_id=TEST_CLUB_ID, ...)`.
   - In `test_routers_sessions.py` line 154: `SET expires_at = ...` causes `psycopg.errors.UndefinedColumn` because `fc27_sessions` defines `absolute_expires_at` (not `expires_at`).
   - In line 165: accesses `data["expires_at"]` which fails because `SessionTouchResponse` outputs `absolute_expires_at`.
   - In `test_routers_sessions.py` line 261: `test_user_cannot_revoke_other_user_session` fails because of the authorization bypass in `policy.py`.
2. Adversarial test suites authored by challengers:
   - `tests/identity/test_adversarial_m3_2.py` (14 tests authored by challenger_m3_2)
   - `tests/identity/test_session_lifecycle_adversarial.py` (13 tests authored by challenger_m3_1)

## Your Task
1. Inspect all 5 test files mentioned above.
2. Formulate the exact, comprehensive test repair plan:
   - Identify every line in `test_routers_invitations.py`, `test_routers_sessions.py`, and `test_routers_users.py` that needs patching.
   - Align all tests with the database schema (`absolute_expires_at`), the Pydantic models (`SessionTouchResponse`), and the hardened authorization policy (HTTP 403 on other user's session revocation).
   - Ensure the entire suite of tests (packaging, scope, store, policy, routers, adversarial, and E2E) can execute cleanly.
3. Provide the full verification matrix and exact test execution commands. Do NOT modify source files directly.
4. Deliver your complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_suite_1\handoff.md`.
When finished, notify me with your report path via `send_message`.


## 2026-10-04T05:10:17Z
You are explorer_m3_i2_suite_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_suite_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_suite_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Full Forensic Audit Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md
Reviewer Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md
Challenger Reports:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_2\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and the full audit report before starting work.

Your task:
Analyze and formulate the comprehensive test repair plan across all Milestone 3 test suites:
1. Inspect tests/identity/test_routers_invitations.py, test_routers_sessions.py, test_routers_users.py, test_session_lifecycle_adversarial.py, and test_adversarial_m3_2.py.
2. Identify and detail all required fixes (column expires_at -> absolute_expires_at, response model field alignments, remove illegal parameters, verify test_user_cannot_revoke_other_user_session now asserts 403 correctly).
3. Formulate the complete verification matrix and test execution commands for the Worker and subsequent Reviewers.
4. Deliver handoff report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_suite_1\handoff.md
When finished, notify me with your report path via send_message.
