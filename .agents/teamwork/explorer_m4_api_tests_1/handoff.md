# Handoff Report: Milestone 4 Graph API & Test Suite Exploration

**Agent**: `explorer_m4_api_tests_1`  
**Archetype**: `teamwork_preview_explorer`  
**Role**: Milestone 4 API Routes & Test Suite Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_api_tests_1`  
**Timestamp**: 2026-10-03T20:15:00Z  
**Type**: Hard Handoff (Exploration Complete)  

---

## 1. Observation

1. **Task 8 Implementation Plan Specification**:
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md`, lines 919–1040:
     - Outlines SQL objects: `public.activation_gate_log`, `public.activation_gates` view, `public.service_heartbeats`.
     - Outlines Python modules: `src/dispatch/gates.py`, `src/dispatch/creation.py`.
     - Specifies Graph API changes: `ApiSettings` with `code_revision` and `deployment_id`, `reevaluate_gates(app)`, routes `/v1/config/dispatch`, `POST /v1/activation-gates/{gate_id}/challenge`, `POST /v1/activation-gates/{gate_id}`, and wiring into the decision endpoint.
     - Specifies 24 test cases in `tests/test_dispatch_creation.py` and the `enabled_client` fixture in `conftest.py`.

2. **Current Graph API Implementation**:
   - `src/graph_api.py`, lines 59–73: `ApiSettings` currently lacks `code_revision` and `deployment_id`.
   - `src/graph_api.py`, lines 236–237: `lifespan()` initializes static placeholder `app.state.dispatch_effective = False` and `app.state.gate_report = {"configured": settings.dispatch_enabled, "effective": False, "gates": []}`, with no dynamic evaluation loop.
   - `src/graph_api.py`, lines 380–388: `GET /v1/config/dispatch` returns hardcoded `effective: False` and empty `gates: []`.
   - `src/graph_api.py`, lines 562–598: `apply_decision_internal()` passes `dispatch_enabled=settings.dispatch_enabled` instead of `dispatch_enabled=app.state.dispatch_effective`, does not provide `dispatch_creator=create_dispatch_for_approval`, and hardcodes `dispatch_block` with `enabled=False, created=False`.
   - `src/graph_api.py`: Activation gate routes (`/v1/activation-gates/{gate_id}/challenge` and `/v1/activation-gates/{gate_id}`) are completely absent.

3. **Current Approval Store Boundaries**:
   - `src/approval_store.py`, lines 590–605:
     ```python
     dispatch_id: UUID | None = None
     if dispatch_enabled and intent.decision is DecisionKind.APPROVE and dispatch_creator is not None:
         try:
             dispatch_id = dispatch_creator(
                 conn,
                 thread_id=thread_id,
                 thread=thread,
                 audit_id=audit_id,
                 req_id=req["approval_request_id"],
                 correlation_id=corr_id,
             )
         except Exception as exc:
             if exc.__class__.__name__ == "NotDispatchable":
                 raise StoreError(409, f"not_dispatchable: {exc}") from exc
             raise
     ```
     When `dispatch_creator` raises `NotDispatchable` or any DB exception, it bubbles out to rollback the decision transaction cleanly.

4. **Existing E2E Test Expectations**:
   - `tests/e2e/test_tier1_features.py`, lines 500–516:
     - `test_feature_35_activation_gate_evaluation_fails_closed`: Verifies `evaluate_activation_gates(conn)` returns `effective=False`.
     - `test_feature_36_gate_challenge_and_record_routes_exist`: Calls `POST /v1/activation-gates/G-OUTBOX/challenge` with `json={}` and asserts status in `(409, 403)`.

5. **Existing Test Fixtures & Helpers**:
   - `tests/helpers.py`: Has `candidate()`, `fake_packages()`, `make_app()`, `start()`, `decide()`, `signed()`.
   - Lacks `fake_packages_dispatchable()` and `seed_verified_players()`.
   - `tests/conftest.py`: Lacks `enabled_client` fixture.

---

## 2. Logic Chain

1. **Configuration & Posture** (from Observation 1 & 2):
   - To support deployment-bound cryptographic gates, `ApiSettings` must read `FC27_CODE_REVISION` and `FC27_DEPLOYMENT_ID` using `read_secret(..., required=False)`.
   - If either is absent/empty, gate challenge and recording endpoints must return `409 deployment_identity_missing` to fail closed against untracked builds.

2. **Lifespan & Runtime Evaluation** (from Observation 1 & 2):
   - `reevaluate_gates(app: FastAPI)` must execute `evaluate_gates(conn, identity)` using `psycopg.connect(settings.postgres_uri)` and update `app.state.gate_report` and `app.state.dispatch_effective = dispatch_effective(settings.dispatch_enabled, report)`.
   - Running this at startup, every 60s in `_background_loop()`, and exposing it for direct invocation allows tests to dynamically verify gate state changes without server process restarts (satisfying C-1).

3. **Activation Gate Endpoints** (from Observation 1, 2, 4):
   - `POST /v1/activation-gates/{gate_id}/challenge`:
     - Must restrict to Commissioner role via `Depends(commissioner)` and enforce CLI HMAC via `Depends(require_hmac)`.
     - Request body model `GateChallengeRequest` must allow defaulting (`status='passed', evidence_ref='manual-verification'`) to support empty payloads `json={}` used in `test_tier1_features.py`.
     - Rejects automatic gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`) with 422 `automatic gate`.
     - Rejects `G-OUTBOX` with 409 `not_recordable_before_phase_3` (satisfying C-3).
     - Issues single-use nonce stored in `approval_nonces` with `subject_type='activation_gate'`, `gate_id`, payload SHA-256, and expiration <= 300s.
   - `POST /v1/activation-gates/{gate_id}`:
     - Verifies HMAC signature over `GATE_SIGNED_FIELDS` using `APPROVAL_SIGNING_SECRET`.
     - In a single database transaction `FOR UPDATE`: checks nonce is unconsumed, unrevoked, unexpired, and matches gate identity and payload hash; consumes nonce; inserts into `activation_gate_log`; emits `activation.gate_recorded` audit-only event.
     - Returns `effective_after: "next_evaluation"` with timestamp (satisfying C-1).

4. **Decision Route Integration & Transaction Atomicity** (from Observation 1, 2, 3):
   - In `apply_decision_internal()` in `src/graph_api.py`, passing `dispatch_enabled=app.state.dispatch_effective` and `dispatch_creator=create_dispatch_for_approval` ensures dispatch creation only executes when all gates have passed.
   - Because `dispatch_creator` runs inside the same PostgreSQL transaction in `ApprovalStore.apply_decision()`, any failure (such as missing objective, unverified player, or DB trigger error) rolls back the entire decision transaction, leaving the thread and request in `awaiting_approval` and the decision nonce unconsumed.

5. **Test Architecture & Coverage** (from Observation 1 & 5):
   - `fake_packages_dispatchable()` in `tests/helpers.py` provides candidates with valid `Objective` objects and numeric player IDs.
   - `seed_verified_players()` seeds `players` table with active, verified players.
   - `enabled_client` in `tests/conftest.py` configures `dispatch_enabled=True`, seeds test gate records (`G-ID`, `G-ATOMIC`, synthetic `G-OUTBOX`, fresh worker heartbeat), seeds verified players, and executes `reevaluate_gates(app)` directly.
   - 24 targeted tests in `tests/test_dispatch_creation.py` exhaustively verify all security invariants, cryptographic checks, replay rejection, tamper detection, rollback behaviors, and concurrency locks.

---

## 3. Caveats

1. **Phase 1 Invariant Preservation**:
   - `DISPATCH_ENABLED` remains `false` in production environment settings.
   - `enabled_client` is strictly a synthetic test harness fixture. Synthetic records for `G-OUTBOX` and worker heartbeats do not represent production evidence.
2. **No Player Delivery**:
   - Creating a `pending` row in `public.dispatches` does NOT deliver any payload to any player or adapter. Delivery remains strictly out-of-scope for Phase 1.
3. **No Direct Source Mutations**:
   - In accordance with explorer read-only constraints, no changes have been made to `src/` or `migrations/`. Full designs and drop-in code snippets have been provided in `api_tests_analysis.md`.

---

## 4. Conclusion

The design for Milestone 4's Graph API routes, settings, lifecycle evaluation, test helpers, and test suite is fully analyzed, specified, and aligned with Task 8 of the implementation plan and the authoritative user request.

The design satisfies all core constraints:
- Fail-closed security when gates fail.
- Cryptographic HMAC challenge/response verification with deployment-identity binding and nonce single-use.
- Atomic dispatch creation within the decision transaction.
- Complete rollback of the approval decision if dispatch creation fails.
- Complete 24-test verification matrix.

Implementation workers can proceed with implementation of `src/graph_api.py`, `tests/helpers.py`, `tests/conftest.py`, and `tests/test_dispatch_creation.py`.

---

## 5. Verification Method

To verify the implementation once coded:

1. **Run New Milestone 4 Test Suite**:
   ```bash
   python -m pytest -o addopts="" -q tests/test_dispatch_creation.py
   ```
   *Expected Result*: All 24 test cases pass cleanly.

2. **Run Full Test Suite & E2E Regressions**:
   ```bash
   python -m pytest -o addopts="" -q
   ```
   *Expected Result*: All tests pass (including `tests/e2e/test_tier1_features.py` Features 35, 36, and 37).

3. **Verify Read-Only Explorer State**:
   ```bash
   git status --short
   ```
   *Expected Result*: Only files inside `.agents/teamwork/explorer_m4_api_tests_1/` are created/modified. No changes to `src/` or `migrations/`.
