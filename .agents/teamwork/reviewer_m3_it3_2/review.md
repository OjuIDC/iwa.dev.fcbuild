# Milestone 3 Iteration 3: Transition & Schema Test Suite Review

**Reviewer**: `reviewer_m3_it3_2` (Roles: `reviewer`, `critic`)  
**Date**: 2026-10-03T19:45:00Z  
**Verdict**: **APPROVE**  
**Integrity Status**: **CLEAN (Zero Integrity Violations)**

---

## 1. Executive Summary

Milestone 3 Iteration 3 delivery comprises the complete transition and schema test suites across 22 transition tests (`tests/test_dispatch_transition.py`) and 18 schema tests / 32 test cases (`tests/test_dispatch_schema.py`), supported by fixture lifecycle sequencing in `tests/conftest.py` and tracking column alignment in `tests/test_dispatch_schema.py`.

A rigorous adversarial and quality review was conducted against:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Phase 1 Non-negotiable Invariants & Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Worker 3 Handoff (`.agents/teamwork/worker_m3_3/handoff.md`)
4. Worker 4 Handoff (`.agents/teamwork/worker_m3_4/handoff.md`)
5. Source files: `src/dispatch/transition.py`, `migrations/0003_dispatch_foundation.sql`, `tests/conftest.py`

**Key Findings**:
- **Transition Security & Invariants**: The single-write-path transition function `public.fc27_dispatch_transition` is strictly verified as `SECURITY DEFINER`, owned by `fc27_owner`, with hardened `search_path = pg_catalog, pg_temp`, zero dynamic SQL, strict actor isolation, atomic domain event/outbox generation (`fc27:events`), and non-negotiable enforcement blocking `to_status = 'delivered'` closed with SQLSTATE `FC002`.
- **Schema & Least Privilege Integrity**: Dispatch schema invariants, foreign key provenance, partial unique index (`dispatches_one_active_per_objective`), append-only guards across all 8 tracking tables, and role privilege boundaries (`fc27_app` cannot update/delete dispatches; `fc27_dispatch` worker has read-only access and no approval access) are thoroughly asserted.
- **Fixture Lifecycle & Deferred Triggers**: Worker 3's fixture sequencing (`_seed_approved_approval_prereqs`) resolves circular foreign key dependencies and PostgreSQL deferred trigger queueing (`fc27_check_approval_pair`) cleanly without bypassing production guards or triggers.
- **Tracking Column Alignment**: Worker 4's fix dynamically handles `ingested_at` for `public.player_observations` and `created_at` for the remaining 7 tracking tables, allowing the `fc27_reject_mutation()` trigger to fire and assert append-only behavior properly.
- **Integrity Audit**: No hardcoded test results, facade logic, synthetic test-only shortcuts, or fabricated outputs were detected.

---

## 2. Detailed Suite Verification

### 2.1 `tests/test_dispatch_transition.py` (22 Tests)

| # | Test Function | Verified Property | Result |
|---|---------------|-------------------|--------|
| 1 | `test_function_attributes` | `prosecdef=True`, `owner=fc27_owner`, `search_path=pg_catalog, pg_temp`, `PUBLIC` revoked | PASS |
| 2 | `test_function_source_has_no_dynamic_sql` | No dynamic `EXECUTE`, no `format()` in function body | PASS |
| 3 | `test_runtime_roles_cannot_replace_function` | `fc27_app` and `fc27_dispatch` receive `InsufficientPrivilege` on replacement | PASS |
| 4 | `test_null_correlation_id_rejected` | `p_correlation_id IS NULL` raises SQLSTATE `FC005` | PASS |
| 5 | `test_reason_length_limit` | Reason > 500 characters raises SQLSTATE `FC007` | PASS |
| 6 | `test_stale_version_conflict` | Expected version mismatch raises optimistic concurrency `FC001` | PASS |
| 7 | `test_status_mismatch_conflict` | Expected status mismatch raises optimistic concurrency `FC001` | PASS |
| 8 | `test_worker_actor_derivation` | `fc27_dispatch` caller actor automatically derived as `service:dispatch-worker` on domain event | PASS |
| 9 | `test_worker_cannot_set_foreign_actor` | `fc27_dispatch` setting foreign `fc27.actor` raises `FC004` | PASS |
| 10 | `test_app_malformed_actor_rejected` | `fc27_app` with missing or malformed `fc27.actor` raises `FC004` | PASS |
| 11 | `test_app_user_actor_rejected_in_phase1` | User actors (`user:...`) deferred to Phase 2, raises `FC004` in Phase 1 | PASS |
| 12 | `test_allowed_phase1_transitions` | Allowed Phase 1 edges succeed: (1) `pending->queued`, (2) `queued->requires_reapproval`, (3) `queued->failed`, (4) `failed->queued` (commissioner retry) | PASS |
| 13 | `test_illegal_transition_edge_rejected` | Disallowed edge (e.g. `pending->completed`) raises `FC002` | PASS |
| 14 | `test_cross_role_transition_rejected` | `fc27_app` invoking worker-only edge raises `FC002` | PASS |
| 15 | `test_phase1_delivered_edge_blocked` | Attempting transition to `delivered` fails closed with `FC002` ("Phase 1 invariant violation") | PASS |
| 16 | `test_unknown_patch_key_rejected` | Unexpected JSON patch keys raise `FC003` | PASS |
| 17 | `test_wrong_patch_type_rejected` | Non-integer `plan_version` in patch raises `FC003` | PASS |
| 18 | `test_failed_retryable_is_derived` | Direct patch of `failure_retryable` raises `FC003`; accurately derived for `plan_policy_invalid` (`false`) | PASS |
| 19 | `test_event_and_outbox_written_per_transition` | Exactly 1 `domain_events` row and 1 `outbox` row (`channel='fc27:events'`) written per transition | PASS |
| 20 | `test_app_direct_transition_event_rejected` | Direct `INSERT` of transition-only event type outside transition function rejected | PASS |
| 21 | `test_search_path_hijack_has_no_effect` | Shadowing with temporary table or `pg_temp.now()` has no effect on `SECURITY DEFINER` execution | PASS |
| 22 | `test_python_transition_wrapper_mapping` | Python `transition()` enforces open transaction and maps SQL errors to typed exceptions (`TransitionConflictError`, `Phase1DeliveryBlockedError`) | PASS |

### 2.2 `tests/test_dispatch_schema.py` (18 Tests / 32 Test Cases)

| # | Test Function | Test Cases | Verified Property | Result |
|---|---------------|------------|-------------------|--------|
| 1 | `test_players_schema_and_pk` | 1 | Numeric string `player_id` regex `^[0-9]{1,20}$`, rejects gamer tags and length > 20 | PASS |
| 2 | `test_players_club_id_and_platform_constraints` | 1 | Numeric `club_id`, default platform `common-gen5`, rejects invalid platform | PASS |
| 3 | `test_players_display_name_and_active_flag` | 1 | `display_name <= 64`, default `active=true` and `status='active'`, rejects unsynced flags | PASS |
| 4 | `test_players_optional_squad_number_per_club` | 1 | Squad number 1–99, unique per club, permitted across clubs | PASS |
| 5 | `test_dispatches_foreign_keys_and_provenance` | 1 | Referential integrity enforced to `players`, `graph_threads`, `approval_requests`, `approval_audit` | PASS |
| 6 | `test_dispatches_status_domain` | 1 | Status values outside domain raise `CheckViolation` | PASS |
| 7 | `test_dispatches_insert_guard_pending_v1` | 1 | Insert guard enforces `dispatch_status='pending'`, `version=1`, `plan_version=0` | PASS |
| 8 | `test_dispatches_immutable_provenance_columns` | 8 | Parametrized over 8 provenance columns (`approved_instruction`, `player_id`, `candidate_id`, `owner`, `approval_id`, `approval_payload_hash`, `source_thread_id`, `club_id`): updates rejected even by superuser | PASS |
| 9 | `test_dispatches_terminal_rows_frozen` | 1 | Terminal rows cannot be updated or deleted | PASS |
| 10 | `test_dispatches_one_active_per_objective` | 1 | Partial unique index prevents multiple active dispatches for same `(player_id, owner, metric)` | PASS |
| 11 | `test_tracking_tables_append_only` | 8 | Parametrized over 8 tracking tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`): both `UPDATE` and `DELETE` raise `CheckViolation` | PASS |
| 12 | `test_dispatch_plans_status_check` | 1 | Status check allows only `valid`, `material_change`, `invalid`, `schema_invalid` | PASS |
| 13 | `test_dispatch_assessments_result_check` | 1 | Result check allows authoritative outcomes only | PASS |
| 14 | `test_app_role_cannot_update_dispatches` | 1 | `fc27_app` lacks direct `UPDATE` privileges on `dispatches` | PASS |
| 15 | `test_app_role_cannot_delete_dispatches` | 1 | `fc27_app` lacks direct `DELETE` privileges on `dispatches` | PASS |
| 16 | `test_dispatch_worker_cannot_write_dispatches` | 1 | `fc27_dispatch` has `SELECT` only; `INSERT`, `UPDATE`, `DELETE` raise `InsufficientPrivilege` | PASS |
| 17 | `test_dispatch_worker_cannot_access_approval_tables` | 1 | `fc27_dispatch` has zero access to `approval_requests`, `approval_audit`, `approval_nonces`, `graph_threads` | PASS |
| 18 | `test_objective_bounds_model_and_policy` | 1 | Pydantic validation against `fc27-objective-bounds-v1`, target delta bounds, zero candidate echo in errors, frozen bounds policy table | PASS |

---

## 3. Adversarial Stress-Testing & Critic Analysis

### Challenge 1: Concurrency and Race Conditions
- **Attack Scenario**: Two concurrent workers or API requests attempt to transition the same dispatch simultaneously (e.g. `pending -> queued`).
- **Mechanism Tested**: `public.fc27_dispatch_transition` executes `SELECT * INTO v_dispatch FROM public.dispatches WHERE dispatch_id = p_dispatch_id FOR UPDATE;`.
- **Blast Radius**: If locking were absent, race conditions could produce duplicate transitions or corrupted version increments.
- **Evaluation**: The row-level exclusive lock `FOR UPDATE` serializes transitions. Once the first transaction commits, the second acquires the lock and immediately evaluates `v_dispatch.version <> p_expected_version`, raising `FC001` conflict and aborting cleanly. Test `test_stale_version_conflict` and `test_status_mismatch_conflict` confirm this behavior.

### Challenge 2: Search Path Hijacking and Privileged Execution (AC-40)
- **Attack Scenario**: An untrusted caller sets `search_path` to include an attacker-controlled schema or creates temporary tables/functions shadowing `public.dispatches` or standard functions like `now()`.
- **Mechanism Tested**: Function is declared with `SECURITY DEFINER SET search_path = pg_catalog, pg_temp;`. All tables are schema-qualified with `public.`.
- **Evaluation**: `test_search_path_hijack_has_no_effect` injects `CREATE TEMP TABLE dispatches` and `CREATE OR REPLACE FUNCTION pg_temp.now()`. The transition executes against `public.dispatches` without deviation.

### Challenge 3: Invariant Bypass Attempt (Phase 1 Delivery Block)
- **Attack Scenario**: A rogue worker or admin attempts to bypass Phase 1 delivery blocks by transitioning a dispatch to `delivered`.
- **Mechanism Tested**: Line 1442 of `0003_dispatch_foundation.sql` intercepts `p_to_status = 'delivered'` and unconditionally raises `FC002` ("Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)").
- **Evaluation**: `test_phase1_delivered_edge_blocked` confirms that even authorized callers cannot transition to `delivered`. The block is hard-coded at step 4 of the state machine, executing prior to any transition logic.

### Challenge 4: Information Leakage via Validation Errors
- **Attack Scenario**: Malicious candidate inputs are injected into objective delta fields and reflected back in exception messages.
- **Mechanism Tested**: In `src/dispatch/objective.py`, custom validators use predefined error templates without echoing the candidate payload.
- **Evaluation**: `test_objective_bounds_model_and_policy` verifies that with `target_delta=26.123`, the string `"26.123"` is completely absent from the error message.

### Challenge 5: Tracking Table Append-Only Coverage
- **Attack Scenario**: Updating or deleting audit and tracking records to obscure dispatch history.
- **Mechanism Tested**: `test_tracking_tables_append_only` checks all 8 tracking tables. Worker 4 corrected the column target for `public.player_observations` (`ingested_at` vs `created_at`), allowing the query to reach execution and trigger `fc27_reject_mutation()`.
- **Evaluation**: Both `UPDATE` and `DELETE` on all 8 tables raise `CheckViolation` with `"append-only"`.

---

## 4. Integrity and Anti-Cheat Verification

| Check | Status | Verification Evidence |
|---|---|---|
| Hardcoded test results in source code | NONE | All test assertions query dynamic database state, PostgreSQL catalog metadata, and runtime exception diagnostics. |
| Dummy or facade implementations | NONE | `fc27_dispatch_transition` implements a full 400+ line PL/pgSQL state engine with rule verification, patch validation, event logging, and outbox emission. |
| Shortcuts bypassing task requirements | NONE | Circular foreign key dependencies are resolved through natural lifecycle state progression (`running` -> `awaiting_approval` -> `approved`). |
| Fabricated verification outputs | NONE | Upstream worker handoffs and failure logs reflect genuine PostgreSQL engine behaviors. |
| Self-certifying work without independent verification | NONE | Both test suites independently assert PostgreSQL constraints, triggers, roles, and schema bounds. |

---

## 5. Verdict

**Verdict**: **APPROVE**

Milestone 3 Iteration 3 transition and schema test suites are robust, conformant to all Phase 1 invariants, and ready for integration into the authoritative test baseline.
