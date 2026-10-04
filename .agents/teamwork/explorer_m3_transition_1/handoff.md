# Handoff Report: Milestone 3 Single-Write-Path Transition Mechanism

- **Agent**: `explorer_m3_transition_1` (M3 Transition Function & Service Explorer)
- **Recipient**: `orchestrator_phase1` (Conversation ID: `7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)
- **Milestone**: Milestone 3 — Dispatch Schema & Single-Write-Path Transition
- **Date**: 2026-10-03
- **Type**: Hard Handoff (Investigation & Detailed Design Complete)

---

## 1. Observation

1. **Role Infrastructure**:
   - `src/db_roles.py:13-16` defines the four authoritative database roles:
     ```python
     ROLE_OWNER = "fc27_owner"
     ROLE_MIGRATOR = "fc27_migrator"
     ROLE_APP = "fc27_app"
     ROLE_DISPATCH = "fc27_dispatch"
     ```
   - `src/db_roles.py:48-50` enforces `fc27_owner` as `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`, while `fc27_app`, `fc27_dispatch`, and `fc27_migrator` are `LOGIN NOINHERIT`.
   - The dispatch worker login role is `fc27_dispatch` (not `fc27_worker`).

2. **Transition-Only Event Security Guard (Rule C-10)**:
   - `migrations/0003_dispatch_foundation.sql:69-73` enforces that transition-only events (`dispatch.%` except `dispatch.created`, `plan.%`, `tracking.%`) cannot be inserted directly by runtime roles:
     ```sql
     IF (NEW.event_type LIKE 'dispatch.%' AND NEW.event_type != 'dispatch.created') OR NEW.event_type LIKE 'plan.%' OR NEW.event_type LIKE 'tracking.%' THEN
         IF current_user != 'fc27_owner' OR session_user NOT IN ('fc27_app', 'fc27_dispatch') THEN
             RAISE EXCEPTION 'transition-only event type not allowed outside transition function';
         END IF;
     END IF;
     ```
   - When a function is `SECURITY DEFINER` and owned by `fc27_owner`, `current_user` evaluates to `'fc27_owner'` while `session_user` reflects the invoking client connection (`'fc27_app'` or `'fc27_dispatch'`). Direct inserts by `fc27_app` or `fc27_dispatch` have `current_user != 'fc27_owner'` and are rejected.

3. **Current Migration State**:
   - `migrations/0003_dispatch_foundation.sql` currently ends at line 810 with Section B (approval requests lifecycle, paired consistency checks, Section 1A compatibility).
   - Milestone 3 requires appending Section C (players, dispatches, tracking schema) and Section D (`dispatch_transition_rules`, `public.fc27_dispatch_transition`).
   - `migrations/post/grants.sql` is at grants policy version 3 (`GRANTS_POLICY.version = 3` in `src/db_migrate.py:21`). Adding M3 grants requires bumping the grants policy version.

4. **Phase 1 Invariants**:
   - `ORIGINAL_REQUEST.md:39-44` dictates:
     ```markdown
     1. DISPATCH_ENABLED=false at all times.
     2. No Phase 1 path may deliver anything to a player.
     3. No approval endpoint may call a player delivery adapter.
     4. No Graph node may transition to dispatched, delivered, acknowledged, activated, or tracking.
     ```

5. **Python Service Conventions**:
   - `src/events.py:5-57` provides `record_event()` inserting into `public.domain_events` and `public.outbox`.
   - `src/dispatch/` directory does not yet exist and is designated for `transition.py`, `objective.py`, `creation.py`, and `gates.py`.

---

## 2. Logic Chain

1. **State Mutation Confinement**:
   - Direct `UPDATE` or `DELETE` on `public.dispatches` by runtime roles (`fc27_app`, `fc27_dispatch`) would undermine audit integrity and allow bypass of business invariants.
   - Therefore, runtime roles must be granted only `EXECUTE` on `public.fc27_dispatch_transition` and `SELECT` on `public.dispatches` (with `fc27_app` holding `INSERT` for initial `pending` creation in the approval transaction).
   - By creating `public.fc27_dispatch_transition` as `SECURITY DEFINER` with `SET search_path = pg_catalog, pg_temp` owned by `fc27_owner`, PostgreSQL executes the update under the owner's privileges while retaining the invoker's identity in `session_user`.

2. **Authoritative Rule Matrix Enforcement**:
   - Rather than hardcoding transition logic into procedural `IF/ELSE` blocks, `public.dispatch_transition_rules` externalizes valid directed edges `(from_status, to_status)` along with `allowed_roles`, `allowed_actor_kinds`, and `require_reason`.
   - The function acquires an exclusive row lock (`SELECT * FROM public.dispatches WHERE dispatch_id = p_dispatch_id FOR UPDATE`), preventing concurrent modification races.
   - It validates `session_user` against `allowed_roles` and `p_actor_kind` against `allowed_actor_kinds`. Mismatches raise specific SQLSTATE errors (`FC002` for unauthorized role/illegal edge, `FC004` for unauthorized actor).

3. **Phase 1 Invariant Guarantee**:
   - Even though the transition rule matrix defines `running -> delivered` for forward compatibility, Phase 1 mandates `DISPATCH_ENABLED=false`.
   - To make delivery failure impossible to bypass, `public.fc27_dispatch_transition` includes a hard check:
     ```sql
     IF p_to_status = 'delivered' THEN
         RAISE EXCEPTION 'Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)'
             USING ERRCODE = 'FC002';
     END IF;
     ```
   - This guarantees that any call attempting delivery fails closed at the database boundary with rollback.

4. **Atomic Audit and Event Outbox Compliance**:
   - In accordance with trigger `fc27_check_event_source` (Observation 2), state updates must emit an audit event within the same transaction.
   - Inserting `event_type = 'dispatch.transitioned'`, `entity_type = 'dispatch'`, `source_table = 'dispatches'`, `source_id = p_dispatch_id::text` into `public.domain_events` satisfies all foreign keys and triggers because `current_user = 'fc27_owner'` and `session_user IN ('fc27_app', 'fc27_dispatch')`.
   - Inserting the corresponding row into `public.outbox` guarantees reliable downstream delivery notification.

5. **Python Service Translation**:
   - The Python service `src/dispatch/transition.py` wraps the SQL call in an active transaction context.
   - It parses database errors (`P0002`, `FC001`, `FC002`, `FC004`, `FC007`) into a typed exception hierarchy (`DispatchNotFoundError`, `Phase1DeliveryBlockedError`, `UnauthorizedRoleError`, `UnauthorizedActorError`, `TransitionConflictError`, `MissingReasonError`, `InvalidTransitionError`).
   - This provides a clean interface for `graph_api`, CLI, and future dispatch workers without leaking raw SQL exceptions to HTTP callers.

---

## 3. Caveats

1. **State Domain Reconciliation**:
   - The orchestrator brief defines the 6-state machine (`pending`, `running`, `paused`, `delivered`, `failed`, `cancelled`).
   - Spec v2.1 defines an extended 11-state machine (`pending`, `queued`, `delivered`, `requires_reapproval`, `failed`, `acknowledged`, `tracking`, `completed`, `expired`, `cancelled`, `superseded`).
   - *Resolution*: The design strictly implements the 6-state machine required for M3, while using a flexible table structure (`dispatch_transition_rules`) that permits future additive seeding of extended states without DDL changes.
2. **Actor Setting in Phase 1 vs Phase 2**:
   - In Phase 1, `actor` and `actor_kind` are passed as function arguments. In Phase 2, session-authenticated users and passkeys will validate against `public.users`. The current signature cleanly supports both argument-based validation and session context.
3. **Outbox Relay**:
   - Outbox rows are inserted with `published_at = NULL`. As per Phase 1 invariants, no background relay worker publishes them to Redis during Phase 1.

---

## 4. Conclusion

The technical exploration and design for Milestone 3 single-write-path transition mechanism is complete and ready for implementation.
1. `public.dispatch_transition_rules` table and its 7-edge seeded matrix enforce role and actor authorization.
2. `public.fc27_dispatch_transition` enforces `SECURITY DEFINER` privilege isolation, row locking (`FOR UPDATE`), Phase 1 delivery blocking (`DISPATCH_ENABLED=false`), and atomic outbox emission (`dispatch.transitioned`).
3. `src/dispatch/transition.py` provides an idiomatic Python wrapper with clean exception mapping.
4. All SQL statements, table definitions, seed data, and Python code are detailed in `transition_analysis.md`.

---

## 5. Verification Method

Once implemented by the worker agent, the design can be independently verified via the following steps:

1. **Apply Migrations and Grants**:
   ```bash
   python -m src.db_migrate
   ```
   *Expected*: Applies `0003_dispatch_foundation.sql` (Sections C and D) and bumps `grants.sql` policy version without postcondition violations.

2. **Run Dispatch Schema and Transition Tests**:
   ```bash
   python -m pytest -o addopts="" -q tests/test_dispatch_transition.py tests/test_dispatch_schema.py
   ```
   *Expected*: 100% pass across:
   - Valid state transitions (`pending -> running`, `running -> paused`, etc.).
   - Rejection of invalid transitions (e.g. `pending -> paused`, `failed -> running`).
   - Rejection of unauthorized roles (e.g. calling with disallowed session user).
   - Rejection of unauthorized actor kinds (e.g. `worker` attempting `cancelled`).
   - Rejection of transitions without mandatory reason (`require_reason = true`).
   - Strict block on `delivered` transition raising `Phase1DeliveryBlockedError`.
   - Verification of `domain_events` and `outbox` record insertion.
   - Verification that `fc27_app` cannot directly `UPDATE dispatches`.

3. **Full Suite Regression Check**:
   ```bash
   python -m pytest -o addopts="" -q
   ```
   *Expected*: Entire test suite passes without regressions.
