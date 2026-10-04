# Technical Analysis & Schema Design: Phase 2 Milestone 1 Identity Data Foundation

## 1. Executive Summary

Phase 2 Milestone 1 establishes the authoritative identity and cryptographic data foundation for the FC27 Player Dispatch programme. In accordance with:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (§5, §17)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md` (Features 1–3, 6–12, 13–17, 27–33, 41, 42)
- Existing Phase 1 migrations (`0001_initial.sql`, `0002_approval_security.sql`, `0003_dispatch_foundation.sql`, `post/grants.sql`, `src/db_migrate.py`)

This design specifies:
1. Exact DDL for `migrations/0004_identity_foundation.sql` defining 9 new relational tables (`fc27_users`, `fc27_user_roles`, `fc27_invitations`, `fc27_sessions`, `fc27_webauthn_credentials`, `fc27_totp_credentials`, `fc27_recovery_codes`, `fc27_action_tokens`, `fc27_challenges`).
2. Defensive database triggers enforcing status state machine transitions, authenticator clone protection (sign-counter monotonicity), single-use consumption, and append-only deletion blocking (`fc27_reject_mutation()`).
3. Expansion of `public.domain_events`'s `source_table` check constraint to support identity audit events without schema violations.
4. Updates to `migrations/post/grants.sql` bumping policy version from 5 to 6 and granting least-privilege DML to `fc27_app` with zero grants to `fc27_dispatch`.
5. Updates to `src/db_migrate.py` reflecting `GrantsPolicy(version=6)`.
6. Rollback / idempotency design ensuring clean recovery in case of migration failures.

---

## 2. Problem Boundary & Architectural Alignment

### 2.1 Non-Negotiable Invariants
- **PostgreSQL Authority**: Identities, credentials, sessions, tokens, and decisions reside authoritatively in PostgreSQL. Graph checkpoints remain transient workflow state.
- **Dispatch Dormancy**: Strict `DISPATCH_ENABLED=false` and zero player delivery. Identity operations do not unlock delivery.
- **No Impersonation**: Agents (LLMs/Graph nodes/workers) and services (BFF) cannot authenticate or decide for humans.
- **Cryptographic Storage Standards**:
  - One-way tokens (invitations, sessions, action tokens): Stored strictly as SHA-256 hex digests (`[0-9a-f]{64}`).
  - Recovery codes: Stored irreversibly via Argon2id (`$argon2id$...`).
  - Sensitive recoverable secrets (TOTP seeds): Stored encrypted at rest using AES-GCM / MultiFernet envelope encryption with key versioning (`key_version >= 1`).
  - WebAuthn credentials: Only public keys (`public_key bytea`) and raw credential IDs (`credential_id bytea`) are persisted. Authenticator private keys are never handled or stored.
- **Separation of Concerns**:
  - Application roles (`fc27_user_roles`) are decoupled from database login roles (`fc27_owner`, `fc27_app`, `fc27_dispatch`, `fc27_migrator`).
  - Local authenticated player account (`fc27_users`) is decoupled from EA player identity evidence (`public.players`). No gamertag fallback.

---

## 3. Relational Schema Architecture (0004_identity_foundation.sql)

```
                            +--------------------------+
                            |       fc27_users         |
                            |--------------------------|
                            | user_id (UUID PK)        |
                            | club_id (text)           |
                            | login_id (text UNIQUE)   |
                            | display_name (text)      |
                            | status (enum)            |
                            | player_id (text FK NULL) |
                            +--------------------------+
                                     |       |
                 +-------------------+       +-----------------------+
                 | 1:N                       | 1:N                   | 1:N
                 v                           v                       v
       +-------------------+       +-------------------+   +--------------------+
       |  fc27_user_roles  |       |   fc27_sessions   |   | fc27_webauthn_     |
       |-------------------|       |-------------------|   | credentials        |
       | user_id (UUID PK) |       | session_id (PK)   |   |--------------------|
       | role (text PK)    |       | token_hash (SHA)  |   | credential_id (PK) |
       | granted_at        |       | auth_level        |   | user_id (UUID FK)  |
       | granted_by        |       | idle_expires_at   |   | public_key (bytea) |
       +-------------------+       | absolute_expires  |   | sign_counter       |
                                   +-------------------+   +--------------------+
                 | 1:1                       | 1:N                   | 1:N
                 v                           v                       v
       +-------------------+       +-------------------+   +--------------------+
       |    fc27_totp_     |       |   fc27_recovery_  |   | fc27_action_       |
       |   credentials     |       |       codes       |   |      tokens        |
       |-------------------|       |-------------------|   |--------------------|
       | user_id (UUID PK) |       | code_id (UUID PK) |   | token_id (UUID PK) |
       | encrypted_seed    |       | user_id (UUID FK) |   | user_id (UUID FK)  |
       | key_version       |       | code_hash(Argon2) |   | assignment_id (FK) |
       | status (enum)     |       | consumed_at       |   | token_hash (SHA)   |
       +-------------------+       +-------------------+   +--------------------+

       +------------------------------------+   +------------------------------------+
       |          fc27_invitations          |   |          fc27_challenges           |
       |------------------------------------|   |------------------------------------|
       | invitation_id (UUID PK)            |   | challenge_id (UUID PK)             |
       | token_hash (SHA-256 UNIQUE)        |   | challenge_type (enum)              |
       | club_id (text)                     |   | challenge_str (text UNIQUE)        |
       | intended_role (enum)               |   | user_id (UUID FK NULL)             |
       | invited_email (text)               |   | bound_payload_hash (SHA-256 NULL)  |
       | issued_at, expires_at, consumed_at |   | expires_at, consumed_at            |
       +------------------------------------+   +------------------------------------+
```

---

## 4. Complete SQL DDL Specification (`migrations/0004_identity_foundation.sql`)

```sql
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
```

---

## 5. Security & Least Privilege Design (`migrations/post/grants.sql`)

### 5.1 Policy Version Bump
`migrations/post/grants.sql` line 1 must be bumped from:
```sql
-- grants-policy-version: 5
```
to:
```sql
-- grants-policy-version: 6
```

### 5.2 Table-by-Table Privilege Allocation
Under principle of least privilege:
- `fc27_app` receives strictly the required DML privileges.
- `fc27_dispatch` receives **ZERO** privileges on all 9 `fc27_*` tables.
- No `fc27_*` table grants `DELETE` except `fc27_sessions` (session cleanup), `fc27_challenges` (expired ceremony garbage collection), and `fc27_user_roles` (role revocation).
- `fc27_recovery_codes` has a `BEFORE DELETE` trigger calling `fc27_reject_mutation()`, preventing any row deletion even if `fc27_app` attempted it.

```sql
    -- Phase 2 Milestone 1: Identity Data Foundation Grants
    IF to_regclass('public.fc27_users') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_users TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_user_roles') IS NOT NULL THEN
        GRANT SELECT, INSERT, DELETE ON public.fc27_user_roles TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_invitations') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_invitations TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_sessions') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_sessions TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_webauthn_credentials') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_webauthn_credentials TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_totp_credentials') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_totp_credentials TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_recovery_codes') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_recovery_codes TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_action_tokens') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_action_tokens TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_challenges') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_challenges TO fc27_app;
    END IF;
```

---

## 6. Migration Runner Updates (`src/db_migrate.py`)

In `src/db_migrate.py`:
Update line 25:
```python
GRANTS_POLICY = GrantsPolicy(version=6, sha256=_compute_grants_sha256())
```

When `migrate()` runs:
1. It verifies `grants.sql` starts with `-- grants-policy-version: 6`.
2. It verifies the SHA-256 digest of `grants.sql` matches `GRANTS_POLICY.sha256`.
3. It applies `0004_identity_foundation.sql`, records it in `schema_migrations`, executes `grants.sql`, and records version 6 in `schema_policy_versions`.
4. It calls `verify_postconditions(conn)` ensuring `fc27_app` is not superuser, not member of `fc27_owner`, and lacks `CREATE` on `public` schema.

---

## 7. Rollback & Idempotency Specification

### 7.1 Idempotency
- All table creations use `CREATE TABLE IF NOT EXISTS`.
- All index creations use `CREATE INDEX IF NOT EXISTS`.
- All trigger creations drop any pre-existing trigger before creating (`DROP TRIGGER IF EXISTS ...; CREATE TRIGGER ...`).
- All helper functions use `CREATE OR REPLACE FUNCTION ...`.

### 7.2 Down-Migration Script (`migrations/down/0004_identity_foundation_down.sql`)
```sql
-- Rollback for 0004_identity_foundation.sql
DROP TRIGGER IF EXISTS fc27_challenges_consumption_guard ON public.fc27_challenges;
DROP FUNCTION IF EXISTS public.fc27_guard_challenge_consumption();

DROP TRIGGER IF EXISTS fc27_action_tokens_guard ON public.fc27_action_tokens;
DROP FUNCTION IF EXISTS public.fc27_guard_action_token_mutation();

DROP TRIGGER IF EXISTS fc27_recovery_codes_update_guard ON public.fc27_recovery_codes;
DROP FUNCTION IF EXISTS public.fc27_guard_recovery_code_consumption();
DROP TRIGGER IF EXISTS fc27_recovery_codes_immutable ON public.fc27_recovery_codes;

DROP TRIGGER IF EXISTS fc27_webauthn_counter_guard ON public.fc27_webauthn_credentials;
DROP FUNCTION IF EXISTS public.fc27_guard_webauthn_counter();

DROP TRIGGER IF EXISTS fc27_users_status_guard ON public.fc27_users;
DROP FUNCTION IF EXISTS public.fc27_guard_user_status_transition();

DROP TABLE IF EXISTS public.fc27_challenges CASCADE;
DROP TABLE IF EXISTS public.fc27_action_tokens CASCADE;
DROP TABLE IF EXISTS public.fc27_recovery_codes CASCADE;
DROP TABLE IF EXISTS public.fc27_totp_credentials CASCADE;
DROP TABLE IF EXISTS public.fc27_webauthn_credentials CASCADE;
DROP TABLE IF EXISTS public.fc27_sessions CASCADE;
DROP TABLE IF EXISTS public.fc27_invitations CASCADE;
DROP TABLE IF EXISTS public.fc27_user_roles CASCADE;
DROP TABLE IF EXISTS public.fc27_users CASCADE;

-- Revert domain_events source_table check
ALTER TABLE public.domain_events DROP CONSTRAINT IF EXISTS domain_events_source_table_check;
ALTER TABLE public.domain_events ADD CONSTRAINT domain_events_source_table_check
CHECK (source_table IN (
    'approval_audit','approval_requests','dispatches','dispatch_acknowledgments',
    'dispatch_assessments','dispatch_plans','dispatch_plan_decisions',
    'dispatch_delivery_attempts','user_roles','activation_gate_log'
));
```

---

## 8. Verification Strategy

1. **Migration Verification**:
   - Fresh install test: `migrate()` executes `0001` -> `0002` -> `0003` -> `0004` -> `grants.sql` without error.
   - Upgrade test: Starting from migrated `0003` database, applying `0004` cleanly adds all 9 tables.
   - Idempotency test: Re-executing `migrate()` performs no DDL and succeeds.
2. **Security & Posture Verification**:
   - `test_db_migrate_roles.py` passes with `GRANTS_POLICY.version == 6`.
   - `fc27_app` connection can SELECT, INSERT, UPDATE on `fc27_users` and `fc27_sessions`.
   - `fc27_app` cannot DROP tables or CREATE new tables.
   - `fc27_dispatch` cannot SELECT, INSERT, UPDATE, or DELETE on any `fc27_*` table.
3. **Trigger & Invariant Verification**:
   - `fc27_recovery_codes`: DELETE is rejected with `check_violation` (`fc27_recovery_codes is append-only`).
   - `fc27_recovery_codes`: UPDATE setting `consumed_at` succeeds once; second UPDATE fails.
   - `fc27_users`: Transitioning from `disabled` to `active` is rejected.
   - `fc27_webauthn_credentials`: Decreasing `sign_counter` is rejected.
