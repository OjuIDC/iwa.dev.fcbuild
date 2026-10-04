# DISPATCH: explorer_m3_transition_1

- **Assigned Role**: M3 Transition Function & Service Explorer
- **Archetype**: teamwork_preview_explorer
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_transition_1
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `migrations/0003_dispatch_foundation.sql`
4. `src/events.py`
5. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 7)

## Exploration Objectives
Explore and provide exact design for the single-write-path transition mechanism in Milestone 3:
1. `public.dispatch_transition_rules` table:
   - Primary key or unique on `(from_status, to_status)`.
   - Columns: `from_status`, `to_status`, `allowed_roles` (text[]), `allowed_actor_kinds` (text[]), `require_reason` (boolean).
   - Initial seeded matrix of valid transitions:
     - `pending -> running` (allowed: `fc27_app`, `fc27_worker`; actor_kinds: `worker`, `system`)
     - `running -> paused` (allowed: `fc27_app`, `fc27_worker`; actor_kinds: `commissioner`, `worker`, `system`)
     - `paused -> running` (allowed: `fc27_app`, `fc27_worker`; actor_kinds: `commissioner`, `worker`, `system`)
     - `running -> failed` (allowed: `fc27_app`, `fc27_worker`; actor_kinds: `worker`, `system`)
     - `pending -> cancelled` (allowed: `fc27_app`; actor_kinds: `commissioner`, `system`)
     - `paused -> cancelled` (allowed: `fc27_app`; actor_kinds: `commissioner`, `system`)
     - `running -> delivered`: In Phase 1, `DISPATCH_ENABLED=false` MUST block any transition to `delivered`! Evaluate how this is enforced (e.g., check `DISPATCH_ENABLED` or disallow rule in Phase 1 / raise exception).
2. `public.fc27_dispatch_transition` function:
   - Signature: `(p_dispatch_id uuid, p_to_status text, p_actor text, p_actor_kind text, p_reason text DEFAULT NULL, p_correlation_id uuid DEFAULT NULL) -> jsonb`
   - Properties: `SECURITY DEFINER`, `SET search_path = pg_catalog, pg_temp`.
   - Locks row: `SELECT * FROM public.dispatches WHERE dispatch_id = p_dispatch_id FOR UPDATE`.
   - Checks caller role (`CURRENT_USER` or session roles) against `allowed_roles`.
   - Checks `p_actor_kind` against `allowed_actor_kinds`.
   - If `p_to_status = 'delivered'`, enforce Phase 1 invariant: `DISPATCH_ENABLED=false` blocks delivery.
   - Updates `public.dispatches` status, updated_at, version = version + 1.
   - Inserts audit / outbox event `dispatch.transitioned` atomically.
   - Returns updated record state.
3. Python wrapper service (`src/dispatch/transition.py` or `src/dispatch_store.py`):
   - Method `transition_dispatch(conn, dispatch_id, to_status, actor, actor_kind, reason=None, correlation_id=None)`.
   - Clean exception mapping for invalid transition, unauthorized role, unauthorized actor, or not found.

## Output Requirements
1. Write `transition_analysis.md` and `handoff.md` in your working directory.
2. Outline exact SQL function definition, seed rules, and Python service class/functions.
3. Notify orchestrator via `send_message`.
Do NOT modify production source code in `src/` or `migrations/`.
