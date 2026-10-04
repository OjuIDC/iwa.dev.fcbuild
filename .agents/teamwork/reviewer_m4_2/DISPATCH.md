# Dispatch: Milestone 4 Reviewer 2 (Gates, Creation, Graph API & Test Suite)

**Agent**: `reviewer_m4_2`
**Role**: Milestone 4 Reviewer 2 (Gates, Creation, Graph API & Test Suite)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2`
**Timestamp**: 2026-10-03T20:36:00Z

---

## 1. Objective
Objectively review and challenge Milestone 4 gate evaluation logic, atomic approval-to-dispatch creation, Graph API routes, and the 24-test suite.

## 2. Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m4_1\handoff.md`
4. `src/dispatch/gates.py`
5. `src/dispatch/creation.py`
6. `src/approval_store.py`
7. `src/graph_api.py`
8. `tests/helpers.py`, `tests/conftest.py`, `tests/test_dispatch_creation.py`

## 3. Scope of Review
1. **Activation Gates Logic (`src/dispatch/gates.py`)**:
   - Verify `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})` (`G-OUTBOX` rejected in Phase 1; C-3).
   - Verify `GATE_SIGNED_FIELDS` strictly excludes `X-Correlation-Id` (C-4, C-5).
   - Verify `DeploymentIdentity` validation and gate matching.
   - Verify `evaluate_gates()` checks G-MIGRATION, G-GRANTS, G-WORKER, G-ID, G-ATOMIC, G-OUTBOX.
   - Verify `dispatch_effective()` returns False in Phase 1 production.
2. **Atomic Dispatch Creation (`src/dispatch/creation.py` & `src/approval_store.py`)**:
   - Verify candidate objective model/bounds check.
   - Verify numeric player ID format `^[0-9]{1,20}$` and exact matching in `public.players` with active/verified status.
   - Verify advisory transaction lock `pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))`.
   - Verify supersede transition before new dispatch insert.
   - Verify `ApprovalStore.apply_decision` runs dispatch creation in transaction block with clean rollback on `NotDispatchable`.
   - Verify idempotent replay returns winner's `dispatch_id`.
3. **Graph API Endpoints & Test Suite**:
   - Verify `/v1/config/dispatch`, `/v1/activation-gates/{gate_id}/challenge`, `/v1/activation-gates/{gate_id}`.
   - Verify all 24 tests in `tests/test_dispatch_creation.py`.
4. **Execution Verification**:
   Run tests:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_creation.py tests/test_dispatch_transition.py
   ```
5. Formulate explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
6. Write `review.md` and `handoff.md` in your working directory.
7. Notify orchestrator via `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.
