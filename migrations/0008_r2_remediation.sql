-- 0008_r2_remediation.sql: Phase 2 Milestone R2 Schema Remediation (R7 & R9)

-- 1. Allow bootstrap_enrollment in public.fc27_sessions.auth_level
ALTER TABLE public.fc27_sessions DROP CONSTRAINT IF EXISTS fc27_sessions_auth_level_check;
ALTER TABLE public.fc27_sessions ADD CONSTRAINT fc27_sessions_auth_level_check
    CHECK (auth_level IN ('bootstrap_enrollment', 'authenticated', 'mfa_totp', 'step_up_passkey'));

-- 2. Allow bootstrap in public.fc27_sessions.auth_method
ALTER TABLE public.fc27_sessions DROP CONSTRAINT IF EXISTS fc27_sessions_auth_method_check;
ALTER TABLE public.fc27_sessions ADD CONSTRAINT fc27_sessions_auth_method_check
    CHECK (auth_method IN ('bootstrap', 'passkey', 'totp', 'service_token', 'recovery_code'));

-- 3. Add last_consumed_step to fc27_totp_credentials for atomic replay prevention (R9)
ALTER TABLE public.fc27_totp_credentials
    ADD COLUMN IF NOT EXISTS last_consumed_step bigint NULL;

COMMENT ON COLUMN public.fc27_totp_credentials.last_consumed_step
    IS 'Last consumed TOTP time step counter; strictly monotonic';

-- 4. Reaffirm grants to fc27_app and fc27_migrator
GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_sessions TO fc27_app;
GRANT SELECT, INSERT, UPDATE ON public.fc27_totp_credentials TO fc27_app;
