# Milestone 3 Schema & Triggers Analysis: Part C and Part D

**Document**: `schema_analysis.md`  
**Agent**: `explorer_m3_schema_1`  
**Milestone**: Milestone 3: Dispatch Schema & Single-Write-Path Transition  
**Date**: 2026-10-03  
**Status**: COMPLETE EXPLORATION & TECHNICAL DESIGN  

---

## 1. Executive Summary & Architectural Scope

Milestone 3 establishes the database schema, immutability guards, and single-write-path state transition machinery for approved player recommendation dispatches. This investigation analyzes the complete Data Definition Language (DDL), constraint definitions, trigger functions, rule matrices, and least-privilege role permissions required for:
- **Part C**: Core player and dispatch entities (`public.players`, `public.dispatches`), objective modeling, and forward-compatible tracking schemas (`public.dispatch_plans`, `public.player_observations`, `public.dispatch_assessments`, etc.).
- **Part D**: The single write path SECURITY DEFINER transition function `public.fc27_dispatch_transition`, transition rules matrix `public.dispatch_transition_rules`, event source validation extensions, and PostgreSQL grant policies.

### Core Non-Negotiable Invariants
1. **Phase 1 Boundary**: `DISPATCH_ENABLED = false` at all times in operational deployment. Milestone 3 provides the authoritative database foundation and state machine constraints; it does *not* execute player delivery.
2. **PostgreSQL as Authoritative Source of Truth**: All dispatch lifecycles, states, and transitions are governed by PostgreSQL ACID transactions, immutable triggers, and strict referential constraints.
3. **No Direct Mutation on Dispatches**: The application runtime role (`fc27_app`) is granted `SELECT` and `INSERT` (to atomically create the initial `pending` dispatch inside the Commissioner approval transaction), but **ZERO** direct `UPDATE` or `DELETE` permissions. All status changes MUST pass through the SECURITY DEFINER function `public.fc27_dispatch_transition`.
4. **Worker Isolation**: The dispatch worker role (`fc27_dispatch`) has `SELECT` only on `dispatches` (no direct write capability) and must invoke `public.fc27_dispatch_transition` to progress items through worker lifecycle stages.
5. **Deterministic Provenance and Terminal Immutability**: All provenance fields (`dispatch_id`, `source_thread_id`, `approval_id`, `approval_request_id`, `player_id`, `candidate_id`, `owner`, `selection`, `club_id`, `approved_instruction`, etc.) are frozen forever upon row creation. Terminal rows (`completed`, `expired`, `cancelled`, `superseded`) are immutable against modification or deletion.

---

## 2. DDL Design: `public.players` Table

The `public.players` table anchors the stable EA Sports FC player identity, club association, verification posture, and platform scoping.

### 2.1 Schema Definition

```sql
CREATE TABLE IF NOT EXISTS public.players (
    player_id text PRIMARY KEY CHECK (player_id ~ '^[0-9]{1,20}$'),
    club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$'),
    platform text NOT NULL DEFAULT 'common-gen5' CHECK (platform = 'common-gen5'),
    display_name text CHECK (char_length(display_name) <= 64),
    active boolean NOT NULL DEFAULT true,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'suspended')),
    identity_verified_at timestamptz NULL,
    identity_evidence_ref text NULL CHECK (identity_evidence_ref IS NULL OR char_length(identity_evidence_ref) <= 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT players_status_active_sync_chk CHECK ((status = 'active') = active)
);
```

### 2.2 Technical Analysis & Constraints
1. **Primary Key (`player_id`)**:
   - `CHECK (player_id ~ '^[0-9]{1,20}$')` strictly enforces numeric string representation (up to 20 ASCII digits) matching EA match data structures (`match.players[<clubId>]`).
   - Prevents use of gamertags or non-numeric identifiers as identities (Specification §13.1, Acceptance Criteria AC-46). Exact string equality is enforced; leading-zero variants are treated distinctly and must match EA records.
2. **Club Scoping (`club_id`)**:
   - `CHECK (club_id ~ '^[0-9]+$')` binds players strictly to numeric club identifiers, ensuring tenant boundary enforcement and event projection isolation (§17.4).
3. **Platform & Scope Tags**:
   - `platform text NOT NULL DEFAULT 'common-gen5' CHECK (platform = 'common-gen5')` ensures standard cross-platform generation tagging across all entities.
4. **Display Name (`display_name`)**:
   - Bounded presentation string (`<= 64` chars). Treated strictly as untrusted presentation metadata; never used for authentication or authorization.
5. **Status & Active Synchronization**:
   - Both `active boolean` (required by Spec §14.3) and `status text` (requested in exploration brief) are supported.
   - `CONSTRAINT players_status_active_sync_chk CHECK ((status = 'active') = active)` guarantees 100% semantic consistency between boolean and text queries.
6. **Identity Verification (`identity_verified_at`, `identity_evidence_ref`)**:
   - Prerequisite for dispatchability under Activation Gate `G-ID` (Spec §13.1, §31.2). A candidate recommendation cannot be dispatched if `identity_verified_at` is NULL.
7. **Indexes**:
```sql
CREATE INDEX IF NOT EXISTS players_club_idx 
    ON public.players(club_id);

CREATE INDEX IF NOT EXISTS players_club_active_idx 
    ON public.players(club_id, active);

CREATE INDEX IF NOT EXISTS players_verified_idx 
    ON public.players(player_id) 
    WHERE identity_verified_at IS NOT NULL;
```

---

## 3. DDL Design: `public.dispatches` Table

The `public.dispatches` table is the central authoritative entity governing the lifecycle of approved recommendation instructions for specific players.

### 3.1 Schema Definition

```sql
CREATE TABLE IF NOT EXISTS public.dispatches (
    dispatch_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_thread_id text NOT NULL REFERENCES public.graph_threads(thread_id),
    approval_id bigint UNIQUE NOT NULL REFERENCES public.approval_audit(id),
    approval_request_id uuid NOT NULL REFERENCES public.approval_requests(approval_request_id),
    approval_payload_hash text NOT NULL CHECK (approval_payload_hash ~ '^[0-9a-f]{64}$'),
    package_version text NOT NULL CHECK (package_version ~ '^[0-9a-f]{64}$'),
    player_id text NOT NULL REFERENCES public.players(player_id),
    candidate_id text NOT NULL CHECK (candidate_id ~ '^[A-Za-z0-9._:-]{1,128}$'),
    owner text NOT NULL CHECK (owner IN ('scout', 'analyst', 'coach', 'tactics', 'director')),
    selection text NOT NULL CHECK (selection IN ('best', 'best_runner_up', 'middle', 'middle_runner_up', 'lowest', 'ignore')),
    club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$'),
    approved_instruction jsonb NOT NULL,
    plan_version integer NOT NULL DEFAULT 0 CHECK (plan_version >= 0),
    delivery_channels text[] NOT NULL DEFAULT '{dashboard,websocket}' CHECK (delivery_channels <@ ARRAY['dashboard','websocket','discord']::text[]),
    dispatch_status text NOT NULL CHECK (dispatch_status IN (
        'pending',
        'queued',
        'running',
        'paused',
        'delivered',
        'requires_reapproval',
        'failed',
        'acknowledged',
        'tracking',
        'completed',
        'expired',
        'cancelled',
        'superseded'
    )),
    status text GENERATED ALWAYS AS (dispatch_status) STORED,
    dispatch_effective boolean NOT NULL DEFAULT false,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    acknowledged_at timestamptz,
    tracking_started_at timestamptz,
    tracking_ended_at timestamptz,
    acknowledgment_deadline timestamptz,
    baseline_metrics jsonb,
    baseline_match_ids text[],
    target_metrics jsonb,
    observation_window jsonb,
    failure_reason text CHECK (failure_reason IS NULL OR failure_reason IN (
        'personalization_unavailable',
        'plan_schema_invalid',
        'plan_policy_invalid',
        'player_not_enrolled',
        'player_identity_unverified',
        'delivery_store_error'
    )),
    failure_retryable boolean,
    last_error text CHECK (last_error IS NULL OR char_length(last_error) <= 500),
    expiry_reason text CHECK (expiry_reason IS NULL OR expiry_reason IN ('ack_deadline', 'insufficient_data')),
    cancel_reason text CHECK (cancel_reason IS NULL OR char_length(cancel_reason) <= 500),
    outcome text CHECK (outcome IS NULL OR outcome IN ('target_met', 'improving', 'no_change', 'regressed')),
    final_assessment_id uuid,
    superseded_by uuid REFERENCES public.dispatches(dispatch_id) DEFERRABLE INITIALLY DEFERRED,
    created_by_principal text NOT NULL CHECK (char_length(created_by_principal) <= 64),
    created_correlation_id uuid NOT NULL,
    fc_version text NOT NULL DEFAULT '27' CHECK (fc_version = '27'),
    platform text NOT NULL DEFAULT 'common-gen5' CHECK (platform = 'common-gen5')
);
```

### 3.2 Technical Analysis & Reconciliation of Requirements

#### 3.2.1 Foreign Key & Provenance Bindings
- `source_thread_id`: References `graph_threads(thread_id)`. Links directly to the generating LangGraph execution thread.
- `approval_id`: `UNIQUE NOT NULL REFERENCES public.approval_audit(id)`. Ensures a 1:1 relationship between a committed Commissioner approval audit receipt and the created dispatch.
- `approval_request_id`: `NOT NULL REFERENCES public.approval_requests(approval_request_id)`. Binds to the authoritative approval lifecycle request.
- `player_id`: `NOT NULL REFERENCES public.players(player_id)`. Binds directly to the verified numeric player row.
- `superseded_by`: `DEFERRABLE INITIALLY DEFERRED REFERENCES public.dispatches(dispatch_id)`. Allows an existing active dispatch to point to a newly minted dispatch ID in the same transaction before that new row is inserted.

#### 3.2.2 Status Domain Reconciliation
The exploration assignment prompted for: `(pending, running, paused, delivered, failed, cancelled)`.  
The approved architectural specification (§11.3, §14.3) and Section 1A human authority model define:
- `pending`: Newly inserted in the approval transaction (plan_version 0).
- `queued`: Claimed by the dispatch worker for personalization. (Equivalent to `running` pipeline stage).
- `running`: Included as an explicit legal status in the domain check for compatibility with task orchestration callers.
- `paused`: Accommodates player-initiated pauses under Section 1A.3 / 1A.11 ("player pause: temporary prevention of activation").
- `delivered`: Plan stored and visible in dashboard.
- `requires_reapproval`: Personalization generated a valid material change requiring human re-authorization.
- `failed`: Personalization, schema validation, policy check, or delivery failed.
- `acknowledged`: Affected player submitted authenticated receipt/acknowledgment.
- `tracking`: Tracker has frozen baseline metrics and is ingesting matches.
- `completed`: Observation window finished with final assessment outcome.
- `expired`: Acknowledgment deadline elapsed or tracking window timed out without sufficient data.
- `cancelled`: Explicitly cancelled by authorized Commissioner.
- `superseded`: A newer approval for the same player, category, and objective has superseded this item.

**Unified Status Constraint**:
The CHECK constraint encompasses all 13 states. Furthermore, `status text GENERATED ALWAYS AS (dispatch_status) STORED` ensures that queries using either column name (`dispatch_status` or `status`) succeed transparently without performance penalty.

#### 3.2.3 `dispatch_effective` Column
`dispatch_effective boolean NOT NULL DEFAULT false` records the evaluation posture under which the dispatch record was instantiated.
- In production Phase 1: remains `false` because `DISPATCH_ENABLED = false` and rollout gates are blocked.
- In automated test suites (`enabled_client` fixture): records `true`, proving that gate evaluation was satisfied when the row was created.

#### 3.2.4 Partial Unique Index: Concurrency Control
```sql
CREATE UNIQUE INDEX IF NOT EXISTS dispatches_one_active_per_objective 
    ON public.dispatches (
        player_id, 
        owner, 
        (approved_instruction->'objective'->>'metric')
    ) 
    WHERE dispatch_status NOT IN ('completed', 'expired', 'cancelled', 'superseded');
```
This partial index guarantees that a player can have **at most one active dispatch per role category (`owner`) and metric objective**. If a new recommendation is approved for the same objective, the previous dispatch must transition to `superseded` in the same transaction.

---

## 4. Immutability Triggers & Transition Guards

Two specialized triggers protect `public.dispatches`:
1. `fc27_dispatch_insert_guard`: Enforces strict initial conditions on INSERT.
2. `fc27_dispatch_immutable`: Enforces provenance immutability and terminal row freezing on UPDATE/DELETE.

### 4.1 `fc27_dispatch_insert_guard` Trigger

```sql
CREATE OR REPLACE FUNCTION public.fc27_guard_dispatch_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Initial status MUST be pending
    IF NEW.dispatch_status <> 'pending' THEN
        RAISE EXCEPTION 'new dispatch must have status pending (was %)', NEW.dispatch_status
            USING ERRCODE = 'check_violation';
    END IF;

    -- Initial version MUST be 1
    IF NEW.version <> 1 THEN
        RAISE EXCEPTION 'new dispatch must have version 1 (was %)', NEW.version
            USING ERRCODE = 'check_violation';
    END IF;

    -- Initial plan_version MUST be 0
    IF NEW.plan_version <> 0 THEN
        RAISE EXCEPTION 'new dispatch must have plan_version 0 (was %)', NEW.plan_version
            USING ERRCODE = 'check_violation';
    END IF;

    -- Lifecycle timestamps MUST be NULL on creation
    IF NEW.delivered_at IS NOT NULL OR
       NEW.acknowledged_at IS NOT NULL OR
       NEW.tracking_started_at IS NOT NULL OR
       NEW.tracking_ended_at IS NOT NULL OR
       NEW.outcome IS NOT NULL OR
       NEW.final_assessment_id IS NOT NULL OR
       NEW.superseded_by IS NOT NULL THEN
        RAISE EXCEPTION 'new dispatch must have null lifecycle timestamps and outcome fields'
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_dispatch_insert_guard ON public.dispatches;
CREATE TRIGGER fc27_dispatch_insert_guard
BEFORE INSERT ON public.dispatches
FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_dispatch_insert();
```

### 4.2 `fc27_dispatch_immutable` Trigger

```sql
CREATE OR REPLACE FUNCTION public.fc27_guard_dispatch_immutable()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Prohibit DELETE completely
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'dispatches cannot be deleted'
            USING ERRCODE = 'check_violation';
    END IF;

    -- Provenance field immutability
    IF NEW.dispatch_id <> OLD.dispatch_id THEN
        RAISE EXCEPTION 'dispatch_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.source_thread_id <> OLD.source_thread_id THEN
        RAISE EXCEPTION 'source_thread_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approval_id <> OLD.approval_id THEN
        RAISE EXCEPTION 'approval_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approval_request_id <> OLD.approval_request_id THEN
        RAISE EXCEPTION 'approval_request_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approval_payload_hash <> OLD.approval_payload_hash THEN
        RAISE EXCEPTION 'approval_payload_hash is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.package_version <> OLD.package_version THEN
        RAISE EXCEPTION 'package_version is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.player_id <> OLD.player_id THEN
        RAISE EXCEPTION 'player_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.candidate_id <> OLD.candidate_id THEN
        RAISE EXCEPTION 'candidate_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.owner <> OLD.owner THEN
        RAISE EXCEPTION 'owner is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.selection <> OLD.selection THEN
        RAISE EXCEPTION 'selection is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.club_id <> OLD.club_id THEN
        RAISE EXCEPTION 'club_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'created_at is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_by_principal <> OLD.created_by_principal THEN
        RAISE EXCEPTION 'created_by_principal is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_correlation_id <> OLD.created_correlation_id THEN
        RAISE EXCEPTION 'created_correlation_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.fc_version <> OLD.fc_version THEN
        RAISE EXCEPTION 'fc_version is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.platform <> OLD.platform THEN
        RAISE EXCEPTION 'platform is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approved_instruction IS DISTINCT FROM OLD.approved_instruction THEN
        RAISE EXCEPTION 'approved_instruction is immutable' USING ERRCODE = 'check_violation';
    END IF;

    -- Terminal Row Freeze: once terminal, no updates permitted
    IF OLD.dispatch_status IN ('completed', 'expired', 'cancelled', 'superseded') THEN
        RAISE EXCEPTION 'dispatch % is terminal (%) and frozen from updates', OLD.dispatch_id, OLD.dispatch_status
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_dispatch_immutable ON public.dispatches;
CREATE TRIGGER fc27_dispatch_immutable
BEFORE UPDATE OR DELETE ON public.dispatches
FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_dispatch_immutable();
```

---

## 5. Forward-Compatible Tracking & Companion Schema

These tables prepare the persistent structure for Phase 3 (Dispatch Worker) and Phase 5 (Tracking & Analytics) without implementing runtime execution in Phase 1.

### 5.1 `public.dispatch_plans` (Personalized Plans)

Stores each version of a personalized plan generated by the logical player agent.

```sql
CREATE TABLE IF NOT EXISTS public.dispatch_plans (
    dispatch_id uuid NOT NULL REFERENCES public.dispatches(dispatch_id),
    plan_version integer NOT NULL CHECK (plan_version > 0),
    plan jsonb NOT NULL,
    plan_sha256 text NOT NULL CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('valid', 'material_change', 'invalid', 'schema_invalid')),
    validation jsonb NOT NULL DEFAULT '{}'::jsonb,
    model text,
    prompt_version text,
    policy_version text,
    code_revision text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dispatch_id, plan_version)
);

CREATE TRIGGER fc27_dispatch_plans_immutable
BEFORE UPDATE OR DELETE ON public.dispatch_plans
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();
```

### 5.2 `public.player_observations` (Match Ingestion)

Stores deterministic game observations per player and match.

```sql
CREATE TABLE IF NOT EXISTS public.player_observations (
    player_id text NOT NULL REFERENCES public.players(player_id),
    match_id text NOT NULL CHECK (char_length(match_id) BETWEEN 1 AND 128),
    match_type text NOT NULL DEFAULT 'competitive',
    match_ts timestamptz NOT NULL,
    metrics jsonb NOT NULL,
    source text NOT NULL DEFAULT 'mcp',
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, match_id)
);

CREATE TRIGGER fc27_player_observations_immutable
BEFORE UPDATE OR DELETE ON public.player_observations
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();
```

### 5.3 `public.dispatch_assessments` (Objective Tracking Assessments)

Stores periodic and final outcome assessments computed by tracker agents.

```sql
CREATE TABLE IF NOT EXISTS public.dispatch_assessments (
    assessment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_id uuid NOT NULL REFERENCES public.dispatches(dispatch_id),
    assessed_at timestamptz NOT NULL DEFAULT now(),
    result text NOT NULL CHECK (result IN (
        'tracking_error',
        'insufficient_data',
        'target_met',
        'regressed',
        'improving',
        'no_change'
    )),
    match_ids text[] NOT NULL DEFAULT '{}',
    calculated jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_final boolean NOT NULL DEFAULT false,
    explanation text,
    explanation_model text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dispatch_assessments_dispatch_idx 
    ON public.dispatch_assessments(dispatch_id, assessed_at);

CREATE TRIGGER fc27_dispatch_assessments_immutable
BEFORE UPDATE OR DELETE ON public.dispatch_assessments
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();
```

### 5.4 Companion Lifecycle & Delivery Tables

```sql
-- 5.4.1 Re-approval Decisions on Materially Changed Plans
CREATE TABLE IF NOT EXISTS public.dispatch_plan_decisions (
    decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_id uuid NOT NULL,
    plan_version integer NOT NULL,
    decision text NOT NULL CHECK (decision IN ('APPROVE', 'REJECT')),
    approval_nonce text NOT NULL,
    proof_type text NOT NULL DEFAULT 'hmac' CHECK (proof_type IN ('hmac', 'webauthn')),
    actor text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (dispatch_id, plan_version) REFERENCES public.dispatch_plans(dispatch_id, plan_version)
);
CREATE TRIGGER fc27_dispatch_plan_decisions_immutable
BEFORE UPDATE OR DELETE ON public.dispatch_plan_decisions
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- 5.4.2 Delivery Attempts
CREATE TABLE IF NOT EXISTS public.dispatch_delivery_attempts (
    attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_id uuid NOT NULL REFERENCES public.dispatches(dispatch_id),
    channel text NOT NULL CHECK (channel IN ('dashboard', 'websocket', 'discord')),
    plan_version integer NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    result text NOT NULL CHECK (result IN ('stored', 'pushed', 'no_subscriber', 'error')),
    error text,
    idempotency_key text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER fc27_dispatch_delivery_attempts_immutable
BEFORE UPDATE OR DELETE ON public.dispatch_delivery_attempts
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- 5.4.3 Player Acknowledgments
CREATE TABLE IF NOT EXISTS public.dispatch_acknowledgments (
    dispatch_id uuid PRIMARY KEY REFERENCES public.dispatches(dispatch_id),
    player_id text NOT NULL REFERENCES public.players(player_id),
    user_id uuid,
    session_id_sha256 text,
    plan_version integer NOT NULL,
    action_token_sha256 text UNIQUE,
    webauthn_credential_id bytea,
    acknowledged_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER fc27_dispatch_acknowledgments_immutable
BEFORE UPDATE OR DELETE ON public.dispatch_acknowledgments
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- 5.4.4 Dispatch Comments
CREATE TABLE IF NOT EXISTS public.dispatch_comments (
    comment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dispatch_id uuid NOT NULL REFERENCES public.dispatches(dispatch_id),
    user_id uuid,
    body text NOT NULL CHECK (char_length(body) <= 1000),
    request_coach_review boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER fc27_dispatch_comments_immutable
BEFORE UPDATE OR DELETE ON public.dispatch_comments
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- 5.4.5 Analyst Notes
CREATE TABLE IF NOT EXISTS public.analyst_notes (
    note_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    user_id uuid,
    body text NOT NULL CHECK (char_length(body) <= 4000),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER fc27_analyst_notes_immutable
BEFORE UPDATE OR DELETE ON public.analyst_notes
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- 5.4.6 Idempotency Keys (API / Action deduplication)
CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key text NOT NULL,
    principal text NOT NULL,
    route text NOT NULL,
    request_sha256 text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, route, key)
);
```

---

## 6. Single Write Path: `fc27_dispatch_transition` & Rule Matrix (Part D)

### 6.1 `public.dispatch_transition_rules` Matrix

Defines the permissible state machine edges parameterized by calling database role and actor kind.

```sql
CREATE TABLE IF NOT EXISTS public.dispatch_transition_rules (
    from_status text NOT NULL,
    to_status text NOT NULL,
    db_role name NOT NULL,
    actor_kind text NOT NULL,
    PRIMARY KEY (from_status, to_status, db_role, actor_kind)
);
```

#### Seed Matrix Entries
```sql
INSERT INTO public.dispatch_transition_rules (from_status, to_status, db_role, actor_kind)
VALUES
    -- Worker (fc27_dispatch / service) edges:
    ('pending',              'queued',               'fc27_dispatch', 'service'),
    ('queued',               'delivered',            'fc27_dispatch', 'service'),
    ('queued',               'requires_reapproval',  'fc27_dispatch', 'service'),
    ('queued',               'failed',               'fc27_dispatch', 'service'),
    ('delivered',            'expired',              'fc27_dispatch', 'service'),
    ('acknowledged',         'tracking',             'fc27_dispatch', 'service'),
    ('tracking',             'completed',            'fc27_dispatch', 'service'),
    ('tracking',             'expired',              'fc27_dispatch', 'service'),

    -- App (fc27_app / commissioner) edges:
    ('requires_reapproval',  'delivered',            'fc27_app',      'commissioner'),
    ('requires_reapproval',  'cancelled',            'fc27_app',      'commissioner'),
    ('failed',               'queued',               'fc27_app',      'commissioner'),
    ('failed',               'queued',               'fc27_app',      'user'),
    ('delivered',            'acknowledged',         'fc27_app',      'user'),

    -- Commissioner administrative cancellation:
    ('pending',              'cancelled',            'fc27_app',      'commissioner'),
    ('queued',               'cancelled',            'fc27_app',      'commissioner'),
    ('delivered',            'cancelled',            'fc27_app',      'commissioner'),
    ('acknowledged',         'cancelled',            'fc27_app',      'commissioner'),
    ('tracking',             'cancelled',            'fc27_app',      'commissioner'),
    ('failed',               'cancelled',            'fc27_app',      'commissioner'),

    -- Commissioner supersession in approval transaction:
    ('pending',              'superseded',           'fc27_app',      'commissioner'),
    ('queued',               'superseded',           'fc27_app',      'commissioner'),
    ('delivered',            'superseded',           'fc27_app',      'commissioner'),
    ('acknowledged',         'superseded',           'fc27_app',      'commissioner'),
    ('tracking',             'superseded',           'fc27_app',      'commissioner'),
    ('requires_reapproval',  'superseded',           'fc27_app',      'commissioner'),
    ('failed',               'superseded',           'fc27_app',      'commissioner')
ON CONFLICT DO NOTHING;
```

### 6.2 SQLSTATE Error Code Standard
The function maps domain errors to unambiguous, standardized SQLSTATE values:

| SQLSTATE | Semantic Meaning | Mapping in API |
|---|---|---|
| `FC001` | Optimistic concurrency conflict (version or status mismatch) | HTTP 409 Conflict |
| `FC002` | Illegal transition edge (not in `dispatch_transition_rules`) | HTTP 409 Conflict / 422 Unprocessable |
| `FC003` | Invalid or prohibited patch keys/types for the target edge | HTTP 422 Unprocessable |
| `FC004` | Invalid actor format, unauthorized actor kind, or missing proof | HTTP 403 Forbidden / 409 Conflict |
| `FC005` | NULL correlation ID parameter | HTTP 422 Unprocessable |
| `FC006` | Missing precondition row (acknowledgment, final assessment, or plan) | HTTP 409 Conflict |
| `FC007` | Transition reason string exceeds maximum length (500 chars) | HTTP 422 Unprocessable |

### 6.3 Transition Function Implementation

```sql
CREATE OR REPLACE FUNCTION public.fc27_dispatch_transition(
    p_dispatch_id uuid,
    p_expected_status text,
    p_expected_version integer,
    p_to_status text,
    p_reason text,
    p_correlation_id uuid,
    p_patch jsonb DEFAULT '{}'::jsonb
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_actor text;
    v_actor_kind text;
    v_dispatch public.dispatches%ROWTYPE;
    v_rule_exists boolean;
    v_new_version integer;
    v_event_id bigint;
    v_event_type text;
    v_visibility text;
    v_source_table text;
    v_source_id text;
    v_failure_retryable boolean;
    v_plan_version integer;
    v_final_assessment public.dispatch_assessments%ROWTYPE;
    v_has_audit boolean;
BEGIN
    -- 1. Derive Actor and Actor Kind
    IF session_user = 'fc27_dispatch' THEN
        v_actor := 'service:dispatch-worker';
        v_actor_kind := 'service';
        IF current_setting('fc27.actor', true) IS NOT NULL AND 
           current_setting('fc27.actor', true) <> '' AND 
           current_setting('fc27.actor', true) <> 'service:dispatch-worker' THEN
            RAISE EXCEPTION 'fc27_dispatch cannot set foreign actor' USING ERRCODE = 'FC004';
        END IF;
    ELSIF session_user = 'fc27_app' THEN
        v_actor := current_setting('fc27.actor', true);
        IF v_actor IS NULL OR v_actor = '' THEN
            RAISE EXCEPTION 'missing fc27.actor setting' USING ERRCODE = 'FC004';
        END IF;
        IF v_actor !~ '^(service:graph-api|commissioner:[A-Za-z0-9._-]{1,64}|user:[0-9a-f-]{36})$' THEN
            RAISE EXCEPTION 'malformed actor %', v_actor USING ERRCODE = 'FC004';
        END IF;
        IF v_actor LIKE 'user:%' THEN
            RAISE EXCEPTION 'user actors require Phase 2' USING ERRCODE = 'FC004';
        ELSIF v_actor LIKE 'commissioner:%' THEN
            v_actor_kind := 'commissioner';
        ELSE
            v_actor_kind := 'service';
        END IF;
    ELSE
        RAISE EXCEPTION 'insufficient privilege for session_user %', session_user USING ERRCODE = 'FC004';
    END IF;

    -- 2. Validate Correlation ID
    IF p_correlation_id IS NULL THEN
        RAISE EXCEPTION 'correlation_id cannot be null' USING ERRCODE = 'FC005';
    END IF;

    -- 3. Validate Reason Length
    IF p_reason IS NOT NULL AND char_length(p_reason) > 500 THEN
        RAISE EXCEPTION 'reason exceeds 500 characters' USING ERRCODE = 'FC007';
    END IF;

    -- 4. Lock Dispatch Row and Check Optimistic Concurrency
    SELECT * INTO v_dispatch
      FROM public.dispatches
     WHERE dispatch_id = p_dispatch_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'dispatch % not found', p_dispatch_id USING ERRCODE = 'FC001';
    END IF;

    IF v_dispatch.dispatch_status <> p_expected_status OR v_dispatch.version <> p_expected_version THEN
        RAISE EXCEPTION 'conflict on dispatch %: expected status %, version %; actual status %, version %',
            p_dispatch_id, p_expected_status, p_expected_version, v_dispatch.dispatch_status, v_dispatch.version
            USING ERRCODE = 'FC001';
    END IF;

    -- 5. Validate Transition Rule in Allowlist
    SELECT EXISTS (
        SELECT 1 FROM public.dispatch_transition_rules
         WHERE from_status = p_expected_status
           AND to_status = p_to_status
           AND db_role = session_user::name
           AND actor_kind = v_actor_kind
    ) INTO v_rule_exists;

    IF NOT v_rule_exists THEN
        RAISE EXCEPTION 'transition from % to % not permitted for role % and actor_kind %',
            p_expected_status, p_to_status, session_user, v_actor_kind
            USING ERRCODE = 'FC002';
    END IF;

    -- 6. Commissioner In-Transaction Proof
    IF v_actor_kind = 'commissioner' THEN
        IF p_to_status = 'superseded' THEN
            -- Check that approval_audit was inserted in the current transaction by this commissioner
            SELECT EXISTS (
                SELECT 1 FROM public.approval_audit
                 WHERE xmin = pg_current_xact_id()::xid
                   AND approved_by = substring(v_actor from 14) -- strip 'commissioner:'
            ) INTO v_has_audit;
            IF NOT v_has_audit THEN
                RAISE EXCEPTION 'supersede requires approval_audit committed in current transaction'
                    USING ERRCODE = 'FC004';
            END IF;
        ELSE
            -- In Phase 1, non-supersede commissioner edges raise FC004 (Phase 2 adds nonces/step-up)
            RAISE EXCEPTION 'commissioner edge to % deferred to Phase 2', p_to_status
                USING ERRCODE = 'FC004';
        END IF;
    END IF;

    -- 7. Validate Patch Keys & Types Per Target Edge
    IF p_to_status = 'queued' THEN
        IF p_patch <> '{}'::jsonb THEN
            RAISE EXCEPTION 'patch not allowed for queued' USING ERRCODE = 'FC003';
        END IF;

    ELSIF p_to_status = 'delivered' THEN
        IF NOT (p_patch ? 'plan_version') OR jsonb_typeof(p_patch->'plan_version') <> 'number' THEN
            RAISE EXCEPTION 'delivered requires integer plan_version' USING ERRCODE = 'FC003';
        END IF;
        v_plan_version := (p_patch->>'plan_version')::integer;
        IF p_expected_status = 'queued' THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.dispatch_plans
                 WHERE dispatch_id = p_dispatch_id
                   AND plan_version = v_plan_version
                   AND status = 'valid'
            ) THEN
                RAISE EXCEPTION 'valid dispatch_plan missing for version %', v_plan_version
                    USING ERRCODE = 'FC006';
            END IF;
        ELSIF p_expected_status = 'requires_reapproval' THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.dispatch_plans p
                 JOIN public.dispatch_plan_decisions d USING (dispatch_id, plan_version)
                WHERE p.dispatch_id = p_dispatch_id
                  AND p.plan_version = v_plan_version
                  AND p.status = 'material_change'
                  AND d.decision = 'APPROVE'
            ) THEN
                RAISE EXCEPTION 'approved material change plan missing for version %', v_plan_version
                    USING ERRCODE = 'FC006';
            END IF;
        END IF;

    ELSIF p_to_status = 'requires_reapproval' THEN
        IF NOT (p_patch ? 'plan_version') OR jsonb_typeof(p_patch->'plan_version') <> 'number' THEN
            RAISE EXCEPTION 'requires_reapproval requires integer plan_version' USING ERRCODE = 'FC003';
        END IF;
        v_plan_version := (p_patch->>'plan_version')::integer;
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatch_plans
             WHERE dispatch_id = p_dispatch_id
               AND plan_version = v_plan_version
               AND status = 'material_change'
        ) THEN
            RAISE EXCEPTION 'material_change plan missing for version %', v_plan_version
                USING ERRCODE = 'FC006';
        END IF;

    ELSIF p_to_status = 'failed' THEN
        IF NOT (p_patch ? 'failure_reason') OR 
           (p_patch->>'failure_reason') NOT IN (
               'personalization_unavailable', 'plan_schema_invalid', 'plan_policy_invalid',
               'player_not_enrolled', 'player_identity_unverified', 'delivery_store_error'
           ) THEN
            RAISE EXCEPTION 'failed requires valid failure_reason' USING ERRCODE = 'FC003';
        END IF;
        IF p_patch ? 'failure_retryable' THEN
            RAISE EXCEPTION 'failure_retryable is derived, cannot be patched' USING ERRCODE = 'FC003';
        END IF;
        -- Derive retryability
        IF (p_patch->>'failure_reason') IN ('plan_policy_invalid', 'player_identity_unverified') THEN
            v_failure_retryable := false;
        ELSE
            v_failure_retryable := true;
        END IF;

    ELSIF p_to_status = 'acknowledged' THEN
        IF p_patch <> '{}'::jsonb THEN
            RAISE EXCEPTION 'patch not allowed for acknowledged' USING ERRCODE = 'FC003';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatch_acknowledgments
             WHERE dispatch_id = p_dispatch_id
               AND plan_version = v_dispatch.plan_version
        ) THEN
            RAISE EXCEPTION 'acknowledgment record missing for plan_version %', v_dispatch.plan_version
                USING ERRCODE = 'FC006';
        END IF;

    ELSIF p_to_status = 'tracking' THEN
        IF NOT (p_patch ? 'baseline_metrics') OR jsonb_typeof(p_patch->'baseline_metrics') <> 'object' THEN
            RAISE EXCEPTION 'tracking requires baseline_metrics object' USING ERRCODE = 'FC003';
        END IF;
        IF p_patch ? 'baseline_match_ids' AND jsonb_typeof(p_patch->'baseline_match_ids') <> 'array' THEN
            RAISE EXCEPTION 'baseline_match_ids must be array' USING ERRCODE = 'FC003';
        END IF;

    ELSIF p_to_status = 'completed' THEN
        IF NOT (p_patch ? 'final_assessment_id') THEN
            RAISE EXCEPTION 'completed requires final_assessment_id' USING ERRCODE = 'FC003';
        END IF;
        SELECT * INTO v_final_assessment
          FROM public.dispatch_assessments
         WHERE assessment_id = (p_patch->>'final_assessment_id')::uuid
           AND dispatch_id = p_dispatch_id
           AND is_final = true;
        IF NOT FOUND OR v_final_assessment.result = 'insufficient_data' THEN
            RAISE EXCEPTION 'valid is_final assessment missing for completed' USING ERRCODE = 'FC006';
        END IF;

    ELSIF p_to_status = 'expired' THEN
        IF NOT (p_patch ? 'expiry_reason') OR 
           (p_patch->>'expiry_reason') NOT IN ('ack_deadline', 'insufficient_data') THEN
            RAISE EXCEPTION 'expired requires valid expiry_reason' USING ERRCODE = 'FC003';
        END IF;

    ELSIF p_to_status = 'cancelled' THEN
        IF NOT (p_patch ? 'cancel_reason') THEN
            RAISE EXCEPTION 'cancelled requires cancel_reason' USING ERRCODE = 'FC003';
        END IF;

    ELSIF p_to_status = 'superseded' THEN
        IF NOT (p_patch ? 'superseded_by') THEN
            RAISE EXCEPTION 'superseded requires superseded_by uuid' USING ERRCODE = 'FC003';
        END IF;
        -- Superseded_by must NOT already exist in dispatches (new row inserted right after)
        IF EXISTS (SELECT 1 FROM public.dispatches WHERE dispatch_id = (p_patch->>'superseded_by')::uuid) THEN
            RAISE EXCEPTION 'superseded_by id already exists' USING ERRCODE = 'FC003';
        END IF;
    END IF;

    -- 8. Apply Updates
    v_new_version := v_dispatch.version + 1;

    UPDATE public.dispatches
       SET dispatch_status = p_to_status,
           version = v_new_version,
           plan_version = CASE 
               WHEN p_to_status = 'delivered' THEN (p_patch->>'plan_version')::integer 
               ELSE plan_version 
           END,
           delivered_at = CASE 
               WHEN p_to_status = 'delivered' THEN now() 
               ELSE delivered_at 
           END,
           acknowledged_at = CASE 
               WHEN p_to_status = 'acknowledged' THEN now() 
               ELSE acknowledged_at 
           END,
           tracking_started_at = CASE 
               WHEN p_to_status = 'tracking' THEN now() 
               ELSE tracking_started_at 
           END,
           tracking_ended_at = CASE 
               WHEN p_to_status IN ('completed', 'expired') THEN now() 
               ELSE tracking_ended_at 
           END,
           baseline_metrics = CASE 
               WHEN p_to_status = 'tracking' THEN p_patch->'baseline_metrics' 
               ELSE baseline_metrics 
           END,
           baseline_match_ids = CASE 
               WHEN p_to_status = 'tracking' AND (p_patch ? 'baseline_match_ids') 
               THEN ARRAY(SELECT jsonb_array_elements_text(p_patch->'baseline_match_ids')) 
               ELSE baseline_match_ids 
           END,
           failure_reason = CASE 
               WHEN p_to_status = 'failed' THEN p_patch->>'failure_reason' 
               ELSE failure_reason 
           END,
           failure_retryable = CASE 
               WHEN p_to_status = 'failed' THEN v_failure_retryable 
               ELSE failure_retryable 
           END,
           last_error = CASE 
               WHEN p_to_status = 'failed' AND (p_patch ? 'last_error') THEN p_patch->>'last_error' 
               ELSE last_error 
           END,
           expiry_reason = CASE 
               WHEN p_to_status = 'expired' THEN p_patch->>'expiry_reason' 
               ELSE expiry_reason 
           END,
           cancel_reason = CASE 
               WHEN p_to_status = 'cancelled' THEN p_patch->>'cancel_reason' 
               ELSE cancel_reason 
           END,
           outcome = CASE 
               WHEN p_to_status = 'completed' THEN v_final_assessment.result 
               ELSE outcome 
           END,
           final_assessment_id = CASE 
               WHEN p_to_status = 'completed' THEN (p_patch->>'final_assessment_id')::uuid 
               ELSE final_assessment_id 
           END,
           superseded_by = CASE 
               WHEN p_to_status = 'superseded' THEN (p_patch->>'superseded_by')::uuid 
               ELSE superseded_by 
           END
     WHERE dispatch_id = p_dispatch_id;

    -- 9. Determine Event Type & Visibility
    CASE p_to_status
        WHEN 'queued' THEN
            v_event_type := 'dispatch.queued';
            v_visibility := 'player_owner';
            v_source_table := NULL;
            v_source_id := NULL;
        WHEN 'delivered' THEN
            v_event_type := 'dispatch.delivered';
            v_visibility := 'player_owner';
            v_source_table := 'dispatch_plans';
            v_source_id := p_dispatch_id || ':' || (p_patch->>'plan_version');
        WHEN 'requires_reapproval' THEN
            v_event_type := 'plan.requires_reapproval';
            v_visibility := 'staff';
            v_source_table := 'dispatch_plans';
            v_source_id := p_dispatch_id || ':' || (p_patch->>'plan_version');
        WHEN 'failed' THEN
            v_event_type := 'dispatch.failed';
            v_visibility := 'staff';
            v_source_table := NULL;
            v_source_id := NULL;
        WHEN 'acknowledged' THEN
            v_event_type := 'dispatch.acknowledged';
            v_visibility := 'player_owner';
            v_source_table := 'dispatch_acknowledgments';
            v_source_id := p_dispatch_id::text;
        WHEN 'tracking' THEN
            v_event_type := 'tracking.started';
            v_visibility := 'player_owner';
            v_source_table := NULL;
            v_source_id := NULL;
        WHEN 'completed' THEN
            v_event_type := 'tracking.completed';
            v_visibility := 'player_owner';
            v_source_table := 'dispatch_assessments';
            v_source_id := p_patch->>'final_assessment_id';
        WHEN 'expired' THEN
            v_event_type := 'dispatch.expired';
            v_visibility := 'player_owner';
            v_source_table := NULL;
            v_source_id := NULL;
        WHEN 'cancelled' THEN
            v_event_type := 'dispatch.cancelled';
            v_visibility := 'player_owner';
            v_source_table := NULL;
            v_source_id := NULL;
        WHEN 'superseded' THEN
            v_event_type := 'dispatch.superseded';
            v_visibility := 'staff';
            v_source_table := NULL;
            v_source_id := NULL;
    END CASE;

    -- 10. Insert Domain Event and Outbox Entry Atomically
    INSERT INTO public.domain_events (
        event_type, entity_type, entity_id, entity_version,
        actor, from_status, to_status, reason, correlation_id,
        source_table, source_id, scope_club_id, scope_player_id,
        visibility, fc_version, platform
    ) VALUES (
        v_event_type, 'dispatch', p_dispatch_id::text, v_new_version,
        v_actor, p_expected_status, p_to_status, p_reason, p_correlation_id,
        v_source_table, v_source_id, v_dispatch.club_id, v_dispatch.player_id,
        v_visibility, '27', 'common-gen5'
    ) RETURNING event_id INTO v_event_id;

    INSERT INTO public.outbox (domain_event_id, channel)
    VALUES (v_event_id, 'fc27:events');

    RETURN v_new_version;
END;
$$;

-- Ownership & Execution Privileges
ALTER FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
    OWNER TO fc27_owner;

REVOKE ALL ON FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
    TO fc27_app, fc27_dispatch;
```

---

## 7. Least-Privilege Role Permissions (`migrations/post/grants.sql`)

Milestone 3 updates `migrations/post/grants.sql` to grant the minimum required table and function privileges to `fc27_app` and `fc27_dispatch`.

### 7.1 Detailed Privilege Breakdown

| Schema Object | `fc27_app` (Graph API) | `fc27_dispatch` (Worker) | Security Rationale |
|---|---|---|---|
| `public.players` | `SELECT, INSERT` | `SELECT` | Graph API seeds/queries players. Worker queries players for job validation. |
| `public.dispatches` | `SELECT, INSERT` | `SELECT` | **NO UPDATE/DELETE**: Graph API inserts initial pending dispatch in approval transaction. Worker reads pending/delivered items. All updates MUST use transition function. |
| `public.dispatch_transition_rules` | `SELECT` | `SELECT` | Both roles query valid transition rules. Owned by `fc27_owner`. |
| `public.dispatch_plans` | `SELECT` | `SELECT, INSERT` | Worker persists generated personalized plans. Graph API projects plans. |
| `public.dispatch_delivery_attempts` | `SELECT` | `SELECT, INSERT` | Worker logs delivery attempt results. |
| `public.player_observations` | `SELECT` | `SELECT, INSERT` | Worker ingests match observations. |
| `public.dispatch_assessments` | `SELECT` | `SELECT, INSERT` | Worker records assessment outcomes. |
| `public.dispatch_acknowledgments` | `SELECT, INSERT` | `SELECT` | Graph API records player acknowledgment. Worker verifies prior to tracking. |
| `public.dispatch_plan_decisions` | `SELECT, INSERT` | `SELECT` | Graph API commits Commissioner re-approval decision. Worker verifies prior to delivery. |
| `public.dispatch_comments` | `SELECT, INSERT` | `SELECT` | Graph API records staff/coach comments. |
| `public.analyst_notes` | `SELECT, INSERT` | `SELECT` | Graph API records notes. |
| `public.idempotency_keys` | `SELECT, INSERT, UPDATE` | `none` | Graph API request deduplication cache. |
| `public.domain_events` | `SELECT, INSERT` | `SELECT` | `fc27_dispatch` CANNOT insert domain events directly. All worker events emanate from `fc27_dispatch_transition`. |
| `public.outbox` | `INSERT` | `SELECT, UPDATE` | Graph API enqueues outbox records. Worker claims rows via `FOR UPDATE SKIP LOCKED` and updates `published_at`. |
| `public.fc27_dispatch_transition` | `EXECUTE` | `EXECUTE` | Both roles execute permitted state machine transitions. |

### 7.2 Updated Grants Policy Script (`migrations/post/grants.sql`)

```sql
-- grants-policy-version: 4
DO $$
BEGIN
    -- Existing M0-M2 Grants...
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
        GRANT SELECT ON public.outbox TO fc27_dispatch;
        GRANT UPDATE (published_at, attempts, last_error, locked_until) ON public.outbox TO fc27_dispatch;
    END IF;
    IF to_regclass('public.outbox_outbox_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.outbox_outbox_id_seq TO fc27_app;
    END IF;

    IF to_regclass('public.approval_requests') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.approval_requests TO fc27_app;
    END IF;
    IF to_regclass('public.fc27_approval_inconsistencies') IS NOT NULL THEN
        GRANT SELECT ON public.fc27_approval_inconsistencies TO fc27_app;
    END IF;

    -- Milestone 3 Part C & D Grants
    IF to_regclass('public.players') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.players TO fc27_app;
        GRANT SELECT ON public.players TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatches') IS NOT NULL THEN
        -- CRITICAL: NO UPDATE, NO DELETE on dispatches for any runtime role!
        GRANT SELECT, INSERT ON public.dispatches TO fc27_app;
        GRANT SELECT ON public.dispatches TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_transition_rules') IS NOT NULL THEN
        GRANT SELECT ON public.dispatch_transition_rules TO fc27_app, fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_plans') IS NOT NULL THEN
        GRANT SELECT ON public.dispatch_plans TO fc27_app;
        GRANT SELECT, INSERT ON public.dispatch_plans TO fc27_dispatch;
    END IF;

    IF to_regclass('public.player_observations') IS NOT NULL THEN
        GRANT SELECT ON public.player_observations TO fc27_app;
        GRANT SELECT, INSERT ON public.player_observations TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_assessments') IS NOT NULL THEN
        GRANT SELECT ON public.dispatch_assessments TO fc27_app;
        GRANT SELECT, INSERT ON public.dispatch_assessments TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_delivery_attempts') IS NOT NULL THEN
        GRANT SELECT ON public.dispatch_delivery_attempts TO fc27_app;
        GRANT SELECT, INSERT ON public.dispatch_delivery_attempts TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_acknowledgments') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.dispatch_acknowledgments TO fc27_app;
        GRANT SELECT ON public.dispatch_acknowledgments TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_plan_decisions') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.dispatch_plan_decisions TO fc27_app;
        GRANT SELECT ON public.dispatch_plan_decisions TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_comments') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.dispatch_comments TO fc27_app;
        GRANT SELECT ON public.dispatch_comments TO fc27_dispatch;
    END IF;

    IF to_regclass('public.analyst_notes') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.analyst_notes TO fc27_app;
        GRANT SELECT ON public.analyst_notes TO fc27_dispatch;
    END IF;

    IF to_regclass('public.idempotency_keys') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.idempotency_keys TO fc27_app;
    END IF;

    -- Function Execution Grants
    IF to_regprocedure('public.fc27_dispatch_transition(uuid,text,integer,text,text,uuid,jsonb)') IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION public.fc27_dispatch_transition(uuid,text,integer,text,text,uuid,jsonb) 
            TO fc27_app, fc27_dispatch;
    END IF;
END $$;
```

---

## 8. Summary Comparison & Verification Strategy

| Design Aspect | Current Baseline (0003 Section A/B) | Milestone 3 Target Design |
|---|---|---|
| `players` Table | Non-existent | Created with strict numeric `player_id`, club scoping, platform defaults, sync check |
| `dispatches` Table | Non-existent | Created with 1:1 `approval_id`, complete status domain, versioning, correlation ID, partial unique index |
| Direct Updates on Dispatches | N/A | Forbidden for `fc27_app` and `fc27_dispatch` via DB privileges and immutability trigger |
| Transition Engine | Direct SQL updates | Hardened SECURITY DEFINER function `public.fc27_dispatch_transition` with rule matrix and atomic event emission |
| Forward Tracking Schema | Non-existent | Append-only tracking, plan, and assessment tables created for forward compatibility |
| Grants Policy Version | Version 3 | Bumped to Version 4 with sha256 lock in `src/db_migrate.py` |

The design presented here fully prepares Milestone 3 Part C and Part D for implementation without violating any Phase 1 constraints or modifying production source code.
