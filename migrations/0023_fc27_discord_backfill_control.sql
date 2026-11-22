-- Migration 0023: Durable FC27 Operator Discord Backfill Control and Audit
-- Additive, idempotent migration. Contiguous after Migration 0022.
--
-- 1. Adds `public.fc27_discord_backfill_requests` to track planned, confirmed, and executed
--    historical backfill operations with actor-bound, guild-bound CSPRNG token digests,
--    durable cooldowns, and plan digests.
-- 2. Enforces least-privilege runtime security with narrow SECURITY DEFINER procedures.
--    Direct table mutation is revoked from all runtime roles; execution is granted
--    only to authorized writer roles.

CREATE TABLE IF NOT EXISTS public.fc27_discord_backfill_requests (
    request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_digest TEXT UNIQUE NOT NULL,
    actor_digest TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    destination_key TEXT NOT NULL REFERENCES public.fc27_discord_destinations(destination_key),
    message_kind TEXT NOT NULL CHECK (message_kind IN ('FORMAL_RESULT', 'TEAM_CHAT_SUMMARY')),
    latest_count INT NOT NULL CHECK (latest_count BETWEEN 1 AND 25),
    planned_count INT NOT NULL DEFAULT 0,
    plan_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLANNED'
        CHECK (status IN ('PLANNED', 'CONFIRMED', 'RUNNING', 'COMPLETED', 'FAILED', 'EXPIRED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    delivered_count INT NOT NULL DEFAULT 0,
    skipped_count INT NOT NULL DEFAULT 0,
    failed_count INT NOT NULL DEFAULT 0,
    last_error_category TEXT,

    CONSTRAINT fc27_backfill_guild_id_chk CHECK (guild_id ~ '^[0-9]{1,30}$'),
    CONSTRAINT fc27_backfill_token_digest_chk CHECK (token_digest ~ '^[a-f0-9]{64}$'),
    CONSTRAINT fc27_backfill_actor_digest_chk CHECK (actor_digest ~ '^[a-f0-9]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_fc27_backfill_requests_guild_status
    ON public.fc27_discord_backfill_requests (guild_id, status);

CREATE INDEX IF NOT EXISTS idx_fc27_backfill_requests_actor_created
    ON public.fc27_discord_backfill_requests (actor_digest, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_fc27_backfill_requests_destination
    ON public.fc27_discord_backfill_requests (destination_key, status);

ALTER TABLE public.fc27_discord_backfill_requests OWNER TO fc27_owner;
REVOKE ALL ON public.fc27_discord_backfill_requests FROM PUBLIC;

-- Procedure to create and record a short-lived backfill plan.
-- Enforces:
--   1. One active (PLANNED / CONFIRMED / RUNNING) request per guild
--   2. Per-actor cooldown
--   3. Per-guild cooldown
CREATE OR REPLACE FUNCTION public.fc27_create_backfill_request(
    p_token_digest TEXT,
    p_actor_digest TEXT,
    p_guild_id TEXT,
    p_destination_key TEXT,
    p_message_kind TEXT,
    p_latest_count INT,
    p_planned_count INT,
    p_plan_digest TEXT,
    p_ttl_seconds INT,
    p_cooldown_seconds INT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_req_id UUID;
    v_active_count INT;
    v_actor_recent INT;
    v_guild_recent INT;
BEGIN
    IF p_token_digest IS NULL OR length(p_token_digest) != 64 THEN
        RAISE EXCEPTION 'p_token_digest must be a 64-character hex digest';
    END IF;
    IF p_actor_digest IS NULL OR length(p_actor_digest) != 64 THEN
        RAISE EXCEPTION 'p_actor_digest must be a 64-character hex digest';
    END IF;
    IF p_guild_id IS NULL OR p_guild_id = '' THEN
        RAISE EXCEPTION 'p_guild_id must not be empty';
    END IF;
    IF p_latest_count < 1 OR p_latest_count > 25 THEN
        RAISE EXCEPTION 'p_latest_count must be between 1 and 25';
    END IF;
    IF p_message_kind NOT IN ('FORMAL_RESULT', 'TEAM_CHAT_SUMMARY') THEN
        RAISE EXCEPTION 'Invalid message_kind: %', p_message_kind;
    END IF;

    -- Concurrency lock per guild to enforce 1 active request per guild
    PERFORM pg_advisory_xact_lock(hashtext('fc27_backfill_guild'), hashtext(p_guild_id));

    -- Single active request per guild check
    -- Auto-expire any expired PLANNED requests for this guild
    UPDATE public.fc27_discord_backfill_requests
       SET status = 'EXPIRED'
     WHERE guild_id = p_guild_id
       AND status = 'PLANNED'
       AND expires_at <= clock_timestamp();

    SELECT count(*)
      INTO v_active_count
      FROM public.fc27_discord_backfill_requests
     WHERE guild_id = p_guild_id
       AND status IN ('PLANNED', 'CONFIRMED', 'RUNNING')
       AND (status != 'PLANNED' OR expires_at > clock_timestamp());

    IF v_active_count > 0 THEN
        RAISE EXCEPTION 'GUILD_BACKFILL_ACTIVE: Another backfill request is active for this guild';
    END IF;

    -- Per-actor cooldown check
    IF p_cooldown_seconds > 0 THEN
        SELECT count(*)
          INTO v_actor_recent
          FROM public.fc27_discord_backfill_requests
         WHERE actor_digest = p_actor_digest
           AND status IN ('CONFIRMED', 'RUNNING', 'COMPLETED')
           AND created_at >= clock_timestamp() - (p_cooldown_seconds || ' seconds')::interval;

        IF v_actor_recent > 0 THEN
            RAISE EXCEPTION 'ACTOR_COOLDOWN_ACTIVE: Backfill cooldown is active for this operator';
        END IF;

        -- Per-guild cooldown check
        SELECT count(*)
          INTO v_guild_recent
          FROM public.fc27_discord_backfill_requests
         WHERE guild_id = p_guild_id
           AND status IN ('CONFIRMED', 'RUNNING', 'COMPLETED')
           AND created_at >= clock_timestamp() - (p_cooldown_seconds || ' seconds')::interval;

        IF v_guild_recent > 0 THEN
            RAISE EXCEPTION 'GUILD_COOLDOWN_ACTIVE: Backfill cooldown is active for this guild';
        END IF;
    END IF;

    INSERT INTO public.fc27_discord_backfill_requests (
        token_digest,
        actor_digest,
        guild_id,
        destination_key,
        message_kind,
        latest_count,
        planned_count,
        plan_digest,
        status,
        created_at,
        expires_at
    ) VALUES (
        p_token_digest,
        p_actor_digest,
        p_guild_id,
        p_destination_key,
        p_message_kind,
        p_latest_count,
        p_planned_count,
        p_plan_digest,
        'PLANNED',
        clock_timestamp(),
        clock_timestamp() + (p_ttl_seconds || ' seconds')::interval
    )
    RETURNING request_id INTO v_req_id;

    RETURN v_req_id;
END;
$$;

ALTER FUNCTION public.fc27_create_backfill_request(TEXT, TEXT, TEXT, TEXT, TEXT, INT, INT, TEXT, INT, INT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_create_backfill_request(TEXT, TEXT, TEXT, TEXT, TEXT, INT, INT, TEXT, INT, INT) FROM PUBLIC;

-- Procedure to validate confirmation token and atomically transition to CONFIRMED.
CREATE OR REPLACE FUNCTION public.fc27_confirm_backfill_request(
    p_token_digest TEXT,
    p_actor_digest TEXT,
    p_guild_id TEXT,
    p_plan_digest TEXT
)
RETURNS TABLE (
    request_id UUID,
    status TEXT,
    destination_key TEXT,
    message_kind TEXT,
    latest_count INT,
    planned_count INT,
    plan_digest TEXT,
    created_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    delivered_count INT,
    skipped_count INT,
    failed_count INT,
    last_error_category TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_req RECORD;
BEGIN
    IF p_token_digest IS NULL OR length(p_token_digest) != 64 THEN
        RAISE EXCEPTION 'p_token_digest must be a 64-character hex digest';
    END IF;

    -- Concurrency lock on token
    PERFORM pg_advisory_xact_lock(hashtext('fc27_backfill_token'), hashtext(p_token_digest));

    SELECT r.*
      INTO v_req
      FROM public.fc27_discord_backfill_requests r
     WHERE r.token_digest = p_token_digest
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'TOKEN_NOT_FOUND: Confirmation token not recognized';
    END IF;

    IF v_req.actor_digest != p_actor_digest THEN
        RAISE EXCEPTION 'TOKEN_ACTOR_MISMATCH: Confirmation token was issued to a different operator';
    END IF;

    IF v_req.guild_id != p_guild_id THEN
        RAISE EXCEPTION 'TOKEN_GUILD_MISMATCH: Confirmation token is bound to a different guild';
    END IF;

    IF v_req.status = 'PLANNED' AND v_req.expires_at <= clock_timestamp() THEN
        UPDATE public.fc27_discord_backfill_requests
           SET status = 'EXPIRED'
         WHERE public.fc27_discord_backfill_requests.request_id = v_req.request_id;
        RAISE EXCEPTION 'TOKEN_EXPIRED: Confirmation token has expired';
    END IF;

    IF v_req.plan_digest != p_plan_digest THEN
        RAISE EXCEPTION 'PLAN_DIGEST_MISMATCH: Plan has materially changed since planning; re-planning required';
    END IF;

    -- Atomic transition PLANNED -> CONFIRMED
    IF v_req.status = 'PLANNED' THEN
        UPDATE public.fc27_discord_backfill_requests
           SET status = 'CONFIRMED',
               confirmed_at = clock_timestamp()
         WHERE public.fc27_discord_backfill_requests.request_id = v_req.request_id
        RETURNING * INTO v_req;
    ELSIF v_req.status IN ('CONFIRMED', 'RUNNING', 'COMPLETED') THEN
        -- Idempotent repeat confirmation
        NULL;
    ELSE
        RAISE EXCEPTION 'INVALID_REQUEST_STATUS: Request is in state %', v_req.status;
    END IF;

    RETURN QUERY
    SELECT v_req.request_id,
           v_req.status,
           v_req.destination_key,
           v_req.message_kind,
           v_req.latest_count,
           v_req.planned_count,
           v_req.plan_digest,
           v_req.created_at,
           v_req.expires_at,
           v_req.confirmed_at,
           v_req.completed_at,
           v_req.delivered_count,
           v_req.skipped_count,
           v_req.failed_count,
           v_req.last_error_category;
END;
$$;

ALTER FUNCTION public.fc27_confirm_backfill_request(TEXT, TEXT, TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_confirm_backfill_request(TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;

-- Procedure to mark confirmed request as RUNNING.
CREATE OR REPLACE FUNCTION public.fc27_mark_backfill_running(
    p_request_id UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    UPDATE public.fc27_discord_backfill_requests
       SET status = 'RUNNING'
     WHERE request_id = p_request_id
       AND status = 'CONFIRMED';
    RETURN FOUND;
END;
$$;

ALTER FUNCTION public.fc27_mark_backfill_running(UUID) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_mark_backfill_running(UUID) FROM PUBLIC;

-- Procedure to finalize backfill request outcome.
CREATE OR REPLACE FUNCTION public.fc27_finalize_backfill_request(
    p_request_id UUID,
    p_status TEXT,
    p_delivered_count INT,
    p_skipped_count INT,
    p_failed_count INT,
    p_error_category TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF p_status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED') THEN
        RAISE EXCEPTION 'Invalid finalization status: %', p_status;
    END IF;

    UPDATE public.fc27_discord_backfill_requests
       SET status = p_status,
           completed_at = clock_timestamp(),
           delivered_count = COALESCE(p_delivered_count, 0),
           skipped_count = COALESCE(p_skipped_count, 0),
           failed_count = COALESCE(p_failed_count, 0),
           last_error_category = p_error_category
     WHERE request_id = p_request_id;

    RETURN FOUND;
END;
$$;

ALTER FUNCTION public.fc27_finalize_backfill_request(UUID, TEXT, INT, INT, INT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_finalize_backfill_request(UUID, TEXT, INT, INT, INT, TEXT) FROM PUBLIC;

-- Procedure to query backfill status safely.
CREATE OR REPLACE FUNCTION public.fc27_get_backfill_request_status(
    p_lookup TEXT,
    p_guild_id TEXT
)
RETURNS TABLE (
    request_id UUID,
    status TEXT,
    destination_key TEXT,
    message_kind TEXT,
    latest_count INT,
    planned_count INT,
    delivered_count INT,
    skipped_count INT,
    failed_count INT,
    last_error_category TEXT,
    created_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- If lookup is empty/null, return latest request for guild
    IF p_lookup IS NULL OR TRIM(p_lookup) = '' THEN
        RETURN QUERY
        SELECT r.request_id,
               r.status,
               r.destination_key,
               r.message_kind,
               r.latest_count,
               r.planned_count,
               r.delivered_count,
               r.skipped_count,
               r.failed_count,
               r.last_error_category,
               r.created_at,
               r.expires_at,
               r.confirmed_at,
               r.completed_at
          FROM public.fc27_discord_backfill_requests r
         WHERE r.guild_id = p_guild_id
         ORDER BY r.created_at DESC
         LIMIT 1;
        RETURN;
    END IF;

    -- Try lookup as UUID first
    BEGIN
        RETURN QUERY
        SELECT r.request_id,
               r.status,
               r.destination_key,
               r.message_kind,
               r.latest_count,
               r.planned_count,
               r.delivered_count,
               r.skipped_count,
               r.failed_count,
               r.last_error_category,
               r.created_at,
               r.expires_at,
               r.confirmed_at,
               r.completed_at
          FROM public.fc27_discord_backfill_requests r
         WHERE r.request_id = p_lookup::UUID
           AND r.guild_id = p_guild_id;
        IF FOUND THEN
            RETURN;
        END IF;
    EXCEPTION WHEN invalid_text_representation THEN
        NULL;
    END;

    -- Try lookup as token digest
    RETURN QUERY
    SELECT r.request_id,
           r.status,
           r.destination_key,
           r.message_kind,
           r.latest_count,
           r.planned_count,
           r.delivered_count,
           r.skipped_count,
           r.failed_count,
           r.last_error_category,
           r.created_at,
           r.expires_at,
           r.confirmed_at,
           r.completed_at
      FROM public.fc27_discord_backfill_requests r
     WHERE r.token_digest = p_lookup
       AND r.guild_id = p_guild_id;
END;
$$;

ALTER FUNCTION public.fc27_get_backfill_request_status(TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_get_backfill_request_status(TEXT, TEXT) FROM PUBLIC;
