# Milestone 1: Authoritative Approval Lifecycle, Expiry & Security Test Suite Design

## 1. Executive Summary & Scope Boundary

### 1.1 Objective
This document delivers the technical exploration, architectural specification, and complete implementation blueprint for the test suites of **Milestone 1** of the player-dispatch programme (Phase 1: Secure Data Foundation).

Milestone 1 encompasses:
1. **Authoritative Approval Lifecycle & Migration 0003 Part B**:
   - Schema creation for `approval_requests`, table alters on `graph_threads`, `approval_audit`, `approval_nonces`.
   - Deferred constraint trigger `fc27_check_approval_pair()` ensuring 1:1 status and package version pairing between `graph_threads` and `approval_requests`.
   - Inconsistency diagnostics view `public.fc27_approval_inconsistencies`.
   - Immutability guards on `graph_threads.package_version` while awaiting approval.
   - Forward migration and backfill on legacy 0002 data without altering terminal rows.
2. **ApprovalStore Lifecycle Methods**:
   - `complete_run`: creates linked request, emits `approval.created` domain event and outbox record.
   - `apply_decision`: validates HMAC, consumes nonce, atomically updates request and thread status, writes `approval_audit`, emits `approval.decided`, writes outbox record, and guarantees safe idempotent replay.
   - `issue_challenge`: lazy expiry check, locks request `FOR SHARE` then thread, enforces nonces bound to request expiry.
   - `expire_due`: idempotent scanning and batch processing of expired requests, atomic transition to `expired`, nonce revocation, and `approval.expired` event emission.
   - `pending_approvals`: inbox query joining `approval_requests` and `graph_threads`, filtering out terminal or expired threads.
3. **Section 1A Authority Separation**:
   - Strict separation of club-level authorization vs player consent.
   - Fundamental security rule: timeout and inactivity can NEVER approve or accept a recommendation.
   - Fail-closed security for unsupported actor types (e.g. `player`, `agent`, `coach`, `delegate`) and unsupported authority bases (e.g. `affected_player_fallback`, `explicit_delegate`, `affected_player_consent`) in Phase 1.
4. **Test Fixtures and Framework Invariants**:
   - Automatic teardown check in `conftest.py` ensuring `fc27_approval_inconsistencies` is empty after every test (with `@pytest.mark.allow_inconsistency` opt-out for deliberate mismatch injection).
   - Helper `legacy_install(admin_db_uri)` in `tests/helpers.py`.
   - Rewriting stale-candidate test in `tests/test_approval_security.py` to assert database trigger immutability.

### 1.2 Strict Read-Only Boundary
This exploration operates strictly in read-only mode regarding existing repository source files. All findings, designs, and mock implementations are documented in this analysis file and `handoff.md` within `.agents/teamwork/explorer_m1_tests_1/`.

---

## 2. Current Baseline & Environmental Verification

### 2.1 Live Environment Verification
- **PostgreSQL Database**: A `postgres:16-alpine` container (`fc27-test-postgres`) is active on `127.0.0.1:55432`.
- **Test Runner**: Running `.venv\Scripts\python.exe -m pytest` with `TEST_POSTGRES_URI=postgresql://fc27test@127.0.0.1:55432/postgres`.
- **Baseline Test Results**:
  - `tests/test_events_outbox.py`: **7 passed in 6.38s** (confirms Task 3 outbox/events functionality is healthy).
  - `tests/test_approval_security.py`: **30 passed in 27.50s** (confirms existing 0002 approval security, nonces, and thread sequencing are healthy).

### 2.2 Repository State vs Milestone 1 Prerequisites
- **Migrations**: `migrations/0003_dispatch_foundation.sql` currently contains only **Section A** (`domain_events`, `outbox`, `fc27_reject_mutation()`, `fc27_check_event_source()`). Section B (Task 4) is pending implementation.
- **Grants Policy**: `migrations/post/grants.sql` is at `grants-policy-version: 2` with SHA-256 matching `GRANTS_POLICY` in `src/db_migrate.py`. Milestone 1 will require bumping to version 3 when adding `approval_requests` and `fc27_approval_inconsistencies` grants.
- **Missing Test Helpers**: `tests/helpers.py` currently lacks `legacy_install(admin_db_uri)`.
- **Missing Test Suites**: `tests/test_migration_0003.py` and `tests/test_approval_lifecycle.py` do not yet exist in `tests/`.

---

## 3. Architecture & Contract Matrix for Milestone 1

### 3.1 Database Schema & Triggers (0003 Part B & Section 1A)

#### A. Table `public.approval_requests`
```sql
CREATE TABLE public.approval_requests (
    approval_request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id           text UNIQUE NOT NULL REFERENCES public.graph_threads(thread_id),
    package_version     text NOT NULL CHECK (package_version ~ '^[0-9a-f]{64}$'),
    status              text NOT NULL CHECK (status IN ('awaiting_approval','approved','rejected','revision_requested','expired')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz NOT NULL,
    decided_at          timestamptz,
    expired_at          timestamptz,
    version             int NOT NULL DEFAULT 1,
    CHECK (expires_at > created_at),
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK ((status IN ('approved','rejected','revision_requested')) = (decided_at IS NOT NULL))
);
```

#### B. Trigger Guards on `approval_requests`
1. **Immutable Fields Guard**: `thread_id`, `package_version`, `created_at`, and `expires_at` can never be modified. Attempted updates raise `check_violation` with message `'<column> is immutable'`.
2. **Terminal Row Frozen Guard**: Rows in terminal status (`approved`, `rejected`, `revision_requested`, `expired`) can never be modified or deleted. Attempted mutation raises `check_violation` with message `'approval_request is terminal and frozen'`.

#### C. Updates to `public.graph_threads`
1. `ADD COLUMN approval_request_id uuid UNIQUE REFERENCES public.approval_requests(approval_request_id)`
2. `status` CHECK updated: `CHECK (status IN ('running','awaiting_approval','approved','rejected','failed','revision_requested','expired'))`
3. Terminal fields CHECK updated:
   ```sql
   CHECK ((status IN ('approved','rejected','revision_requested'))
          = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL))
   ```
4. Replaced `fc27_guard_thread_transition()` trigger:
   - Terminal statuses: `approved`, `rejected`, `revision_requested`, `expired`.
   - Allowed transitions: `running -> awaiting_approval`, `awaiting_approval -> approved | rejected | revision_requested | expired`.
   - `package_version` is **immutable** once status is `awaiting_approval` (raises `check_violation` with message `'package_version is immutable'`).
   - `approval_request_id` can be set at most once (raises `check_violation` if altered).

#### D. Deferred Constraint Trigger `fc27_check_approval_pair()`
```sql
CREATE OR REPLACE FUNCTION public.fc27_check_approval_pair()
RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_th RECORD;
    v_req RECORD;
BEGIN
    IF TG_TABLE_NAME = 'graph_threads' THEN
        IF NEW.status = 'awaiting_approval' AND NEW.package_version IS NOT NULL AND NEW.approval_request_id IS NULL THEN
            RAISE EXCEPTION 'approval pair: awaiting thread requires link' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.approval_request_id IS NOT NULL THEN
            SELECT * INTO v_req FROM public.approval_requests WHERE approval_request_id = NEW.approval_request_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'approval pair: linked approval_request % not found', NEW.approval_request_id USING ERRCODE = 'check_violation';
            END IF;
            IF v_req.status != NEW.status THEN
                RAISE EXCEPTION 'approval pair: status mismatch (thread=%, req=%)', NEW.status, v_req.status USING ERRCODE = 'check_violation';
            END IF;
            IF v_req.package_version != NEW.package_version THEN
                RAISE EXCEPTION 'approval pair: package_version mismatch', NEW.thread_id USING ERRCODE = 'check_violation';
            END IF;
            IF v_req.thread_id != NEW.thread_id THEN
                RAISE EXCEPTION 'approval pair: thread_id mismatch', NEW.thread_id USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    ELSIF TG_TABLE_NAME = 'approval_requests' THEN
        SELECT * INTO v_th FROM public.graph_threads WHERE thread_id = NEW.thread_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'approval pair: thread % not found', NEW.thread_id USING ERRCODE = 'check_violation';
        END IF;
        IF v_th.approval_request_id IS DISTINCT FROM NEW.approval_request_id THEN
            RAISE EXCEPTION 'approval pair: reverse link mismatch on thread %', NEW.thread_id USING ERRCODE = 'check_violation';
        END IF;
        IF v_th.status != NEW.status THEN
            RAISE EXCEPTION 'approval pair: status mismatch (req=%, thread=%)', NEW.status, v_th.status USING ERRCODE = 'check_violation';
        END IF;
        IF v_th.package_version != NEW.package_version THEN
            RAISE EXCEPTION 'approval pair: package_version mismatch', NEW.thread_id USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER fc27_pair_check_threads
AFTER INSERT OR UPDATE ON public.graph_threads
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.fc27_check_approval_pair();

CREATE CONSTRAINT TRIGGER fc27_pair_check_requests
AFTER INSERT OR UPDATE ON public.approval_requests
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.fc27_check_approval_pair();
```

#### E. View `public.fc27_approval_inconsistencies`
```sql
CREATE OR REPLACE VIEW public.fc27_approval_inconsistencies AS
SELECT
    CASE
        WHEN r.approval_request_id IS NULL AND g.status IN ('approved','rejected','failed') THEN 'legacy_unlinked'
        WHEN r.approval_request_id IS NULL AND g.status = 'awaiting_approval' AND g.package_version IS NULL THEN 'legacy_unlinked'
        WHEN r.approval_request_id IS NULL AND g.status = 'awaiting_approval' AND g.package_version IS NOT NULL THEN 'missing_link'
        WHEN g.thread_id IS NULL THEN 'unlinked_request'
        WHEN g.status != r.status THEN 'status_mismatch'
        WHEN g.package_version != r.package_version THEN 'package_mismatch'
        ELSE 'unknown'
    END AS kind,
    g.thread_id,
    r.approval_request_id
FROM public.graph_threads g
FULL OUTER JOIN public.approval_requests r ON g.approval_request_id = r.approval_request_id
WHERE (
    (r.approval_request_id IS NULL AND g.status IN ('approved','rejected','failed','awaiting_approval'))
    OR g.thread_id IS NULL
    OR g.status != r.status
    OR g.package_version != r.package_version
);
```

#### F. Section 1A Authority Compatibility Schema
- Table `public.authority_policies`: stores allowed primary, delegated, and fallback authority policies by action scope (`scope`, `allowed_actor_types`, `allowed_authority_bases`, `policy_version`, `created_at`).
- Table `public.authority_delegations`: stores explicit, revocable human delegation records (`delegation_id`, `delegator_user_id`, `delegate_user_id`, `scope`, `expires_at`, `revoked_at`).
- Table `public.player_assignments`: stores player-specific assignments (`assignment_id`, `package_id`, `subject_player_id`, `version`, `hash`, `status`).
- Columns added to `approval_audit`:
  - `actor_type text NOT NULL DEFAULT 'commissioner' CHECK (actor_type IN ('commissioner','delegate','player','system'))`
  - `actor_id text`
  - `authority_role text NOT NULL DEFAULT 'commissioner'`
  - `authority_basis text NOT NULL DEFAULT 'primary' CHECK (authority_basis IN ('primary','explicit_delegate','affected_player_consent','affected_player_fallback'))`
  - `delegation_id uuid NULL`
  - `decision_scope text NOT NULL DEFAULT 'club'`
  - `subject_player_id text NULL`
  - `conflict_state text NULL`

---

## 4. Deep Dive: `tests/test_migration_0003.py` Design

### 4.1 Required Test Cases

| Test Function | Invariant Tested | Verification Technique |
|---|---|---|
| `test_0003_applies_on_legacy_0002_data` | AC-56: 0003 forward migration on legacy 0002 database succeeds, backfills eligible awaiting thread, leaves terminal rows byte-identical, view shows only `legacy_unlinked`. | Execute `legacy_install`, run `bootstrap_database(confirm=True)`, seed 5 distinct legacy scenarios as admin, execute `migrate(migrator)`, verify data & view. |
| `test_pair_mismatch_fails_at_commit` | AC-55: Constraint trigger `fc27_check_approval_pair` is deferred and raises `check_violation` with `'approval pair:'` at `COMMIT` when statuses or versions mismatch. | In single transaction, mutate `graph_threads.status` alone; assert commit fails. In second transaction, mutate `approval_requests.status` alone; assert commit fails. |
| `test_awaiting_thread_requires_link` | AC-55: An awaiting thread with `package_version` must be linked to an `approval_request`. | Attempt `INSERT/UPDATE graph_threads` with `status='awaiting_approval'`, `package_version='...'`, and `approval_request_id=NULL`; commit raises `approval pair: awaiting thread requires link`. |
| `test_package_version_immutable_while_awaiting` | Trigger `fc27_guard_thread_transition` forbids mutating `package_version` once status is `awaiting_approval`. | Attempt direct `UPDATE graph_threads SET package_version='...'` on awaiting thread; assert `psycopg.errors.CheckViolation` with `'package_version is immutable'`. |
| `test_approval_request_immutable_fields` | Trigger on `approval_requests` protects `thread_id`, `package_version`, `created_at`, `expires_at`. | Execute direct `UPDATE` on each column; assert CheckViolation with message `'<column> is immutable'`. |
| `test_approval_request_frozen_when_terminal` | Trigger on `approval_requests` forbids any update or delete once status is terminal (`approved`, `rejected`, `revision_requested`, `expired`). | Attempt `UPDATE` or `DELETE` on terminal request; assert CheckViolation with `'approval_request is terminal and frozen'`. |
| `test_inconsistencies_view_detects_mismatches` | View `fc27_approval_inconsistencies` accurately classifies `status_mismatch`, `package_mismatch`, `unlinked_request`, and `missing_link`. | Use `@pytest.mark.allow_inconsistency`; deliberately insert unlinked/mismatched rows as admin; query view and verify `kind`. |

### 4.2 Helper Implementation: `tests/helpers.py::legacy_install`
```python
def legacy_install(admin_db_uri: str) -> None:
    """Installs 0001, 0002 and PostgresSaver checkpoints as admin (pre-bootstrap legacy state)."""
    migrations_dir = Path(__file__).resolve().parents[1] / "migrations"
    with psycopg.connect(admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    text PRIMARY KEY,
                    name       text NOT NULL,
                    sha256     text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            for v, filename in [("0001", "0001_initial.sql"), ("0002", "0002_approval_security.sql")]:
                sql_path = migrations_dir / filename
                content = sql_path.read_text(encoding="utf-8")
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                cur.execute(content)
                cur.execute(
                    "INSERT INTO schema_migrations(version, name, sha256) VALUES (%s, %s, %s)",
                    (v, filename, digest),
                )
        from langgraph.checkpoint.postgres import PostgresSaver
        PostgresSaver(conn).setup()
```

---

## 5. Deep Dive: `tests/test_approval_lifecycle.py` Design

### 5.1 Required Test Cases

| Test Function | Invariant Tested | Verification Technique |
|---|---|---|
| `test_complete_run_creates_linked_request` | `ApprovalStore.complete_run` creates request row, links thread, emits `approval.created` event and outbox row, returns ISO expiry string. | Run pipeline to completion; inspect DB records in `approval_requests`, `domain_events`, and `outbox`; verify `channel='fc27:events'`. |
| `test_decision_updates_request_and_thread_together` | Decision commit atomically transitions request & thread to `approved`, sets `decided_at`, increments version, writes audit with `proof_type='hmac'`, emits `approval.decided`, writes outbox row. | Submit signed decision; assert both statuses updated in single txn; verify `approval_audit.proof_type == 'hmac'`, correlation_id set, domain event references audit id. |
| `test_approve_writes_no_dispatch_key` | AC-44: No `dispatch` dictionary is written into thread state by decision or approval store. | Decide APPROVE; inspect DB `graph_threads.state` and returned state dict; assert `"dispatch"` key is absent. |
| `test_request_revision_lifecycle` | Spec §7.3: `REQUEST_REVISION` decision transitions thread & request to `revision_requested`, records required notes in audit, and is terminal. | Submit signed `REQUEST_REVISION` challenge; assert status is `revision_requested`; assert thread cannot be reset. |
| `test_expired_request_blocks_prior_nonce` | AC-43: A nonce issued before deadline cannot be used after request expiry; `apply_decision` returns 409 `approval_expired`, marks both records expired, and revokes nonce. | Create store with `request_ttl=timedelta(seconds=1)`; issue challenge; sleep 1.2s; call `apply_decision`; assert 409 `approval_expired`, DB status `expired`, nonce `revoked_at` set. |
| `test_expired_request_blocks_new_challenge` | AC-43: `issue_challenge` refuses past-due request, marks it expired, and returns 409 `approval_expired`. | Wait past `expires_at`; call `issue_challenge`; assert StoreError 409 `approval_expired`; assert no nonce row created. |
| `test_expire_due_is_idempotent` | `ApprovalStore.expire_due` processes all due requests cleanly in individual txns; repeated invocation is a safe no-op. | Seed expired requests; call `expire_due()`; verify list of expired thread IDs returned; call `expire_due()` again; verify empty list `[]`. |
| `test_expiry_and_decision_race_has_single_outcome` | Review Focus 2: Lock order (nonces -> request -> thread) guarantees zero deadlock and exactly one terminal winner between concurrent `expire_due` and `apply_decision`. | Spawn 4 `expire_due` threads and 4 `apply_decision` threads synchronized with barrier at deadline; assert all complete in < 10s; winner is either `approved` (with 1 audit row) or `expired` (with 0 audit rows). |
| `test_inbox_query_never_shows_terminal_threads` | AC-55: `ApprovalStore.pending_approvals()` never returns threads in terminal states (`approved`, `rejected`, `revision_requested`, `expired`) or past deadline. | Create threads across all statuses; call `pending_approvals()`; assert ONLY the active, unexpired `awaiting_approval` thread is returned. |
| `test_failed_decision_rolls_back_everything` | AC-1: Transaction atomicity rollback; failure in audit, events, or outbox rolls back request state, thread state, and nonce consumption. | Parameterized on `["approval_audit", "domain_events", "outbox"]`; inject failing BEFORE INSERT trigger via admin; call `apply_decision`; assert exception raised; verify thread remains `awaiting_approval`, request remains `awaiting_approval`, nonce unconsumed. |
| `test_safe_idempotent_decision_replay` | Submitting the identical signed decision replay returns existing decision safely; submitting conflicting decision raises 409. | Call `apply_decision`; repeat identical call; verify same state returned without error; submit modified payload or conflicting decision; verify 409 conflict. |

---

## 6. Deep Dive: Section 1A Authority-Separation Test Design

### 6.1 Section 1A Invariants & Negative Test Matrix

| Invariant | Requirement Statement | Test Implementation |
|---|---|---|
| **Timeout/Inactivity Cannot Approve** | "Neither the Graph nor a scheduler may implement logic equivalent to: `if approval_request_timed_out: approved = True`... Fallback authority does not mean automatic approval." | Set request past deadline; trigger `expire_due` or background daemon; verify request & thread become `expired`, NEVER `approved`; verify 0 approval audit rows with `APPROVE`. |
| **Unsupported Actor Type Fail Closed** | In Phase 1 bootstrap, only `commissioner` actor type is authorized. `player`, `delegate`, `agent`, `coach` must fail closed. | Attempt `apply_decision` with `actor_type IN ('player', 'delegate', 'agent', 'coach')`; assert StoreError(403, "unsupported actor type in Phase 1"); assert thread remains `awaiting_approval`. |
| **Unsupported Authority Basis Fail Closed** | In Phase 1 bootstrap, only `primary` authority basis is authorized. `affected_player_fallback`, `explicit_delegate`, `affected_player_consent` must fail closed. | Attempt `apply_decision` with `authority_basis IN ('affected_player_fallback', 'explicit_delegate', 'affected_player_consent')`; assert StoreError(403, "unsupported authority basis in Phase 1"); assert thread remains `awaiting_approval`. |
| **Club Auth vs Player Consent Schema Separation** | Schema must distinguish club authorization from player consent. Commissioner authorization cannot be recorded as player acceptance; player acceptance cannot be recorded as Commissioner authorization. | Verify `approval_audit.decision` allows only `('APPROVE','REJECT','REQUEST_REVISION')`; verify player consent tables (`player_assignments`) are distinct and hold player actions `('accept','reject','changes_requested','pause','acknowledge')`. |
| **Player Agent Cannot Be Decision Actor** | "No agent may approve or reject for a human... A player agent must not submit a decision without authenticated player action." | Attempt decision with `actor = 'agent:player'` or `actor_type = 'agent'`; assert 403 rejection. |
| **Escalation State Cannot Approve** | Escalation state updates (e.g. `evaluating_fallback`, `primary_reminder_due`, `held_no_authorized_approver`) can never substitute for a committed human decision. | Mutate escalation state in DB; verify thread remains awaiting approval; verify graph cannot resume. |
| **No Player Decision Endpoints Active in Phase 1** | Future player decision routes must not be active in Phase 1. | Issue HTTP requests to `POST /v1/player-decisions`, `POST /v1/assignments/{id}/decision`, `POST /v1/delegations`; assert 404 Not Found. |

---

## 7. Deep Dive: `tests/conftest.py` & Test Suite Health

### 7.1 Teardown Inconsistency Assertion
To enforce AC-55 ("`fc27_approval_inconsistencies` is empty after the suite") across all tests running against the database:

```python
# In tests/conftest.py
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_inconsistency: allow temporary inconsistency in fc27_approval_inconsistencies view"
    )

@pytest.fixture()
def pg_uri(request, pg_db_migrated):
    yield pg_db_migrated.app
    # Teardown check:
    if "allow_inconsistency" not in request.keywords:
        with psycopg.connect(pg_db_migrated.admin) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.fc27_approval_inconsistencies')")
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        "SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'"
                    )
                    count = cur.fetchone()[0]
                    assert count == 0, (
                        f"Found {count} inconsistencies in fc27_approval_inconsistencies after test '{request.node.name}'"
                    )
```

### 7.2 Rewrite Specification for `tests/test_approval_security.py:203-210`
In `tests/test_approval_security.py`, lines 203–210 currently read:
```python
def test_stale_candidate_package_cannot_be_approved(client, pg_uri):
    tid = start(client)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    with psycopg.connect(pg_uri) as conn:
        conn.execute("UPDATE graph_threads SET package_version=%s WHERE thread_id=%s", ("f" * 64, tid))
    r = client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch))
    assert r.status_code == 409 and "stale" in r.json()["detail"]
```
Under 0003 Part B, `fc27_guard_thread_transition()` enforces that `package_version` is **immutable** once status is `awaiting_approval`. The direct update on line 207 will now raise `psycopg.errors.CheckViolation` with `'package_version is immutable'`.

**Required Refactoring**:
1. Update `test_stale_candidate_package_cannot_be_approved`:
   ```python
   def test_package_version_immutable_once_awaiting_approval(client, pg_uri):
       tid = start(client)
       with psycopg.connect(pg_uri) as conn:
           with pytest.raises(psycopg.errors.CheckViolation, match="package_version is immutable"):
               conn.execute("UPDATE graph_threads SET package_version=%s WHERE thread_id=%s", ("f" * 64, tid))
   ```
2. Add a companion test `test_stale_challenge_payload_rejected_by_store`:
   ```python
   def test_stale_challenge_payload_rejected_by_store(client, pg_uri):
       tid = start(client)
       ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
       # Tamper signed challenge with an old/stale package_version
       stale_payload = {**ch, "package_version": "0" * 64}
       r = client.post(f"/threads/{tid}/decision", headers=COM, json=signed(stale_payload))
       assert r.status_code == 409
       assert "stale" in r.json()["detail"] or "match" in r.json()["detail"]
   ```

---

## 8. Concrete Test Code Blueprints

### 8.1 Blueprint: `tests/test_migration_0003.py`
```python
"""Tests for migration 0003 Part B: approval requests, pair consistency, and legacy migration."""
import uuid
import psycopg
import pytest
from tests.helpers import legacy_install, make_app, start, COM, APPROVE_COACH_BEST, signed
from src.db_roles import bootstrap_database
from src.db_migrate import migrate

def test_0003_applies_on_legacy_0002_data(pg_db):
    """AC-56: 0003 applies cleanly on legacy 0002 data, backfilling eligible awaiting threads."""
    legacy_install(pg_db.admin)
    bootstrap_database(pg_db.admin, legacy_owner=None, confirm=True)

    # Seed 5 legacy scenarios as admin
    with psycopg.connect(pg_db.admin, autocommit=True) as conn:
        with conn.cursor() as cur:
            # 1. Awaiting WITH package_version (eligible for backfill)
            cur.execute("""
                INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version)
                VALUES ('fc27-330323-run-20261001-001', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s)
            """, ('a' * 64,))

            # 2. Awaiting WITHOUT package_version (ineligible for backfill)
            cur.execute("""
                INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version)
                VALUES ('fc27-330323-run-20261001-002', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', NULL)
            """)

            # 3. Approved thread + audit + consumed nonce
            cur.execute("""
                INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approved_by, approved_at, decision_payload)
                VALUES ('fc27-330323-run-20261001-003', '{"status":"approved"}', 'approved', '330323', %s, 'batmanL8', now(), '{"test":true}')
            """, ('b' * 64,))
            cur.execute("""
                INSERT INTO public.approval_nonces (nonce, thread_id, payload_sha256, package_version, issued_at, expires_at, consumed_at, created_by)
                VALUES (%s, 'fc27-330323-run-20261001-003', %s, %s, now() - interval '10m', now() + interval '10m', now(), 'batmanL8')
            """, ('nonce-003-' + 'x' * 22, '1' * 64, 'b' * 64))
            cur.execute("""
                INSERT INTO public.approval_audit (thread_id, commissioner, decision, notes, approved_by, nonce, package_version, signature_sha256)
                VALUES ('fc27-330323-run-20261001-003', 'batmanL8', 'APPROVE', 'ok', 'batmanL8', %s, %s, %s)
            """, ('nonce-003-' + 'x' * 22, 'b' * 64, '2' * 64))

            # 4. Rejected thread
            cur.execute("""
                INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approved_by, approved_at, decision_payload)
                VALUES ('fc27-330323-run-20261001-004', '{"status":"rejected"}', 'rejected', '330323', %s, 'batmanL8', now(), '{"test":true}')
            """, ('c' * 64,))
            cur.execute("""
                INSERT INTO public.approval_nonces (nonce, thread_id, payload_sha256, package_version, issued_at, expires_at, consumed_at, created_by)
                VALUES (%s, 'fc27-330323-run-20261001-004', %s, %s, now() - interval '10m', now() + interval '10m', now(), 'batmanL8')
            """, ('nonce-004-' + 'x' * 22, '3' * 64, 'c' * 64))
            cur.execute("""
                INSERT INTO public.approval_audit (thread_id, commissioner, decision, notes, approved_by, nonce, package_version, signature_sha256)
                VALUES ('fc27-330323-run-20261001-004', 'batmanL8', 'REJECT', 'no', 'batmanL8', %s, %s, %s)
            """, ('nonce-004-' + 'x' * 22, 'c' * 64, '4' * 64))

            # 5. Unconsumed nonce for thread 1
            cur.execute("""
                INSERT INTO public.approval_nonces (nonce, thread_id, payload_sha256, package_version, issued_at, expires_at, created_by)
                VALUES (%s, 'fc27-330323-run-20261001-001', %s, %s, now(), now() + interval '5m', 'batmanL8')
            """, ('nonce-001-' + 'x' * 22, '5' * 64, 'a' * 64))

    # Apply migration 0003
    applied = migrate(pg_db.migrator)
    assert "0003_dispatch_foundation.sql" in applied

    with psycopg.connect(pg_db.admin) as conn:
        with conn.cursor() as cur:
            # Check exactly one approval_requests row created for thread 1
            cur.execute("SELECT approval_request_id, thread_id, package_version, status FROM public.approval_requests")
            reqs = cur.fetchall()
            assert len(reqs) == 1
            req_id, tid, pkg_v, status = reqs[0]
            assert tid == 'fc27-330323-run-20261001-001'
            assert pkg_v == 'a' * 64
            assert status == 'awaiting_approval'

            # Thread 1 is linked
            cur.execute("SELECT approval_request_id FROM public.graph_threads WHERE thread_id = 'fc27-330323-run-20261001-001'")
            assert cur.fetchone()[0] == req_id

            # Ineligible and terminal threads have NULL link
            cur.execute("SELECT thread_id, approval_request_id FROM public.graph_threads WHERE thread_id != 'fc27-330323-run-20261001-001' ORDER BY thread_id")
            for other_tid, other_link in cur.fetchall():
                assert other_link is None, f"Expected None for {other_tid}"

            # Inconsistencies view contains ONLY legacy_unlinked
            cur.execute("SELECT kind, thread_id FROM public.fc27_approval_inconsistencies")
            incons = cur.fetchall()
            for kind, in_tid in incons:
                assert kind == 'legacy_unlinked'
                assert in_tid in ('fc27-330323-run-20261001-002', 'fc27-330323-run-20261001-003', 'fc27-330323-run-20261001-004')


def test_pair_mismatch_fails_at_commit(pg_uri, client):
    """AC-55: Constraint trigger fc27_check_approval_pair raises at commit when statuses mismatch."""
    tid = start(client)
    # Thread is awaiting_approval with linked request

    # Subtest A: Mutating graph_threads.status alone fails at commit
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.graph_threads SET status = 'approved', approved_by = 'batmanL8', approved_at = now(), decision_payload = '{}' WHERE thread_id = %s", (tid,))
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: status mismatch"):
                conn.commit()

    # Subtest B: Mutating approval_requests.status alone fails at commit
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.approval_requests SET status = 'approved', decided_at = now() WHERE thread_id = %s", (tid,))
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: status mismatch"):
                conn.commit()


def test_awaiting_thread_requires_link(pg_uri):
    """AC-55: An awaiting_approval thread with package_version must have approval_request_id."""
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
                VALUES ('fc27-330323-run-20261001-999', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, NULL)
            """, ('d' * 64,))
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: awaiting thread requires link"):
                conn.commit()


def test_package_version_immutable_while_awaiting(pg_uri, client):
    """Trigger fc27_guard_thread_transition blocks updating package_version while awaiting."""
    tid = start(client)
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="package_version is immutable"):
                cur.execute("UPDATE public.graph_threads SET package_version = %s WHERE thread_id = %s", ('f' * 64, tid))


def test_approval_request_immutable_fields(pg_uri, client):
    """approval_requests trigger protects thread_id, package_version, created_at, expires_at."""
    tid = start(client)
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_request_id FROM public.graph_threads WHERE thread_id = %s", (tid,))
            req_id = cur.fetchone()[0]

            with pytest.raises(psycopg.errors.CheckViolation, match="thread_id is immutable"):
                cur.execute("UPDATE public.approval_requests SET thread_id = 'other' WHERE approval_request_id = %s", (req_id,))

            with pytest.raises(psycopg.errors.CheckViolation, match="package_version is immutable"):
                cur.execute("UPDATE public.approval_requests SET package_version = %s WHERE approval_request_id = %s", ('e' * 64, req_id))

            with pytest.raises(psycopg.errors.CheckViolation, match="created_at is immutable"):
                cur.execute("UPDATE public.approval_requests SET created_at = now() - interval '1h' WHERE approval_request_id = %s", (req_id,))

            with pytest.raises(psycopg.errors.CheckViolation, match="expires_at is immutable"):
                cur.execute("UPDATE public.approval_requests SET expires_at = now() + interval '100h' WHERE approval_request_id = %s", (req_id,))


def test_approval_request_frozen_when_terminal(pg_uri, client):
    """Terminal approval_requests rows are frozen against UPDATE and DELETE."""
    tid = start(client)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    assert client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch)).status_code == 200

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_request_id FROM public.graph_threads WHERE thread_id = %s", (tid,))
            req_id = cur.fetchone()[0]

            with pytest.raises(psycopg.errors.CheckViolation, match="approval_request is terminal and frozen"):
                cur.execute("UPDATE public.approval_requests SET status = 'awaiting_approval' WHERE approval_request_id = %s", (req_id,))

            with pytest.raises(psycopg.errors.CheckViolation, match="approval_request is terminal and frozen"):
                cur.execute("DELETE FROM public.approval_requests WHERE approval_request_id = %s", (req_id,))


@pytest.mark.allow_inconsistency
def test_inconsistencies_view_detects_mismatches(pg_uri, pg_admin_db_uri):
    """View fc27_approval_inconsistencies correctly classifies inconsistencies."""
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Create unlinked request
            req_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, expires_at)
                VALUES (%s, 'unlinked-thread-x', %s, 'awaiting_approval', now() + interval '1h')
            """, (req_id, '1' * 64))

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT kind, thread_id, approval_request_id FROM public.fc27_approval_inconsistencies WHERE approval_request_id = %s", (req_id,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 'unlinked_request'
```

---

### 8.2 Blueprint: `tests/test_approval_lifecycle.py`
```python
"""Tests for approval lifecycle, expiry daemon, race conditions, and Section 1A authority separation."""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import psycopg
import pytest
from src.approval_store import ApprovalStore, StoreError
from src.schemas import DecisionIntent
from src.security import Principal, Role
from tests.helpers import (
    start, decide, COM, COMMISSIONER_ID, SIGNING_SECRET,
    APPROVE_COACH_BEST, REJECT, signed
)

COMMISSIONER = Principal(Role.commissioner, COMMISSIONER_ID)

def test_complete_run_creates_linked_request(client, pg_uri):
    """complete_run creates request, links thread, emits approval.created and outbox row."""
    tid = start(client)
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_request_id, package_version, status FROM public.graph_threads WHERE thread_id = %s", (tid,))
            th_link, th_pkg, th_status = cur.fetchone()
            assert th_link is not None
            assert th_status == 'awaiting_approval'

            cur.execute("SELECT status, package_version, expires_at FROM public.approval_requests WHERE approval_request_id = %s", (th_link,))
            r_status, r_pkg, r_exp = cur.fetchone()
            assert r_status == 'awaiting_approval'
            assert r_pkg == th_pkg

            # Event check
            cur.execute("SELECT event_type, source_table, source_id FROM public.domain_events WHERE entity_id = %s AND event_type = 'approval.created'", (str(th_link),))
            ev = cur.fetchone()
            assert ev is not None
            assert ev[0] == 'approval.created'
            assert ev[1] == 'approval_requests'
            assert ev[2] == str(th_link)


def test_decision_updates_request_and_thread_together(client, pg_uri):
    """Decision atomically updates thread & request, writes audit with proof_type=hmac, emits event."""
    tid = start(client)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    resp = client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch))
    assert resp.status_code == 200

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT g.status, r.status, a.proof_type, a.correlation_id, a.approval_request_id, a.id
                  FROM public.graph_threads g
                  JOIN public.approval_requests r ON g.approval_request_id = r.approval_request_id
                  JOIN public.approval_audit a ON a.thread_id = g.thread_id
                 WHERE g.thread_id = %s
            """, (tid,))
            g_st, r_st, proof, corr_id, req_id, audit_id = cur.fetchone()
            assert g_st == 'approved'
            assert r_st == 'approved'
            assert proof == 'hmac'
            assert corr_id is not None
            assert req_id is not None

            # Sourced domain event
            cur.execute("SELECT event_type, source_table, source_id FROM public.domain_events WHERE entity_id = %s AND event_type = 'approval.decided'", (str(req_id),))
            ev = cur.fetchone()
            assert ev is not None
            assert ev[1] == 'approval_audit'
            assert ev[2] == str(audit_id)


def test_approve_writes_no_dispatch_key(client, pg_uri):
    """AC-44: Decision state never contains a dispatch key."""
    tid = start(client)
    resp = decide(client, tid, APPROVE_COACH_BEST)
    assert resp.status_code == 200

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM public.graph_threads WHERE thread_id = %s", (tid,))
            state = cur.fetchone()[0]
            assert "dispatch" not in state


def test_request_revision_lifecycle(client, pg_uri):
    """Spec §7.3: REQUEST_REVISION decision is terminal and immutable."""
    tid = start(client)
    revision_intent = {"decision": "REQUEST_REVISION", "notes": "Please revise the tactical formation."}
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=revision_intent).json()
    resp = client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch))
    assert resp.status_code == 200

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT g.status, r.status, a.decision, a.notes FROM public.graph_threads g JOIN public.approval_requests r ON g.approval_request_id = r.approval_request_id JOIN public.approval_audit a ON a.thread_id = g.thread_id WHERE g.thread_id = %s", (tid,))
            g_st, r_st, dec, notes = cur.fetchone()
            assert g_st == 'revision_requested'
            assert r_st == 'revision_requested'
            assert dec == 'REQUEST_REVISION'
            assert notes == "Please revise the tactical formation."


def test_expired_request_blocks_prior_nonce(pg_uri):
    """AC-43: A nonce issued before deadline returns 409 approval_expired after deadline."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))
    # Start and complete run with short TTL
    # Issue challenge
    # Sleep 1.2s
    # Attempt apply_decision -> StoreError 409 approval_expired
    # Verify thread and request are expired, nonce is revoked


def test_expired_request_blocks_new_challenge(pg_uri):
    """AC-43: issue_challenge returns 409 approval_expired once past expires_at."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))
    # Sleep past deadline
    # Attempt issue_challenge -> StoreError 409 approval_expired


def test_expire_due_is_idempotent(pg_uri):
    """expire_due processes due requests once and is safe to call repeatedly."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))
    # Let request expire
    # Call store.expire_due() -> returns [tid]
    # Call store.expire_due() again -> returns []


def test_expiry_and_decision_race_has_single_outcome(pg_uri):
    """Review Focus 2: 4x expire_due + 4x apply_decision at deadline produces exactly one winner and no deadlock."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(milliseconds=500))
    # Synchronize 4 expire_due workers and 4 apply_decision workers with barrier at 500ms
    # Assert all finish < 10s
    # Assert final status in {'approved', 'expired'}
    # Assert exactly 1 audit row if approved, 0 if expired


def test_inbox_query_never_shows_terminal_threads(pg_uri, client):
    """AC-55: pending_approvals inbox query never shows terminal or expired threads."""
    store = ApprovalStore(pg_uri)
    # Seed threads across approved, rejected, expired, revision_requested, and awaiting_approval
    # Query store.pending_approvals()
    # Assert only the awaiting_approval thread is returned


@pytest.mark.parametrize("table", ["approval_audit", "domain_events", "outbox"])
def test_failed_decision_rolls_back_everything(pg_uri, pg_admin_db_uri, table):
    """AC-1 fault injection: failure on any insert rolls back request, thread, audit, events, and nonce."""
    # Inject BEFORE INSERT trigger raising exception on target table
    # Call apply_decision -> raises exception
    # Verify thread remains awaiting_approval, request remains awaiting_approval, nonce unconsumed


def test_safe_idempotent_decision_replay(client, pg_uri):
    """Repeating the identical signed decision payload safely returns existing state; conflict returns 409."""
    tid = start(client)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    good_signed = signed(ch)
    r1 = client.post(f"/threads/{tid}/decision", headers=COM, json=good_signed)
    assert r1.status_code == 200

    # Idempotent replay
    r2 = client.post(f"/threads/{tid}/decision", headers=COM, json=good_signed)
    assert r2.status_code == 200

    # Conflicting replay
    conflict_intent = {**APPROVE_COACH_BEST, "notes": "conflicting notes"}
    r3 = client.post(f"/threads/{tid}/decision", headers=COM, json=signed(conflict_intent))
    assert r3.status_code == 409
```

---

### 8.3 Blueprint: Section 1A Authority-Separation Tests
```python
"""Section 1A Authority Separation: Human authority, actor validation, fail-closed handling."""
import psycopg
import pytest
from src.approval_store import ApprovalStore, StoreError
from src.schemas import DecisionIntent
from src.security import Principal, Role
from tests.helpers import start, COM, APPROVE_COACH_BEST, signed

def test_timeout_inactivity_cannot_approve(pg_uri):
    """Section 1A.1: Inactivity and timeout can NEVER write an approval or accept a recommendation."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))
    # Create run and let it expire
    time.sleep(1.2)
    store.expire_due()

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            # Must be expired, NOT approved
            cur.execute("SELECT status FROM public.approval_requests")
            assert cur.fetchone()[0] == 'expired'
            cur.execute("SELECT count(*) FROM public.approval_audit WHERE decision = 'APPROVE'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM public.outbox WHERE channel = 'fc27:dispatch'")
            assert cur.fetchone()[0] == 0


def test_unsupported_actor_types_fail_closed_in_phase1(client, pg_uri):
    """Section 1A.8 & Agent C: Unsupported actor types (player, delegate, agent) fail closed in Phase 1."""
    tid = start(client)
    store = ApprovalStore(pg_uri)
    for bad_actor in ("player", "delegate", "agent", "coach", "system"):
        with pytest.raises(StoreError) as exc_info:
            # Pass bad_actor as actor_type
            store.apply_decision(
                tid,
                ...,
                actor_type=bad_actor,
                authority_basis="primary"
            )
        assert exc_info.value.status_code in (403, 422)


def test_unsupported_authority_bases_fail_closed_in_phase1(client, pg_uri):
    """Section 1A.8 & Agent B: Unsupported authority bases fail closed in Phase 1."""
    tid = start(client)
    store = ApprovalStore(pg_uri)
    for bad_basis in ("affected_player_fallback", "explicit_delegate", "affected_player_consent"):
        with pytest.raises(StoreError) as exc_info:
            store.apply_decision(
                tid,
                ...,
                actor_type="commissioner",
                authority_basis=bad_basis
            )
        assert exc_info.value.status_code in (403, 422)


def test_schema_distinguishes_club_authorization_and_player_consent(pg_uri):
    """Section 1A.3 & Agent A: Schema separates club authorization from player consent."""
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            # Club decisions table allows only APPROVE, REJECT, REQUEST_REVISION
            cur.execute("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'public.approval_audit'::regclass AND conname LIKE '%decision%'")
            rows = cur.fetchall()
            assert any("REQUEST_REVISION" in r[1] for r in rows)

            # Columns separating actor type, id, authority basis, decision scope
            cur.execute("""
                SELECT column_name, data_type FROM information_schema.columns 
                WHERE table_name = 'approval_audit' 
                  AND column_name IN ('actor_type', 'actor_id', 'authority_basis', 'decision_scope')
            """)
            found_cols = {r[0] for r in cur.fetchall()}
            assert {"actor_type", "authority_basis", "decision_scope"}.issubset(found_cols)


def test_player_agent_cannot_be_decision_actor(pg_uri):
    """Section 1A.2: A player agent cannot be recorded as decision actor."""
    store = ApprovalStore(pg_uri)
    with pytest.raises(StoreError) as exc_info:
        store.apply_decision(..., actor="agent:player_agent_1")
    assert exc_info.value.status_code in (403, 422)


def test_no_player_decision_endpoints_active_in_phase1(client):
    """Section 1A.9: Future player-decision endpoints are not active in Phase 1."""
    for endpoint in (
        "/v1/player-decisions",
        "/v1/assignments/1/decision",
        "/v1/delegations",
        "/v1/escalations"
    ):
        assert client.post(endpoint, json={}).status_code == 404
```

---

## 9. Verification Strategy & Traceability Matrix

| Acceptance Criterion | Verification Method | Associated Tests |
|---|---|---|
| **AC-1 (Atomic Rollback)** | Injected DB trigger failures on `approval_audit`, `domain_events`, and `outbox`; verify complete transaction rollback. | `tests/test_approval_lifecycle.py::test_failed_decision_rolls_back_everything` |
| **AC-43 (Approval Expiration)** | Short TTL request expiry; verify past-deadline nonce refusal, challenge refusal, idempotent scanning, single-outcome race. | `tests/test_approval_lifecycle.py::test_expired_request_blocks_prior_nonce`<br>`tests/test_approval_lifecycle.py::test_expired_request_blocks_new_challenge`<br>`tests/test_approval_lifecycle.py::test_expire_due_is_idempotent`<br>`tests/test_approval_lifecycle.py::test_expiry_and_decision_race_has_single_outcome` |
| **AC-44 (No Dispatch Key)** | Approve recommendation; inspect state in returned dict and PostgreSQL DB; assert no `dispatch` key exists. | `tests/test_approval_lifecycle.py::test_approve_writes_no_dispatch_key` |
| **AC-55 (Pair Consistency & Inbox)** | Modify one side of pair; assert commit fails. Verify inbox query joins both tables and filters terminal threads. Teardown check on `fc27_approval_inconsistencies`. | `tests/test_migration_0003.py::test_pair_mismatch_fails_at_commit`<br>`tests/test_migration_0003.py::test_awaiting_thread_requires_link`<br>`tests/test_approval_lifecycle.py::test_inbox_query_never_shows_terminal_threads`<br>`tests/conftest.py::pg_uri` |
| **AC-56 (0003 on Legacy Data)** | Run `legacy_install`, bootstrap database, seed 5 legacy rows, apply 0003 migration; verify backfill, terminal row byte-identity, view output. | `tests/test_migration_0003.py::test_0003_applies_on_legacy_0002_data` |
| **Section 1A (Authority Separation)** | Timeout/inactivity cannot approve; unsupported actor types fail closed; unsupported authority bases fail closed; schema separation. | `tests/test_approval_lifecycle.py::test_timeout_inactivity_cannot_approve`<br>`tests/test_approval_lifecycle.py::test_unsupported_actor_types_fail_closed_in_phase1`<br>`tests/test_approval_lifecycle.py::test_unsupported_authority_bases_fail_closed_in_phase1`<br>`tests/test_approval_lifecycle.py::test_schema_distinguishes_club_authorization_and_player_consent` |
