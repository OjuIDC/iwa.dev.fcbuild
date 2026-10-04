# DISPATCH: explorer_m3_tests_1

- **Assigned Role**: M3 Test Architecture Explorer
- **Archetype**: teamwork_preview_explorer
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_tests_1
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `tests/test_migration_0003.py`, `tests/conftest.py`
4. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Tasks 6 & 7)

## Exploration Objectives
Explore and provide exact test plan and test cases for Milestone 3:
1. `tests/test_dispatch_schema.py`:
   - Verification of `players` table constraints, squad_number unique per club, foreign keys.
   - Verification of `dispatches` table constraints: status domain, initial versions, foreign keys.
   - Immutability guards on `dispatches`: updating terminal dispatch raises error, updating immutable provenance (`thread_id`, `player_id`, `approval_request_id`) raises error.
   - Tracking tables verification: `dispatch_plans`, `player_observations`, `dispatch_assessments`.
   - Role permissions: `fc27_app` cannot directly UPDATE or DELETE `dispatches` (must use transition function).
2. `tests/test_dispatch_transition.py`:
   - Valid state machine transitions (pending -> running, running -> paused, etc.).
   - Transition rules matrix enforcement: invalid transitions rejected with check/custom error.
   - Single write path security: calling `fc27_dispatch_transition` as `fc27_app` succeeds for allowed roles.
   - Role restriction: calling with unauthorized role fails closed.
   - Actor kind restriction: calling with disallowed `actor_kind` fails closed.
   - Phase 1 Invariant: transition to `delivered` fails closed (`DISPATCH_ENABLED=false`).
   - Transactional audit and outbox verification: each successful transition emits `dispatch.transitioned` outbox event.
3. Fixtures needed in `tests/conftest.py` (e.g. `pg_dispatch_uri`, sample player, sample dispatch).

## Output Requirements
1. Write `tests_analysis.md` and `handoff.md` in your working directory.
2. Outline exact test function specifications, assertions, and execution commands.
3. Notify orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T17:22:27Z
You are explorer_m3_tests_1, a teamwork_preview_explorer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_tests_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_tests_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. tests/test_migration_0003.py, tests/conftest.py
4. docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md (Tasks 6 & 7)

Task:
Perform deep technical exploration and detailed design for Milestone 3 test architecture:
1. tests/test_dispatch_schema.py: table schema tests, constraint checks, triggers, role privileges (fc27_app cannot directly UPDATE dispatches).
2. tests/test_dispatch_transition.py: valid transitions, rules matrix checks, unauthorized role/actor rejection, Phase 1 delivered block, outbox event verification.
3. Test fixtures and conftest requirements.
Write tests_analysis.md and handoff.md in your working directory.
Notify orchestrator via send_message when finished.
Do NOT modify production source code in src/ or migrations/.
