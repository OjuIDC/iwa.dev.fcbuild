# Task Assignment — e2e_test_writer_phase2_1

## Objective
Design and author the requirement-driven, opaque-box Phase 2 E2E test infrastructure and comprehensive test cases covering all 46 Phase 2 features across Tiers 1 through 4.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Specifically Phase 2 §15, §16, §22)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Existing E2E test suite in `tests/e2e/`

## Requirements
- Produce `TEST_INFRA.md` summarizing test philosophy, feature inventory coverage, test architecture, and scenarios.
- Author opaque-box tests in `tests/e2e/`:
  - Tier 1: Feature Coverage (>=5 tests per functional domain)
  - Tier 2: Boundary & Corner Cases (limits, nulls, expired tokens, invalid states)
  - Tier 3: Cross-Feature Combinations (invitations -> sessions -> passkeys -> step-up)
  - Tier 4: Real-World Scenarios (complete Commissioner lifecycle, player consent lifecycle, recovery lifecycle)
- Publish `TEST_READY.md` when the test suite is ready.
- Report at `.agents/teamwork/e2e_test_writer_phase2_1/handoff.md`.


## 2026-10-04T02:46:14Z
From: parent (25210876-a9ad-4f78-a87c-dec88e86edb9)
Priority: MESSAGE_PRIORITY_HIGH

You are e2e_test_writer_phase2_1, a Test Writer agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
1. Design the comprehensive opaque-box E2E test infrastructure for Phase 2: Identity and Authorization.
2. Produce TEST_INFRA.md following the dual-track testing principles (covering all 46 features across Tiers 1–4).
3. Author the test cases in tests/e2e/ for Phase 2:
   - Tier 1: Feature Coverage (>=5 per feature domain)
   - Tier 2: Boundary & Corner Cases (>=5 per feature domain)
   - Tier 3: Cross-Feature Combinations (pairwise interactions, state flow)
   - Tier 4: Real-World Scenarios (end-to-end Commissioner and Player workflows)
4. Verify tests follow opaque-box principles, testing via HTTP routes and public interfaces without depending on internal implementation details.
5. Publish TEST_READY.md when test infrastructure and test cases are ready.
6. Write your complete handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_phase2_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
