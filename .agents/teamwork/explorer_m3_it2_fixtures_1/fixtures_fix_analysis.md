# Milestone 3 Iteration 2: Fixtures Fix Analysis Report

**Agent**: explorer_m3_it2_fixtures_1 (teamwork_preview_explorer)  
**Date**: 2026-10-03  
**Target File**: `tests/conftest.py`  
**Target Fixtures**: `sample_approved_dispatch_prereqs`, `make_dispatch`, `sample_player`

---

## 1. Executive Summary

Milestone 3 adversarial evaluation by Challenger 2 (`challenger_m3_2/challenge_report.md`) uncovered 44 test errors across `test_dispatch_schema.py` (26 errors) and `test_dispatch_transition.py` (18 errors) rooted in `tests/conftest.py:270`.
Our in-depth static and structural analysis of `tests/conftest.py` against `migrations/0001_initial.sql`, `migrations/0002_approval_security.sql`, and `migrations/0003_dispatch_foundation.sql` revealed **three interdependent constraint violations** in the test fixture layer:

1. **Check Constraint `graph_threads_terminal_fields_chk` Violation**:
   `sample_approved_dispatch_prereqs` inserts into `public.graph_threads` with `status = 'approved'` without providing `approved_by`, `approved_at`, or `decision_payload`.
2. **Autocommit & Deferred Trigger Premature Evaluation**:
   `sample_approved_dispatch_prereqs` connects with `autocommit=True`. Under PostgreSQL autocommit semantics, deferred constraint trigger `fc27_graph_threads_pair_check` fires at the conclusion of the first statement before the paired `approval_requests` record is inserted, raising `CheckViolation ('approval pair: linked approval_request ... not found')`.
3. **Unique Index `approval_audit_one_decision_per_thread` Collisions**:
   - `sample_approved_dispatch_prereqs` executes a second insert into `public.approval_audit` for `audit_id_2` using the identical `thread_id` (`tid`).
   - `make_dispatch` factory executes an insert into `public.approval_audit` on every call reusing `prereqs["thread_id"]`.
   `0002_approval_security.sql:96` enforces `CREATE UNIQUE INDEX approval_audit_one_decision_per_thread ON approval_audit(thread_id)`. Reusing `thread_id` raises `UniqueViolation`.

---

## 2. Constraint Analysis

### 2.1 Constraint `graph_threads_terminal_fields_chk`
Defined in `migrations/0003_dispatch_foundation.sql:226–231`:
```sql
ALTER TABLE public.graph_threads
    ADD CONSTRAINT graph_threads_terminal_fields_chk
    CHECK (
        (status IN ('approved', 'rejected', 'revision_requested'))
        = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)
    );
```
- **Rule**: A boolean biconditional equality (`=`). If `status` is `'approved'`, `'rejected'`, or `'revision_requested'`, all three columns (`approved_by`, `approved_at`, `decision_payload`) MUST be non-null. Conversely, if `status` is non-terminal (`running`, `awaiting_approval`, etc.), at least one of these columns must be null.
- **Fixture Defect**: `tests/conftest.py:270` inserted `status='approved'` with omitted terminal columns, defaulting to `NULL`. `TRUE = FALSE` evaluated to `FALSE`, immediately throwing `psycopg.errors.CheckViolation`.

### 2.2 Trigger `fc27_graph_threads_pair_check`
Defined in `migrations/0003_dispatch_foundation.sql:400–405`:
```sql
CREATE CONSTRAINT TRIGGER fc27_graph_threads_pair_check
AFTER INSERT OR UPDATE ON public.graph_threads
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.fc27_check_approval_pair();
```
- **Rule**: Requires that any `graph_threads` row with `approval_request_id IS NOT NULL` points to an existing `approval_requests` row with matching `status`, `package_version`, and `thread_id`.
- **Fixture Defect**: `tests/conftest.py:268` opened `psycopg.connect(pg_uri, autocommit=True)`. In autocommit mode, each SQL statement is an independent single-statement transaction. Deferred triggers fire at statement commit. Statement 1 (`INSERT INTO graph_threads`) commits before Statement 2 (`INSERT INTO approval_requests`), causing `fc27_check_approval_pair()` to fail with `approval pair: linked approval_request not found`.
- **Fix**: Run all paired inserts inside a transaction block (`with psycopg.connect(pg_uri) as conn:` / `with conn.transaction():`), allowing the deferred triggers to evaluate only at transaction commit when both records exist.

### 2.3 Unique Constraint `approval_audit_one_decision_per_thread`
Defined in `migrations/0002_approval_security.sql:96`:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS approval_audit_one_decision_per_thread ON approval_audit(thread_id);
```
- **Rule**: In Milestone 1, each graph thread may have at most one decision recorded in `approval_audit`.
- **Fixture Defect in `sample_approved_dispatch_prereqs`**:
  `sample_approved_dispatch_prereqs` defined `req_id_2 = uuid.uuid4()` but reused `tid` when inserting `audit_id_2`. As soon as check constraint #1 is resolved, inserting `audit_id_2` with `thread_id = tid` triggers `psycopg.errors.UniqueViolation` on `approval_audit_one_decision_per_thread`.
  - **Resolution**: Introduce a paired second thread `tid_2` for `req_id_2` and `audit_id_2`.
- **Fixture Defect in `make_dispatch`**:
  The `make_dispatch` factory function `_make(...)` inserted into `approval_audit` using `prereqs["thread_id"]`. This guarantees a collision with `audit_id` from `sample_approved_dispatch_prereqs`, and additionally fails whenever a test calls `make_dispatch` more than once (e.g. `test_allowed_phase1_transitions` calls `make_dispatch` 4 times; `test_failed_retryable_is_derived` calls it twice).
  - **Resolution**: Every invocation of `_make()` must generate its own distinct thread `d_tid` and paired approval request `d_req_id`, inserting a valid paired thread, approval request, approval audit, and dispatch record.

---

## 3. Audit of All Other Fixtures in `tests/conftest.py`

| Fixture | Line Numbers | Table(s) Touched | Status | Notes |
|---|---|---|---|---|
| `pg_admin_uri` | 32–42 | None | **CLEAN** | Connects to admin DB; skips if unset |
| `client` | 47–52 | None | **CLEAN** | TestClient factory |
| `FakeRoles` | 63–76 | None | **CLEAN** | Mock role runner |
| `wired` | 77–102 | None | **CLEAN** | App and runner factory |
| `pg_roles` | 112–115 | Postgres roles | **CLEAN** | Bootstraps roles |
| `pg_db` | 116–146 | Database DDL | **CLEAN** | Temporary DB lifecycle |
| `pg_db_migrated` | 149–153 | Migrations | **CLEAN** | Executes migration runner |
| `pg_admin_db_uri` | 154–156 | None | **CLEAN** | URI provider |
| `pg_migrator_uri` | 158–160 | None | **CLEAN** | URI provider |
| `pg_uri` | 162–177 | `fc27_approval_inconsistencies` | **CLEAN** | Verifies zero inconsistencies on teardown |
| `pg_dispatch_uri`| 178–181 | None | **CLEAN** | URI provider |
| `client_v1` | 185–194 | None | **CLEAN** | TestClient provider |
| `api_settings_factory` | 196–205 | None | **CLEAN** | Settings factory |
| `wired_restart` | 207–241 | None | **CLEAN** | Runner factory |
| `sample_player` | 245–258 | `public.players` | **CLEAN** | Complies with `player_id ~ '^[0-9]{1,20}$'`, `club_id ~ '^[0-9]+$'`, `players_status_active_sync_chk` (`status='active'`, `active=true`). |
| `sample_approved_dispatch_prereqs` | 260–309 | `graph_threads`, `approval_requests`, `approval_audit` | **VIOLATION** | Violates `graph_threads_terminal_fields_chk`, autocommit deferred trigger failure, and `approval_audit_one_decision_per_thread`. |
| `make_dispatch` | 311–382 | `approval_audit`, `dispatches` | **VIOLATION** | Reuses `prereqs["thread_id"]` in `approval_audit`, violating `approval_audit_one_decision_per_thread` on multiple calls. |

---

## 4. Exact Replacement Code for Worker

The worker should replace lines 260 to 382 of `tests/conftest.py` with the following implementation:

```python
# ==============================================================================
# Exact replacement for tests/conftest.py (lines 260–382)
# ==============================================================================

@pytest.fixture()
def sample_approved_dispatch_prereqs(pg_uri, sample_player):
    tid = f"thread-dispatch-prereq-{uuid.uuid4().hex[:8]}"
    tid_2 = f"thread-dispatch-prereq-2-{uuid.uuid4().hex[:8]}"
    req_id = uuid.uuid4()
    req_id_2 = uuid.uuid4()
    pkg_v = "a" * 64
    payload_hash = "b" * 64

    with psycopg.connect(pg_uri) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # 1. Primary approved thread, request, and audit record
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
                    ) VALUES (%s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now())""",
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

                # 2. Secondary approved thread, request, and audit record (provides unused approval_id_2)
                cur.execute(
                    """INSERT INTO public.graph_threads (
                        thread_id, state, status, club_id, package_version, approval_request_id,
                        approved_by, approved_at, decision_payload
                    ) VALUES (
                        %s, '{"status":"approved"}', 'approved', '330323', %s, %s,
                        'batmanL8', now(), '{"decision":"approved"}'::jsonb
                    )""",
                    (tid_2, pkg_v, req_id_2),
                )
                cur.execute(
                    """INSERT INTO public.approval_requests (
                        approval_request_id, thread_id, package_version, status,
                        created_at, expires_at, decided_at
                    ) VALUES (%s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now())""",
                    (req_id_2, tid_2, pkg_v),
                )
                cur.execute(
                    """INSERT INTO public.approval_audit (
                        thread_id, commissioner, decision, notes, owner, selection,
                        candidate_id, player_id, approved_by, approval_request_id, package_version
                    ) VALUES (%s, 'batmanL8', 'APPROVE', 'ok 2', 'coach', 'best_runner_up', 'cand-2', %s, 'batmanL8', %s, %s)
                    RETURNING id""",
                    (tid_2, sample_player["player_id"], req_id_2, pkg_v),
                )
                audit_id_2 = cur.fetchone()[0]

    return {
        "thread_id": tid,
        "thread_id_2": tid_2,
        "approval_request_id": req_id,
        "approval_request_id_2": req_id_2,
        "approval_id": audit_id,
        "approval_id_2": audit_id_2,
        "hash": payload_hash,
        "player_id": sample_player["player_id"],
    }


@pytest.fixture()
def make_dispatch(pg_uri, pg_admin_db_uri, sample_approved_dispatch_prereqs):
    import json
    prereqs = sample_approved_dispatch_prereqs

    def _make(status="pending", version=1, owner="coach", metric="pass_accuracy", instruction=None):
        d_id = uuid.uuid4()
        corr_id = uuid.uuid4()
        d_tid = f"thread-dispatch-make-{uuid.uuid4().hex[:8]}"
        d_req_id = uuid.uuid4()
        pkg_v = prereqs["hash"]
        inst = instruction or {
            "action": "drill",
            "objective": {
                "metric": metric,
                "direction": "increase",
                "target_delta": 10.0,
                "bounds_policy": "fc27-objective-bounds-v1",
            },
        }
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
                        (d_tid, pkg_v, d_req_id),
                    )
                    cur.execute(
                        """INSERT INTO public.approval_requests (
                            approval_request_id, thread_id, package_version, status,
                            created_at, expires_at, decided_at
                        ) VALUES (%s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now())""",
                        (d_req_id, d_tid, pkg_v),
                    )
                    cur.execute(
                        """INSERT INTO public.approval_audit (
                            thread_id, commissioner, decision, notes, owner, selection,
                            candidate_id, player_id, approved_by, approval_request_id, package_version
                        ) VALUES (%s, 'batmanL8', 'APPROVE', 'ok', %s, 'best', %s, %s, 'batmanL8', %s, %s)
                        RETURNING id""",
                        (
                            d_tid,
                            owner,
                            f"{owner}-cand-{uuid.uuid4().hex[:6]}",
                            prereqs["player_id"],
                            d_req_id,
                            pkg_v,
                        ),
                    )
                    unique_audit_id = cur.fetchone()[0]

                    cur.execute(
                        """INSERT INTO public.dispatches (
                            dispatch_id, source_thread_id, approval_id, approval_request_id,
                            approval_payload_hash, package_version, player_id, candidate_id, owner, selection,
                            club_id, approved_instruction, dispatch_status, version, plan_version,
                            created_by_principal, created_correlation_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'best', '330323', %s, 'pending', 1, 0, 'batmanL8', %s)""",
                        (
                            d_id,
                            d_tid,
                            unique_audit_id,
                            d_req_id,
                            pkg_v,
                            pkg_v,
                            prereqs["player_id"],
                            f"{owner}-best-1",
                            owner,
                            json.dumps(inst),
                            corr_id,
                        ),
                    )

        if status != "pending" or version != 1:
            with psycopg.connect(pg_admin_db_uri, autocommit=True) as admin_conn:
                admin_conn.execute("SET session_replication_role = 'replica'")
                try:
                    admin_conn.execute(
                        "UPDATE public.dispatches SET dispatch_status = %s, version = %s WHERE dispatch_id = %s",
                        (status, version, d_id),
                    )
                finally:
                    admin_conn.execute("SET session_replication_role = 'origin'")

        return d_id

    return _make
```
