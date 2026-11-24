-- Migration 0024: Persist expired backfill request state safely
-- Additive, idempotent migration. Contiguous after Migration 0023.
--
-- 1. Replaces `public.fc27_confirm_backfill_request` so an expired PLANNED token
--    transitions to EXPIRED and returns the EXPIRED row instead of raising after
--    the UPDATE. The caller commits the transition before mapping the returned
--    status to a bounded ephemeral TOKEN_EXPIRED response.
-- 2. Adds `public.fc27_expire_overdue_backfill_requests()` for bounded operational
--    reconciliation of already-expired PLANNED rows. It only touches rows whose
--    `expires_at <= clock_timestamp()` and returns a count, never identifiers.
-- 3. Preserves `fc27_owner` ownership, SECURITY DEFINER, fixed safe search_path,
--    PUBLIC revocation, and narrow runtime grants.

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

    -- Commit-friendly expiration: update the state and return the EXPIRED row.
    -- Never raise after this UPDATE; raising would roll the transition back.
    IF v_req.status = 'PLANNED' AND v_req.expires_at <= clock_timestamp() THEN
        UPDATE public.fc27_discord_backfill_requests
           SET status = 'EXPIRED'
         WHERE public.fc27_discord_backfill_requests.request_id = v_req.request_id
        RETURNING * INTO v_req;
    END IF;

    IF v_req.status = 'EXPIRED' THEN
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
        RETURN;
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

-- Bounded reconciliation for already-expired PLANNED rows. This is time-bounded,
-- returns only a count, and cannot modify a valid (unexpired) request.
CREATE OR REPLACE FUNCTION public.fc27_expire_overdue_backfill_requests()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_expired_count INTEGER;
BEGIN
    UPDATE public.fc27_discord_backfill_requests
       SET status = 'EXPIRED'
     WHERE status = 'PLANNED'
       AND expires_at <= clock_timestamp();
    GET DIAGNOSTICS v_expired_count = ROW_COUNT;
    RETURN v_expired_count;
END;
$$;

ALTER FUNCTION public.fc27_confirm_backfill_request(TEXT, TEXT, TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_confirm_backfill_request(TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_confirm_backfill_request(text, text, text, text) TO fc27_match_results_writer';
    END IF;
END $$;

ALTER FUNCTION public.fc27_expire_overdue_backfill_requests() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_expire_overdue_backfill_requests() FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_expire_overdue_backfill_requests() TO fc27_match_results_writer';
    END IF;
END $$;
