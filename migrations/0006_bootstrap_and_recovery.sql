-- 0006_bootstrap_and_recovery.sql: Phase 2 Milestone 4 Bootstrap and Recovery Schema
-- Establishes public.fc27_bootstrap_locks with permanent immutability,
-- adds public.fc27_credentials view, adds login_identifier generated column,
-- and updates domain_events check constraint.

-- 1. Generated column login_identifier on fc27_users for compatibility
ALTER TABLE public.fc27_users
    ADD COLUMN IF NOT EXISTS login_identifier text GENERATED ALWAYS AS (login_id) STORED;

-- 2. fc27_bootstrap_locks table
CREATE TABLE IF NOT EXISTS public.fc27_bootstrap_locks (
    lock_id text PRIMARY KEY DEFAULT 'commissioner_bootstrap',
    commissioner_user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE RESTRICT,
    club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$'),
    bootstrapped_at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL DEFAULT 'system',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT single_bootstrap_lock CHECK (lock_id = 'commissioner_bootstrap')
);

-- Permanent immutability: Prevent UPDATE and DELETE
DROP TRIGGER IF EXISTS fc27_bootstrap_locks_immutable ON public.fc27_bootstrap_locks;
CREATE TRIGGER fc27_bootstrap_locks_immutable
    BEFORE UPDATE OR DELETE ON public.fc27_bootstrap_locks
    FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- 3. Compatibility View public.fc27_credentials
CREATE OR REPLACE VIEW public.fc27_credentials AS
SELECT
    credential_id,
    user_id,
    public_key,
    sign_counter AS sign_count,
    label,
    aaguid,
    created_at,
    last_used_at,
    revoked_at
FROM public.fc27_webauthn_credentials;

-- 4. Expand domain_events check constraint to accept fc27_bootstrap_locks
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
    'fc27_delegations',
    'fc27_bootstrap_locks'
));
