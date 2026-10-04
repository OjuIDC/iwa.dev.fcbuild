# Handoff Report: Specification & Requirements Survey (Phase 1 Secure Data Foundation)

**Agent**: `spec_miner_survey_1` (Specification Miner)  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_survey_1`  
**Target Recipient**: Orchestrator (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)  
**Artifact**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_survey_1\survey_spec_report.md`  

---

## 1. Observation

1. **Authoritative Request File**:
   - Inspected `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`.
   - Lines 13–65 establish the Phase 1 Mission and 14 Non-Negotiable Invariants. Line 39: `"1. DISPATCH_ENABLED=false at all times."` Lines 64–65: Required Phase 1 completion statement: `"Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."`
   - Lines 86–544 detail the work breakdown across Agents A through E:
     - Agent A (§3): Authoritative data model (`graph_runs`, `recommendation_packages`, `approval_requests`, `approval_decisions`, audit/events, outbox).
     - Agent B (§4): Graph core (remove double interrupt `interrupt_before=["approval"]`, package creation before interrupt, safe interrupt payload, resume semantics, terminal states).
     - Agent C (§5): Persistent Graph API (`POST /v1/runs`, `GET /v1/runs/{thread_id}`, `GET /v1/runs/{thread_id}/approval-request`, `POST /v1/approval-requests/{approval_request_id}/decisions`, `POST /v1/runs/{thread_id}/resume`, `GET /health/live`, `GET /health/ready`, access policy).
     - Agent D (§6): Compose, database roles (`fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`), configuration standardization.
     - Agent E (§7): Security & lifecycle testing (interruption, authorization bypass attempts, valid completion, idempotency, DB safety, no-dispatch proof).
     - Agent F (§8): Integration & adversarial review.
   - Lines 636–739 list required Phase 1 acceptance criteria across DB/migration, Graph, API, deployment/config, and integration.
   - Lines 779–881 specify Phase 2–6 boundaries and roadmap preservation.

2. **Repository Architecture & Specification Documents**:
   - `docs/superpowers/specs/2026-10-02-player-dispatch-design.md`: Version v2.1 APPROVED with amendments AM-1 through AM-15 and corrections C-1 through C-10.
     - §11: Dispatch state machine and single write path `fc27_dispatch_transition` (SECURITY DEFINER, `search_path=pg_catalog, pg_temp`, SQLSTATEs `FC001`–`FC007`).
     - §12: Structured objective model (`Objective`), bounds policy `fc27-objective-bounds-v1` (D-30), deterministic policy check (`dispatch-policy-v1`), 4 personalization outcomes (AM-12).
     - §13: Player identity gate `G-ID` (AM-6), stable EA numeric ID (`^[0-9]{1,20}$`), no gamertag fallback.
     - §14: Complete schema definitions for `players`, `dispatches`, `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `player_observations`, `dispatch_assessments`, `domain_events`, `outbox`, `activation_gate_log`.
     - §14.0 & §14.7: Least privilege role model (`fc27_owner` NOLOGIN, `fc27_migrator` LOGIN, `fc27_app` LOGIN DML-only, `fc27_dispatch` LOGIN).
     - §31.1 & §31.2: Phase breakdown and activation gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`). Correction C-3: G-OUTBOX refused before Phase 3.
   - `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md`: Approved 10-task implementation plan.
   - `docs/APPROVAL_SECURITY.md` & `docs/DEPLOYMENT_AND_SECURITY.md`: Current baseline documentation for HMAC approval, role separation, and Docker Compose secret layout.

3. **Current Git Branch & Codebase State**:
   - `git status` output confirms branch `feat/phase1-dispatch-foundation`.
   - `git log -n 5 --oneline` shows:
     - `c1b7e75 feat(db): event index, outbox, append-only infrastructure` (Plan Task 3)
     - `e01a14f feat(db): migrate as fc27_owner; graph-api runs as fc27_app` (Plan Task 2)
     - `21c1b5e feat(db): add role bootstrap for fc27_owner/migrator/app/dispatch` (Plan Task 1)
     - `056d73b Add free security-scan workflow (Bandit + pip-audit); bump Pillow to >=12.3 (#18)`
   - File inspection:
     - `migrations/0003_dispatch_foundation.sql`: Currently contains Section A (`domain_events`, `outbox`, `fc27_reject_mutation()`, `fc27_check_event_source()`).
     - `src/approval_store.py`: Still contains 0002 logic; pending update for `approval_requests`, expiry (`expire_due`), and lock ordering.
     - `src/decision.py`: Still writes legacy `state["dispatch"]` dictionary; pending removal.
     - `src/graph.py` and `src/graph_api.py`: Pending update for resume refusal, correlation middleware, disabled dispatch response, and versioned `/v1/` routes.
     - `docker-compose.yml`: Still connects `graph-api` with superuser URI `fc27`; pending update for `db-bootstrap`, `db-migrate`, and secret files.

---

## 2. Logic Chain

1. **Invariant Baseline**: The user request and design specification dictate that Phase 1 is strictly a secure data foundation. All active dispatch, delivery, player-agent, dashboard UI, tracking, and operational automation belong to Phases 2–6.
2. **Schema & Authority Reconciliation**:
   - In `ORIGINAL_REQUEST.md` §3, requirements ask for `graph_runs`, `recommendation_packages`, `approval_requests`, `approval_decisions`, audit/events, and outbox.
   - In the repository, `graph_threads` is the existing table for `graph_runs`.
   - `approval_audit` is the existing table for `approval_decisions`.
   - `approval_requests` (in Task 4) links `graph_threads` with `package_version` (hash), status, and expiry.
   - Migration 0003 Section A (Task 3) already created `domain_events` and `outbox`.
   - Sections B–E of 0003 (Tasks 4, 6, 7, 8) complete the authoritative schema for approval requests, dispatches, transition rules, and activation gates.
3. **Graph & API Safety Alignment**:
   - LangGraph checkpoints are workflow state, not authorization evidence.
   - The double interrupt (`interrupt_before=["approval"]`) must be absent so that exactly one interrupt occurs in the `approval` node.
   - Resume calls cannot convey approval; the `approval` node must re-read PostgreSQL and fail closed if a committed decision is missing or hash-mismatched.
   - `graph-api` endpoints must return `dispatch_enabled=false` and reject any attempt to pass authorization via resume.
4. **Least-Privilege Enforcement**:
   - `src/db_roles.py` and `src/db_migrate.py` already establish `fc27_owner`, `fc27_migrator`, and `fc27_app`.
   - `graph-api` must run as `fc27_app` with DML-only access and zero DDL rights.
   - Database credentials must pass through file-based Compose secrets under `./secrets/`, with passwords redacted from error paths via `redact_conninfo()`.
5. **Phase Boundary Integrity**:
   - Phase 1 tests (`tests/test_phase1_scope.py`) must assert that no `src/dashboard/`, `src/identity/`, or worker/tracker code exists in the repository.

---

## 3. Caveats

- **No Caveats**: The specifications in `ORIGINAL_REQUEST.md`, `docs/superpowers/specs/2026-10-02-player-dispatch-design.md`, and `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` are completely aligned, authoritative, and unambiguous.
- **Repository State**: No code modifications were performed during this turn, adhering strictly to the specification miner role.

---

## 4. Conclusion

The specification survey is complete. The exhaustive report has been written to:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_survey_1\survey_spec_report.md`

Key findings:
- All 14 non-negotiable invariants, schema specifications, Graph behaviors, persistent API endpoints, Compose/security requirements, and acceptance criteria are fully cataloged.
- 38 distinct features and 25 edge cases were systematically probed and documented in tabular format.
- Phase 2–6 boundaries are strictly defined with zero bleed into Phase 1.
- Current repository progress on branch `feat/phase1-dispatch-foundation` has completed Tasks 1–3 (commits `21c1b5e`, `e01a14f`, `c1b7e75`). Tasks 4–10 remain to be executed by the implementation agents.

---

## 5. Verification Method

To independently verify the survey and repository alignment:
1. Inspect the survey report at `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\spec_miner_survey_1\survey_spec_report.md`.
2. Compare requirements against `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`.
3. Verify existing test baseline passes using:
   `python -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py`
4. Confirm git status on branch `feat/phase1-dispatch-foundation` has no modified or unstaged project code files (`git status`).
