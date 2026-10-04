## 2026-10-03T20:15:58Z

You are worker_m4_1, a teamwork_preview_worker agent.
Your working directory is: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1

Read your instructions in:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\DISPATCH.md

MANDATORY: You MUST read:
1. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md (authoritative user request + Section 1A patch)
2. c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md
3. Explorer 1 Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_schema_1\schema_analysis.md and handoff.md
4. Explorer 2 Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_creation_1\creation_analysis.md and handoff.md
5. Explorer 3 Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_api_tests_1\api_tests_analysis.md and handoff.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write Ownership (Exclusive):
- migrations/0003_dispatch_foundation.sql (Section E)
- migrations/post/grants.sql (bump to policy version 5)
- src/db_migrate.py (update GRANTS_POLICY version 5 and sha256)
- src/dispatch/gates.py (new module)
- src/dispatch/creation.py (new module)
- src/approval_store.py
- src/graph_api.py
- tests/helpers.py
- tests/conftest.py
- tests/test_dispatch_creation.py

Task:
Implement Milestone 4: Activation Gates & Atomic Approval-to-Dispatch Creation per the 3 Explorer handoff reports:
1. Database Schema (Section E in migrations/0003_dispatch_foundation.sql):
   - Append Section E: public.activation_gate_log, fc27_activation_gate_log_immutable trigger calling fc27_reject_mutation(), public.activation_gates view (DISTINCT ON gate_id), public.service_heartbeats table.
   - Update fc27_check_event_source() to validate activation.gate_recorded domain events against activation_gate_log.
   - Update migrations/post/grants.sql to -- grants-policy-version: 5 (grant SELECT, INSERT on activation_gate_log, USAGE on sequence, SELECT on view, SELECT on heartbeats to fc27_app; grant SELECT, INSERT, UPDATE on heartbeats to fc27_dispatch).
   - Update src/db_migrate.py GRANTS_POLICY = GrantsPolicy(version=5, sha256="...") matching grants.sql.
2. Activation Gates Logic (src/dispatch/gates.py):
   - GateResult, RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"}), GATE_SIGNED_FIELDS (excluding X-Correlation-Id), DeploymentIdentity, evaluate_gates(conn, identity), dispatch_effective(configured, results).
3. Atomic Dispatch Creation (src/dispatch/creation.py & src/approval_store.py):
   - NotDispatchable exception.
   - create_dispatch_for_approval(...) with pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0)), objective validation, verified active player check, supersede transition of active dispatches, insert pending dispatch, emit dispatch.created event and outbox row.
   - Wire inside ApprovalStore.apply_decision within conn.transaction(), clean rollback on NotDispatchable or DB errors. Update idempotent replay to return dispatch_id from dispatches.
4. Graph API Routes (src/graph_api.py):
   - ApiSettings: code_revision, deployment_id.
   - reevaluate_gates(app) on lifespan startup and periodically / on demand.
   - GET /v1/config/dispatch: returns configured, effective, gates, deployment_id, code_revision.
   - POST /v1/activation-gates/{gate_id}/challenge: commissioner only, HMAC enforced, approval_nonces single-use challenge.
   - POST /v1/activation-gates/{gate_id}: verify HMAC signature over GATE_SIGNED_FIELDS, consume nonce, record in activation_gate_log, emit event.
   - Wire apply_decision_internal: pass dispatch_enabled=app.state.dispatch_effective and dispatch_creator=create_dispatch_for_approval.
5. Tests (tests/helpers.py, tests/conftest.py, tests/test_dispatch_creation.py):
   - tests/helpers.py: fake_packages_dispatchable, seed_verified_players.
   - tests/conftest.py: enabled_client fixture.
   - tests/test_dispatch_creation.py: all 24 tests per Explorer 3 specification.
6. Execution Verification:
   Run tests:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_creation.py tests/test_dispatch_schema.py tests/test_dispatch_transition.py tests/test_db_migrate_roles.py
   Run core regression tests:
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_api_v1.py
