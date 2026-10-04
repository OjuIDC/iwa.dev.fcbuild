# BRIEFING — 2026-10-03T14:10:00Z

## Mission
Comprehensive survey of LangGraph implementation, Graph API, runtime state, approval mechanics, and tests against Phase 1 requirements.

## 🔒 My Identity
- Archetype: teamwork_preview_explorer
- Roles: explorer, analyst, investigator
- Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_graph_1
- Original parent: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b
- Milestone: phase_1_survey

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Do NOT edit, write, or modify repository code files
- Only write metadata, reports, and handoffs in .agents/teamwork/explorer_survey_graph_1/

## Current Parent
- Conversation ID: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `ORIGINAL_REQUEST.md` (authoritative specification for Phase 1)
  - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` & `specs/2026-10-02-player-dispatch-design.md`
  - `src/graph.py`, `src/graph_api.py`, `src/approval.py`, `src/approval_store.py`, `src/decision.py`, `src/lead_packages.py`, `src/events.py`, `src/security.py`, `src/thread_ids.py`, `src/db_roles.py`, `src/db_migrate.py`
  - `migrations/0001_initial.sql`, `migrations/0002_approval_security.sql`, `migrations/0003_dispatch_foundation.sql`, `migrations/post/grants.sql`
  - `tests/test_langgraph_flow.py`, `tests/test_approval_security.py`, `tests/test_events_outbox.py`, `tests/test_compose_isolation.py`, `tests/test_workflow.py`, `tests/conftest.py`, `tests/helpers.py`
- **Key findings**:
  1. LangGraph Core (`src/graph.py`):
     - Uses `StateGraph` with sync PostgresSaver checkpointer.
     - Single interruption point exists at `interrupt(state["approval_request"])` inside the `approval` node. `interrupt_before=["approval"]` does not exist in code (confirmed absent).
     - Packages are built in `prepare_approval` but NOT persisted to an authoritative database table before interrupt.
     - Interrupt payload lacks required fields: `approval_request_id`, `package_id`, `package_hash`, safe summary, and required authority.
     - Resume ignores `Command(resume=...)` payload and re-reads Postgres `graph_threads`, but only checks `package_version` and status. It does not validate package ID, hash, or approval request validity.
     - `src/decision.py` sets `state["dispatch"] = {"dispatched_to_player_agent": intent.player_id}`, violating the non-negotiable invariant that Phase 1 must not transition to dispatched or deliver to a player.
     - Terminal states are currently `"approved"` / `"rejected"` rather than `"APPROVED, NOT DISPATCHED"` / `"REJECTED, NO ACTION TAKEN"`.
  2. Persistent Graph API (`src/graph_api.py`):
     - Endpoints are unversioned: `/run`, `/threads/{id}`, `/threads/{id}/decision-challenge`, `/threads/{id}/decision`, `/health`. Missing `/v1/` prefix and versioned routes required by Phase 1.
     - Missing routes: `POST /v1/runs`, `GET /v1/runs/{thread_id}` (projection), `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/resume`, `GET /health/live`, `GET /health/ready`, `GET /v1/config/dispatch`.
     - `GET /threads/{id}` leaks entire raw state dictionary instead of safe caller projection.
     - `dispatch_enabled` is missing from `ApiSettings`, startup posture checks, and run responses.
     - Decision writes are keyed on `thread_id` instead of `approval_request_id`, and do not write to `domain_events`/`outbox`.
  3. Test Suite:
     - 254 tests existed in prior baseline (v2.0.0); new tests for db_roles, db_migrate, events_outbox added in Tasks 1-3.
     - Existing tests verify HMAC signatures, single-use nonces, atomicity, and basic LangGraph interrupt/resume.
     - Gaps exist for: versioned API routes, safe projections, approval_requests lifecycle, package immutability, gate evaluation, and negative authorization tests.
- **Unexplored areas**:
  - Full automated pytest run was skipped because default system python is Python 3.14 without pytest; test structure and test code were surveyed statically.

## Key Decisions Made
- Proceeding to author comprehensive survey report `survey_graph_report.md` and `handoff.md`.

## Artifact Index
- DISPATCH.md — incoming dispatch log
- BRIEFING.md — persistent situational awareness
- progress.md — liveness heartbeat
- survey_graph_report.md — detailed survey report
- handoff.md — 5-component handoff report
