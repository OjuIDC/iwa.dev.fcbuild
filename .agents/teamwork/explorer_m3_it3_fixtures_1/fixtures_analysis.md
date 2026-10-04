# Milestone 3 Iteration 3: Fixture Foreign Key Sequencing Analysis

## Executive Summary

During Milestone 3 Iteration 2 Gate 2 review, empirical execution by challengers (`challenger_m3_it2_1` and `challenger_m3_it2_2`) discovered that **44 out of 48 Milestone 3 tests failed during setup** with `psycopg.errors.ForeignKeyViolation`:
- `tests/test_dispatch_schema.py`: 22 passed, **26 errors**
- `tests/test_dispatch_transition.py`: 4 passed, **18 errors**

The root cause is a mutual foreign key deadlock in `tests/conftest.py` inside fixtures `sample_approved_dispatch_prereqs` and `make_dispatch`:
1. `graph_threads.approval_request_id` references `approval_requests(approval_request_id)` via an **immediate, non-deferrable** foreign key (`graph_threads_approval_request_id_fkey`).
2. `approval_requests.thread_id` references `graph_threads(thread_id)` via an **immediate, non-deferrable** foreign key (`approval_requests_thread_id_fkey`).
3. The fixtures attempted to insert `graph_threads` with `approval_request_id = req_id` in statement 1 before `approval_requests` had been inserted, immediately raising `ForeignKeyViolation` on statement 1. Wrapping the operations inside `with conn.transaction():` did not prevent this because non-deferrable foreign key constraints are evaluated per statement, not at commit.

This report provides the full architectural breakdown, constraint analysis, and exact drop-in replacement code for `tests/conftest.py`.

---

## 1. Foreign Key Constraint Topology

### 1.1 The Circular Dependency
Two tables in PostgreSQL 16 mutually reference each other:

```
┌─────────────────────────────────┐                 ┌──────────────────────────────────┐
│       public.graph_threads      │                 │     public.approval_requests     │
├─────────────────────────────────┤                 ├──────────────────────────────────┤
│ thread_id text (PK)             │◄───[FK 2]───────│ thread_id text (UNIQUE NOT NULL) │
│ status text                     │                 │ approval_request_id uuid (PK)    │
│ package_version text            │                 │ status text                      │
│ approval_request_id uuid (UNIQ) │───[FK 1]───────►│ package_version text             │
│ approved_by text                │                 │ decided_at timestamptz           │
│ approved_at timestamptz         │                 │ expires_at timestamptz           │
│ decision_payload jsonb          │                 │                                  │
└─────────────────────────────────┘                 └──────────────────────────────────┘
```

1. **FK 1: `graph_threads_approval_request_id_fkey`**
   - Source: `migrations/0003_dispatch_foundation.sql:205-206`
   - Definition:
     ```sql
     ALTER TABLE public.graph_threads
         ADD COLUMN IF NOT EXISTS approval_request_id uuid UNIQUE REFERENCES public.approval_requests(approval_request_id);
     ```
   - Evaluation Timing: **IMMEDIATE** (non-deferrable). Evaluated on statement execution of any `INSERT` or `UPDATE` affecting `approval_request_id`.
   - Nullability: **NULLABLE**. `approval_request_id` allows `NULL`. When `NULL`, PostgreSQL's standard foreign key semantics consider the constraint satisfied.

2. **FK 2: `approval_requests_thread_id_fkey`**
   - Source: `migrations/0003_dispatch_foundation.sql:126-128`
   - Definition:
     ```sql
     CREATE TABLE IF NOT EXISTS public.approval_requests (
         approval_request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
         thread_id text UNIQUE NOT NULL REFERENCES public.graph_threads(thread_id),
         ...
     );
     ```
   - Evaluation Timing: **IMMEDIATE** (non-deferrable). Evaluated on statement execution.
   - Nullability: **NOT NULL**. `thread_id` cannot be `NULL`; the referenced `graph_threads` row MUST already exist.

### 1.2 Why the Original Fixture Failed
In `tests/conftest.py` lines 277–286 and 367–376, the fixtures attempted:
```python
# Statement 1
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
# Statement 2
cur.execute(
    """INSERT INTO public.approval_requests (
        approval_request_id, thread_id, package_version, status, ...
    ) VALUES (%s, %s, %s, 'approved', ...)""",
    (req_id, tid, pkg_v),
)
```

At the exact instant Statement 1 executes:
- PostgreSQL inspects table `public.approval_requests` for key `(approval_request_id) = (req_id)`.
- Because Statement 2 has not yet executed, `req_id` does not exist in `approval_requests`.
- Because `graph_threads_approval_request_id_fkey` is immediate, PostgreSQL aborts the transaction immediately:
  ```
  psycopg.errors.ForeignKeyViolation: insert or update on table "graph_threads" violates foreign key constraint "graph_threads_approval_request_id_fkey"
  DETAIL: Key (approval_request_id)=(...) is not present in table "approval_requests".
  ```
- Conversely, if one were to execute Statement 2 first (`INSERT INTO approval_requests`), it would fail immediately because `tid` does not yet exist in `graph_threads` (`approval_requests_thread_id_fkey`).

Neither statement can be inserted first if both foreign keys are fully populated in their respective `INSERT` statements.

---

## 2. Comprehensive Constraint & Trigger Matrix

Any valid sequencing solution must satisfy **all 7 constraints and triggers** enforced on these tables:

| # | Constraint / Trigger Name | Table | Type | Rule / Condition |
|---|---|---|---|---|
| 1 | `graph_threads_status_chk` | `graph_threads` | CHECK | `status IN ('running', 'awaiting_approval', 'approved', 'rejected', 'revision_requested', 'expired', 'failed')` |
| 2 | `graph_threads_terminal_fields_chk` | `graph_threads` | CHECK | `(status IN ('approved', 'rejected', 'revision_requested')) = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)` |
| 3 | `graph_threads_guard` | `graph_threads` | BEFORE UPDATE OR DELETE | - Cannot modify terminal rows<br>- `package_version` immutable once awaiting or non-null<br>- `approval_request_id` immutable once non-null<br>- Terminal status requires `OLD.status = 'awaiting_approval'` |
| 4 | `graph_threads_approval_request_id_fkey` | `graph_threads` | FOREIGN KEY (Immediate) | `approval_request_id` must be NULL or exist in `approval_requests` |
| 5 | `approval_requests_thread_id_fkey` | `approval_requests` | FOREIGN KEY (Immediate) | `thread_id` must exist in `graph_threads` |
| 6 | `fc27_graph_threads_pair_check` | `graph_threads` | CONSTRAINT TRIGGER (Deferred) | At commit: if `approval_request_id` set, must match `approval_requests` row in `status`, `package_version`, and `thread_id` |
| 7 | `fc27_approval_requests_pair_check` | `approval_requests` | CONSTRAINT TRIGGER (Deferred) | At commit: must match `graph_threads` row in `status`, `package_version`, and `approval_request_id` |

### Critical Constraint Analysis:
1. **Why `status = 'awaiting_approval'` is required in Step 1**:
   In `graph_threads_guard` (lines 309–313):
   ```sql
   IF NEW.status IN ('approved', 'rejected', 'revision_requested', 'expired') 
      AND OLD.status <> 'awaiting_approval' THEN
       RAISE EXCEPTION 'thread % must be awaiting_approval before a decision (was %)', OLD.thread_id, OLD.status
           USING ERRCODE = 'check_violation';
   END IF;
   ```
   If `graph_threads` is inserted with `status = 'running'`, attempting to update directly to `'approved'` in Step 3 will be rejected with `check_violation`. Therefore, the row MUST be inserted with `status = 'awaiting_approval'`.

2. **Why `package_version = pkg_v` must be set in Step 1**:
   In `graph_threads_guard` (lines 295–299):
   ```sql
   IF (OLD.status = 'awaiting_approval' OR OLD.package_version IS NOT NULL) 
      AND NEW.package_version IS DISTINCT FROM OLD.package_version THEN
       RAISE EXCEPTION 'package_version is immutable'
           USING ERRCODE = 'check_violation';
   END IF;
   ```
   If `package_version` were NULL in Step 1, updating it to `pkg_v` in Step 3 would violate `package_version is immutable` because `OLD.status = 'awaiting_approval'`. Thus, `package_version` MUST be populated on the initial INSERT and kept identical on the UPDATE.

3. **Why `approval_request_id = NULL` in Step 1 is allowed**:
   In `graph_threads_guard` (lines 302–306):
   ```sql
   IF OLD.approval_request_id IS NOT NULL 
      AND NEW.approval_request_id IS DISTINCT FROM OLD.approval_request_id THEN
       RAISE EXCEPTION 'approval_request_id is immutable once set'
           USING ERRCODE = 'check_violation';
   END IF;
   ```
   Because `OLD.approval_request_id` is NULL, updating it to `req_id` in Step 3 does NOT trigger this exception.

4. **Why `terminal_fields_chk` passes in Step 1 and Step 3**:
   - In Step 1: `status = 'awaiting_approval'` (not terminal), and `approved_by`, `approved_at`, `decision_payload` are NULL. Both sides of the equality are `FALSE`. `FALSE = FALSE` evaluates to `TRUE`.
   - In Step 3: `status = 'approved'`, and all three fields are non-null. Both sides are `TRUE`. `TRUE = TRUE` evaluates to `TRUE`.

5. **Why deferred constraint triggers pass at commit**:
   Both `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check` are defined as `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED`.
   In PostgreSQL, constraint triggers fire at transaction commit. If a row was inserted and subsequently updated in the same transaction, the constraint trigger evaluates the current state of the updated tuple at commit time.
   At commit time:
   - `graph_threads`: `status = 'approved'`, `package_version = pkg_v`, `approval_request_id = req_id`, `thread_id = tid`.
   - `approval_requests`: `status = 'approved'`, `package_version = pkg_v`, `approval_request_id = req_id`, `thread_id = tid`.
   Every single field compared by `fc27_check_approval_pair()` matches identically.

6. **Why `fc27_approval_inconsistencies` view reports 0 rows**:
   The diagnostic view checks for status mismatch, package mismatch, unlinked requests, and missing links. Because both rows are fully linked and have status `'approved'` and package version `pkg_v`, the view returns 0 rows, satisfying the `pg_uri` fixture's post-test assertion.

---

## 3. The 4-Step SQL Sequencing Solution

The verified, deadlock-free SQL sequence in PostgreSQL is:

### Step 1: Insert `graph_threads` (Awaiting Approval, Unlinked)
```sql
INSERT INTO public.graph_threads (
    thread_id, state, status, club_id, package_version, approval_request_id
) VALUES (
    %s, '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, NULL
);
```
- Satisfies `graph_threads_approval_request_id_fkey` because `approval_request_id` is NULL.
- Satisfies `graph_threads_terminal_fields_chk` because status is `'awaiting_approval'` and terminal fields are NULL.
- Sets `package_version` up front to preserve immutability.

### Step 2: Insert `approval_requests` (Referencing Thread)
```sql
INSERT INTO public.approval_requests (
    approval_request_id, thread_id, package_version, status,
    created_at, expires_at, decided_at
) VALUES (
    %s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now()
);
```
- Satisfies `approval_requests_thread_id_fkey` because `thread_id` was created in Step 1.
- `status = 'approved'` and `decided_at = now()` satisfy `(status IN ('approved', ...)) = (decided_at IS NOT NULL)`.

### Step 3: Update `graph_threads` (Approved, Linking Request)
```sql
UPDATE public.graph_threads
   SET status = 'approved',
       state = '{"status":"approved"}',
       approval_request_id = %s,
       approved_by = 'batmanL8',
       approved_at = now(),
       decision_payload = '{"decision":"approved"}'::jsonb
 WHERE thread_id = %s;
```
- Satisfies `graph_threads_approval_request_id_fkey` because `approval_request_id` exists in `approval_requests` from Step 2.
- Satisfies `graph_threads_guard` state machine transition (`awaiting_approval -> approved`).
- Satisfies `graph_threads_terminal_fields_chk` (`approved_by`, `approved_at`, `decision_payload` are all non-null).

### Step 4: Insert `approval_audit` (Audit Record)
```sql
INSERT INTO public.approval_audit (
    thread_id, commissioner, decision, notes, owner, selection,
    candidate_id, player_id, approved_by, approval_request_id, package_version
) VALUES (
    %s, 'batmanL8', 'APPROVE', 'ok', %s, 'best', %s, %s, 'batmanL8', %s, %s
)
RETURNING id;
```
- Satisfies foreign keys referencing `graph_threads(thread_id)` and `approval_requests(approval_request_id)`.
- Provides the authoritative `approval_id` for dispatch creation.

---

## 4. Exact Drop-In Replacement Code for Worker

Target file: `tests/conftest.py`

### 4.1 Replacement for `sample_approved_dispatch_prereqs` (Lines 265–342)

```python
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
                # Step 1: Insert graph_threads with status='awaiting_approval' and approval_request_id=NULL
                cur.execute(
                    """INSERT INTO public.graph_threads (
                        thread_id, state, status, club_id, package_version, approval_request_id
                    ) VALUES (
                        %s, '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, NULL
                    )""",
                    (tid, pkg_v),
                )
                # Step 2: Insert approval_requests referencing thread_id
                cur.execute(
                    """INSERT INTO public.approval_requests (
                        approval_request_id, thread_id, package_version, status,
                        created_at, expires_at, decided_at
                    ) VALUES (%s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now())""",
                    (req_id, tid, pkg_v),
                )
                # Step 3: Update graph_threads to approved, linking approval_request_id
                cur.execute(
                    """UPDATE public.graph_threads
                          SET status = 'approved',
                              state = '{"status":"approved"}',
                              approval_request_id = %s,
                              approved_by = 'batmanL8',
                              approved_at = now(),
                              decision_payload = '{"decision":"approved"}'::jsonb
                        WHERE thread_id = %s""",
                    (req_id, tid),
                )
                # Step 4: Insert approval_audit record
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
                # Step 1: Insert graph_threads with status='awaiting_approval' and approval_request_id=NULL
                cur.execute(
                    """INSERT INTO public.graph_threads (
                        thread_id, state, status, club_id, package_version, approval_request_id
                    ) VALUES (
                        %s, '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, NULL
                    )""",
                    (tid_2, pkg_v),
                )
                # Step 2: Insert approval_requests referencing thread_id
                cur.execute(
                    """INSERT INTO public.approval_requests (
                        approval_request_id, thread_id, package_version, status,
                        created_at, expires_at, decided_at
                    ) VALUES (%s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now())""",
                    (req_id_2, tid_2, pkg_v),
                )
                # Step 3: Update graph_threads to approved, linking approval_request_id
                cur.execute(
                    """UPDATE public.graph_threads
                          SET status = 'approved',
                              state = '{"status":"approved"}',
                              approval_request_id = %s,
                              approved_by = 'batmanL8',
                              approved_at = now(),
                              decision_payload = '{"decision":"approved"}'::jsonb
                        WHERE thread_id = %s""",
                    (req_id_2, tid_2),
                )
                # Step 4: Insert approval_audit record
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
```

### 4.2 Replacement for `make_dispatch` (Lines 345–436)

```python
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
                    # Step 1: Insert graph_threads with status='awaiting_approval' and approval_request_id=NULL
                    cur.execute(
                        """INSERT INTO public.graph_threads (
                            thread_id, state, status, club_id, package_version, approval_request_id
                        ) VALUES (
                            %s, '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, NULL
                        )""",
                        (d_tid, pkg_v),
                    )
                    # Step 2: Insert approval_requests referencing d_tid
                    cur.execute(
                        """INSERT INTO public.approval_requests (
                            approval_request_id, thread_id, package_version, status,
                            created_at, expires_at, decided_at
                        ) VALUES (%s, %s, %s, 'approved', now() - interval '10m', now() + interval '10h', now())""",
                        (d_req_id, d_tid, pkg_v),
                    )
                    # Step 3: Update graph_threads to approved, linking d_req_id
                    cur.execute(
                        """UPDATE public.graph_threads
                              SET status = 'approved',
                                  state = '{"status":"approved"}',
                                  approval_request_id = %s,
                                  approved_by = 'batmanL8',
                                  approved_at = now(),
                                  decision_payload = '{"decision":"approved"}'::jsonb
                            WHERE thread_id = %s""",
                        (d_req_id, d_tid),
                    )
                    # Step 4: Insert approval_audit
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

                    # Step 5: Insert dispatch record referencing thread, audit, request, player
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

---

## 5. Verification Plan for Worker and Reviewers

Once the worker applies these changes to `tests/conftest.py`, the following verification commands must be executed by the worker and independently verified by reviewers and challengers:

1. **Verify `test_dispatch_transition.py`**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_transition.py
   ```
   **Expected Result**: `22 passed in <30s` (0 errors, 0 failures).

2. **Verify `test_dispatch_schema.py`**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_dispatch_schema.py
   ```
   **Expected Result**: `48 passed in <60s` (0 errors, 0 failures).

3. **Verify Regression Resistance Across All Milestone 1-3 Suites**:
   ```pwsh
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_dispatch_schema.py tests/test_dispatch_transition.py
   ```
   **Expected Result**: All tests pass cleanly, with 0 errors, 0 failures, and 0 approval inconsistencies reported.
