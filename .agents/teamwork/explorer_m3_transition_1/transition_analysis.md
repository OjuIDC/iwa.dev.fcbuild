# Milestone 3 Single-Write-Path Transition Mechanism: Technical Analysis & Design

- **Agent**: `explorer_m3_transition_1` (M3 Transition Function & Service Explorer)
- **Milestone**: Milestone 3 — Dispatch Schema & Single-Write-Path Transition
- **Date**: 2026-10-03
- **Status**: Complete Technical Design

---

## 1. Executive Summary & Problem Boundary

In the FC27 Player-Dispatch Programme, state transitions on the `public.dispatches` table represent the core execution lifecycle of player assignments. To preserve strict system integrity, prevent race conditions, enforce least privilege, and uphold the programme's non-negotiable security invariants, **all dispatch state modifications must pass through a single, hardened write path**: the `public.fc27_dispatch_transition` PostgreSQL function.

Runtime application roles (`fc27_app` for graph-api, `fc27_dispatch` for background workers) hold **no direct `UPDATE` or `DELETE` privileges** on `public.dispatches`. Every lifecycle mutation must execute through this `SECURITY DEFINER` function, which validates session identity, checks caller role and actor permissions against an authoritative rule matrix (`public.dispatch_transition_rules`), enforces optimistic concurrency or row locks, blocks illegal transitions (such as player delivery in Phase 1), mutates the row version, and atomically emits an authoritative domain event and outbox message in the same database transaction.

This analysis provides the complete technical design for:
1. `public.dispatch_transition_rules`: Table schema, constraints, and initial seeded rule matrix.
2. `public.fc27_dispatch_transition`: Hardened `SECURITY DEFINER` function with row locking, invariant checks, and atomic outbox emission.
3. Python Wrapper Service (`src/dispatch/transition.py`): Clean, idiomatic interface with granular exception mapping.

---

## 2. Architecture Analysis & Model Synthesis

### 2.1 Role Identity Verification
The repository's authoritative role architecture (`src/db_roles.py`, `src/db_migrate.py`) defines the following PostgreSQL roles:
- `fc27_owner`: `NOLOGIN`, owns all schema tables, views, sequences, and functions.
- `fc27_migrator`: `LOGIN`, member of `fc27_owner`, used only during migration runs.
- `fc27_app`: `LOGIN`, runtime DML role used by `graph-api`. Holds `SELECT, INSERT` on dispatches (pending only), `EXECUTE` on transition function.
- `fc27_dispatch`: `LOGIN`, runtime role used by dispatch workers. Holds `SELECT` on dispatches, `EXECUTE` on transition function. **No direct `INSERT`, `UPDATE`, or `DELETE` on dispatches**.

*(Note on naming: The orchestrator dispatch prompt colloquially referenced `fc27_worker`; the real PostgreSQL role is `fc27_dispatch`. Our design explicitly supports both names in the rule matrix for complete forward and backward compatibility).*

### 2.2 Lifecycle State Model Reconciliation
Two lifecycle models exist across project documents:
- **Model A (Orchestrator M3 Brief)**: Focused 6-state machine: `pending`, `running`, `paused`, `delivered`, `failed`, `cancelled`.
- **Model B (Superpowers Spec v2.1 §11 / Plan Task 7)**: Extended 11-state machine: `pending`, `queued`, `delivered`, `requires_reapproval`, `failed`, `acknowledged`, `tracking`, `completed`, `expired`, `cancelled`, `superseded`.

**Synthesis Decision**:
Model A is the core target assigned for Milestone 3 implementation by the orchestrator. Our schema and function design fully implements Model A while being architected with flexible rule-matrix lookups that allow seamless forward extension to Model B in future phases without schema breakage.

---

## 3. Transition Rules Table: `public.dispatch_transition_rules`

### 3.1 DDL Schema
The table defines valid directed edges `(from_status -> to_status)` and the constraints required to traverse each edge.

```sql
CREATE TABLE public.dispatch_transition_rules (
    rule_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_status text NOT NULL,
    to_status text NOT NULL,
    allowed_roles text[] NOT NULL,
    allowed_actor_kinds text[] NOT NULL,
    require_reason boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dispatch_transition_rules_edge_key UNIQUE (from_status, to_status),
    CONSTRAINT dispatch_transition_rules_no_self_edge CHECK (from_status <> to_status),
    CONSTRAINT dispatch_transition_rules_roles_nonempty CHECK (array_length(allowed_roles, 1) > 0),
    CONSTRAINT dispatch_transition_rules_actors_nonempty CHECK (array_length(allowed_actor_kinds, 1) > 0)
);

COMMENT ON TABLE public.dispatch_transition_rules IS 
    'Authoritative state machine rule matrix governing permissible dispatch lifecycle transitions.';
```

### 3.2 Seeded Transition Rule Matrix
The table is seeded with the initial valid transitions specified by the programme brief:

| `from_status` | `to_status` | `allowed_roles` | `allowed_actor_kinds` | `require_reason` | Purpose / Lifecycle Meaning |
|---|---|---|---|---|---|
| `pending` | `running` | `{'fc27_app', 'fc27_dispatch', 'fc27_worker'}` | `{'worker', 'system'}` | `false` | Worker claims pending dispatch and begins processing |
| `running` | `paused` | `{'fc27_app', 'fc27_dispatch', 'fc27_worker'}` | `{'commissioner', 'worker', 'system'}` | `true` | Commissioner or system halts active execution; reason mandatory |
| `paused` | `running` | `{'fc27_app', 'fc27_dispatch', 'fc27_worker'}` | `{'commissioner', 'worker', 'system'}` | `false` | Resumption of previously paused execution |
| `running` | `failed` | `{'fc27_app', 'fc27_dispatch', 'fc27_worker'}` | `{'worker', 'system'}` | `true` | Unrecoverable error during execution; failure reason mandatory |
| `pending` | `cancelled` | `{'fc27_app'}` | `{'commissioner', 'system'}` | `true` | Commissioner cancels pending dispatch; reason mandatory |
| `paused` | `cancelled` | `{'fc27_app'}` | `{'commissioner', 'system'}` | `true` | Commissioner cancels paused dispatch; reason mandatory |
| `running` | `delivered` | `{'fc27_app', 'fc27_dispatch', 'fc27_worker'}` | `{'worker', 'system'}` | `false` | Delivery to player. **Blocked closed in Phase 1 by DISPATCH_ENABLED=false invariant** |

### 3.3 Idempotent Seed Script
```sql
INSERT INTO public.dispatch_transition_rules (
    from_status,
    to_status,
    allowed_roles,
    allowed_actor_kinds,
    require_reason
) VALUES
    ('pending', 'running',   ARRAY['fc27_app', 'fc27_dispatch', 'fc27_worker'], ARRAY['worker', 'system'], false),
    ('running', 'paused',    ARRAY['fc27_app', 'fc27_dispatch', 'fc27_worker'], ARRAY['commissioner', 'worker', 'system'], true),
    ('paused',  'running',   ARRAY['fc27_app', 'fc27_dispatch', 'fc27_worker'], ARRAY['commissioner', 'worker', 'system'], false),
    ('running', 'failed',    ARRAY['fc27_app', 'fc27_dispatch', 'fc27_worker'], ARRAY['worker', 'system'], true),
    ('pending', 'cancelled', ARRAY['fc27_app'],                                  ARRAY['commissioner', 'system'], true),
    ('paused',  'cancelled', ARRAY['fc27_app'],                                  ARRAY['commissioner', 'system'], true),
    ('running', 'delivered', ARRAY['fc27_app', 'fc27_dispatch', 'fc27_worker'], ARRAY['worker', 'system'], false)
ON CONFLICT (from_status, to_status) DO UPDATE SET
    allowed_roles = EXCLUDED.allowed_roles,
    allowed_actor_kinds = EXCLUDED.allowed_actor_kinds,
    require_reason = EXCLUDED.require_reason;
```

---

## 4. Single-Write-Path Function: `public.fc27_dispatch_transition`

### 4.1 Security Definer & Isolation Properties
- `LANGUAGE plpgsql`: Standard procedural language.
- `SECURITY DEFINER`: Executes with privileges of `fc27_owner`, allowing controlled row update and event emission without granting runtime roles direct table DML.
- `SET search_path = pg_catalog, pg_temp`: Eliminates search-path hijacking attacks. All schema references are strictly qualified (`public.dispatches`, `public.domain_events`, etc.).
- `session_user` check: Identifies the true client login role (`fc27_app` or `fc27_dispatch`) rather than the definer role (`fc27_owner`).

### 4.2 Exact SQL Definition

```sql
CREATE OR REPLACE FUNCTION public.fc27_dispatch_transition(
    p_dispatch_id uuid,
    p_to_status text,
    p_actor text,
    p_actor_kind text,
    p_reason text DEFAULT NULL,
    p_correlation_id uuid DEFAULT NULL,
    p_expected_status text DEFAULT NULL,
    p_expected_version integer DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_dispatch public.dispatches%ROWTYPE;
    v_rule public.dispatch_transition_rules%ROWTYPE;
    v_caller_role text;
    v_correlation_id uuid;
    v_new_version integer;
    v_event_id bigint;
    v_updated public.dispatches%ROWTYPE;
BEGIN
    -- 1. Parameter Validation
    IF p_dispatch_id IS NULL THEN
        RAISE EXCEPTION 'p_dispatch_id cannot be null'
            USING ERRCODE = 'FC004';
    END IF;

    IF p_to_status IS NULL OR char_length(trim(p_to_status)) = 0 THEN
        RAISE EXCEPTION 'p_to_status cannot be null or empty'
            USING ERRCODE = 'FC002';
    END IF;

    IF p_actor IS NULL OR char_length(trim(p_actor)) = 0 THEN
        RAISE EXCEPTION 'p_actor cannot be null or empty'
            USING ERRCODE = 'FC004';
    END IF;

    IF p_actor_kind IS NULL OR char_length(trim(p_actor_kind)) = 0 THEN
        RAISE EXCEPTION 'p_actor_kind cannot be null or empty'
            USING ERRCODE = 'FC004';
    END IF;

    IF p_reason IS NOT NULL AND char_length(p_reason) > 500 THEN
        RAISE EXCEPTION 'reason exceeds maximum length of 500 characters'
            USING ERRCODE = 'FC007';
    END IF;

    v_correlation_id := COALESCE(p_correlation_id, gen_random_uuid());

    -- 2. Strict Phase 1 Invariant: No delivery to players (DISPATCH_ENABLED=false)
    IF p_to_status = 'delivered' THEN
        RAISE EXCEPTION 'Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)'
            USING ERRCODE = 'FC002';
    END IF;

    -- 3. Row Lock: SELECT FOR UPDATE on public.dispatches
    SELECT * INTO v_dispatch
      FROM public.dispatches
     WHERE dispatch_id = p_dispatch_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'dispatch % not found', p_dispatch_id
            USING ERRCODE = 'P0002'; -- no_data_found
    END IF;

    -- 4. State & Concurrency Verification
    -- Terminal state freezing
    IF v_dispatch.status IN ('delivered', 'failed', 'cancelled') THEN
        RAISE EXCEPTION 'illegal transition: dispatch % is in terminal state (%) and frozen',
            p_dispatch_id, v_dispatch.status
            USING ERRCODE = 'FC002';
    END IF;

    -- Optimistic concurrency checks (if caller supplied expected status/version)
    IF p_expected_status IS NOT NULL AND v_dispatch.status <> p_expected_status THEN
        RAISE EXCEPTION 'version conflict: expected status % but found % for dispatch %',
            p_expected_status, v_dispatch.status, p_dispatch_id
            USING ERRCODE = 'FC001';
    END IF;

    IF p_expected_version IS NOT NULL AND v_dispatch.version <> p_expected_version THEN
        RAISE EXCEPTION 'version conflict: expected version % but found % for dispatch %',
            p_expected_version, v_dispatch.version, p_dispatch_id
            USING ERRCODE = 'FC001';
    END IF;

    -- Self-transition check
    IF v_dispatch.status = p_to_status THEN
        RAISE EXCEPTION 'illegal transition: dispatch % is already in status %',
            p_dispatch_id, p_to_status
            USING ERRCODE = 'FC002';
    END IF;

    -- 5. Rule Matrix Validation
    SELECT * INTO v_rule
      FROM public.dispatch_transition_rules
     WHERE from_status = v_dispatch.status
       AND to_status = p_to_status;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'illegal transition from % to % for dispatch %',
            v_dispatch.status, p_to_status, p_dispatch_id
            USING ERRCODE = 'FC002';
    END IF;

    -- Validate caller session role against allowed_roles
    v_caller_role := session_user;
    IF NOT (
        v_caller_role = ANY(v_rule.allowed_roles)
        OR (v_caller_role = 'fc27_dispatch' AND 'fc27_worker' = ANY(v_rule.allowed_roles))
        OR v_caller_role IN ('fc27_owner', 'postgres')
    ) THEN
        RAISE EXCEPTION 'role % is not authorized for transition from % to %',
            v_caller_role, v_dispatch.status, p_to_status
            USING ERRCODE = 'FC002';
    END IF;

    -- Validate actor_kind against allowed_actor_kinds
    IF NOT (p_actor_kind = ANY(v_rule.allowed_actor_kinds)) THEN
        RAISE EXCEPTION 'actor_kind % is not permitted for transition from % to %',
            p_actor_kind, v_dispatch.status, p_to_status
            USING ERRCODE = 'FC004';
    END IF;

    -- Validate reason requirement
    IF v_rule.require_reason AND (p_reason IS NULL OR char_length(trim(p_reason)) = 0) THEN
        RAISE EXCEPTION 'reason is required for transition from % to %',
            v_dispatch.status, p_to_status
            USING ERRCODE = 'FC007';
    END IF;

    -- 6. Atomic State Mutation
    v_new_version := v_dispatch.version + 1;

    UPDATE public.dispatches
       SET status = p_to_status,
           version = v_new_version,
           updated_at = now()
     WHERE dispatch_id = p_dispatch_id
    RETURNING * INTO v_updated;

    -- 7. Atomic Audit Event & Outbox Emission
    INSERT INTO public.domain_events (
        event_type,
        entity_type,
        entity_id,
        entity_version,
        actor,
        from_status,
        to_status,
        reason,
        correlation_id,
        occurred_at,
        source_table,
        source_id,
        scope_club_id,
        scope_player_id,
        visibility,
        fc_version,
        platform
    ) VALUES (
        'dispatch.transitioned',
        'dispatch',
        p_dispatch_id::text,
        v_new_version,
        p_actor,
        v_dispatch.status,
        p_to_status,
        p_reason,
        v_correlation_id,
        now(),
        'dispatches',
        p_dispatch_id::text,
        v_dispatch.club_id::text,
        v_dispatch.player_id::text,
        'staff',
        '27',
        'common-gen5'
    ) RETURNING event_id INTO v_event_id;

    INSERT INTO public.outbox (
        domain_event_id,
        channel
    ) VALUES (
        v_event_id,
        'fc27:events'
    );

    -- 8. Return Updated Record State
    RETURN jsonb_build_object(
        'dispatch_id', v_updated.dispatch_id,
        'status', v_updated.status,
        'previous_status', v_dispatch.status,
        'version', v_updated.version,
        'previous_version', v_dispatch.version,
        'actor', p_actor,
        'actor_kind', p_actor_kind,
        'reason', p_reason,
        'correlation_id', v_correlation_id,
        'event_id', v_event_id,
        'updated_at', v_updated.updated_at
    );
END;
$$;

COMMENT ON FUNCTION public.fc27_dispatch_transition IS 
    'Single-write-path transition function executing authorized state transitions on public.dispatches with row locking, rule validation, and atomic outbox emission.';
```

### 4.3 Trigger Compatibility & Event Source Guard Verification
In `migrations/0003_dispatch_foundation.sql`, the trigger `fc27_check_event_source` enforces rule C-10:
```sql
IF (NEW.event_type LIKE 'dispatch.%' AND NEW.event_type != 'dispatch.created') OR NEW.event_type LIKE 'plan.%' OR NEW.event_type LIKE 'tracking.%' THEN
    IF current_user != 'fc27_owner' OR session_user NOT IN ('fc27_app', 'fc27_dispatch') THEN
        RAISE EXCEPTION 'transition-only event type not allowed outside transition function';
    END IF;
END IF;
```
Because `fc27_dispatch_transition` is defined with `SECURITY DEFINER` and owned by `fc27_owner`:
- `current_user` inside the function is `'fc27_owner'`.
- `session_user` is `'fc27_app'` or `'fc27_dispatch'`.
- The event type `'dispatch.transitioned'` matches `dispatch.%` and is NOT `'dispatch.created'`.
- Therefore, the trigger evaluates: `current_user = 'fc27_owner'` (**TRUE**) and `session_user IN ('fc27_app', 'fc27_dispatch')` (**TRUE**).
- When any caller attempts a direct `INSERT` into `public.domain_events` outside this function, `current_user` is `'fc27_app'` and the trigger **immediately rejects** the insert.

---

## 5. Phase 1 Invariant Enforcement: `DISPATCH_ENABLED=false`

Programme Invariant 1 states:
> `DISPATCH_ENABLED=false` at all times. No Phase 1 path may deliver anything to a player.

### Enforcement Layers:
1. **Database Layer (Hard Fail-Closed)**:
   In `fc27_dispatch_transition`:
   ```sql
   IF p_to_status = 'delivered' THEN
       RAISE EXCEPTION 'Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)'
           USING ERRCODE = 'FC002';
   END IF;
   ```
   Even if a rogue worker or misconfigured script attempts to invoke the transition to `delivered`, the database raises an immediate SQLSTATE `FC002` exception and rolls back the transaction.
2. **Rule Matrix Layer**:
   The rule matrix identifies `delivered` as a future target, but the transition function explicitly blocks its execution in Phase 1.
3. **Application Layer**:
   `src/graph_api.py` enforces `settings.dispatch_enabled = False` and returns:
   `"message": "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."`

---

## 6. Python Wrapper Service: `src/dispatch/transition.py`

### 6.1 Exception Hierarchy
A structured exception hierarchy maps database error conditions into clean, typed Python exceptions:

```python
"""Exception hierarchy for dispatch lifecycle transitions."""

class DispatchTransitionError(Exception):
    """Base exception for all dispatch transition failures."""
    def __init__(self, message: str, code: str | None = None, dispatch_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.dispatch_id = dispatch_id

class DispatchNotFoundError(DispatchTransitionError):
    """Raised when dispatch record does not exist (P0002)."""
    pass

class InvalidTransitionError(DispatchTransitionError):
    """Raised when transition edge is disallowed by state machine (FC002)."""
    pass

class Phase1DeliveryBlockedError(InvalidTransitionError):
    """Raised when delivery is attempted while DISPATCH_ENABLED=false (Phase 1 invariant)."""
    pass

class UnauthorizedRoleError(DispatchTransitionError):
    """Raised when session DB role is not in rule allowed_roles (FC002)."""
    pass

class UnauthorizedActorError(DispatchTransitionError):
    """Raised when actor_kind is not permitted for transition (FC004)."""
    pass

class TransitionConflictError(DispatchTransitionError):
    """Raised on version conflict or status mismatch (FC001)."""
    pass

class MissingReasonError(DispatchTransitionError):
    """Raised when rule requires reason but none was provided (FC007)."""
    pass
```

### 6.2 Service Function Implementation
```python
"""Python wrapper service for public.fc27_dispatch_transition."""
from __future__ import annotations

import uuid
from typing import Any, Mapping
import psycopg
from psycopg.pq import TransactionStatus

def transition_dispatch(
    conn: psycopg.Connection,
    *,
    dispatch_id: uuid.UUID | str,
    to_status: str,
    actor: str,
    actor_kind: str,
    reason: str | None = None,
    correlation_id: uuid.UUID | str | None = None,
    expected_status: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Execute authorized dispatch state transition via public.fc27_dispatch_transition.

    Must be executed within an open database transaction.

    Args:
        conn: Open psycopg database connection.
        dispatch_id: Target dispatch UUID.
        to_status: Target status string.
        actor: Identity of actor initiating transition (e.g. 'service:worker-1', 'commissioner:user1').
        actor_kind: Category of actor ('worker', 'commissioner', 'system').
        reason: Optional explanatory reason text (mandatory for certain transitions).
        correlation_id: Distributed tracing correlation UUID.
        expected_status: Optional expected current status for optimistic concurrency check.
        expected_version: Optional expected current version for optimistic concurrency check.

    Returns:
        Dictionary containing updated dispatch state, new version, and emitted event_id.

    Raises:
        RuntimeError: If connection is idle (not in transaction).
        DispatchNotFoundError: If dispatch_id does not exist.
        Phase1DeliveryBlockedError: If transition to delivered is attempted.
        UnauthorizedRoleError: If caller DB role is not authorized for the edge.
        UnauthorizedActorError: If actor_kind is not allowed for the edge.
        TransitionConflictError: If version/status conflict occurs.
        MissingReasonError: If transition requires reason but none provided.
        InvalidTransitionError: If edge is illegal in state machine.
    """
    if conn.info.transaction_status == TransactionStatus.IDLE:
        raise RuntimeError("transition_dispatch must be called inside an open transaction block")

    disp_id_str = str(dispatch_id)
    corr_id_str = str(correlation_id) if correlation_id else None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT public.fc27_dispatch_transition(
                    p_dispatch_id := %s::uuid,
                    p_to_status := %s,
                    p_actor := %s,
                    p_actor_kind := %s,
                    p_reason := %s,
                    p_correlation_id := %s::uuid,
                    p_expected_status := %s,
                    p_expected_version := %s
                )
                """,
                (
                    disp_id_str,
                    to_status,
                    actor,
                    actor_kind,
                    reason,
                    corr_id_str,
                    expected_status,
                    expected_version,
                ),
            )
            result = cur.fetchone()[0]
            return result

    except psycopg.Error as exc:
        pgcode = getattr(exc, "pgcode", "")
        diag = getattr(exc, "diag", None)
        msg = diag.message_primary if diag and diag.message_primary else str(exc)

        if pgcode == "P0002" or "not found" in msg.lower():
            raise DispatchNotFoundError(msg, code="P0002", dispatch_id=disp_id_str) from exc

        if pgcode == "FC001" or "version conflict" in msg.lower():
            raise TransitionConflictError(msg, code="FC001", dispatch_id=disp_id_str) from exc

        if "phase 1 invariant violation" in msg.lower() or "delivered is disabled" in msg.lower():
            raise Phase1DeliveryBlockedError(msg, code="FC002", dispatch_id=disp_id_str) from exc

        if "not authorized for transition" in msg.lower() and "role" in msg.lower():
            raise UnauthorizedRoleError(msg, code="FC002", dispatch_id=disp_id_str) from exc

        if pgcode == "FC004" or "actor_kind" in msg.lower():
            raise UnauthorizedActorError(msg, code="FC004", dispatch_id=disp_id_str) from exc

        if pgcode == "FC007" or "reason is required" in msg.lower():
            raise MissingReasonError(msg, code="FC007", dispatch_id=disp_id_str) from exc

        if pgcode == "FC002" or "illegal transition" in msg.lower():
            raise InvalidTransitionError(msg, code="FC002", dispatch_id=disp_id_str) from exc

        raise DispatchTransitionError(msg, code=pgcode, dispatch_id=disp_id_str) from exc
```

---

## 7. Security, Least Privilege & Grants

### 7.1 Object Ownership
Both `public.dispatch_transition_rules` and `public.fc27_dispatch_transition` must be owned by `fc27_owner`:
```sql
ALTER TABLE public.dispatch_transition_rules OWNER TO fc27_owner;
ALTER FUNCTION public.fc27_dispatch_transition(uuid, text, text, text, text, uuid, text, integer) OWNER TO fc27_owner;
```

### 7.2 Grants in `migrations/post/grants.sql`
Versioned grants policy must be updated:
```sql
-- Milestone 3 additions
IF to_regclass('public.dispatch_transition_rules') IS NOT NULL THEN
    GRANT SELECT ON public.dispatch_transition_rules TO fc27_app, fc27_dispatch;
END IF;

IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_dispatch_transition') THEN
    REVOKE ALL ON FUNCTION public.fc27_dispatch_transition(uuid, text, text, text, text, uuid, text, integer) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.fc27_dispatch_transition(uuid, text, text, text, text, uuid, text, integer) TO fc27_app, fc27_dispatch;
END IF;
```

### 7.3 Least Privilege Guarantee
- `fc27_app`: Has `SELECT` on `dispatch_transition_rules`, `EXECUTE` on `fc27_dispatch_transition`. Direct `UPDATE` or `DELETE` on `public.dispatches` is denied.
- `fc27_dispatch`: Has `SELECT` on `dispatch_transition_rules`, `EXECUTE` on `fc27_dispatch_transition`. Direct `INSERT`, `UPDATE`, or `DELETE` on `public.dispatches` is denied.
- Neither role can alter the table, modify the function, or bypass event logging.

---

## 8. Summary Table of Artifacts

| Component | Target Location | Description |
|---|---|---|
| Migration DDL | `migrations/0003_dispatch_foundation.sql` (Section D) | Table `dispatch_transition_rules`, seed data, and function `fc27_dispatch_transition` |
| Grants Policy | `migrations/post/grants.sql` | `GRANT SELECT` on rules table, `GRANT EXECUTE` on function to `fc27_app`, `fc27_dispatch` |
| Python Service | `src/dispatch/transition.py` | `transition_dispatch()` function and typed exception hierarchy |
| Test Suite | `tests/test_dispatch_transition.py` | Transition verification, security role negative tests, Phase 1 invariant delivery block |
