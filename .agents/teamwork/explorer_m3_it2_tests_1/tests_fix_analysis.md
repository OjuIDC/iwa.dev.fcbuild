# Milestone 3 Test Suite Analysis & Fix Blueprint

**Author**: explorer_m3_it2_tests_1 (Teamwork Explorer)  
**Date**: 2026-10-03  
**Target Files**: `tests/test_dispatch_schema.py`, `tests/test_dispatch_transition.py`, `tests/conftest.py`, `src/schemas.py`, `migrations/0003_dispatch_foundation.sql`

---

## 1. Executive Summary

A comprehensive, static line-by-line review of all **40 test functions** (18 in `tests/test_dispatch_schema.py` and 22 in `tests/test_dispatch_transition.py`) was performed to identify primary and secondary blockers preventing a clean test pass.

The investigation uncovered **five distinct issues**:
1. **[Critical] Fixture Missing Terminal Fields (`tests/conftest.py:270`)**: `sample_approved_dispatch_prereqs` inserts `graph_threads` with `status='approved'` without required terminal fields, violating `graph_threads_terminal_fields_chk`.
2. **[Critical] Fixture Deferred Trigger Violation with Autocommit (`tests/conftest.py:268`)**: `sample_approved_dispatch_prereqs` uses `autocommit=True`. Statement 1 (`INSERT INTO graph_threads`) commits immediately, triggering deferred constraint trigger `fc27_graph_threads_pair_check` before statement 2 (`INSERT INTO approval_requests`) runs, failing with `check_violation: approval pair: linked approval_request not found`.
3. **[High] Pydantic Input Echo in ValidationError (`src/schemas.py:7`)**: `Objective.model_config` lacks `hide_input_in_errors=True`, leaking candidate values (e.g. `26.123`) in error strings and failing `test_objective_bounds_model_and_policy`.
4. **[High - Secondary] Psycopg SQLSTATE Exception Mapping Mismatch (`tests/test_dispatch_transition.py`)**: 14 tests expect `pytest.raises(psycopg.errors.RaiseException, match="FC00x")`. Because custom SQLSTATEs `FC001`–`FC007` start with `"F"`, Psycopg 3's fallback resolver `get_base_exception()` maps them to `psycopg.errors.OperationalError` (not `RaiseException`). Unless `_sqlcodes` is populated in `conftest.py` or tests expect `psycopg.Error`, all 14 tests will fail with `Failed: DID NOT RAISE <class 'psycopg.errors.RaiseException'>`.
5. **[High - Secondary] Stored Procedure Blocks Commissioner Retry (`migrations/0003_dispatch_foundation.sql:1511`)**: `fc27_dispatch_transition` Step 7 unconditionally raises `commissioner edge to % deferred to Phase 2` for any commissioner transition where `p_to_status <> 'superseded'`. This directly breaks `test_allowed_phase1_transitions` Case 4 (`failed -> queued` commissioner retry), which is an explicitly allowed Phase 1 edge in `dispatch_transition_rules`.

---

## 2. Test Function Inventory & Health Assessment

### 2.1 `tests/test_dispatch_schema.py` (18 Tests)

| # | Test Function | Fixture Dependencies | Status / Risk | Finding |
|---|---|---|---|---|
| 1 | `test_players_schema_and_pk` | `pg_uri` | **PASS** | Validates regex `^[0-9]{1,20}$`, rejects gamer tags and lengths > 20. |
| 2 | `test_players_club_id_and_platform_constraints` | `pg_uri` | **PASS** | Validates numeric `club_id`, default platform `common-gen5`, rejects `xbox`. |
| 3 | `test_players_display_name_and_active_flag` | `pg_uri` | **PASS** | Validates `display_name <= 64`, verifies `players_status_active_sync_chk`. |
| 4 | `test_players_optional_squad_number_per_club` | `pg_uri` | **PASS** | Validates squad numbers 1–99, unique per club. |
| 5 | `test_dispatches_foreign_keys_and_provenance` | `pg_uri`, `sample_approved_dispatch_prereqs` | **BLOCKED (Fixture)** | Blocked by Issues 1 & 2 in `sample_approved_dispatch_prereqs`. Once fixed, passes cleanly. |
| 6 | `test_dispatches_status_domain` | `pg_uri`, `sample_approved_dispatch_prereqs` | **BLOCKED (Fixture)** | Validates `dispatch_status` domain rejection. Clean once fixture fixed. |
| 7 | `test_dispatches_insert_guard_pending_v1` | `pg_uri`, `sample_approved_dispatch_prereqs` | **BLOCKED (Fixture)** | Validates trigger `fc27_dispatch_insert_guard` forcing `pending`, `v1`, `plan_v0`. Clean once fixture fixed. |
| 8 | `test_dispatches_immutable_provenance_columns` (8 params) | `pg_admin_db_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Validates immutability on 8 provenance columns via trigger `fc27_dispatch_immutable`. Clean once fixture fixed. |
| 9 | `test_dispatches_terminal_rows_frozen` | `pg_admin_db_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Freezes terminal rows from update/delete. Clean once fixture fixed. |
| 10 | `test_dispatches_one_active_per_objective` | `pg_uri`, `make_dispatch`, `sample_approved_dispatch_prereqs` | **BLOCKED (Fixture)** | Validates partial unique index `dispatches_one_active_per_objective`. Clean once fixture fixed. |
| 11 | `test_tracking_tables_append_only` (8 params) | `pg_admin_db_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Validates `fc27_reject_mutation` on all 8 companion tables. Clean once fixture fixed. |
| 12 | `test_dispatch_plans_status_check` | `pg_admin_db_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Validates `dispatch_plans` status domain. Clean once fixture fixed. |
| 13 | `test_dispatch_assessments_result_check` | `pg_admin_db_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Validates `dispatch_assessments` result domain. Clean once fixture fixed. |
| 14 | `test_app_role_cannot_update_dispatches` | `pg_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Validates `fc27_app` cannot directly update dispatches (`InsufficientPrivilege`). Clean once fixture fixed. |
| 15 | `test_app_role_cannot_delete_dispatches` | `pg_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Validates `fc27_app` cannot delete dispatches (`InsufficientPrivilege`). Clean once fixture fixed. |
| 16 | `test_dispatch_worker_cannot_write_dispatches` | `pg_dispatch_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Validates `fc27_dispatch` has SELECT only. Clean once fixture fixed. |
| 17 | `test_dispatch_worker_cannot_access_approval_tables` | `pg_dispatch_uri` | **PASS** | Validates `fc27_dispatch` has no access to approval tables. |
| 18 | `test_objective_bounds_model_and_policy` | none | **FAIL (Schema)** | Fails due to Issue 3 (`hide_input_in_errors=True` missing). |

---

### 2.2 `tests/test_dispatch_transition.py` (22 Tests)

| # | Test Function | Fixture Dependencies | Status / Risk | Finding |
|---|---|---|---|---|
| 1 | `test_function_attributes` | `pg_uri` | **PASS** | Verified `SECURITY DEFINER`, owner `fc27_owner`, search_path, no `PUBLIC`. |
| 2 | `test_function_source_has_no_dynamic_sql` | none | **PASS** | Static regex confirms no `EXECUTE`, no `format()`. |
| 3 | `test_runtime_roles_cannot_replace_function` | `pg_uri`, `pg_dispatch_uri` | **PASS** | Confirms `fc27_app` and `fc27_dispatch` get `InsufficientPrivilege`. |
| 4 | `test_null_correlation_id_rejected` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC005"`. Psycopg raises `OperationalError` unless Issue 4 is addressed. |
| 5 | `test_reason_length_limit` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC007"`. Blocked by Issue 4. |
| 6 | `test_stale_version_conflict` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC001"`. Blocked by Issue 4. |
| 7 | `test_status_mismatch_conflict` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC001"`. Blocked by Issue 4. |
| 8 | `test_worker_actor_derivation` | `pg_dispatch_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Successful transition `pending -> queued`. Verifies actor `service:dispatch-worker`. Clean once fixture fixed. |
| 9 | `test_worker_cannot_set_foreign_actor` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC004"`. Blocked by Issue 4. |
| 10 | `test_app_malformed_actor_rejected` | `pg_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC004"`. Blocked by Issue 4. |
| 11 | `test_app_user_actor_rejected_in_phase1` | `pg_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC004"`. Blocked by Issue 4. |
| 12 | `test_allowed_phase1_transitions` | `pg_dispatch_uri`, `pg_uri`, `pg_admin_db_uri`, `make_dispatch` | **FAIL (Migration Bug)** | Case 4 (`failed -> queued` by commissioner) fails closed with `FC004` due to Issue 5 in `0003_dispatch_foundation.sql:1511`. |
| 13 | `test_illegal_transition_edge_rejected` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC002"`. Blocked by Issue 4. |
| 14 | `test_cross_role_transition_rejected` | `pg_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC002"`. Blocked by Issue 4. |
| 15 | `test_phase1_delivered_edge_blocked` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC002"`. Blocked by Issue 4. |
| 16 | `test_unknown_patch_key_rejected` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC003"`. Blocked by Issue 4. |
| 17 | `test_wrong_patch_type_rejected` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Expects `RaiseException, match="FC003"`. Blocked by Issue 4. |
| 18 | `test_failed_retryable_is_derived` | `pg_dispatch_uri`, `make_dispatch` | **RISK (SQLSTATE Mapping)** | Part 1 expects `RaiseException, match="FC003"`. Blocked by Issue 4. Part 2 passes. |
| 19 | `test_event_and_outbox_written_per_transition` | `pg_dispatch_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Successful transition, verifies 1 domain event and 1 outbox row. Clean once fixture fixed. |
| 20 | `test_app_direct_transition_event_rejected` | `pg_uri` | **PASS** | Verifies trigger rejects transition-only event outside function. |
| 21 | `test_search_path_hijack_has_no_effect` | `pg_dispatch_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Verifies temp table/func shadowing is ignored by security definer. Clean once fixture fixed. |
| 22 | `test_python_transition_wrapper_mapping` | `pg_dispatch_uri`, `make_dispatch` | **BLOCKED (Fixture)** | Tests Python wrapper `transition()`. Catches typed `TransitionError` subclasses, so it is immune to Issue 4. |

---

## 3. Deep Dive into Secondary Issues

### 3.1 Issue 2: Autocommit vs Deferred Constraint Trigger

In `tests/conftest.py`, lines 268–279:
```python
with psycopg.connect(pg_uri, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id, ...) ..."""
        )
        cur.execute(
            """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, ...) ..."""
        )
```
- **Mechanism**: Trigger `fc27_graph_threads_pair_check` is an `AFTER INSERT OR UPDATE ON public.graph_threads DEFERRABLE INITIALLY DEFERRED` constraint trigger.
- **Problem**: When `autocommit=True`, the first `cur.execute` constitutes a complete transaction that commits immediately. At commit time, PostgreSQL invokes `fc27_check_approval_pair()`. Line 352 of `0003_dispatch_foundation.sql` attempts:
  `SELECT * INTO v_req FROM public.approval_requests WHERE approval_request_id = NEW.approval_request_id;`
  Because `public.approval_requests` has not yet received its row, `NOT FOUND` evaluates to true, immediately raising:
  `check_violation: approval pair: linked approval_request ... not found for thread ...`.
- **Resolution**: Both inserts must be wrapped in an explicit transaction block:
  ```python
  with psycopg.connect(pg_uri) as conn:
      with conn.transaction():
          with conn.cursor() as cur:
              ...
  ```
  This ensures that transaction commit occurs only after both `graph_threads` and `approval_requests` rows are present.

---

### 3.2 Issue 4: Psycopg 3 Exception Resolution for SQLSTATE `FC001`–`FC007`

- **Mechanism**: In `psycopg/errors.py` (lines 573–585):
  ```python
  def _class_for_state(sqlstate: str) -> type[Error]:
      try:
          return lookup(sqlstate)
      except KeyError:
          return get_base_exception(sqlstate)

  def get_base_exception(sqlstate: str) -> type[Error]:
      return (
          _base_exc_map.get(sqlstate[:2])
          or _base_exc_map.get(sqlstate[:1])
          or DatabaseError
      )
  ```
  And `_base_exc_map` defines:
  `"F": OperationalError,  # Configuration File Error`
- **Problem**:
  1. `lookup("FC001")` fails with `KeyError` because `"FC001"` is an application-defined code, not a standard PostgreSQL code.
  2. `sqlstate[:2]` (`"FC"`) is not in `_base_exc_map`.
  3. `sqlstate[:1]` (`"F"`) matches line 618: `"F": OperationalError`.
  4. Psycopg raises `psycopg.errors.OperationalError` (with message containing `[SQLSTATE FC001]`).
  5. In Python's exception hierarchy:
     - `OperationalError` inherits from `DatabaseError`.
     - `RaiseException` inherits from `ProgrammingError` (PL/pgSQL code `P0001`).
     - `issubclass(OperationalError, RaiseException)` is `False`.
  6. Therefore, `with pytest.raises(psycopg.errors.RaiseException, match="FC00x"):` fails with `Failed: DID NOT RAISE <class 'psycopg.errors.RaiseException'>`.
- **Resolution**:
  In `tests/conftest.py`, dynamically register the `"FC"` codes into `psycopg.errors._sqlcodes`:
  ```python
  import psycopg.errors

  for _code in ("FC001", "FC002", "FC003", "FC004", "FC005", "FC006", "FC007"):
      psycopg.errors._sqlcodes[_code] = psycopg.errors.RaiseException
  ```
  When registered, `lookup("FC00x")` finds the entry and immediately returns `psycopg.errors.RaiseException`, making all 14 tests in `test_dispatch_transition.py` pass without changing test code.

---

### 3.3 Issue 5: Step 7 in `fc27_dispatch_transition` Blocks Commissioner Retry

- **Mechanism**: In `migrations/0003_dispatch_foundation.sql`, lines 1499–1514:
  ```sql
  -- 7. Commissioner In-Transaction Proof
  IF v_actor_kind = 'commissioner' THEN
      IF p_to_status = 'superseded' THEN
          SELECT EXISTS (
              SELECT 1 FROM public.approval_audit
               WHERE xmin = pg_catalog.pg_current_xact_id()::xid
                 AND approved_by = substring(v_actor from 14)
          ) INTO v_has_audit;
          IF NOT v_has_audit THEN
              RAISE EXCEPTION 'supersede requires approval_audit committed in current transaction'
                  USING ERRCODE = 'FC004';
          END IF;
      ELSE
          RAISE EXCEPTION 'commissioner edge to % deferred to Phase 2', p_to_status
              USING ERRCODE = 'FC004';
      END IF;
  END IF;
  ```
- **Conflict**:
  1. `public.dispatch_transition_rules` explicitly seeds:
     `('failed', 'queued', 'fc27_app', 'commissioner', false)`
  2. `tests/test_dispatch_transition.py:253-264` executes Case 4:
     ```python
     # 4. failed -> queued (commissioner retry via fc27_app)
     d4 = make_dispatch(status="failed", version=1)
     with psycopg.connect(pg_uri) as conn:
         with conn.transaction():
             conn.execute("SELECT set_config('fc27.actor', 'commissioner:batmanL8', true)")
             v2 = conn.execute(
                 """SELECT public.fc27_dispatch_transition(
                     %s, 'failed', 1, 'queued', 'retry requested', %s, '{}'::jsonb
                 )""",
                 (d4, uuid.uuid4())
             ).fetchone()[0]
             assert v2 == 2
     ```
  3. Because `p_to_status = 'queued'`, Step 7 hits the `ELSE` branch and raises:
     `RAISE EXCEPTION 'commissioner edge to queued deferred to Phase 2' USING ERRCODE = 'FC004';`
- **Resolution**:
  Step 7 was designed to enforce in-transaction Commissioner audit proof *specifically for supersession*. Other valid commissioner edges in `dispatch_transition_rules` (such as `failed -> queued` retry) do not involve package supersession.
  Change lines 1500–1513 in `migrations/0003_dispatch_foundation.sql` to:
  ```sql
  -- 7. Commissioner In-Transaction Proof
  IF v_actor_kind = 'commissioner' AND p_to_status = 'superseded' THEN
      SELECT EXISTS (
          SELECT 1 FROM public.approval_audit
           WHERE xmin = pg_catalog.pg_current_xact_id()::xid
             AND approved_by = substring(v_actor from 14)
      ) INTO v_has_audit;
      IF NOT v_has_audit THEN
          RAISE EXCEPTION 'supersede requires approval_audit committed in current transaction'
              USING ERRCODE = 'FC004';
      END IF;
  END IF;
  ```
  Note: General unpermitted transitions for `commissioner` are already rejected in Step 6 against `dispatch_transition_rules` with `FC002`.

---

## 4. Actionable Fix Plan for Worker

### Fix 1: Update `src/schemas.py`
Add `hide_input_in_errors=True` to `Objective.model_config`:
```python
class Objective(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
```

### Fix 2: Update `tests/conftest.py`
1. Register `FC001`–`FC007` in `psycopg.errors._sqlcodes` at module level:
```python
import psycopg.errors

for _code in ("FC001", "FC002", "FC003", "FC004", "FC005", "FC006", "FC007"):
    psycopg.errors._sqlcodes[_code] = psycopg.errors.RaiseException
```
2. Refactor `sample_approved_dispatch_prereqs` to provide terminal fields and wrap in an explicit transaction:
```python
@pytest.fixture()
def sample_approved_dispatch_prereqs(pg_uri, sample_player):
    tid = f"thread-dispatch-prereq-{uuid.uuid4().hex[:8]}"
    req_id = uuid.uuid4()
    req_id_2 = uuid.uuid4()
    pkg_v = "a" * 64
    payload_hash = "b" * 64

    with psycopg.connect(pg_uri) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO public.graph_threads (
                        thread_id, state, status, club_id, package_version, approval_request_id,
                        approved_by, approved_at, decision_payload
                    ) VALUES (
                        %s, '{"status":"approved"}', 'approved', '330323', %s, %s,
                        'batmanL8', now(), '{"decision":"approved"}'::jsonb
                    )""",
                    (tid, pkg_v, req_id),
                )
                cur.execute(
                    """INSERT INTO public.approval_requests (
                        approval_request_id, thread_id, package_version, status,
                        created_at, expires_at, decided_at
                    ) VALUES (
                        %s, %s, %s, 'approved',
                        now() - interval '10m', now() + interval '10h', now()
                    )""",
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

                cur.execute(
                    """INSERT INTO public.approval_audit (
                        thread_id, commissioner, decision, notes, owner, selection,
                        candidate_id, player_id, approved_by, approval_request_id, package_version
                    ) VALUES (%s, 'batmanL8', 'APPROVE', 'ok 2', 'coach', 'best_runner_up', 'cand-2', %s, 'batmanL8', %s, %s)
                    RETURNING id""",
                    (tid, sample_player["player_id"], req_id, pkg_v),
                )
                audit_id_2 = cur.fetchone()[0]

    return {
        "thread_id": tid,
        "approval_request_id": req_id,
        "approval_request_id_2": req_id_2,
        "approval_id": audit_id,
        "approval_id_2": audit_id_2,
        "hash": payload_hash,
        "player_id": sample_player["player_id"],
    }
```

### Fix 3: Update `migrations/0003_dispatch_foundation.sql`
In `public.fc27_dispatch_transition` Step 7 (lines 1499–1514), replace the `IF / ELSE` with a scoped check:
```sql
    -- 7. Commissioner In-Transaction Proof
    IF v_actor_kind = 'commissioner' AND p_to_status = 'superseded' THEN
        SELECT EXISTS (
            SELECT 1 FROM public.approval_audit
             WHERE xmin = pg_catalog.pg_current_xact_id()::xid
               AND approved_by = substring(v_actor from 14)
        ) INTO v_has_audit;
        IF NOT v_has_audit THEN
            RAISE EXCEPTION 'supersede requires approval_audit committed in current transaction'
                USING ERRCODE = 'FC004';
        END IF;
    END IF;
```

---

## 5. Worker Test Execution Commands

Once the above 3 fixes are applied, execute the following commands in order:

```powershell
# 1. Targeted execution of M3 test suites
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
.venv\Scripts\python.exe -m pytest -o addopts="" -v tests/test_dispatch_schema.py tests/test_dispatch_transition.py

# Expected: 40 passed in ~5-10s

# 2. Migration and infrastructure regression suite
.venv\Scripts\python.exe -m pytest -o addopts="" -v tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py

# 3. Approval lifecycle and graph API regression suite
.venv\Scripts\python.exe -m pytest -o addopts="" -v tests/test_approval_lifecycle.py tests/test_approval_security.py tests/test_graph_persistence.py tests/test_graph_api_v1.py
```
