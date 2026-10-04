# Dispatch: Milestone 1 Test Explorer

## Identity
- Archetype: teamwork_preview_explorer
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_tests_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. `tests/conftest.py`, `tests/test_approval_security.py`.
4. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 4 test requirements).

## Objective
Analyze and design the test suites for Milestone 1:
1. `tests/test_migration_0003.py`:
   - 0003 migration applied on legacy 0002 data (`legacy_install`).
   - Pair mismatch fails at commit (`fc27_check_approval_pair`).
   - Awaiting thread requires link.
   - Package version immutable while awaiting.
2. `tests/test_approval_lifecycle.py`:
   - `complete_run` creates linked request.
   - Decision updates request and thread together.
   - Decision writes no dispatch key.
   - Expired request blocks prior nonce & blocks new challenge.
   - `expire_due` is idempotent.
   - Expiry and decision race has single outcome (no deadlock).
   - Inbox query never shows terminal threads.
   - Failed decision rolls back everything (audit, events, outbox, request state, nonce).
   - Safe idempotent decision replay.
3. Authority-Separation & Section 1A tests:
   - Timeout/inactivity cannot approve.
   - Unsupported actor types & authority bases fail closed in Phase 1.
   - Schema distinguishes club authorization vs player consent.
4. Fixtures and conftest updates:
   - Check `fc27_approval_inconsistencies` empty in teardown.

## Scope Boundaries
- Read-only exploration and test design. Do NOT modify source files.

## Output Requirements
- Write `tests_analysis.md` in your working directory.
- Write `handoff.md` following the Handoff Protocol.
- Notify orchestrator with `send_message`.


## 2026-10-03T14:15:38Z
You are explorer_m1_tests_1, a teamwork_preview_explorer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_tests_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_tests_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. tests/conftest.py, tests/test_approval_security.py
4. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Task 4)

Task:
Perform a deep technical exploration and detailed design for Milestone 1 unit/security tests:
- tests/test_migration_0003.py: legacy 0002 data migration, pair mismatch commit failure, immutable package_version while awaiting.
- tests/test_approval_lifecycle.py: complete_run request link, decision update, expired request blocks, expire_due idempotency, expiry/decision race, inbox query, rollback verification, safe idempotent replay.
- Section 1A authority separation tests: timeout cannot approve, unsupported actor/basis fail closed.
- conftest updates: fc27_approval_inconsistencies empty check.
Write tests_analysis.md and handoff.md in your working directory, then notify orchestrator with send_message.
Do NOT modify repository files.
