# Dispatch: Milestone 2 Test Architecture Explorer

## Identity
- Archetype: teamwork_preview_explorer
- Working Directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_tests_1

## Mandatory Inputs
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (including Section 1A patch).
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`.
3. Existing test files: `tests/test_graph.py`, `tests/test_approval_lifecycle.py`, `tests/test_approval_security.py`.
4. `tests/e2e/test_tier1_features.py`.
5. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Tasks 5 and 6).

## Task
Perform deep exploration and detailed design for Milestone 2 unit, integration, and security tests:
1. Graph Core & Persistence Tests (`tests/test_graph_persistence.py` or updates to `tests/test_graph.py`):
   - Single interrupt verification: graph stops at approval node with exactly 1 interrupt.
   - Resume without committed PostgreSQL decision raises `ApprovalNotCommitted`.
   - Resume payload tampering: client cannot inject decision via resume request; only PostgreSQL decision is read.
   - Terminal state formatting: `"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"`.
   - Checkpoint recovery: checkpointer restores workflow state across process/server restart simulation.
2. Persistent Graph API Tests (`tests/test_graph_api_v1.py`):
   - Versioned `/v1/` routes coverage: `POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/runs/{thread_id}/resume`, `POST /v1/runs/{thread_id}/decision-challenge`, `POST /v1/runs/{thread_id}/decision`, `POST /v1/runs/{thread_id}/kill`.
   - Correlation middleware tests: `X-Correlation-ID` header preservation and generation, context propagation.
   - Safe projection tests: assert absence of sensitive error traces, DB passwords, signing keys in responses.
   - Health probes: `GET /health` and `GET /health/ready` verification.
3. Test Harness & Fixtures:
   - Identify needed client fixtures, mocked/wired dependencies, checkpointer setup in `tests/conftest.py`.
4. Write complete test architecture to `tests_analysis.md` and complete handoff report to `handoff.md` in your working directory.
5. Notify orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.


## 2026-10-03T15:29:54Z
Received message:
You are explorer_m2_tests_1, a teamwork_preview_explorer agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_tests_1
Read instructions in DISPATCH.md, perform deep exploration and detailed design for Milestone 2 test architecture.
