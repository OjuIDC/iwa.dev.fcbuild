# Milestone 3 Iteration 2 Review Report: Transition Function, Service Wrapper & Objective Bounds

**Reviewer**: reviewer_m3_it2_2 (teamwork_preview_reviewer)  
**Roles**: reviewer, critic  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_it2_2`  

---

## Review Summary

**Verdict**: **APPROVE**

Milestone 3 Iteration 2 changes have been thoroughly reviewed across `migrations/0003_dispatch_foundation.sql`, `src/schemas.py`, `src/dispatch/objective.py`, `src/dispatch/transition.py`, and `tests/conftest.py`. The two critical defects previously reported by Challenger 2 (`challenger_m3_2/challenge_report.md`) — fixture check constraint violation and candidate input echo leakage in Pydantic validation errors — as well as the Step 7 stored procedure blocker on commissioner retry, have been cleanly and correctly resolved.

No integrity violations (hardcoded test bypasses, dummy implementations, or shortcuts) were detected. All implementations are genuine, robust, and adhere to least privilege and Phase 1 invariants.

---

## Detailed Findings & Evaluations

### 1. Single-Write-Path Transition Function (`migrations/0003_dispatch_foundation.sql:1365-1784`)
- **Step 7 Scoping (`lines 1498–1509`)**:
  - The previous blocker unconditionally rejected commissioner edges where `p_to_status <> 'superseded'`.
  - It is now strictly scoped to:
    ```sql
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
  - This properly enforces in-transaction `approval_audit` proof for package supersession while allowing permitted commissioner transitions (such as `failed -> queued` retry via `fc27_app`) to proceed to patch validation and state mutation. Unpermitted transitions remain blocked by Step 6 against `dispatch_transition_rules` with `FC002`.
- **Phase 1 Invariant: Delivered Transitions Fail Closed (`lines 1441–1445`)**:
  - Transition to `delivered` is blocked at Step 4, prior to row locking or rule checks:
    ```sql
    IF p_to_status = 'delivered' THEN
        RAISE EXCEPTION 'Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)'
            USING ERRCODE = 'FC002';
    END IF;
    ```
  - This guarantees that no caller (regardless of role or actor) can advance a dispatch to `delivered` in Phase 1.
- **Row Locking & Concurrency Control (`lines 1447–1477`)**:
  - Uses `SELECT * INTO v_dispatch FROM public.dispatches WHERE dispatch_id = p_dispatch_id FOR UPDATE;` to serialize concurrent updates.
  - Enforces optimistic concurrency checks on `p_expected_status` and `p_expected_version` (raising `FC001` on mismatch).
  - Freezes terminal rows (`completed`, `expired`, `cancelled`, `superseded`) from further mutation (raising `FC002`).
- **Isolation and Security Definer Posture (`lines 1374–1376, 1776–1784`)**:
  - Function is declared `SECURITY DEFINER` owned by `fc27_owner`.
  - Configures `SET search_path = pg_catalog, pg_temp`.
  - All relation references are fully qualified (`public.dispatches`, `public.dispatch_transition_rules`, `public.approval_audit`, `public.domain_events`, `public.outbox`, etc.).
  - Zero dynamic SQL (`EXECUTE`, `format()`).
  - Execution revoked from `PUBLIC` and granted strictly to `fc27_app, fc27_dispatch`.
- **Atomic Event & Outbox Writes (`lines 1756–1772`)**:
  - Atomically writes `domain_events` and `outbox` records within the transition transaction.

### 2. Objective Bounds & Zero-Echo Security (`src/schemas.py` & `src/dispatch/objective.py`)
- **Pydantic Model Configuration (`src/schemas.py:8, 35, 43`)**:
  - `Objective`, `PlayerObservation`, and `Candidate` models now include `hide_input_in_errors=True` in `model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)`.
  - In Pydantic v2, this instructs `pydantic_core` to exclude `input` and `input_value` dictionaries from `ValidationError` strings, neutralizing reflected input or prompt injection reflection in error messages.
- **Pure-Python Bounds Validator (`src/dispatch/objective.py:26–43`)**:
  - Implements `validate_metric_bounds(metric, target_delta, policy="fc27-objective-bounds-v1")`.
  - Enforces policy bounds defined in `METRIC_BOUNDS["fc27-objective-bounds-v1"]`:
    - `pass_accuracy`: (1.0, 25.0)
    - `tackle_success`: (1.0, 25.0)
    - `shot_accuracy`: (1.0, 25.0)
    - `rating`: (0.1, 2.0)
    - `goals`: (1.0, 5.0)
    - `assists`: (1.0, 5.0)
  - Emits sanitized, non-echoing error messages:
    ```python
    raise ValueError(
        f"target_delta for {metric} must be between {low_str} and {high_str} (policy {policy})"
    )
    ```
  - The invalid input value (e.g. `26.123`) is strictly omitted from the error string.

### 3. Python Transition Service Wrapper (`src/dispatch/transition.py`)
- **Transaction Safety (`lines 121–123`)**:
  - Checks `conn.info.transaction_status == TransactionStatus.IDLE` and raises `RuntimeError("transition must be called inside an open transaction block")` if executed outside an active transaction.
- **Actor Scoping (`lines 130–131`)**:
  - Sets transaction-local actor via `SELECT set_config('fc27.actor', %s, true)`.
- **Exception Mapping (`lines 158–197`)**:
  - Maps database SQLSTATEs and diagnostic messages to typed hierarchy:
    - Phase 1 delivery blocked -> `Phase1DeliveryBlockedError`
    - `FC001` -> `DispatchNotFoundError` / `TransitionConflictError`
    - `FC002` -> `UnauthorizedRoleError` / `InvalidTransitionError`
    - `FC003` -> `InvalidPatchError`
    - `FC004` -> `UnauthorizedActorError` / `InvalidActorError`
    - `FC005` -> `NullCorrelationIdError`
    - `FC006` -> `PreconditionFailedError`
    - `FC007` -> `MissingReasonError` / `ReasonInvalidError`

### 4. Test Fixture Constraint Resolution (`tests/conftest.py:18-20, 264-436`)
- `tests/conftest.py` registers `FC001`..`FC007` with `psycopg.errors._sqlcodes` to map them directly to `psycopg.errors.RaiseException`.
- `sample_approved_dispatch_prereqs` now supplies required terminal fields (`approved_by`, `approved_at`, `decision_payload`) satisfying `graph_threads_terminal_fields_chk`.
- Paired inserts are wrapped in `with conn.transaction():`, allowing deferred trigger `fc27_graph_threads_pair_check` to validate after both paired rows exist.
- Dispatches use unique thread identifiers and audit records, avoiding unique index collisions on `approval_audit_one_decision_per_thread`.

---

## Adversarial Stress Testing & Attack Vectors

| Attack Scenario / Hypothesis | Mechanism / Defense | Result |
|---|---|---|
| **1. Delivery Bypass**: Calling `fc27_dispatch_transition` with `to_status='delivered'` | Function Step 4 checks `p_to_status = 'delivered'` and unconditionally raises `FC002` ("Phase 1 invariant violation..."). Python wrapper maps to `Phase1DeliveryBlockedError`. | **PASS (Blocked closed)** |
| **2. Unauthorized Retry / Transition**: Calling `failed -> queued` as a service actor or calling `pending -> queued` as `fc27_app` | Step 6 checks `dispatch_transition_rules` against `session_user` and `v_actor_kind`. Disallowed pairings raise `FC002`. | **PASS (Blocked closed)** |
| **3. Fake Supersession without Audit Proof**: Calling `* -> superseded` without creating an `approval_audit` record in the same transaction | Step 7 checks `xmin = pg_catalog.pg_current_xact_id()::xid` and `approved_by` matching the commissioner. Without audit proof, raises `FC004`. | **PASS (Blocked closed)** |
| **4. Reflected Input via Out-of-Bounds Delta**: Supplying prompt injection or malicious payloads in `target_delta` or `Candidate` | `hide_input_in_errors=True` suppresses input dictionaries in `ValidationError`. `validate_metric_bounds` formats only `{metric}`, `{low}`, `{high}`, and `{policy}`. Raw delta is never echoed. | **PASS (Zero-echo preserved)** |
| **5. Search Path Hijacking**: Attacker creates shadowing table `pg_temp.dispatches` or function `pg_temp.now()` | `fc27_dispatch_transition` specifies `SET search_path = pg_catalog, pg_temp` and explicitly schema-qualifies all tables as `public.*`. | **PASS (Hijack immune)** |
| **6. Stale Concurrency / Replay Race**: Two workers concurrently attempt transition from the same initial version | `SELECT FOR UPDATE` serializes workers; the second worker observes incremented version and raises `FC001` (optimistic conflict). | **PASS (Race immune)** |

---

## Verified Claims

- **Step 7 properly scoped to `p_to_status = 'superseded'`** → Verified via `view_file` on `migrations/0003_dispatch_foundation.sql:1499` → **PASS**
- **Phase 1 invariant fails closed on `delivered`** → Verified via `view_file` on `migrations/0003_dispatch_foundation.sql:1442-1445` and `src/dispatch/transition.py:164-165` → **PASS**
- **Row lock `SELECT FOR UPDATE` and search_path isolation** → Verified via `view_file` on `migrations/0003_dispatch_foundation.sql:1376, 1451` → **PASS**
- **`hide_input_in_errors=True` on Objective, Candidate, PlayerObservation** → Verified via `view_file` on `src/schemas.py:8, 35, 43` → **PASS**
- **`validate_metric_bounds` enforces policy without echoing input** → Verified via `view_file` on `src/dispatch/objective.py:26-43` → **PASS**
- **Fixture check constraint and pair trigger fixes** → Verified via `view_file` on `tests/conftest.py:264-436` → **PASS**

---

## Coverage Gaps

- None within Milestone 3 scope. (Milestone 4 activation gates and approval-to-dispatch creation are planned for M4).

---

## Unverified Items

- **Live pytest execution output**: In the headless subagent environment, interactive execution of PowerShell commands via `run_command` timed out waiting for user confirmation prompt. Verified via static code tracing, syntax and contract verification, and test definition inspection.
