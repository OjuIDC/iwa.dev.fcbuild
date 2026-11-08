-- 0005_delegations.sql: Phase 2 Milestone 2 Delegations Schema Adaptation
-- Non-destructively adapts public.authority_delegations for club scoping,
-- relaxed scope formatting, and adds public.fc27_delegations compatibility view.

-- 1. Extend authority_delegations table
ALTER TABLE public.authority_delegations
    ADD COLUMN IF NOT EXISTS club_id text NOT NULL DEFAULT '1899' CHECK (club_id ~ '^[0-9]+$'),
    ADD COLUMN IF NOT EXISTS starts_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS target_player_id text NULL CHECK (target_player_id IS NULL OR target_player_id ~ '^[0-9]{1,20}$'),
    ADD COLUMN IF NOT EXISTS target_package_type text NULL;

-- 2. Relax scope constraint to permit descriptive scopes (e.g. 'tactical_drills', 'club', 'player_specific')
ALTER TABLE public.authority_delegations
    DROP CONSTRAINT IF EXISTS authority_delegations_scope_check;

ALTER TABLE public.authority_delegations
    ADD CONSTRAINT authority_delegations_scope_check
    CHECK (char_length(scope) BETWEEN 1 AND 64);

-- 3. Add time window check (expires_at > starts_at AND expires_at > issued_at)
ALTER TABLE public.authority_delegations
    DROP CONSTRAINT IF EXISTS authority_delegations_time_window_chk;

ALTER TABLE public.authority_delegations
    ADD CONSTRAINT authority_delegations_time_window_chk
    CHECK (expires_at > starts_at AND expires_at > issued_at);

-- 4. Index for active delegations by club and delegate
CREATE INDEX IF NOT EXISTS authority_delegations_club_delegate_idx
    ON public.authority_delegations(club_id, delegate_id, expires_at)
    WHERE revoked_at IS NULL;

-- 5. Compatibility View public.fc27_delegations
CREATE OR REPLACE VIEW public.fc27_delegations AS
    SELECT 
        delegation_id,
        club_id,
        delegator_id,
        delegate_id,
        delegate_role,
        scope,
        allowed_action_types,
        target_player_id,
        target_package_type,
        starts_at,
        expires_at,
        issued_at,
        revoked_at,
        revoked_by,
        revocation_reason,
        created_at
    FROM public.authority_delegations;

-- 6. Expand domain_events check constraint to accept authority_delegations & fc27_delegations
ALTER TABLE public.domain_events DROP CONSTRAINT IF EXISTS domain_events_source_table_check;
ALTER TABLE public.domain_events ADD CONSTRAINT domain_events_source_table_check
CHECK (source_table IN (
    'approval_audit',
    'approval_requests',
    'dispatches',
    'dispatch_acknowledgments',
    'dispatch_assessments',
    'dispatch_plans',
    'dispatch_plan_decisions',
    'dispatch_delivery_attempts',
    'user_roles',
    'activation_gate_log',
    'fc27_users',
    'fc27_user_roles',
    'fc27_invitations',
    'fc27_sessions',
    'fc27_webauthn_credentials',
    'fc27_totp_credentials',
    'fc27_recovery_codes',
    'fc27_action_tokens',
    'fc27_challenges',
    'authority_delegations',
    'fc27_delegations'
));
