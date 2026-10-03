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
