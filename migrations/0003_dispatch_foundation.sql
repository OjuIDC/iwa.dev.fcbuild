-- Phase 1. This file grows across Tasks 3-8 and must not be applied to a deployed database before the Phase 1 PR merges.

-- Section A: event index, outbox, append-only infrastructure

CREATE TABLE public.domain_events (
    event_id bigserial PRIMARY KEY,
    event_type text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    entity_version int,
    actor text NOT NULL,
    from_status text,
    to_status text,
    reason text,
    correlation_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    source_table text CHECK (source_table IN ('approval_audit','approval_requests','dispatches','dispatch_acknowledgments','dispatch_assessments','dispatch_plans','dispatch_plan_decisions','dispatch_delivery_attempts','user_roles','activation_gate_log')),
    source_id text,
    scope_club_id text,
    scope_player_id text,
    visibility text NOT NULL CHECK (visibility IN ('staff','player_owner','audit_only','ops')),
    fc_version text NOT NULL DEFAULT '27',
    platform text NOT NULL DEFAULT 'common-gen5',
    policy_version text,
    code_revision text,
    CHECK ((source_table IS NULL) = (source_id IS NULL))
);

CREATE TABLE public.outbox (
    outbox_id bigserial PRIMARY KEY,
    domain_event_id bigint NOT NULL UNIQUE REFERENCES public.domain_events(event_id),
    channel text NOT NULL DEFAULT 'fc27:events',
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    attempts int NOT NULL DEFAULT 0,
    last_error text,
    locked_until timestamptz
);

CREATE OR REPLACE FUNCTION public.fc27_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE check_violation USING MESSAGE = TG_TABLE_NAME || ' is append-only';
END;
$$;

CREATE TRIGGER fc27_domain_events_immutable
BEFORE UPDATE OR DELETE ON public.domain_events
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

CREATE TRIGGER fc27_approval_audit_immutable
BEFORE UPDATE OR DELETE ON public.approval_audit
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

CREATE OR REPLACE FUNCTION public.fc27_check_event_source()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Transition-only event types (C-10).
    -- Rationale:
    -- inside the SECURITY DEFINER transition function, current_user is the function owner (fc27_owner) while session_user is the caller's login role;
    -- fc27_app and fc27_dispatch are not members of fc27_owner, so for them current_user can be fc27_owner only inside an owner-owned definer function;
    -- a migrator session that runs SET ROLE fc27_owner has session_user = 'fc27_migrator' and is rejected.
    IF (NEW.event_type LIKE 'dispatch.%' AND NEW.event_type != 'dispatch.created') OR NEW.event_type LIKE 'plan.%' OR NEW.event_type LIKE 'tracking.%' THEN
        IF current_user != 'fc27_owner' OR session_user NOT IN ('fc27_app', 'fc27_dispatch') THEN
            RAISE EXCEPTION 'transition-only event type not allowed outside transition function';
        END IF;
    END IF;

    IF NEW.event_type = 'approval.decided' THEN
        IF NEW.source_table IS DISTINCT FROM 'approval_audit' THEN
            RAISE EXCEPTION 'approval.decided requires source_table = approval_audit';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'approval.decided requires a source_id';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM public.approval_audit WHERE id = NEW.source_id::bigint AND thread_id = NEW.entity_id) THEN
            RAISE EXCEPTION 'source row not found or entity mismatch';
        END IF;
    ELSIF NEW.event_type IN ('approval.created', 'approval.expired') THEN
        IF NEW.source_table IS DISTINCT FROM 'approval_requests' THEN
            RAISE EXCEPTION 'event_type requires source_table = approval_requests';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
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

CREATE TRIGGER fc27_domain_events_source_check
BEFORE INSERT ON public.domain_events
FOR EACH ROW EXECUTE FUNCTION public.fc27_check_event_source();


-- Section B: Authoritative approval lifecycle, pair consistency, and Section 1A compatibility

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

CREATE OR REPLACE FUNCTION public.fc27_guard_approval_request_transition()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('approved', 'rejected', 'revision_requested', 'expired') THEN
            RAISE EXCEPTION 'approval_request is terminal and frozen'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- Freeze terminal rows
    IF OLD.status IN ('approved', 'rejected', 'revision_requested', 'expired') THEN
        RAISE EXCEPTION 'approval_request is terminal and frozen'
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

-- Alter graph_threads
ALTER TABLE public.graph_threads
    ADD COLUMN IF NOT EXISTS approval_request_id uuid UNIQUE REFERENCES public.approval_requests(approval_request_id);

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

ALTER TABLE public.graph_threads
    DROP CONSTRAINT IF EXISTS graph_threads_terminal_fields_chk;

ALTER TABLE public.graph_threads
    ADD CONSTRAINT graph_threads_terminal_fields_chk
    CHECK (
        (status IN ('approved', 'rejected', 'revision_requested'))
        = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)
    );

-- Legacy Data Backfill
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

-- Replacement fc27_guard_thread_transition trigger function
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

-- Paired Consistency Constraint Trigger
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
            RAISE EXCEPTION 'approval pair: awaiting thread requires link'
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
                RAISE EXCEPTION 'approval pair: package_version mismatch between thread % and approval_request %',
                    NEW.thread_id, v_req.approval_request_id
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
            RAISE EXCEPTION 'approval pair: package_version mismatch between approval_request % and thread %',
                NEW.approval_request_id, v_thread.thread_id
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

-- Diagnostic view fc27_approval_inconsistencies
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

-- Section 1A Compatibility Tables

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
    CHECK (allow_player_fallback = false OR scope = 'player_specific'),
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

-- Extensions to approval_audit
ALTER TABLE public.approval_audit
    DROP CONSTRAINT IF EXISTS approval_audit_decision_check;
ALTER TABLE public.approval_audit
    DROP CONSTRAINT IF EXISTS approval_audit_decision_chk;

ALTER TABLE public.approval_audit
    ADD CONSTRAINT approval_audit_decision_chk
    CHECK (decision IN ('APPROVE', 'REJECT', 'REQUEST_REVISION'));

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

ALTER TABLE public.approval_audit
    ADD COLUMN IF NOT EXISTS actor_type text NOT NULL DEFAULT 'commissioner'
        CHECK (actor_type IN ('commissioner', 'delegate', 'player', 'system')),
    ADD COLUMN IF NOT EXISTS actor_id text NULL,
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

-- Extensions to approval_nonces
ALTER TABLE public.approval_nonces
    ADD COLUMN IF NOT EXISTS revoked_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS subject_type text NOT NULL DEFAULT 'thread',
    ADD COLUMN IF NOT EXISTS dispatch_id uuid NULL,
    ADD COLUMN IF NOT EXISTS user_id uuid NULL,
    ADD COLUMN IF NOT EXISTS gate_id text NULL;

ALTER TABLE public.approval_nonces
    ALTER COLUMN thread_id DROP NOT NULL,
    ALTER COLUMN package_version DROP NOT NULL;

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

-- Trigger function fc27_check_event_source update
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


-- Section C: Players, Dispatches & Forward-Compatible Tracking Schema

CREATE TABLE IF NOT EXISTS public.players (
    player_id text PRIMARY KEY CHECK (player_id ~ '^[0-9]{1,20}$'),
    club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$'),
    platform text NOT NULL DEFAULT 'common-gen5' CHECK (platform = 'common-gen5'),
    display_name text CHECK (char_length(display_name) <= 64),
    squad_number integer NULL CHECK (squad_number IS NULL OR (squad_number BETWEEN 1 AND 99)),
    active boolean NOT NULL DEFAULT true,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'suspended')),
    identity_verified_at timestamptz NULL,
    identity_evidence_ref text NULL CHECK (identity_evidence_ref IS NULL OR char_length(identity_evidence_ref) <= 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT players_status_active_sync_chk CHECK ((status = 'active') = active),
    CONSTRAINT players_club_squad_number_uniq UNIQUE (club_id, squad_number)
);

CREATE INDEX IF NOT EXISTS players_club_idx 
    ON public.players(club_id);

CREATE INDEX IF NOT EXISTS players_club_active_idx 
    ON public.players(club_id, active);

CREATE INDEX IF NOT EXISTS players_verified_idx 
    ON public.players(player_id) 
    WHERE identity_verified_at IS NOT NULL;

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
    dispatch_effective boolean NOT NULL DEFAULT true,
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
    last_transition_reason text,
    outcome text CHECK (outcome IS NULL OR outcome IN ('target_met', 'improving', 'no_change', 'regressed')),
    final_assessment_id uuid,
    superseded_by uuid REFERENCES public.dispatches(dispatch_id) DEFERRABLE INITIALLY DEFERRED,
    created_by_principal text NOT NULL CHECK (char_length(created_by_principal) <= 64),
    created_correlation_id uuid NOT NULL,
    fc_version text NOT NULL DEFAULT '27' CHECK (fc_version = '27'),
    platform text NOT NULL DEFAULT 'common-gen5' CHECK (platform = 'common-gen5')
);

CREATE UNIQUE INDEX IF NOT EXISTS dispatches_one_active_per_objective 
    ON public.dispatches (
        player_id, 
        owner, 
        (approved_instruction->'objective'->>'metric')
    ) 
    WHERE dispatch_status NOT IN ('completed', 'expired', 'cancelled', 'superseded');

CREATE INDEX IF NOT EXISTS dispatches_status_idx 
    ON public.dispatches(dispatch_status);

CREATE INDEX IF NOT EXISTS dispatches_player_idx 
    ON public.dispatches(player_id);

CREATE OR REPLACE FUNCTION public.fc27_guard_dispatch_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Initial status MUST be pending
    IF NEW.dispatch_status <> 'pending' THEN
        RAISE EXCEPTION 'new dispatch must have status pending (initial status must be pending)'
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

    -- Provenance field immutability (17 fields)
    IF NEW.dispatch_id <> OLD.dispatch_id THEN
        RAISE EXCEPTION 'immutable provenance: dispatch_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.source_thread_id <> OLD.source_thread_id THEN
        RAISE EXCEPTION 'immutable provenance: source_thread_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approval_id <> OLD.approval_id THEN
        RAISE EXCEPTION 'immutable provenance: approval_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approval_request_id <> OLD.approval_request_id THEN
        RAISE EXCEPTION 'immutable provenance: approval_request_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approval_payload_hash <> OLD.approval_payload_hash THEN
        RAISE EXCEPTION 'immutable provenance: approval_payload_hash is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.package_version <> OLD.package_version THEN
        RAISE EXCEPTION 'immutable provenance: package_version is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.player_id <> OLD.player_id THEN
        RAISE EXCEPTION 'immutable provenance: player_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.candidate_id <> OLD.candidate_id THEN
        RAISE EXCEPTION 'immutable provenance: candidate_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.owner <> OLD.owner THEN
        RAISE EXCEPTION 'immutable provenance: owner is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.selection <> OLD.selection THEN
        RAISE EXCEPTION 'immutable provenance: selection is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.club_id <> OLD.club_id THEN
        RAISE EXCEPTION 'immutable provenance: club_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'immutable provenance: created_at is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_by_principal <> OLD.created_by_principal THEN
        RAISE EXCEPTION 'immutable provenance: created_by_principal is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.created_correlation_id <> OLD.created_correlation_id THEN
        RAISE EXCEPTION 'immutable provenance: created_correlation_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.fc_version <> OLD.fc_version THEN
        RAISE EXCEPTION 'immutable provenance: fc_version is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.platform <> OLD.platform THEN
        RAISE EXCEPTION 'immutable provenance: platform is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.approved_instruction IS DISTINCT FROM OLD.approved_instruction THEN
        RAISE EXCEPTION 'immutable provenance: approved_instruction is immutable' USING ERRCODE = 'check_violation';
    END IF;

    -- Terminal Row Freeze: once terminal, no updates permitted (except setting superseded_by once)
    IF OLD.dispatch_status IN ('completed', 'expired', 'cancelled', 'superseded') THEN
        IF OLD.superseded_by IS NULL AND NEW.superseded_by IS NOT NULL AND
           NEW.dispatch_status = OLD.dispatch_status AND
           NEW.version = OLD.version THEN
            RETURN NEW;
        END IF;
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

-- Forward-Compatible Tracking Tables

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

CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key text NOT NULL,
    principal text NOT NULL,
    route text NOT NULL,
    request_sha256 text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, route, key)
);

-- Update fc27_check_event_source with full source verifications
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
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatches 
             WHERE dispatch_id = NEW.source_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found for dispatch.created';
        END IF;

    ELSIF NEW.event_type = 'dispatch.acknowledged' THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_acknowledgments' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_acknowledgments';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatch_acknowledgments 
             WHERE dispatch_id = NEW.source_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found for dispatch.acknowledged';
        END IF;

    ELSIF NEW.event_type IN ('tracking.updated', 'tracking.completed') THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_assessments' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_assessments';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatch_assessments 
             WHERE assessment_id = NEW.source_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found for tracking assessment event';
        END IF;

    ELSIF NEW.event_type IN ('dispatch.delivered', 'plan.requires_reapproval') THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_plans' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_plans';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;


-- Section D: Transition Rules Matrix & Hardened Single Write Path Function

CREATE TABLE IF NOT EXISTS public.dispatch_transition_rules (
    from_status text NOT NULL,
    to_status text NOT NULL,
    db_role name NOT NULL,
    actor_kind text NOT NULL,
    require_reason boolean NOT NULL DEFAULT false,
    PRIMARY KEY (from_status, to_status, db_role, actor_kind)
);

ALTER TABLE public.dispatch_transition_rules OWNER TO fc27_owner;

INSERT INTO public.dispatch_transition_rules (from_status, to_status, db_role, actor_kind, require_reason)
VALUES
    -- Worker (fc27_dispatch / service) edges:
    ('pending',              'queued',               'fc27_dispatch', 'service',      false),
    ('queued',               'delivered',            'fc27_dispatch', 'service',      false),
    ('queued',               'requires_reapproval',  'fc27_dispatch', 'service',      false),
    ('queued',               'failed',               'fc27_dispatch', 'service',      true),
    ('delivered',            'expired',              'fc27_dispatch', 'service',      true),
    ('acknowledged',         'tracking',             'fc27_dispatch', 'service',      false),
    ('tracking',             'completed',            'fc27_dispatch', 'service',      false),
    ('tracking',             'expired',              'fc27_dispatch', 'service',      true),

    -- App (fc27_app) edges:
    ('requires_reapproval',  'delivered',            'fc27_app',      'commissioner', false),
    ('requires_reapproval',  'cancelled',            'fc27_app',      'commissioner', true),
    ('failed',               'queued',               'fc27_app',      'commissioner', false),
    ('failed',               'queued',               'fc27_app',      'user',         false),
    ('delivered',            'acknowledged',         'fc27_app',      'user',         false),

    -- Commissioner administrative cancellation:
    ('pending',              'cancelled',            'fc27_app',      'commissioner', true),
    ('queued',               'cancelled',            'fc27_app',      'commissioner', true),
    ('delivered',            'cancelled',            'fc27_app',      'commissioner', true),
    ('acknowledged',         'cancelled',            'fc27_app',      'commissioner', true),
    ('tracking',             'cancelled',            'fc27_app',      'commissioner', true),
    ('failed',               'cancelled',            'fc27_app',      'commissioner', true),

    -- Commissioner supersession in approval transaction:
    ('pending',              'superseded',           'fc27_app',      'commissioner', false),
    ('queued',               'superseded',           'fc27_app',      'commissioner', false),
    ('delivered',            'superseded',           'fc27_app',      'commissioner', false),
    ('acknowledged',         'superseded',           'fc27_app',      'commissioner', false),
    ('tracking',             'superseded',           'fc27_app',      'commissioner', false),
    ('requires_reapproval',  'superseded',           'fc27_app',      'commissioner', false),
    ('failed',               'superseded',           'fc27_app',      'commissioner', false)
ON CONFLICT (from_status, to_status, db_role, actor_kind) DO UPDATE SET
    require_reason = EXCLUDED.require_reason;

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
    v_rule public.dispatch_transition_rules%ROWTYPE;
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
    ELSIF session_user IN ('fc27_owner', 'postgres') THEN
        v_actor := COALESCE(NULLIF(current_setting('fc27.actor', true), ''), 'service:graph-api');
        IF v_actor LIKE 'commissioner:%' THEN
            v_actor_kind := 'commissioner';
        ELSIF v_actor = 'service:dispatch-worker' THEN
            v_actor_kind := 'service';
        ELSE
            v_actor_kind := 'service';
        END IF;
    ELSE
        RAISE EXCEPTION 'insufficient privilege for session_user %', session_user USING ERRCODE = 'FC004';
    END IF;

    -- 2. Validate Correlation ID
    IF p_correlation_id IS NULL THEN
        RAISE EXCEPTION 'null correlation ID' USING ERRCODE = 'FC005';
    END IF;

    -- 3. Validate Reason Length
    IF p_reason IS NOT NULL AND char_length(p_reason) > 500 THEN
        RAISE EXCEPTION 'reason exceeds maximum length of 500 characters' USING ERRCODE = 'FC007';
    END IF;

    -- 4. Phase 1 Invariant Enforcement: Block transition to delivered
    IF p_to_status = 'delivered' THEN
        RAISE EXCEPTION 'Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)'
            USING ERRCODE = 'FC002';
    END IF;

    -- 5. Lock Dispatch Row and Check Optimistic Concurrency
    SELECT * INTO v_dispatch
      FROM public.dispatches
     WHERE dispatch_id = p_dispatch_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'dispatch % not found', p_dispatch_id USING ERRCODE = 'FC001';
    END IF;

    -- Terminal Row Freeze
    IF v_dispatch.dispatch_status IN ('completed', 'expired', 'cancelled', 'superseded') THEN
        RAISE EXCEPTION 'illegal transition: dispatch % is in terminal state (%) and frozen',
            p_dispatch_id, v_dispatch.dispatch_status USING ERRCODE = 'FC002';
    END IF;

    -- Optimistic Concurrency Checks
    IF p_expected_status IS NOT NULL AND v_dispatch.dispatch_status <> p_expected_status THEN
        RAISE EXCEPTION 'status conflict on dispatch %: expected %, found %',
            p_dispatch_id, p_expected_status, v_dispatch.dispatch_status USING ERRCODE = 'FC001';
    END IF;

    IF p_expected_version IS NOT NULL AND v_dispatch.version <> p_expected_version THEN
        RAISE EXCEPTION 'version conflict on dispatch %: expected %, found %',
            p_dispatch_id, p_expected_version, v_dispatch.version USING ERRCODE = 'FC001';
    END IF;

    IF v_dispatch.dispatch_status = p_to_status THEN
        RAISE EXCEPTION 'illegal transition: dispatch % is already in status %',
            p_dispatch_id, p_to_status USING ERRCODE = 'FC002';
    END IF;

    -- 6. Validate Transition Rule in Allowlist
    SELECT * INTO v_rule
      FROM public.dispatch_transition_rules
     WHERE from_status = v_dispatch.dispatch_status
       AND to_status = p_to_status
       AND (db_role = session_user::name OR session_user IN ('fc27_owner', 'postgres'))
       AND actor_kind = v_actor_kind
     LIMIT 1;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'transition from % to % not permitted for role % and actor_kind %',
            v_dispatch.dispatch_status, p_to_status, session_user, v_actor_kind USING ERRCODE = 'FC002';
    END IF;

    IF v_rule.require_reason AND (p_reason IS NULL OR char_length(trim(p_reason)) = 0) THEN
        RAISE EXCEPTION 'reason is required for transition from % to %',
            v_dispatch.dispatch_status, p_to_status USING ERRCODE = 'FC007';
    END IF;

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

    -- 8. Validate Patch Keys & Types Per Target Edge
    IF p_to_status = 'queued' THEN
        IF p_patch <> '{}'::jsonb THEN
            RAISE EXCEPTION 'patch not allowed for queued' USING ERRCODE = 'FC003';
        END IF;

    ELSIF p_to_status = 'requires_reapproval' THEN
        IF NOT (p_patch ? 'plan_version') OR jsonb_typeof(p_patch->'plan_version') <> 'number' THEN
            RAISE EXCEPTION 'requires_reapproval requires integer plan_version' USING ERRCODE = 'FC003';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('plan_version')) > 0 THEN
            RAISE EXCEPTION 'unexpected patch key for requires_reapproval' USING ERRCODE = 'FC003';
        END IF;
        v_plan_version := (p_patch->>'plan_version')::integer;
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatch_plans
             WHERE dispatch_id = p_dispatch_id
               AND plan_version = v_plan_version
               AND status = 'material_change'
        ) THEN
            RAISE EXCEPTION 'material_change plan missing for version %', v_plan_version USING ERRCODE = 'FC006';
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
        IF (SELECT count(*) FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('failure_reason', 'last_error')) > 0 THEN
            RAISE EXCEPTION 'unexpected patch key for failed' USING ERRCODE = 'FC003';
        END IF;
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
            RAISE EXCEPTION 'acknowledgment record missing for plan_version %', v_dispatch.plan_version USING ERRCODE = 'FC006';
        END IF;

    ELSIF p_to_status = 'tracking' THEN
        IF NOT (p_patch ? 'baseline_metrics') OR jsonb_typeof(p_patch->'baseline_metrics') <> 'object' THEN
            RAISE EXCEPTION 'tracking requires baseline_metrics object' USING ERRCODE = 'FC003';
        END IF;
        IF p_patch ? 'baseline_match_ids' AND jsonb_typeof(p_patch->'baseline_match_ids') <> 'array' THEN
            RAISE EXCEPTION 'baseline_match_ids must be array' USING ERRCODE = 'FC003';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('baseline_metrics', 'baseline_match_ids')) > 0 THEN
            RAISE EXCEPTION 'unexpected patch key for tracking' USING ERRCODE = 'FC003';
        END IF;

    ELSIF p_to_status = 'completed' THEN
        IF NOT (p_patch ? 'final_assessment_id') THEN
            RAISE EXCEPTION 'completed requires final_assessment_id' USING ERRCODE = 'FC003';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('final_assessment_id')) > 0 THEN
            RAISE EXCEPTION 'unexpected patch key for completed' USING ERRCODE = 'FC003';
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
        IF (SELECT count(*) FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('expiry_reason')) > 0 THEN
            RAISE EXCEPTION 'unexpected patch key for expired' USING ERRCODE = 'FC003';
        END IF;

    ELSIF p_to_status = 'cancelled' THEN
        IF NOT (p_patch ? 'cancel_reason') THEN
            RAISE EXCEPTION 'cancelled requires cancel_reason' USING ERRCODE = 'FC003';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('cancel_reason')) > 0 THEN
            RAISE EXCEPTION 'unexpected patch key for cancelled' USING ERRCODE = 'FC003';
        END IF;

    ELSIF p_to_status = 'superseded' THEN
        IF NOT (p_patch ? 'superseded_by') THEN
            RAISE EXCEPTION 'superseded requires superseded_by' USING ERRCODE = 'FC003';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(p_patch) k WHERE k NOT IN ('superseded_by')) > 0 THEN
            RAISE EXCEPTION 'unexpected patch key for superseded' USING ERRCODE = 'FC003';
        END IF;
        IF EXISTS (SELECT 1 FROM public.dispatches WHERE dispatch_id = (p_patch->>'superseded_by')::uuid) THEN
            RAISE EXCEPTION 'superseded_by id already exists' USING ERRCODE = 'FC003';
        END IF;

    ELSE
        IF p_patch <> '{}'::jsonb THEN
            RAISE EXCEPTION 'unexpected patch for status %', p_to_status USING ERRCODE = 'FC003';
        END IF;
    END IF;

    -- 9. Apply Updates
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
           last_transition_reason = p_reason,
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

    -- 10. Determine Event Type & Visibility
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
        ELSE
            v_event_type := 'dispatch.transitioned';
            v_visibility := 'staff';
            v_source_table := NULL;
            v_source_id := NULL;
    END CASE;

    -- 11. Insert Domain Event and Outbox Entry Atomically
    INSERT INTO public.domain_events (
        event_type, entity_type, entity_id, entity_version,
        actor, from_status, to_status, reason, correlation_id,
        source_table, source_id, scope_club_id, scope_player_id,
        visibility, fc_version, platform
    ) VALUES (
        v_event_type, 'dispatch', p_dispatch_id::text, v_new_version,
        v_actor, v_dispatch.dispatch_status, p_to_status, p_reason, p_correlation_id,
        v_source_table, v_source_id, v_dispatch.club_id, v_dispatch.player_id,
        v_visibility, '27', 'common-gen5'
    ) RETURNING event_id INTO v_event_id;

    INSERT INTO public.outbox (domain_event_id, channel)
    VALUES (v_event_id, 'fc27:events');

    RETURN v_new_version;
END;
$$;

ALTER FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
    OWNER TO fc27_owner;

REVOKE ALL ON FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
    TO fc27_app, fc27_dispatch;


-- ==============================================================================
-- Section E: Activation Gates, Service Heartbeats & Event Source Alignment
-- ==============================================================================

-- 1. Activation Gate Log (append-only)
CREATE TABLE IF NOT EXISTS public.activation_gate_log (
    id bigserial PRIMARY KEY,
    gate_id text NOT NULL CHECK (gate_id IN ('G-ID', 'G-ATOMIC', 'G-OUTBOX')),
    status text NOT NULL CHECK (status IN ('passed', 'failed')),
    evidence_ref text NOT NULL CHECK (char_length(evidence_ref) BETWEEN 1 AND 500),
    code_revision text NOT NULL,
    deployment_id text NOT NULL,
    fc_version text NOT NULL CHECK (fc_version = '27'),
    platform text NOT NULL CHECK (platform = 'common-gen5'),
    nonce text NOT NULL,
    signature_sha256 text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

-- Immutability trigger: prevent UPDATE and DELETE on activation_gate_log
CREATE TRIGGER fc27_activation_gate_log_immutable
    BEFORE UPDATE OR DELETE ON public.activation_gate_log
    FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- Indexes for activation_gate_log
CREATE INDEX IF NOT EXISTS activation_gate_log_gate_recorded_idx
    ON public.activation_gate_log (gate_id, recorded_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS activation_gate_log_deployment_idx
    ON public.activation_gate_log (code_revision, deployment_id, gate_id, recorded_at DESC);


-- 2. Activation Gates View (latest record per gate_id)
CREATE OR REPLACE VIEW public.activation_gates AS
SELECT DISTINCT ON (gate_id)
    id,
    gate_id,
    status,
    evidence_ref,
    code_revision,
    deployment_id,
    fc_version,
    platform,
    nonce,
    signature_sha256,
    recorded_by,
    recorded_at
FROM public.activation_gate_log
ORDER BY gate_id, recorded_at DESC, id DESC;


-- 3. Service Heartbeats Table
CREATE TABLE IF NOT EXISTS public.service_heartbeats (
    service_name text PRIMARY KEY,
    last_heartbeat timestamptz NOT NULL DEFAULT now(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    service text GENERATED ALWAYS AS (service_name) STORED,
    seen_at timestamptz GENERATED ALWAYS AS (last_heartbeat) STORED
);

CREATE INDEX IF NOT EXISTS service_heartbeats_last_idx
    ON public.service_heartbeats (service_name, last_heartbeat DESC);


-- 4. Update Event Source Trigger to validate activation.gate_recorded
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

    ELSIF NEW.event_type = 'activation.gate_recorded' THEN
        IF NEW.source_table IS DISTINCT FROM 'activation_gate_log' THEN
            RAISE EXCEPTION 'activation.gate_recorded requires source_table = activation_gate_log';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'activation.gate_recorded requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.activation_gate_log
             WHERE id = NEW.source_id::bigint
               AND gate_id = NEW.entity_id
        ) THEN
            RAISE EXCEPTION 'source row not found or entity mismatch for activation.gate_recorded';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

