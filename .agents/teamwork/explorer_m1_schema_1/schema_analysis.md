# Technical Schema Analysis & Implementation Design: Milestone 1

**Document Version:** 1.0.0  
**Date:** 2026-10-03  
**Author:** explorer_m1_schema_1 (teamwork_preview_explorer)  
**Target Milestone:** Milestone 1 — Authoritative Approval Lifecycle & Expiry (Task 4 + Section 1A)  
**Scope Boundaries:** Read-only exploration and design. No repository files modified.

---

## 1. Executive Summary & Architectural Scope

Milestone 1 builds the authoritative approval and human authority data foundation within the player-dispatch programme. It bridges the foundational event/outbox infrastructure (Milestone 0 / 0003 Section A) with the core LangGraph workflow engine (Milestone 2) and subsequent dispatch execution pipeline (Milestones 3–6).

This milestone delivers two tightly integrated SQL components:
1. **0003 Section B (Authoritative Approval Lifecycle & Expiry):**
   - Authoritative `approval_requests` entity managing the approval lifecycle (`awaiting_approval`, `approved`, `rejected`, `revision_requested`, `expired`).
   - Bidirectional foreign key and paired consistency constraint trigger (`fc27_check_approval_pair`) between `graph_threads` and `approval_requests`.
   - Inconsistencies diagnostic view (`fc27_approval_inconsistencies`).
   - Terminal state protection and provenance immutability triggers on both tables.
   - Nonce typing, revocation, and lifecycle extensions (`approval_nonces`).
   - Proof typing and correlation fields on append-only audit (`approval_audit`).
   - Domain event source verification update (`fc27_check_event_source`).
   - Safe, idempotent legacy backfill for pre-existing 0002 threads.
2. **Section 1A Compatibility Model (Human Authority, Player Consent & Continuity):**
   - Resolves the single-approver bottleneck without collapsing club authorization, delegated authority, player consent, and fallback authority into an ambiguous boolean.
   - Compatibility schema across seven normalized tables:
     - `authority_policies`: Declares permitted decision authority rules per action type and scope.
     - `authority_delegations`: Explicit, time-bounded, revocable human delegations.
     - `player_assignments`: Versioned personal package assignments per affected player.
     - `player_decision_requests`: Assignment-specific decision challenge records.
     - `player_decisions`: Immutable player consent, fallback, or rejection decisions.
     - `approval_escalations`: Auditable state progression history preventing silent timeout approvals.
     - `authority_conflicts`: Isolation and tracking of late or conflicting decisions.
   - Grants policy upgrade (`migrations/post/grants.sql` v3) and runner synchronization (`src/db_migrate.py`).

---

## 2. Deep Technical Design: `0003_dispatch_foundation.sql` Section B

### 2.1 Table `public.approval_requests`

`approval_requests` becomes the single authoritative source of truth for all recommendation approvals. `graph_threads` stores execution state, but authorization status lives here.

#### DDL Specification
```sql
CREATE TABLE IF NOT EXISTS public.approval_requests (
    approval_request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id text UNIQUE NOT NULL REFERENCES public.graph_threads(thread_id),
    package_version text NOT NULL CHECK (package_version ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN (
        'awaiting_approval',
        'approved',
        'rejected',
        'revision_requested',
        'expired'
    )),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    decided_at timestamptz,
    expired_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    CHECK (expires_at > created_at),
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK ((status IN ('approved', 'rejected', 'revision_requested')) = (decided_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS approval_requests_status_expires_idx 
    ON public.approval_requests(status, expires_at);
```

#### Invariants & Constraints:
- `expires_at > created_at`: Expiration must be strictly in the future.
- `(status = 'expired') = (expired_at IS NOT NULL)`: Enforces that marking expired records the timestamp, and timestamp implies expired status.
- `(status IN ('approved','rejected','revision_requested')) = (decided_at IS NOT NULL)`: Enforces exact timestamp recording on terminal decisions.

#### Triggers: Provenance Immutability & Terminal Freezing
```sql
CREATE OR REPLACE FUNCTION public.fc27_guard_approval_request_transition()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('approved', 'rejected', 'revision_requested', 'expired') THEN
            RAISE EXCEPTION 'approval_request % is terminal and cannot be deleted', OLD.approval_request_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- Freeze terminal rows
    IF OLD.status IN ('approved', 'rejected', 'revision_requested', 'expired') THEN
        RAISE EXCEPTION 'approval_request % is terminal (%) and cannot be modified', OLD.approval_request_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;

    -- Provenance immutability
    IF NEW.approval_request_id <> OLD.approval_request_id THEN
        RAISE EXCEPTION 'approval_request_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.thread_id <> OLD.thread_id THEN
        RAISE EXCEPTION 'thread_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.package_version <> OLD.package_version THEN
        RAISE EXCEPTION 'package_version is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'created_at is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.expires_at <> OLD.expires_at THEN
        RAISE EXCEPTION 'expires_at is immutable' USING ERRCODE = 'check_violation';
    END IF;

    -- Enforce legal state machine transition: awaiting_approval -> {terminal}
    IF NEW.status IN ('approved', 'rejected', 'revision_requested', 'expired') AND OLD.status <> 'awaiting_approval' THEN
        RAISE EXCEPTION 'approval_request % must be awaiting_approval before transition to % (was %)',
            OLD.approval_request_id, NEW.status, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_approval_requests_guard ON public.approval_requests;
CREATE TRIGGER fc27_approval_requests_guard
BEFORE UPDATE OR DELETE ON public.approval_requests
FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_approval_request_transition();
```

---

### 2.2 Table `public.graph_threads` Modifications

`graph_threads` is enhanced to establish the foreign key link to `approval_requests` and expand its lifecycle constraints to include `revision_requested` and `expired`.

#### Schema Alterations
```sql
-- 1. Add approval_request_id foreign key
ALTER TABLE public.graph_threads
    ADD COLUMN IF NOT EXISTS approval_request_id uuid UNIQUE REFERENCES public.approval_requests(approval_request_id);

-- 2. Extend status CHECK constraint
ALTER TABLE public.graph_threads
    DROP CONSTRAINT IF EXISTS graph_threads_status_chk;

ALTER TABLE public.graph_threads
    ADD CONSTRAINT graph_threads_status_chk
    CHECK (status IN (
        'running',
        'awaiting_approval',
        'approved',
        'rejected',
        'revision_requested',
        'expired',
        'failed'
    ));

-- 3. Replace terminal fields CHECK constraint
ALTER TABLE public.graph_threads
    DROP CONSTRAINT IF EXISTS graph_threads_terminal_fields_chk;

ALTER TABLE public.graph_threads
    ADD CONSTRAINT graph_threads_terminal_fields_chk
    CHECK (
        (status IN ('approved', 'rejected', 'revision_requested'))
        = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)
    );
```
*Note on `expired`:* When a thread transitions to `expired`, it does NOT require `approved_by` or `decision_payload`. Therefore, `expired` is intentionally omitted from the positive condition in `graph_threads_terminal_fields_chk`.

#### Replaced Transition Guard Trigger
The existing `fc27_guard_thread_transition()` trigger must be replaced to enforce:
1. Terminal set includes `'approved'`, `'rejected'`, `'revision_requested'`, `'expired'`.
2. `package_version` is immutable once `status` is `awaiting_approval` (exact message: `'package_version is immutable'`).
3. `approval_request_id` can be set at most once (`'approval_request_id is immutable once set'`).
4. Allowed edges: `awaiting_approval -> {approved, rejected, revision_requested, expired}`.

```sql
CREATE OR REPLACE FUNCTION public.fc27_guard_thread_transition()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('approved', 'rejected', 'revision_requested', 'expired') THEN
            RAISE EXCEPTION 'thread % is terminal and cannot be deleted', OLD.thread_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- Freeze terminal rows
    IF OLD.status IN ('approved', 'rejected', 'revision_requested', 'expired') THEN
        RAISE EXCEPTION 'thread % is terminal (%) and cannot be modified', OLD.thread_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;

    -- Package version immutability
    IF (OLD.status = 'awaiting_approval' OR OLD.package_version IS NOT NULL) 
       AND NEW.package_version IS DISTINCT FROM OLD.package_version THEN
        RAISE EXCEPTION 'package_version is immutable'
            USING ERRCODE = 'check_violation';
    END IF;

    -- approval_request_id set at most once
    IF OLD.approval_request_id IS NOT NULL 
       AND NEW.approval_request_id IS DISTINCT FROM OLD.approval_request_id THEN
        RAISE EXCEPTION 'approval_request_id is immutable once set'
            USING ERRCODE = 'check_violation';
    END IF;

    -- State machine guards
    IF NEW.status IN ('approved', 'rejected', 'revision_requested', 'expired') 
       AND OLD.status <> 'awaiting_approval' THEN
        RAISE EXCEPTION 'thread % must be awaiting_approval before a decision (was %)', OLD.thread_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.status = 'awaiting_approval' AND OLD.status NOT IN ('running', 'awaiting_approval') THEN
        RAISE EXCEPTION 'thread % cannot return to awaiting_approval from %', OLD.thread_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.thread_id <> OLD.thread_id THEN
        RAISE EXCEPTION 'thread_id is immutable' USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS graph_threads_guard ON public.graph_threads;
CREATE TRIGGER graph_threads_guard
BEFORE UPDATE OR DELETE ON public.graph_threads
FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_thread_transition();
```

---

### 2.3 Paired Consistency Trigger: `fc27_check_approval_pair()`

To guarantee transactional alignment between `graph_threads` and `approval_requests`, twin `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED` triggers execute at `COMMIT` time. Any uncoordinated update fails closed.

```sql
CREATE OR REPLACE FUNCTION public.fc27_check_approval_pair()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_req public.approval_requests%ROWTYPE;
    v_thread public.graph_threads%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME = 'graph_threads' THEN
        -- An awaiting thread with a package version MUST be linked
        IF NEW.status = 'awaiting_approval' AND NEW.package_version IS NOT NULL AND NEW.approval_request_id IS NULL THEN
            RAISE EXCEPTION 'approval pair: awaiting_approval thread % with package_version must be linked to an approval_request', NEW.thread_id
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.approval_request_id IS NOT NULL THEN
            SELECT * INTO v_req FROM public.approval_requests WHERE approval_request_id = NEW.approval_request_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'approval pair: linked approval_request % not found for thread %', NEW.approval_request_id, NEW.thread_id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF v_req.status <> NEW.status THEN
                RAISE EXCEPTION 'approval pair: status mismatch between thread % (%) and approval_request % (%)',
                    NEW.thread_id, NEW.status, v_req.approval_request_id, v_req.status
                    USING ERRCODE = 'check_violation';
            END IF;
            IF v_req.package_version IS DISTINCT FROM NEW.package_version THEN
                RAISE EXCEPTION 'approval pair: package_version mismatch between thread % (%) and approval_request % (%)',
                    NEW.thread_id, NEW.package_version, v_req.approval_request_id, v_req.package_version
                    USING ERRCODE = 'check_violation';
            END IF;
            IF v_req.thread_id <> NEW.thread_id THEN
                RAISE EXCEPTION 'approval pair: thread_id mismatch between thread % and approval_request thread %',
                    NEW.thread_id, v_req.thread_id
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;

    ELSIF TG_TABLE_NAME = 'approval_requests' THEN
        SELECT * INTO v_thread FROM public.graph_threads WHERE thread_id = NEW.thread_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'approval pair: linked thread % not found for approval_request %', NEW.thread_id, NEW.approval_request_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF v_thread.approval_request_id IS DISTINCT FROM NEW.approval_request_id THEN
            RAISE EXCEPTION 'approval pair: thread % does not link to approval_request %', NEW.thread_id, NEW.approval_request_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF v_thread.status <> NEW.status THEN
            RAISE EXCEPTION 'approval pair: status mismatch between approval_request % (%) and thread % (%)',
                NEW.approval_request_id, NEW.status, v_thread.thread_id, v_thread.status
                USING ERRCODE = 'check_violation';
        END IF;
        IF v_thread.package_version IS DISTINCT FROM NEW.package_version THEN
            RAISE EXCEPTION 'approval pair: package_version mismatch between approval_request % (%) and thread % (%)',
                NEW.approval_request_id, NEW.package_version, v_thread.thread_id, v_thread.package_version
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_graph_threads_pair_check ON public.graph_threads;
CREATE CONSTRAINT TRIGGER fc27_graph_threads_pair_check
AFTER INSERT OR UPDATE ON public.graph_threads
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.fc27_check_approval_pair();

DROP TRIGGER IF EXISTS fc27_approval_requests_pair_check ON public.approval_requests;
CREATE CONSTRAINT TRIGGER fc27_approval_requests_pair_check
AFTER INSERT OR UPDATE ON public.approval_requests
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.fc27_check_approval_pair();
```

---

### 2.4 Diagnostic View: `public.fc27_approval_inconsistencies`

The view exposes discrepancies between execution state and authorization records. Graph API queries this view at startup and on `/health/ready`. Any non-legacy inconsistency degrades service health and blocks decisions.

```sql
CREATE OR REPLACE VIEW public.fc27_approval_inconsistencies AS
-- 1. status_mismatch: linked, but statuses differ
SELECT 'status_mismatch'::text AS kind,
       g.thread_id,
       r.approval_request_id
  FROM public.graph_threads g
  JOIN public.approval_requests r ON g.approval_request_id = r.approval_request_id
 WHERE g.status <> r.status

UNION ALL

-- 2. package_mismatch: linked, but package_version differs
SELECT 'package_mismatch'::text AS kind,
       g.thread_id,
       r.approval_request_id
  FROM public.graph_threads g
  JOIN public.approval_requests r ON g.approval_request_id = r.approval_request_id
 WHERE g.package_version IS DISTINCT FROM r.package_version

UNION ALL

-- 3. unlinked_request: approval_requests row whose thread doesn't link back to it or doesn't exist
SELECT 'unlinked_request'::text AS kind,
       r.thread_id,
       r.approval_request_id
  FROM public.approval_requests r
  LEFT JOIN public.graph_threads g ON r.approval_request_id = g.approval_request_id
 WHERE g.thread_id IS NULL OR g.thread_id <> r.thread_id

UNION ALL

-- 4. missing_link: active awaiting thread with package_version that has no approval_request_id
SELECT 'missing_link'::text AS kind,
       g.thread_id,
       g.approval_request_id
  FROM public.graph_threads g
 WHERE g.status = 'awaiting_approval'
   AND g.package_version IS NOT NULL
   AND g.approval_request_id IS NULL

UNION ALL

-- 5. legacy_unlinked: historical pre-0003 threads without an approval_request_id
SELECT 'legacy_unlinked'::text AS kind,
       g.thread_id,
       g.approval_request_id
  FROM public.graph_threads g
 WHERE g.approval_request_id IS NULL
   AND NOT (g.status = 'awaiting_approval' AND g.package_version IS NOT NULL);
```

---

### 2.5 Table `public.approval_audit` Extensions

`approval_audit` records immutable evidence of each terminal authorization action.

#### Schema Alterations
```sql
-- Extend decision constraint to accept REQUEST_REVISION
ALTER TABLE public.approval_audit
    DROP CONSTRAINT IF EXISTS approval_audit_decision_check;
ALTER TABLE public.approval_audit
    DROP CONSTRAINT IF EXISTS approval_audit_decision_chk;

ALTER TABLE public.approval_audit
    ADD CONSTRAINT approval_audit_decision_chk
    CHECK (decision IN ('APPROVE', 'REJECT', 'REQUEST_REVISION'));

-- Add Phase 1 Task 4 fields
ALTER TABLE public.approval_audit
    ADD COLUMN IF NOT EXISTS proof_type text NOT NULL DEFAULT 'hmac'
        CHECK (proof_type IN ('hmac', 'webauthn')),
    ADD COLUMN IF NOT EXISTS actor_user_id uuid NULL,
    ADD COLUMN IF NOT EXISTS webauthn_credential_id bytea NULL,
    ADD COLUMN IF NOT EXISTS correlation_id uuid NULL,
    ADD COLUMN IF NOT EXISTS approval_request_id uuid NULL 
        REFERENCES public.approval_requests(approval_request_id);

CREATE INDEX IF NOT EXISTS approval_audit_request_idx 
    ON public.approval_audit(approval_request_id);
```

---

### 2.6 Table `public.approval_nonces` Extensions

Single-use challenge nonces are generalized across thread approvals, dispatch actions, role assignments, and activation gates.

#### Schema Alterations
```sql
-- 1. Add new columns
ALTER TABLE public.approval_nonces
    ADD COLUMN IF NOT EXISTS revoked_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS subject_type text NOT NULL DEFAULT 'thread',
    ADD COLUMN IF NOT EXISTS dispatch_id uuid NULL,
    ADD COLUMN IF NOT EXISTS user_id uuid NULL,
    ADD COLUMN IF NOT EXISTS gate_id text NULL;

-- 2. Make legacy non-nullable fields nullable for non-thread subjects
ALTER TABLE public.approval_nonces
    ALTER COLUMN thread_id DROP NOT NULL,
    ALTER COLUMN package_version DROP NOT NULL;

-- 3. Subject-type CHECK constraint
ALTER TABLE public.approval_nonces
    DROP CONSTRAINT IF EXISTS approval_nonces_subject_type_chk;

ALTER TABLE public.approval_nonces
    ADD CONSTRAINT approval_nonces_subject_type_chk
    CHECK (subject_type IN (
        'thread',
        'dispatch_plan',
        'dispatch_cancel',
        'role_grant',
        'activation_gate'
    ));

-- 4. Exactly-one-subject column coupling constraint
ALTER TABLE public.approval_nonces
    DROP CONSTRAINT IF EXISTS approval_nonces_subject_columns_chk;

ALTER TABLE public.approval_nonces
    ADD CONSTRAINT approval_nonces_subject_columns_chk
    CHECK (
        (subject_type = 'thread' 
            AND thread_id IS NOT NULL AND package_version IS NOT NULL 
            AND dispatch_id IS NULL AND user_id IS NULL AND gate_id IS NULL)
        OR
        (subject_type IN ('dispatch_plan', 'dispatch_cancel') 
            AND dispatch_id IS NOT NULL 
            AND thread_id IS NULL AND user_id IS NULL AND gate_id IS NULL)
        OR
        (subject_type = 'role_grant' 
            AND user_id IS NOT NULL 
            AND thread_id IS NULL AND dispatch_id IS NULL AND gate_id IS NULL)
        OR
        (subject_type = 'activation_gate' 
            AND gate_id IS NOT NULL 
            AND thread_id IS NULL AND dispatch_id IS NULL AND user_id IS NULL)
    );
```

---

### 2.7 Trigger Function `public.fc27_check_event_source()` Update

The trigger function from Section A is replaced to validate source integrity and entity-ownership for `approval_requests` and `approval_audit`.

```sql
CREATE OR REPLACE FUNCTION public.fc27_check_event_source()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Transition-only event types (C-10)
    IF (NEW.event_type LIKE 'dispatch.%' AND NEW.event_type != 'dispatch.created') 
       OR NEW.event_type LIKE 'plan.%' 
       OR NEW.event_type LIKE 'tracking.%' THEN
        IF current_user != 'fc27_owner' OR session_user NOT IN ('fc27_app', 'fc27_dispatch') THEN
            RAISE EXCEPTION 'transition-only event type not allowed outside transition function';
        END IF;
    END IF;

    -- Sourced event validation
    IF NEW.event_type = 'approval.decided' THEN
        IF NEW.source_table IS DISTINCT FROM 'approval_audit' THEN
            RAISE EXCEPTION 'approval.decided requires source_table = approval_audit';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'approval.decided requires a source_id';
        END IF;
        -- Entity ownership check (entity_id matches approval_request_id or thread_id)
        IF NEW.entity_type = 'approval_request' THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.approval_audit 
                 WHERE id = NEW.source_id::bigint 
                   AND approval_request_id = NEW.entity_id::uuid
            ) THEN
                RAISE EXCEPTION 'source row not found or entity mismatch for approval.decided';
            END IF;
        ELSE
            IF NOT EXISTS (
                SELECT 1 FROM public.approval_audit 
                 WHERE id = NEW.source_id::bigint 
                   AND thread_id = NEW.entity_id
            ) THEN
                RAISE EXCEPTION 'source row not found or entity mismatch for approval.decided';
            END IF;
        END IF;

    ELSIF NEW.event_type IN ('approval.created', 'approval.expired') THEN
        IF NEW.source_table IS DISTINCT FROM 'approval_requests' THEN
            RAISE EXCEPTION 'event_type requires source_table = approval_requests';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        -- Validate source row exists and matches entity_id
        IF NOT EXISTS (
            SELECT 1 FROM public.approval_requests 
             WHERE approval_request_id = NEW.source_id::uuid 
               AND approval_request_id = NEW.entity_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found or entity mismatch for approval request event';
        END IF;

    ELSIF NEW.event_type = 'dispatch.created' THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatches' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatches';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;

    ELSIF NEW.event_type = 'dispatch.acknowledged' THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_acknowledgments' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_acknowledgments';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;

    ELSIF NEW.event_type IN ('tracking.updated', 'tracking.completed') THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_assessments' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_assessments';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;
```

---

### 2.8 Legacy Data Backfill

Executed inside migration `0003` to backfill pre-existing data safely.

#### Backfill Logic
1. For each `graph_threads` row where `status = 'awaiting_approval' AND package_version IS NOT NULL`:
   - Generate a new `approval_requests` row.
   - Set `expires_at = greatest(created_at + interval '72 hours', now() + interval '1 hour')`.
   - Update `graph_threads.approval_request_id` to link the new request.
2. Terminal rows (`approved`, `rejected`, `failed`) and awaiting threads without `package_version` are NOT altered.
3. This step is executed BEFORE the constraint triggers are created, ensuring atomic, conflict-free transition.

```sql
DO $$
DECLARE
    r RECORD;
    v_req_id uuid;
BEGIN
    FOR r IN 
        SELECT thread_id, package_version, created_at 
          FROM public.graph_threads 
         WHERE status = 'awaiting_approval' 
           AND package_version IS NOT NULL 
           AND approval_request_id IS NULL
    LOOP
        v_req_id := gen_random_uuid();
        
        INSERT INTO public.approval_requests (
            approval_request_id,
            thread_id,
            package_version,
            status,
            created_at,
            expires_at,
            version
        ) VALUES (
            v_req_id,
            r.thread_id,
            r.package_version,
            'awaiting_approval',
            r.created_at,
            GREATEST(r.created_at + interval '72 hours', now() + interval '1 hour'),
            1
        );

        UPDATE public.graph_threads
           SET approval_request_id = v_req_id
         WHERE thread_id = r.thread_id;
    END LOOP;
END $$;
```

---

## 3. Section 1A Human Authority & Player Consent Compatibility

Section 1A of the project charter establishes that:
- The Commissioner authorizes club-level actions.
- An explicit human delegate may authorize designated scoped actions.
- Affected players authorize their own player-specific assignments.
- Affected-player fallback is permitted only after escalation for personal developmental actions, and never for club-wide or multi-player actions.
- Inactivity or timeout never constitutes an approval.
- Agents (including LLMs and worker processes) never make decisions for humans.

To prevent future destructive migrations while keeping Phase 1 strictly read-only for production authorization, Milestone 1 introduces seven forward-compatible tables and compatibility fields on `approval_audit`.

### 3.1 DDL: Seven Section 1A Compatibility Tables

#### 1. `public.authority_policies`
Defines allowed decision authority, delegation rights, and fallback rules per action scope.
```sql
CREATE TABLE IF NOT EXISTS public.authority_policies (
    policy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_name text UNIQUE NOT NULL CHECK (char_length(policy_name) BETWEEN 1 AND 64),
    scope text NOT NULL CHECK (scope IN ('club', 'player_specific', 'mixed')),
    action_type text NOT NULL CHECK (char_length(action_type) BETWEEN 1 AND 64),
    primary_role text NOT NULL DEFAULT 'commissioner',
    allow_delegation boolean NOT NULL DEFAULT false,
    allow_player_consent boolean NOT NULL DEFAULT false,
    allow_player_fallback boolean NOT NULL DEFAULT false,
    escalation_timeout_seconds integer CHECK (escalation_timeout_seconds IS NULL OR escalation_timeout_seconds > 0),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1,
    -- Invariants: Fallback only allowed for player-specific actions
    CHECK (allow_player_fallback = false OR scope = 'player_specific'),
    -- Prohibited club-level actions from fallback
    CHECK (
        allow_player_fallback = false OR
        action_type NOT IN (
            'club_strategy', 'team_tactics', 'formations', 'programme_release',
            'club_dispatch_activation', 'policy_exception', 'roster',
            'transfer', 'recruitment', 'discipline', 'fair_play',
            'identity_change', 'access_control', 'delegation_policy'
        )
    )
);

CREATE INDEX IF NOT EXISTS authority_policies_scope_idx 
    ON public.authority_policies(scope, action_type);
```

#### 2. `public.authority_delegations`
Manages revocable, time-bounded delegations of human authority.
```sql
CREATE TABLE IF NOT EXISTS public.authority_delegations (
    delegation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    delegator_id text NOT NULL,
    delegate_id text NOT NULL,
    delegate_role text NOT NULL DEFAULT 'deputy',
    scope text NOT NULL CHECK (scope IN ('club', 'player_specific')),
    allowed_action_types text[] NOT NULL DEFAULT '{}',
    issued_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_by text,
    revocation_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > issued_at),
    CHECK (revoked_at IS NULL OR (revoked_by IS NOT NULL AND revocation_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS authority_delegations_active_idx 
    ON public.authority_delegations(delegate_id, scope)
    WHERE revoked_at IS NULL;
```

#### 3. `public.player_assignments`
Records one immutable package assignment per individual player.
```sql
CREATE TABLE IF NOT EXISTS public.player_assignments (
    assignment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    package_id text NOT NULL,
    package_version text NOT NULL CHECK (package_version ~ '^[0-9a-f]{64}$'),
    package_hash text NOT NULL CHECK (package_hash ~ '^[0-9a-f]{64}$'),
    subject_player_id text NOT NULL CHECK (subject_player_id ~ '^[0-9]{1,20}$'),
    club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$'),
    assignment_version integer NOT NULL DEFAULT 1,
    assignment_hash text NOT NULL CHECK (assignment_hash ~ '^[0-9a-f]{64}$'),
    assignment_scope text NOT NULL DEFAULT 'player_specific' CHECK (assignment_scope = 'player_specific'),
    action_type text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'invalidated', 'completed', 'paused')),
    requires_player_consent boolean NOT NULL DEFAULT true,
    fallback_eligible boolean NOT NULL DEFAULT false,
    superseded_by uuid REFERENCES public.player_assignments(assignment_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (package_id, subject_player_id, assignment_version)
);

CREATE INDEX IF NOT EXISTS player_assignments_player_status_idx 
    ON public.player_assignments(subject_player_id, status);
```

#### 4. `public.player_decision_requests`
Explicit challenges issued to players for personal assignment review.
```sql
CREATE TABLE IF NOT EXISTS public.player_decision_requests (
    request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id uuid NOT NULL REFERENCES public.player_assignments(assignment_id),
    subject_player_id text NOT NULL CHECK (subject_player_id ~ '^[0-9]{1,20}$'),
    status text NOT NULL DEFAULT 'awaiting_player' CHECK (status IN (
        'awaiting_player', 'decided', 'expired', 'invalidated', 'superseded'
    )),
    challenge_nonce text UNIQUE,
    action_token_reference text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    decided_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    CHECK (expires_at > created_at),
    CHECK ((status = 'decided') = (decided_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS player_decision_requests_assignment_idx 
    ON public.player_decision_requests(assignment_id);
```

#### 5. `public.player_decisions`
Append-only log of player actions (`accept`, `reject`, `change_request`, `pause`, `acknowledge`).
```sql
CREATE TABLE IF NOT EXISTS public.player_decisions (
    decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid NOT NULL REFERENCES public.player_decision_requests(request_id),
    assignment_id uuid NOT NULL REFERENCES public.player_assignments(assignment_id),
    subject_player_id text NOT NULL CHECK (subject_player_id ~ '^[0-9]{1,20}$'),
    actor_type text NOT NULL CHECK (actor_type = 'player'),
    actor_id text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('accept', 'reject', 'change_request', 'pause', 'acknowledge')),
    authority_basis text NOT NULL CHECK (authority_basis IN ('affected_player_consent', 'affected_player_fallback')),
    assignment_version integer NOT NULL,
    assignment_hash text NOT NULL CHECK (assignment_hash ~ '^[0-9a-f]{64}$'),
    package_id text NOT NULL,
    package_version text NOT NULL CHECK (package_version ~ '^[0-9a-f]{64}$'),
    package_hash text NOT NULL CHECK (package_hash ~ '^[0-9a-f]{64}$'),
    notes text NOT NULL DEFAULT '',
    authentication_evidence_id uuid,
    action_token_reference text,
    idempotency_key text UNIQUE,
    decided_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (actor_id = subject_player_id)
);

CREATE TRIGGER fc27_player_decisions_immutable
BEFORE UPDATE OR DELETE ON public.player_decisions
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();
```

#### 6. `public.approval_escalations`
Append-only progression tracking for unacted requests.
```sql
CREATE TABLE IF NOT EXISTS public.approval_escalations (
    escalation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_request_id uuid NOT NULL REFERENCES public.approval_requests(approval_request_id),
    from_state text NOT NULL,
    to_state text NOT NULL CHECK (to_state IN (
        'awaiting_primary', 'primary_reminder_due', 'evaluating_fallback',
        'awaiting_delegate', 'awaiting_affected_player', 'held_no_authorized_approver',
        'terminal', 'expired', 'invalidated', 'superseded', 'conflict_requires_resolution'
    )),
    trigger_reason text NOT NULL CHECK (trigger_reason IN (
        'timer_elapsed', 'manual_escalation', 'delegate_unavailable', 'policy_rule', 'conflict_detected'
    )),
    fallback_eligible boolean NOT NULL DEFAULT false,
    eligible_actor_type text CHECK (eligible_actor_type IN ('commissioner', 'delegate', 'player')),
    eligible_actor_id text,
    escalated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER fc27_approval_escalations_immutable
BEFORE UPDATE OR DELETE ON public.approval_escalations
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();
```

#### 7. `public.authority_conflicts`
Durable isolation for late or conflicting decisions.
```sql
CREATE TABLE IF NOT EXISTS public.authority_conflicts (
    conflict_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_request_id uuid NOT NULL REFERENCES public.approval_requests(approval_request_id),
    first_decision_id text NOT NULL,
    conflicting_decision_payload jsonb NOT NULL,
    conflict_type text NOT NULL CHECK (conflict_type IN (
        'late_primary_after_delegate', 'late_primary_after_fallback',
        'concurrent_decisions', 'conflicting_scope', 'unauthorized_attempt'
    )),
    status text NOT NULL DEFAULT 'unresolved' CHECK (status IN (
        'unresolved', 'reconciled_primary_held', 'reconciled_fallback_revoked', 'invalidated'
    )),
    detected_at timestamptz NOT NULL DEFAULT now(),
    reconciled_at timestamptz,
    reconciled_by text,
    reconciliation_notes text,
    created_at timestamptz NOT NULL DEFAULT now()
);
```

### 3.2 Compatibility Fields on `public.approval_audit`

To support Section 1A actor typing, authority basis, and conflict recording, `approval_audit` receives the following fields:

```sql
ALTER TABLE public.approval_audit
    ADD COLUMN IF NOT EXISTS actor_type text NOT NULL DEFAULT 'commissioner'
        CHECK (actor_type IN ('commissioner', 'delegate', 'player', 'system')),
    ADD COLUMN IF NOT EXISTS authority_role text NOT NULL DEFAULT 'commissioner',
    ADD COLUMN IF NOT EXISTS authority_basis text NOT NULL DEFAULT 'primary'
        CHECK (authority_basis IN ('primary', 'explicit_delegate', 'affected_player_consent', 'affected_player_fallback')),
    ADD COLUMN IF NOT EXISTS decision_scope text NOT NULL DEFAULT 'club'
        CHECK (decision_scope IN ('club', 'player_specific', 'mixed')),
    ADD COLUMN IF NOT EXISTS delegation_id uuid NULL 
        REFERENCES public.authority_delegations(delegation_id),
    ADD COLUMN IF NOT EXISTS subject_player_id text NULL,
    ADD COLUMN IF NOT EXISTS package_id text NULL,
    ADD COLUMN IF NOT EXISTS package_hash text NULL,
    ADD COLUMN IF NOT EXISTS assignment_id uuid NULL 
        REFERENCES public.player_assignments(assignment_id),
    ADD COLUMN IF NOT EXISTS assignment_version integer NULL,
    ADD COLUMN IF NOT EXISTS assignment_hash text NULL,
    ADD COLUMN IF NOT EXISTS authentication_evidence_id uuid NULL,
    ADD COLUMN IF NOT EXISTS action_token_reference text NULL,
    ADD COLUMN IF NOT EXISTS invalidated_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS superseded_by uuid NULL,
    ADD COLUMN IF NOT EXISTS conflict_state text NULL 
        CHECK (conflict_state IS NULL OR conflict_state IN ('none', 'conflict_detected', 'reconciled'));
```

---

## 4. `migrations/post/grants.sql` Version 3 Update

Every database role conforms strictly to least privilege:
- `fc27_owner`: Owns all objects.
- `fc27_migrator`: Has `CONNECT` and inherits `fc27_owner` membership.
- `fc27_app`: Application role (graph-api). Needs DML on approval and compatibility tables. No DDL, no superuser, no ownership.
- `fc27_dispatch`: Worker role. Has **zero** access to approval tables, nonces, or user decision data.

### 4.1 Grants Script Diff Specification
Bump `-- grants-policy-version: 3` and add conditional grants for Milestone 1 objects:

```sql
-- grants-policy-version: 3
DO $$
BEGIN
    -- Existing M0 objects
    IF to_regclass('public.graph_threads') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.graph_threads TO fc27_app;
    END IF;
    IF to_regclass('public.approval_nonces') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.approval_nonces TO fc27_app;
    END IF;
    IF to_regclass('public.run_sequences') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.run_sequences TO fc27_app;
    END IF;
    IF to_regclass('public.approval_audit') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.approval_audit TO fc27_app;
    END IF;
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        GRANT SELECT ON public.schema_migrations TO fc27_app;
    END IF;
    IF to_regclass('public.checkpoints') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.checkpoints TO fc27_app;
    END IF;
    IF to_regclass('public.checkpoint_blobs') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.checkpoint_blobs TO fc27_app;
    END IF;
    IF to_regclass('public.checkpoint_writes') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.checkpoint_writes TO fc27_app;
    END IF;
    IF to_regclass('public.checkpoint_migrations') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.checkpoint_migrations TO fc27_app;
    END IF;
    IF to_regclass('public.approval_audit_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.approval_audit_id_seq TO fc27_app;
    END IF;
    IF to_regclass('public.domain_events') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.domain_events TO fc27_app;
        GRANT SELECT ON public.domain_events TO fc27_dispatch;
    END IF;
    IF to_regclass('public.domain_events_event_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.domain_events_event_id_seq TO fc27_app;
    END IF;
    IF to_regclass('public.outbox') IS NOT NULL THEN
        GRANT INSERT ON public.outbox TO fc27_app;
    END IF;
    IF to_regclass('public.outbox_outbox_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.outbox_outbox_id_seq TO fc27_app;
    END IF;

    -- Milestone 1 additions
    IF to_regclass('public.approval_requests') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.approval_requests TO fc27_app;
    END IF;
    IF to_regclass('public.fc27_approval_inconsistencies') IS NOT NULL THEN
        GRANT SELECT ON public.fc27_approval_inconsistencies TO fc27_app;
    END IF;

    -- Section 1A compatibility additions
    IF to_regclass('public.authority_policies') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.authority_policies TO fc27_app;
    END IF;
    IF to_regclass('public.authority_delegations') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.authority_delegations TO fc27_app;
    END IF;
    IF to_regclass('public.player_assignments') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.player_assignments TO fc27_app;
    END IF;
    IF to_regclass('public.player_decision_requests') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.player_decision_requests TO fc27_app;
    END IF;
    IF to_regclass('public.player_decisions') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.player_decisions TO fc27_app;
    END IF;
    IF to_regclass('public.approval_escalations') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.approval_escalations TO fc27_app;
    END IF;
    IF to_regclass('public.authority_conflicts') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.authority_conflicts TO fc27_app;
    END IF;
END $$;
```

---

## 5. `src/db_migrate.py` GRANTS_POLICY Update

`src/db_migrate.py` maintains cryptographic tamper verification over `migrations/post/grants.sql`. When editing `grants.sql`, the version and expected SHA-256 hash must be updated in tandem.

### Required Code Change in `src/db_migrate.py`:
```python
# Before (Version 2):
# GRANTS_POLICY = GrantsPolicy(version=2, sha256="a0c8d37ed61daff8900b116f992fa791f5072c11862c17c264cbc9251f0f4f79")

# After (Version 3):
GRANTS_POLICY = GrantsPolicy(version=3, sha256="<computed_sha256_of_version_3_grants_sql>")
```

---

## 6. Concurrency & Global Lock Order Analysis

To eliminate deadlocks between the background expiry daemon (`expire_due`) and concurrent decision execution (`apply_decision`), the strict global lock hierarchy (Spec §7.6) must be observed across all transactions:

1. **Lock Nonces (`public.approval_nonces`):**
   Lock all active nonces for the target thread in ascending order by `nonce`:
   `SELECT nonce FROM public.approval_nonces WHERE thread_id = %s AND consumed_at IS NULL ORDER BY nonce FOR UPDATE;`
2. **Lock Approval Request (`public.approval_requests`):**
   Lock the specific approval request:
   `SELECT * FROM public.approval_requests WHERE approval_request_id = %s FOR UPDATE;`
   *(Reads that only inspect status use `FOR SHARE` in the exact same sequence).*
3. **Lock Thread (`public.graph_threads`):**
   Lock the execution thread record:
   `SELECT * FROM public.graph_threads WHERE thread_id = %s FOR UPDATE;`
4. **Lock Dispatches (`public.dispatches`):**
   (Applies only in Milestone 4 when dispatch creation occurs inside the SECURITY DEFINER transition function).

### Expiry vs Decision Race Resolution
If `apply_decision` and `expire_due` arrive at the deadline simultaneously:
- Whichever acquires the nonces first wins the entire sequence.
- If `expire_due` wins: it sets request status to `expired`, thread status to `expired`, and revokes nonces. When `apply_decision` acquires the lock, it observes `status = 'expired'` and fails safely with HTTP 409 `approval_expired`.
- If `apply_decision` wins: it consumes the nonce and commits `approved`/`rejected`. When `expire_due` acquires the lock, it re-verifies `status = 'awaiting_approval'`, finds that it is already terminal, and takes no action.
- In neither case can a deadlock or corrupt intermediate state occur.

---

## 7. Downstream Implementation Checklist for Implementer

When the implementation agent is dispatched, the following steps must be followed:

- [ ] **1. Append Section B to `migrations/0003_dispatch_foundation.sql`:**
  - Create table `public.approval_requests` with constraints.
  - Create function and trigger `fc27_guard_approval_request_transition`.
  - Alter `public.graph_threads` (add `approval_request_id`, update status CHECK, update terminal fields CHECK).
  - Execute legacy backfill loop on `graph_threads`.
  - Replace trigger function `public.fc27_guard_thread_transition`.
  - Create function and constraint triggers `public.fc27_check_approval_pair`.
  - Create view `public.fc27_approval_inconsistencies`.
  - Alter `public.approval_audit` (extend decision check, add Task 4 & Section 1A fields).
  - Alter `public.approval_nonces` (add revoked_at, subject_type, dispatch_id, user_id, gate_id, update checks).
  - Replace trigger function `public.fc27_check_event_source`.
  - Create Section 1A tables: `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, `approval_escalations`, `authority_conflicts`.
- [ ] **2. Update `migrations/post/grants.sql`:**
  - Bump header to `-- grants-policy-version: 3`.
  - Add grants for `fc27_app` on all new relations.
- [ ] **3. Update `src/db_migrate.py`:**
  - Compute SHA-256 of updated `grants.sql`.
  - Update `GRANTS_POLICY` constant with version `3` and new SHA-256 hash.
- [ ] **4. Update Python Persistence in `src/approval_store.py`:**
  - Link `approval_requests` creation in `complete_run`.
  - Enforce global lock order and consistency verification in `apply_decision` and `expire_due`.
- [ ] **5. Execute Verification Tests:**
  - Run `python -m pytest -o addopts="" -q tests/test_db_migrate_roles.py`
  - Run `python -m pytest -o addopts="" -q tests/test_migration_0003.py`
  - Run `python -m pytest -o addopts="" -q tests/test_approval_lifecycle.py`
  - Confirm zero non-legacy inconsistencies in `fc27_approval_inconsistencies`.
