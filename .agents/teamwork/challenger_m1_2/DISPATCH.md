# Dispatch: Challenger 2 (Adversarial Authority & Schema Tamper Verifier)

## Identity
- Archetype: teamwork_preview_challenger
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1\handoff.md`.

## Task
Adversarially challenge Section 1A authority invariants, trigger immutability, and database pair integrity:
1. Section 1A Authority Attacks:
   - Challenge timeout approval: verify that expired requests NEVER transition to `approved`, under any circumstance.
   - Challenge unauthorized actors: attempt decision submission with `actor_type='player'`, `authority_basis='affected_player_fallback'`, or invalid delegate tokens in Phase 1; verify all fail closed (403/409).
   - Verify zero automated decisions can be recorded.
2. Schema & Trigger Tamper Attacks:
   - Attempt direct SQL mutation of `package_version` on a thread in `awaiting_approval`; verify `CheckViolation` ('package_version is immutable').
   - Attempt direct SQL UPDATE or DELETE on terminal rows in `approval_requests`; verify `CheckViolation` ('approval_request is terminal and frozen').
   - Attempt committing an unlinked or mismatched thread/request pair; verify deferred constraint trigger `fc27_check_approval_pair()` blocks the commit.
   - Verify `public.fc27_approval_inconsistencies` view accurately flags corrupted test fixtures.
3. Run test verification:
   `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_security.py`
4. Formulate explicit verdict: `APPROVE` or `REQUEST_CHANGES`.
5. Write your challenge findings to `challenge_report.md` and complete handoff report to `handoff.md`.
6. Notify the orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T15:19:15Z
[Message] timestamp=2026-10-03T15:19:15Z sender=7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b priority=MESSAGE_PRIORITY_HIGH content=You are challenger_m1_2, a teamwork_preview_challenger agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (including Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_1\handoff.md

Task:
Adversarially challenge Section 1A authority invariants, trigger immutability, and database pair integrity:
1. Section 1A Attacks: timeout never approves; unauthorized actors fail closed.
2. Schema & Trigger Tamper Attacks: package_version immutability while awaiting; terminal row frozen check; twin pair consistency triggers.
3. Run tests:
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_security.py
4. Formulate explicit verdict: APPROVE or REQUEST_CHANGES.
5. Write challenge_report.md and handoff.md in your working directory.
6. Notify orchestrator with send_message when finished.
Do NOT modify production source code in src/ or migrations/.
