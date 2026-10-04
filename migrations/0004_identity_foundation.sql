-- Phase 2 Milestone 1: Identity Data Foundation
-- Defines authoritative users, roles, invitations, server-side sessions,
-- WebAuthn/TOTP credentials, recovery material, action tokens, and challenges.

-- 1. fc27_users -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_users (
    user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$'),
    login_id text NOT NULL UNIQUE CHECK (char_length(login_id) BETWEEN 3 AND 255 AND login_id = lower(trim(login_id))),
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 128),
    status text NOT NULL DEFAULT 'invited' CHECK (status IN ('invited', 'active', 'suspended', 'recovery_required', 'disabled')),
    player_id text NULL REFERENCES public.players(player_id) ON DELETE SET NULL CHECK (player_id IS NULL OR player_id ~ '^[0-9]{1,20}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz NULL,
    suspended_at timestamptz NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fc27_users_club_status_idx 
    ON public.fc27_users(club_id, status);

CREATE INDEX IF NOT EXISTS fc27_users_player_id_idx 
    ON public.fc27_users(player_id) 
    WHERE player_id IS NOT NULL;

-- Guard for user status transitions, immutability of identifiers, and automatic timestamps
CREATE OR REPLACE FUNCTION public.fc27_guard_user_status_transition()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Immutable identifiers
        IF NEW.user_id <> OLD.user_id THEN
            RAISE check_violation USING MESSAGE = 'user_id is immutable';
        END IF;
        IF NEW.club_id <> OLD.club_id THEN
            RAISE check_violation USING MESSAGE = 'club_id is immutable';
        END IF;
        IF NEW.created_at <> OLD.created_at THEN
            RAISE check_violation USING MESSAGE = 'created_at is immutable';
        END IF;

        -- Status transition state machine
        IF OLD.status = 'disabled' AND NEW.status <> 'disabled' THEN
            RAISE check_violation USING MESSAGE = 'disabled user account cannot be reactivated';
        END IF;
        IF OLD.status = 'invited' AND NEW.status NOT IN ('invited', 'active', 'disabled') THEN
            RAISE check_violation USING MESSAGE = 'invited account can only transition to active or disabled';
        END IF;

        -- Auto-maintain activated_at
        IF OLD.status = 'invited' AND NEW.status = 'active' AND NEW.activated_at IS NULL THEN
            NEW.activated_at = now();
        END IF;

        -- Auto-maintain suspended_at
        IF NEW.status = 'suspended' AND OLD.status <> 'suspended' AND NEW.suspended_at IS NULL THEN
            NEW.suspended_at = now();
        ELSIF NEW.status <> 'suspended' AND OLD.status = 'suspended' THEN
            NEW.suspended_at = NULL;
        END IF;

        NEW.updated_at = now();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_users_status_guard ON public.fc27_users;
CREATE TRIGGER fc27_users_status_guard
    BEFORE UPDATE ON public.fc27_users
    FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_user_status_transition();


-- 2. fc27_user_roles --------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_user_roles (
    user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN (
        'commissioner',
        'commissioner_delegate',
        'coach',
        'analyst',
        'staff',
        'player',
        'bff_service',
        'graph_service'
    )),
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by text NOT NULL CHECK (char_length(granted_by) BETWEEN 1 AND 128),
    PRIMARY KEY (user_id, role)
);

CREATE INDEX IF NOT EXISTS fc27_user_roles_role_idx 
    ON public.fc27_user_roles(role, user_id);


-- 3. fc27_invitations -------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_invitations (
    invitation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$'),
    intended_role text NOT NULL CHECK (intended_role IN (
        'commissioner',
        'commissioner_delegate',
        'coach',
        'analyst',
        'staff',
        'player',
        'bff_service',
        'graph_service'
    )),
    invited_email text NOT NULL CHECK (char_length(invited_email) BETWEEN 3 AND 255 AND invited_email = lower(trim(invited_email))),
    creator_id text NOT NULL CHECK (char_length(creator_id) BETWEEN 1 AND 128),
    issued_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NULL,
    revoked_at timestamptz NULL,
    CHECK (expires_at > issued_at),
    CHECK (consumed_at IS NULL OR revoked_at IS NULL)
);

CREATE INDEX IF NOT EXISTS fc27_invitations_club_role_idx 
    ON public.fc27_invitations(club_id, intended_role);

CREATE INDEX IF NOT EXISTS fc27_invitations_active_idx 
    ON public.fc27_invitations(token_hash, expires_at) 
    WHERE consumed_at IS NULL AND revoked_at IS NULL;


-- 4. fc27_sessions ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_sessions (
    session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
    auth_level text NOT NULL CHECK (auth_level IN ('authenticated', 'mfa_totp', 'step_up_passkey')),
    auth_method text NOT NULL CHECK (auth_method IN ('passkey', 'totp', 'service_token', 'recovery_code')),
    ip_address text NULL CHECK (ip_address IS NULL OR char_length(ip_address) <= 64),
    user_agent text NULL CHECK (user_agent IS NULL OR char_length(user_agent) <= 512),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NOT NULL DEFAULT now(),
    idle_expires_at timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at timestamptz NULL,
    CHECK (absolute_expires_at > created_at),
    CHECK (idle_expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS fc27_sessions_user_idx 
    ON public.fc27_sessions(user_id);

CREATE INDEX IF NOT EXISTS fc27_sessions_active_idx 
    ON public.fc27_sessions(token_hash, idle_expires_at, absolute_expires_at) 
    WHERE revoked_at IS NULL;


-- 5. fc27_webauthn_credentials ----------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_webauthn_credentials (
    credential_id bytea PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
    public_key bytea NOT NULL,
    sign_counter bigint NOT NULL DEFAULT 0 CHECK (sign_counter >= 0),
    label text NOT NULL DEFAULT 'Passkey' CHECK (char_length(label) BETWEEN 1 AND 128),
    aaguid uuid NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NULL,
    revoked_at timestamptz NULL
);

CREATE INDEX IF NOT EXISTS fc27_webauthn_credentials_user_idx 
    ON public.fc27_webauthn_credentials(user_id) 
    WHERE revoked_at IS NULL;

-- Sign counter monotonicity guard against cloned authenticators
CREATE OR REPLACE FUNCTION public.fc27_guard_webauthn_counter()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.sign_counter < OLD.sign_counter THEN
            RAISE check_violation USING MESSAGE = 'webauthn sign_counter cannot decrease';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_webauthn_counter_guard ON public.fc27_webauthn_credentials;
CREATE TRIGGER fc27_webauthn_counter_guard
    BEFORE UPDATE ON public.fc27_webauthn_credentials
    FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_webauthn_counter();


-- 6. fc27_totp_credentials --------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_totp_credentials (
    user_id uuid PRIMARY KEY REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
    encrypted_seed text NOT NULL CHECK (char_length(encrypted_seed) >= 16),
    key_version integer NOT NULL DEFAULT 1 CHECK (key_version >= 1),
    status text NOT NULL DEFAULT 'enrolled' CHECK (status IN ('enrolled', 'active', 'revoked')),
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    last_verified_at timestamptz NULL,
    revoked_at timestamptz NULL,
    CHECK (revoked_at IS NULL OR status = 'revoked')
);


-- 7. fc27_recovery_codes ----------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_recovery_codes (
    code_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
    code_hash text NOT NULL CHECK (char_length(code_hash) >= 32),
    consumed_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fc27_recovery_codes_user_idx 
    ON public.fc27_recovery_codes(user_id);

CREATE INDEX IF NOT EXISTS fc27_recovery_codes_active_idx 
    ON public.fc27_recovery_codes(user_id) 
    WHERE consumed_at IS NULL;

-- Deletions strictly forbidden via fc27_reject_mutation()
DROP TRIGGER IF EXISTS fc27_recovery_codes_immutable ON public.fc27_recovery_codes;
CREATE TRIGGER fc27_recovery_codes_immutable
    BEFORE DELETE ON public.fc27_recovery_codes
    FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- Single-use transition guard for consumed_at; immutability for all other attributes
CREATE OR REPLACE FUNCTION public.fc27_guard_recovery_code_consumption()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.consumed_at IS NOT NULL THEN
        RAISE check_violation USING MESSAGE = 'recovery code is already consumed';
    END IF;
    IF NEW.consumed_at IS NULL THEN
        RAISE check_violation USING MESSAGE = 'cannot un-consume recovery code';
    END IF;
    IF NEW.code_id <> OLD.code_id OR NEW.user_id <> OLD.user_id OR NEW.code_hash <> OLD.code_hash OR NEW.created_at <> OLD.created_at THEN
        RAISE check_violation USING MESSAGE = 'recovery code attributes are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_recovery_codes_update_guard ON public.fc27_recovery_codes;
CREATE TRIGGER fc27_recovery_codes_update_guard
    BEFORE UPDATE ON public.fc27_recovery_codes
    FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_recovery_code_consumption();


-- 8. fc27_action_tokens -----------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_action_tokens (
    token_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
    assignment_id uuid NULL REFERENCES public.player_assignments(assignment_id) ON DELETE CASCADE,
    scope text NOT NULL CHECK (scope IN (
        'player_decision',
        'step_up',
        'delegation',
        'invitation',
        'recovery'
    )),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NULL,
    revoked_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at),
    CHECK (consumed_at IS NULL OR revoked_at IS NULL)
);

CREATE INDEX IF NOT EXISTS fc27_action_tokens_user_scope_idx 
    ON public.fc27_action_tokens(user_id, scope);

CREATE INDEX IF NOT EXISTS fc27_action_tokens_assignment_idx 
    ON public.fc27_action_tokens(assignment_id) 
    WHERE assignment_id IS NOT NULL;

-- Action token parameter immutability and consumption guard
CREATE OR REPLACE FUNCTION public.fc27_guard_action_token_mutation()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.consumed_at IS NOT NULL THEN
        RAISE check_violation USING MESSAGE = 'action token is already consumed';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE check_violation USING MESSAGE = 'action token is already revoked';
    END IF;
    IF NEW.token_id <> OLD.token_id OR NEW.token_hash <> OLD.token_hash OR NEW.user_id <> OLD.user_id OR NEW.scope <> OLD.scope OR NEW.expires_at <> OLD.expires_at THEN
        RAISE check_violation USING MESSAGE = 'action token parameters are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_action_tokens_guard ON public.fc27_action_tokens;
CREATE TRIGGER fc27_action_tokens_guard
    BEFORE UPDATE ON public.fc27_action_tokens
    FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_action_token_mutation();


-- 9. fc27_challenges --------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_challenges (
    challenge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    challenge_type text NOT NULL CHECK (challenge_type IN (
        'webauthn_registration',
        'webauthn_login',
        'step_up_decision',
        'player_decision'
    )),
    challenge_str text NOT NULL UNIQUE CHECK (char_length(challenge_str) >= 16),
    user_id uuid NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
    bound_payload_hash text NULL CHECK (bound_payload_hash IS NULL OR bound_payload_hash ~ '^[0-9a-f]{64}$'),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS fc27_challenges_user_idx 
    ON public.fc27_challenges(user_id) 
    WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS fc27_challenges_active_idx 
    ON public.fc27_challenges(challenge_str, expires_at) 
    WHERE consumed_at IS NULL;

-- Challenge immutability and consumption guard
CREATE OR REPLACE FUNCTION public.fc27_guard_challenge_consumption()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.consumed_at IS NOT NULL THEN
        RAISE check_violation USING MESSAGE = 'challenge is already consumed';
    END IF;
    IF NEW.consumed_at IS NULL THEN
        RAISE check_violation USING MESSAGE = 'cannot un-consume challenge';
    END IF;
    IF NEW.challenge_id <> OLD.challenge_id OR NEW.challenge_type <> OLD.challenge_type OR NEW.challenge_str <> OLD.challenge_str OR NEW.expires_at <> OLD.expires_at THEN
        RAISE check_violation USING MESSAGE = 'challenge parameters are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_challenges_consumption_guard ON public.fc27_challenges;
CREATE TRIGGER fc27_challenges_consumption_guard
    BEFORE UPDATE ON public.fc27_challenges
    FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_challenge_consumption();


-- 10. domain_events Check Constraint Expansion ------------------------------
-- Expand source_table check constraint on domain_events to accept identity tables
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
    'fc27_challenges'
));
