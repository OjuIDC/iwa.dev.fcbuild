# Dispatch: E2E Testing Track (Opaque-Box Requirement-Driven Test Suite)

## Identity
- Archetype: teamwork_preview_test_writer
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including the Section 1A update at header `## 2026-10-03T14:11:05Z`).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.

## Mission & Principles
You are the E2E Testing Track agent. You design and implement a comprehensive, opaque-box, requirement-driven test suite derived strictly from user requirements and specifications (not internal implementation details).
Follow the 4-Tier Test Case Design Methodology:
- Tier 1: Feature Coverage (>=5 tests per feature for all Phase 1 features in PROJECT.md Feature Inventory).
- Tier 2: Boundary & Corner Cases (>=5 tests per feature: empty inputs, max sizes, invalid hashes, stale versions, expired nonces, unauthorized roles, DDL rejection).
- Tier 3: Cross-Feature Combinations (pairwise interactions: decision + expiry races, concurrent migrations + active runs, post-commit resume failure + reconciliation, etc.).
- Tier 4: Real-World Application Scenarios (end-to-end full run lifecycle from run creation to approval interruption to PostgreSQL decision commit to resume to terminal state, verifying DISPATCH_ENABLED=false and no player delivery).

Also cover all Section 1A tests:
- Authority separation (club vs player consent)
- Delegation foundation (expiry, scope)
- Player fallback foundation (single player, non-bypassable)
- Late-decision and conflict handling (prior decision immutable)
- Inactivity/timeout NEVER creates approval!

## Scope Boundaries
- You write ONLY test code and test documentation.
- DO NOT modify implementation code in `src/` or `migrations/`.
- Create tests under `tests/e2e/` (e.g. `tests/e2e/test_tier1_features.py`, `tests/e2e/test_tier2_boundaries.py`, `tests/e2e/test_tier3_combinations.py`, `tests/e2e/test_tier4_scenarios.py`, or similar structured files).

## Output Requirements
1. Create `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_1\TEST_INFRA.md` using the template in the orchestrator instructions.
2. Implement the test suite under `tests/e2e/`.
3. When complete, create `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_1\TEST_READY.md`.
4. Write `handoff.md` in your working directory and notify orchestrator with `send_message`.


## 2026-10-03T14:15:37Z
Sender: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b
You are e2e_test_writer_1, a teamwork_preview_test_writer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md

Task:
Design and build the comprehensive opaque-box E2E test suite across Tiers 1-4 for Phase 1.
1. Write c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_1\TEST_INFRA.md
2. Create the test suite in tests/e2e/ covering:
   - Tier 1: Feature coverage for all Phase 1 features in PROJECT.md
   - Tier 2: Boundary & corner cases
   - Tier 3: Cross-feature combinations
   - Tier 4: Real-world application scenarios
   - Section 1A: Authority separation, delegation, player fallback, timeout cannot approve, late decision conflicts.
3. Write c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\e2e_test_writer_1\TEST_READY.md
4. Write handoff.md and send a completion message to the orchestrator.
Do NOT modify any production source code in src/ or migrations/.
