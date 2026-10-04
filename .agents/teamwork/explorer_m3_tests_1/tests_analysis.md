# Milestone 3 Test Architecture Analysis

**Author:** `explorer_m3_tests_1` (M3 Test Architecture Explorer)  
**Date:** 2026-10-03  
**Target Milestone:** Milestone 3 (Dispatch Schema, Hardened Transition Function, and Forward-Compatible Tracking)  
**Scope:** Test Architecture Design for `tests/test_dispatch_schema.py`, `tests/test_dispatch_transition.py`, and `tests/conftest.py`

---

## 1. Executive Summary & Design Scope

Milestone 3 implements the authoritative dispatch foundation and single-write-path state machine transition mechanism in PostgreSQL 16. It consists of:
1. **0003 Part C (`migrations/0003_dispatch_foundation.sql`)**: Core entities (`players`, `dispatches`), partial unique constraints, insert guards, immutability triggers, and forward-compatible tracking tables (`dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`).
2. **0003 Part D (`migrations/0003_dispatch_foundation.sql` & `migrations/post/grants.sql`)**: Hardened `SECURITY DEFINER` function `public.fc27_dispatch_transition`, transition rules matrix `public.dispatch_transition_rules`, actor derivation, SQLSTATE error taxonomy (`FC001`–`FC007`), C-10 event source verification, and Python wrapper service `src/dispatch/transition.py`.
3. **Objective Bounds Model (`src/schemas.py`, `src/dispatch/objective.py`)**: Pydantic `Objective` validation, frozen versioned policy `fc27-objective-bounds-v1`, input-stripping error sanitation, and candidate integration.

This analysis defines the test specifications and fixture architecture across two new test files:
- `tests/test_dispatch_schema.py`: 18 targeted test functions verifying schema constraints, triggers, role boundaries, and objective bounds.
- `tests/test_dispatch_transition.py`: 22 targeted test functions verifying function hardening, actor derivation, rule matrix enforcement, state machine transitions, Phase 1 delivery blocks, outbox atomicity, and event-source guards.
- Fixture extensions in `tests/conftest.py`: Fixtures for player seeding, approved pair prerequisites, and dispatch factory helpers.

---

## 2. Specification Reconciliation Matrix

The table below reconciles the two primary design sources (Spec §11–§14 / Plan Tasks 6–7 vs. Orchestrator Dispatch prompts) to establish unambiguous test expectations:

| Area | Plan Tasks 6–7 & Spec §11–§14 | Orchestrator Prompt Phrasing | Reconciled Test Strategy |
|---|---|---|---|
| **Dispatch Statuses** | `pending`, `queued`, `delivered`, `requires_reapproval`, `failed`, `acknowledged`, `tracking`, `completed`, `expired`, `cancelled`, `superseded` | `pending`, `running`, `paused`, `delivered`, `failed`, `cancelled` | Test the full authoritative domain `('pending', 'queued', 'delivered', 'requires_reapproval', 'failed', 'acknowledged', 'tracking', 'completed', 'expired', 'cancelled', 'superseded')`. If legacy/prompt aliases (`running`, `paused`) are mapped, test their explicit rule rejections or inclusion. |
| **Transition Function Signature** | `public.fc27_dispatch_transition(p_dispatch_id uuid, p_expected_status text, p_expected_version int, p_to_status text, p_reason text, p_correlation_id uuid, p_patch jsonb DEFAULT '{}'::jsonb) -> integer` | `(p_dispatch_id uuid, p_to_status text, p_actor text, p_actor_kind text, p_reason text, p_correlation_id uuid) -> jsonb` | The canonical hardened function adheres to Spec §11.3 / Plan Task 7: caller does NOT pass actor directly (actor is derived from `session_user` and `fc27.actor` session setting). It requires optimistic locking (`p_expected_status`, `p_expected_version`) and returns the new integer version. Tests will assert this exact contract and its SQLSTATE mappings. |
| **Transition Rules Table** | `public.dispatch_transition_rules(from_status text, to_status text, db_role name, actor_kind text, PRIMARY KEY(from_status, to_status, db_role, actor_kind))` | `(from_status, to_status, allowed_roles text[], allowed_actor_kinds text[], require_reason boolean)` | Test row-level rules matching `(from_status, to_status, session_user, actor_kind)`. Each allowed combination is an explicit row. |
| **Event Emission** | Atomic insert to `public.domain_events` with type `dispatch.<to_status>` (e.g. `dispatch.queued`, `dispatch.delivered`, `dispatch.failed`, etc.) and `public.outbox`. | Emits outbox event `dispatch.transitioned`. | Test that each transition emits an atomic domain event and outbox row. Validate both specific event types (Spec §11.3) and generic transition payload visibility. |
| **Phase 1 Invariant: No Player Delivery** | `DISPATCH_ENABLED=false` at all times; G-OUTBOX cannot be satisfied; no player delivery adapter; transition to `delivered` blocked. | Phase 1 blocks transition to `delivered`. | Explicit negative test: attempting transition to `delivered` in Phase 1 fails closed (either via missing operational gate, policy block, or caller rejection). |

---

## 3. Test Suite Specification: `tests/test_dispatch_schema.py`

This module tests all static schema constraints, triggers, append-only guards, role privilege boundaries, and objective bounds models without executing the transition function.

### 3.1 Suite Inventory

| # | Test Function Name | Tested Component | Expected Behavior / Assertions |
|---|---|---|---|
| 1 | `test_players_schema_and_pk` | `public.players` | Primary key `player_id` must be numeric string (`^[0-9]{1,20}$`). Valid IDs succeed; non-numeric (`'p1'`, `'gamer_tag'`) or length > 20 fail with `psycopg.errors.CheckViolation`. |
| 2 | `test_players_club_id_and_platform_constraints` | `public.players` | `club_id` must match `^[0-9]+$`. `platform` defaults to `'common-gen5'` and rejects other values (`CheckViolation`). |
| 3 | `test_players_display_name_and_active_flag` | `public.players` | `display_name` limited to 64 characters. `active` defaults to `true`. |
| 4 | `test_players_optional_squad_number_per_club` | `public.players` | If `squad_number` is defined, values 1–99 are permitted; duplicate squad number within the same club fails with `UniqueViolation`. |
| 5 | `test_dispatches_foreign_keys_and_provenance` | `public.dispatches` | Foreign keys enforce referential integrity to `graph_threads(thread_id)`, `approval_requests(approval_request_id)`, `approval_audit(id)`, `players(player_id)`. Nonexistent parents fail with `ForeignKeyViolation`. |
| 6 | `test_dispatches_status_domain` | `public.dispatches` | Valid statuses accepted; invalid status (e.g., `'active'`, `'processing'`, `'foo'`) raises `CheckViolation`. |
| 7 | `test_dispatches_insert_guard_pending_v1` | `fc27_dispatch_insert_guard` | Direct insert into `dispatches` MUST have `dispatch_status = 'pending'`, `version = 1`, `plan_version = 0`, and NULL lifecycle timestamps. Inserting with status `'queued'`, `'delivered'`, or `version = 2` raises `CheckViolation`. |
| 8 | `test_dispatches_immutable_provenance_columns` | `fc27_dispatch_immutable` | Parameterized test across provenance columns: `dispatch_id`, `source_thread_id`, `approval_id`, `approval_request_id`, `approval_payload_hash`, `player_id`, `candidate_id`, `owner`, `club_id`, `approved_instruction`, `created_by_principal`, `created_correlation_id`. Even superuser UPDATE raises `CheckViolation`. |
| 9 | `test_dispatches_terminal_rows_frozen` | `fc27_dispatch_immutable` | Dispatches in terminal status (`completed`, `expired`, `cancelled`, `superseded`) cannot be updated or deleted. Attempted UPDATE or DELETE raises `CheckViolation`. (Exception: `superseded_by` set once during transition). |
| 10 | `test_dispatches_one_active_per_objective` | Partial unique index | Partial index `dispatches_one_active_per_objective` on `(player_id, owner, (approved_instruction->'objective'->>'metric')) WHERE dispatch_status NOT IN ('completed','expired','cancelled','superseded')`. Inserting second active dispatch for same player/owner/metric raises `UniqueViolation`. Inserting for terminal dispatch succeeds. |
| 11 | `test_tracking_tables_append_only` | Append-only triggers | Parameterized test over 8 tracking tables: `dispatch_plans`, `dispatch_plan_decisions`, `dispatch_delivery_attempts`, `dispatch_acknowledgments`, `dispatch_comments`, `analyst_notes`, `player_observations`, `dispatch_assessments`. Direct UPDATE and DELETE raise `CheckViolation` (`"<table_name> is append-only"`). |
| 12 | `test_dispatch_plans_status_check` | `public.dispatch_plans` | `status` check constraint allows only `'valid'`, `'material_change'`, `'invalid'`, `'schema_invalid'`. Other values raise `CheckViolation`. |
| 13 | `test_dispatch_assessments_result_check` | `public.dispatch_assessments` | `result` check constraint allows only `'tracking_error'`, `'insufficient_data'`, `'target_met'`, `'regressed'`, `'improving'`, `'no_change'`. Other values raise `CheckViolation`. |
| 14 | `test_app_role_cannot_update_dispatches` | `migrations/post/grants.sql` | `fc27_app` connection attempting `UPDATE public.dispatches SET ...` raises `InsufficientPrivilege`. |
| 15 | `test_app_role_cannot_delete_dispatches` | `migrations/post/grants.sql` | `fc27_app` connection attempting `DELETE FROM public.dispatches ...` raises `InsufficientPrivilege`. |
| 16 | `test_dispatch_worker_cannot_write_dispatches` | `migrations/post/grants.sql` | `fc27_dispatch` connection attempting `INSERT`, `UPDATE`, or `DELETE` on `public.dispatches` raises `InsufficientPrivilege`. |
| 17 | `test_dispatch_worker_cannot_access_approval_tables` | `migrations/post/grants.sql` | `fc27_dispatch` connection attempting `SELECT` or mutation on `approval_requests`, `approval_audit`, `approval_nonces`, `graph_threads` raises `InsufficientPrivilege`. |
| 18 | `test_objective_bounds_model_and_policy` | `src/schemas.py`, `src/dispatch/objective.py` | Pydantic validation: `Objective` validates bounded ranges for `pass_accuracy` (1–25), `rating` (0.1–2.0), `goals` (1–5). Out-of-bounds raises `ValidationError`. Bounds policy defaults to `"fc27-objective-bounds-v1"`. Error messages do not echo candidate input. |

### 3.2 Detailed Test Implementations (Specification)

```python
# tests/test_dispatch_schema.py (Conceptual implementation details)

import uuid
import psycopg
import pytest
from pydantic import ValidationError

from src.schemas import Objective
from src.dispatch.objective import METRIC_BOUNDS


def test_players_schema_and_pk(pg_uri):
    """AC-14.3: players table PK requires numeric string format ^[0-9]{1,20}$."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Valid numeric player_id
            cur.execute(
                "INSERT INTO public.players (player_id, club_id, display_name) VALUES ('1234567890', '330323', 'Striker') RETURNING player_id"
            )
            assert cur.fetchone()[0] == '1234567890'

            # Non-numeric gamer tag rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id) VALUES ('gamer_tag_x', '330323')")

            # Length > 20 rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id) VALUES (%s, '330323')", ('1' * 21,))


def test_dispatches_insert_guard_pending_v1(pg_uri, sample_approved_dispatch_prereqs):
    """Trigger fc27_dispatch_insert_guard forces status='pending', version=1, plan_version=0."""
    prereqs = sample_approved_dispatch_prereqs
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Valid insert with status='pending', version=1 succeeds
            cur.execute(
                """INSERT INTO public.dispatches (
                    source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                    player_id, candidate_id, owner, selection, club_id, approved_instruction,
                    dispatch_status, version, plan_version, created_by_principal, created_correlation_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 1, 0, 'batmanL8', %s)
                RETURNING dispatch_id""",
                (
                    prereqs["thread_id"], prereqs["approval_id"], prereqs["approval_request_id"],
                    prereqs["hash"], prereqs["player_id"], "coach-best-1", "coach", "best",
                    "330323", '{"instruction": "press high", "objective": {"metric": "rating"}}',
                    uuid.uuid4()
                )
            )
            dispatch_id = cur.fetchone()[0]
            assert dispatch_id is not None

            # Attempting to insert with status='queued' or 'delivered' fails
            with pytest.raises(psycopg.errors.CheckViolation, match="initial status must be pending"):
                cur.execute(
                    """INSERT INTO public.dispatches (
                        source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                        player_id, candidate_id, owner, selection, club_id, approved_instruction,
                        dispatch_status, version, created_by_principal, created_correlation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', 1, 'batmanL8', %s)""",
                    (
                        prereqs["thread_id"], prereqs["approval_id_2"], prereqs["approval_request_id_2"],
                        prereqs["hash"], prereqs["player_id"], "coach-best-2", "coach", "best",
                        "330323", '{}', uuid.uuid4()
                    )
                )


@pytest.mark.parametrize("column,new_val", [
    ("approved_instruction", '{"tampered": true}'),
    ("player_id", "999999"),
    ("candidate_id", "altered-cand"),
    ("owner", "tactics"),
    ("approval_id", 99999),
    ("approval_payload_hash", "f" * 64),
    ("source_thread_id", "forged-thread"),
    ("club_id", "999999"),
    ("created_correlation_id", "00000000-0000-0000-0000-000000000000"),
])
def test_dispatch_provenance_immutable(pg_admin_db_uri, make_dispatch, column, new_val):
    """AC-2: Even superuser UPDATE of provenance columns raises CheckViolation via trigger."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="immutable provenance"):
                cur.execute(f"UPDATE public.dispatches SET {column} = %s WHERE dispatch_id = %s", (new_val, dispatch_id))


def test_app_cannot_update_dispatches(pg_uri, make_dispatch):
    """Least privilege: fc27_app holds NO UPDATE privilege on public.dispatches."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE public.dispatches SET dispatch_status = 'failed' WHERE dispatch_id = %s", (dispatch_id,))


def test_dispatch_worker_least_privilege(pg_dispatch_uri, make_dispatch):
    """AC-39: fc27_dispatch has SELECT only on dispatches, no access to approval tables."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # SELECT succeeds
            cur.execute("SELECT dispatch_status FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,))
            assert cur.fetchone()[0] == 'pending'

            # INSERT / UPDATE / DELETE fail
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,))

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM public.approval_requests")
```

---

## 4. Test Suite Specification: `tests/test_dispatch_transition.py`

This module tests the single-write-path state machine transition mechanism executed by the `SECURITY DEFINER` function `public.fc27_dispatch_transition` and wrapped by `src/dispatch/transition.py`.

### 4.1 Suite Inventory

| # | Test Function Name | Tested Mechanism | Expected Behavior / Assertions |
|---|---|---|---|
| 1 | `test_function_attributes` | `pg_proc` catalog query | Function `public.fc27_dispatch_transition` has `prosecdef = true`, owner `fc27_owner`, `proconfig` has `search_path=pg_catalog, pg_temp`, PUBLIC has no privileges. |
| 2 | `test_function_source_has_no_dynamic_sql` | Static AST / SQL regex | Static analysis on function body: no `\bEXECUTE\b`, no `format(`, all relation identifiers are schema-qualified (`public.` or `pg_catalog.`). |
| 3 | `test_runtime_roles_cannot_replace_function` | Role privilege | Calling `CREATE OR REPLACE FUNCTION` or `ALTER FUNCTION` as `fc27_app` or `fc27_dispatch` raises `InsufficientPrivilege`. |
| 4 | `test_null_correlation_id_rejected` | Parameter validation | Passing `p_correlation_id = NULL` raises SQLSTATE `FC005` ("null correlation ID"). |
| 5 | `test_reason_length_limit` | Parameter validation | Passing `p_reason` longer than 500 chars raises SQLSTATE `FC007` ("reason too long"). |
| 6 | `test_stale_version_conflict` | Optimistic concurrency | When `p_expected_version` does not match row `version`, function raises SQLSTATE `FC001` ("version conflict"). |
| 7 | `test_status_mismatch_conflict` | State validation | When `p_expected_status` does not match row `dispatch_status`, function raises SQLSTATE `FC001` ("status conflict"). |
| 8 | `test_worker_actor_derivation` | Actor derivation | When called as `fc27_dispatch`, actor is automatically derived as `'service:dispatch-worker'`. Emitted domain event records this actor. |
| 9 | `test_worker_cannot_set_foreign_actor` | Actor validation | When called as `fc27_dispatch` with `set_config('fc27.actor', 'commissioner:x', true)`, function raises SQLSTATE `FC004` ("actor invalid"). |
| 10 | `test_app_malformed_actor_rejected` | Actor validation | When called as `fc27_app` with missing or malformed `fc27.actor` (e.g. `'root'`, `'anonymous'`), function raises SQLSTATE `FC004`. |
| 11 | `test_app_user_actor_rejected_in_phase1` | Phase boundary | Setting `fc27.actor` to `'user:<uuid>'` as `fc27_app` raises SQLSTATE `FC004` ("user actors require phase 2"). |
| 12 | `test_allowed_phase1_transitions` | Valid edges | Parameterized over passable Phase 1 edges: `pending -> queued` (worker), `queued -> requires_reapproval` (worker), `queued -> failed` (worker), `failed -> queued` (commissioner retry). Transitions succeed, row version increments, and timestamps update. |
| 13 | `test_illegal_transition_edge_rejected` | Transition rules matrix | Attempting transitions not defined in `dispatch_transition_rules` (e.g., `pending -> completed`, `completed -> queued`) raises SQLSTATE `FC002` ("illegal transition"). |
| 14 | `test_cross_role_transition_rejected` | Role enforcement | Calling worker edge as `fc27_app` (or commissioner edge as `fc27_dispatch`) raises SQLSTATE `FC002`. |
| 15 | `test_phase1_delivered_edge_blocked` | Safety invariant | Transition to `delivered` in Phase 1 fails closed (`DISPATCH_ENABLED=false`). Direct attempt or unapproved plan delivery is rejected. |
| 16 | `test_unknown_patch_key_rejected` | Patch allowlist | Passing patch with unpermitted key (e.g., `{'status': 'completed'}`) raises SQLSTATE `FC003` ("patch key invalid"). |
| 17 | `test_wrong_patch_type_rejected` | Patch typing | Passing patch key with invalid type (e.g., `{'plan_version': 'not_an_int'}`) raises SQLSTATE `FC003`. |
| 18 | `test_failed_retryable_is_derived` | Derived fields | Supplying `failure_reason = 'plan_policy_invalid'` derives `failure_retryable = false`. Passing caller-provided `failure_retryable` in patch raises `FC003`. |
| 19 | `test_event_and_outbox_written_per_transition` | Transactional outbox | Every successful transition writes exactly 1 `domain_events` row and 1 `outbox` row with `channel = 'fc27:events'`. Rolling back the transaction rolls back all writes. |
| 20 | `test_app_direct_transition_event_rejected` | C-10 Event source guard | Direct `INSERT INTO public.domain_events` with `dispatch.*` event type as `fc27_app` raises exception ("transition-only event type not allowed outside transition function"). |
| 21 | `test_search_path_hijack_has_no_effect` | AC-40 Search path hardening | Creating temporary table `pg_temp.dispatches` or temporary function `pg_temp.now()` as `fc27_dispatch` does not hijack execution; transition correctly mutates `public.dispatches`. |
| 22 | `test_python_transition_wrapper_mapping` | `src/dispatch/transition.py` | Python wrapper `transition(...)` validates open transaction, sets transaction-local actor setting, executes function, maps `FC00x` errors to typed `TransitionError(code, message)`. |

### 4.2 Detailed Test Implementations (Specification)

```python
# tests/test_dispatch_transition.py (Conceptual implementation details)

import uuid
import psycopg
import pytest

from src.dispatch.transition import transition, TransitionError


def test_function_attributes(pg_uri):
    """AC-38: Function public.fc27_dispatch_transition security properties."""
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.prosecdef, r.rolname, p.proconfig
                  FROM pg_proc p
                  JOIN pg_roles r ON p.proowner = r.oid
                 WHERE p.proname = 'fc27_dispatch_transition'
            """)
            row = cur.fetchone()
            assert row is not None, "fc27_dispatch_transition function does not exist"
            prosecdef, owner, proconfig = row
            assert prosecdef is True, "Must be SECURITY DEFINER"
            assert owner == "fc27_owner", "Must be owned by fc27_owner"
            assert proconfig is not None and "search_path=pg_catalog, pg_temp" in proconfig


def test_stale_version_conflict(pg_dispatch_uri, make_dispatch):
    """FC001: Mismatched expected version raises version conflict."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC001"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 99, 'queued', 'worker pickup', %s, '{}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_allowed_worker_transition_pending_to_queued(pg_dispatch_uri, make_dispatch):
    """AC-4: Permitted worker edge pending -> queued succeeds and emits outbox event."""
    dispatch_id = make_dispatch(status="pending", version=1)
    corr_id = uuid.uuid4()
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            cur = conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'pending', 1, 'queued', 'worker pickup', %s, '{}'::jsonb
                )""",
                (dispatch_id, corr_id)
            )
            new_version = cur.fetchone()[0]
            assert new_version == 2

            # Assert status updated
            row = conn.execute(
                "SELECT dispatch_status, version FROM public.dispatches WHERE dispatch_id = %s",
                (dispatch_id,)
            ).fetchone()
            assert row[0] == "queued"
            assert row[1] == 2

            # Assert domain_events and outbox written
            evt = conn.execute(
                "SELECT event_type, actor, correlation_id FROM public.domain_events WHERE entity_id = %s",
                (str(dispatch_id),)
            ).fetchone()
            assert evt is not None
            assert evt[0] == "dispatch.queued"
            assert evt[1] == "service:dispatch-worker"
            assert evt[2] == corr_id


def test_illegal_transition_edge_rejected(pg_dispatch_uri, make_dispatch):
    """FC002: Illegal edge not present in dispatch_transition_rules raises FC002."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC002"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'completed', 'illegal jump', %s, '{}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_phase1_delivered_edge_blocked(pg_dispatch_uri, make_dispatch):
    """Non-negotiable invariant: Dispatch is disabled in Phase 1; transition to delivered blocked."""
    dispatch_id = make_dispatch(status="queued", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'queued', 1, 'delivered', 'attempt delivery', %s, '{"plan_version": 1}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )
            # Fails closed either via precondition check (missing approved plan) or gate block
            assert "FC00" in str(exc_info.value) or "check_violation" in str(exc_info.value)


def test_search_path_hijack_has_no_effect(pg_dispatch_uri, make_dispatch):
    """AC-40: Temporary table shadowing cannot hijack SECURITY DEFINER execution."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            conn.execute("CREATE TEMP TABLE dispatches AS SELECT * FROM public.dispatches WHERE 1=0")
            conn.execute("CREATE OR REPLACE FUNCTION pg_temp.now() RETURNS timestamptz LANGUAGE sql AS $$ SELECT '1970-01-01'::timestamptz $$")

            # Transition still operates on public.dispatches
            conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'pending', 1, 'queued', 'safe pickup', %s, '{}'::jsonb
                )""",
                (dispatch_id, uuid.uuid4())
            )
        
        # Verify public row was mutated
        status = conn.execute("SELECT dispatch_status FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,)).fetchone()[0]
        assert status == "queued"
```

---

## 5. Test Fixtures Architecture: `tests/conftest.py`

To support these tests without leaking state or coupling between tests, the fixture harness in `tests/conftest.py` needs three new fixtures and one helper factory:

### 5.1 New Fixtures Specification

1. **`sample_player(pg_uri)`**:
   - Injects a standard enrolled player (`player_id = "10001"`, `club_id = "330323"`, `display_name = "Marcus Rashford"`, `active = true`) into `public.players`.
   - Returns a dictionary `{"player_id": "10001", "club_id": "330323"}`.

2. **`sample_approved_dispatch_prereqs(pg_uri, sample_player)`**:
   - Foreign key constraints on `dispatches` require valid parent records in `graph_threads`, `approval_requests`, and `approval_audit`.
   - Creates a valid awaiting thread, links an approval request, and commits an `approval_audit` record with `decision = 'APPROVE'`.
   - Returns a dictionary containing `thread_id`, `approval_request_id`, `approval_id`, `hash`, `player_id`.

3. **`make_dispatch(pg_uri, pg_admin_db_uri, sample_approved_dispatch_prereqs)`**:
   - A factory fixture returning a callable `_make(status='pending', version=1, owner='coach', metric='pass_accuracy')`.
   - Inserts a valid dispatch row as `fc27_app` in `pending` status.
   - If a non-pending start state is requested for transition testing (e.g., `'queued'`), uses `pg_admin_db_uri` to set the initial test status under test-only replication role or applies the required transition sequence.
   - Returns the created `dispatch_id` (`uuid.UUID`).

```python
# conftest.py additions (Conceptual)

@pytest.fixture()
def sample_player(pg_uri):
    player_id = "10001"
    club_id = "330323"
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.players (player_id, club_id, display_name, active)
                   VALUES (%s, %s, 'Sample Player', true)
                   ON CONFLICT (player_id) DO NOTHING""",
                (player_id, club_id),
            )
    return {"player_id": player_id, "club_id": club_id}


@pytest.fixture()
def sample_approved_dispatch_prereqs(pg_uri, sample_player):
    tid = f"thread-dispatch-prereq-{uuid.uuid4().hex[:8]}"
    req_id = uuid.uuid4()
    pkg_v = "a" * 64
    payload_hash = "b" * 64

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
                   VALUES (%s, '{"status":"approved"}', 'approved', '330323', %s, %s)""",
                (tid, pkg_v, req_id),
            )
            cur.execute(
                """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, created_at, expires_at, decided_at)
                   VALUES (%s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now())""",
                (req_id, tid, pkg_v),
            )
            cur.execute(
                """INSERT INTO public.approval_audit (
                    thread_id, commissioner, decision, notes, owner, selection,
                    candidate_id, player_id, approved_by, approval_request_id, package_version
                ) VALUES (%s, 'batmanL8', 'APPROVE', 'ok', 'coach', 'best', 'cand-1', %s, 'batmanL8', %s, %s)
                RETURNING id""",
                (tid, sample_player["player_id"], req_id, pkg_v),
            )
            audit_id = cur.fetchone()[0]

    return {
        "thread_id": tid,
        "approval_request_id": req_id,
        "approval_id": audit_id,
        "hash": payload_hash,
        "player_id": sample_player["player_id"],
    }


@pytest.fixture()
def make_dispatch(pg_uri, pg_admin_db_uri, sample_approved_dispatch_prereqs):
    prereqs = sample_approved_dispatch_prereqs

    def _make(status="pending", version=1, owner="coach", metric="pass_accuracy"):
        d_id = uuid.uuid4()
        corr_id = uuid.uuid4()
        inst = {
            "action": "drill",
            "objective": {
                "metric": metric,
                "direction": "increase",
                "target_delta": 10.0,
                "bounds_policy": "fc27-objective-bounds-v1"
            }
        }
        with psycopg.connect(pg_uri, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO public.dispatches (
                        dispatch_id, source_thread_id, approval_id, approval_request_id,
                        approval_payload_hash, player_id, candidate_id, owner, selection,
                        club_id, approved_instruction, dispatch_status, version, plan_version,
                        created_by_principal, created_correlation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'best', '330323', %s, 'pending', 1, 0, 'batmanL8', %s)""",
                    (
                        d_id, prereqs["thread_id"], prereqs["approval_id"], prereqs["approval_request_id"],
                        prereqs["hash"], prereqs["player_id"], f"{owner}-best-1", owner,
                        json.dumps(inst), corr_id
                    )
                )

        if status != "pending" or version != 1:
            with psycopg.connect(pg_admin_db_uri, autocommit=True) as admin_conn:
                admin_conn.execute("SET session_replication_role = 'replica'")
                try:
                    admin_conn.execute(
                        "UPDATE public.dispatches SET dispatch_status = %s, version = %s WHERE dispatch_id = %s",
                        (status, version, d_id)
                    )
                finally:
                    admin_conn.execute("SET session_replication_role = 'origin'")

        return d_id

    return _make
```

---

## 6. Execution Command & Acceptance Traceability

### 6.1 Test Execution Command
Tests must be executed against real PostgreSQL 16 using the established project invocation:
```bash
python -m pytest -o addopts="" -q -rs tests/test_dispatch_schema.py tests/test_dispatch_transition.py
```

### 6.2 Acceptance Criteria Traceability

| Acceptance Criterion | Test Verification Method |
|---|---|
| **AC-2: Immutability of provenance columns** | `test_dispatches_immutable_provenance_columns` (trigger rejects updates to all 11 provenance columns). |
| **AC-4: Permitted state transitions** | `test_allowed_phase1_transitions` (parameterized test across all passable Phase 1 edges). |
| **AC-38: SECURITY DEFINER hardening** | `test_function_attributes` (`prosecdef`, `proowner`, `proconfig`) and `test_function_source_has_no_dynamic_sql`. |
| **AC-39: Runtime role privileges** | `test_app_role_cannot_update_dispatches`, `test_dispatch_worker_cannot_write_dispatches`, `test_dispatch_worker_cannot_access_approval_tables`. |
| **AC-40: Search path hijacking prevention** | `test_search_path_hijack_has_no_effect` (temporary table/function shadowing in `pg_temp` has no effect). |
| **C-4: Gate Nonce & Manual Activation** | `test_dispatches_foreign_keys_and_provenance` (approval nonces subject typing for dispatch plan & cancel). |
| **C-5: Correlation ID Traceability** | `test_null_correlation_id_rejected` (`FC005`), correlation ID propagated to `created_correlation_id` and domain events. |
| **C-8: Tracking Forward Compatibility** | `test_tracking_tables_append_only`, `test_dispatch_plans_status_check`, `test_dispatch_assessments_result_check`. |
| **C-10: Event Source Identity** | `test_app_direct_transition_event_rejected` (direct insert of transition events rejected outside transition function). |
| **D-30: Objective Bounds Policy** | `test_objective_bounds_model_and_policy` (bounds validation, input stripping, versioned policy default). |
| **Phase 1 Non-negotiable Invariant** | `test_phase1_delivered_edge_blocked` (delivery blocked under `DISPATCH_ENABLED=false`). |
