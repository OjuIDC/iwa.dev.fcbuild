-- Migration 0018: Least-Privilege Roster-Refresh Queue (Finding 8)
-- Additive, idempotent migration:
--   1. Permits 'refresh_roster_messages' in public.fc27_discord_jobs_job_type_chk.
--   2. Adds owner-owned SECURITY DEFINER function public.fc27_enqueue_roster_refresh_job
--      so fc27_match_results_writer can atomically enqueue roster-refresh jobs in the
--      same DB transaction as fc27_roster mutations without any direct privileges on
--      public.fc27_discord_jobs.
--   3. Preserves all existing jobs and never modifies Migrations 0001-0017.

ALTER TABLE public.fc27_discord_jobs
    DROP CONSTRAINT IF EXISTS fc27_discord_jobs_job_type_chk;

ALTER TABLE public.fc27_discord_jobs
    ADD CONSTRAINT fc27_discord_jobs_job_type_chk
        CHECK (job_type IN ('post_new_results', 'evaluate_suggestions', 'refresh_roster_messages'));

CREATE OR REPLACE FUNCTION public.fc27_enqueue_roster_refresh_job(
    p_player_id text,
    p_roster_revision text,
    p_reason_code text,
    p_requested_at timestamptz DEFAULT pg_catalog.clock_timestamp()
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_player_id text;
    v_revision text;
    v_reason text;
    v_requested_at timestamptz;
    v_idempotency_key text;
    v_payload jsonb;
    v_job_id bigint;
    v_existing_job_type text;
    v_existing_player_id text;
BEGIN
    IF p_player_id IS NULL THEN
        RAISE EXCEPTION 'player_id is required' USING ERRCODE = '22023';
    END IF;
    v_player_id := pg_catalog.btrim(p_player_id);
    IF pg_catalog.char_length(v_player_id) < 1 OR pg_catalog.char_length(v_player_id) > 64 THEN
        RAISE EXCEPTION 'player_id must be between 1 and 64 characters' USING ERRCODE = '22023';
    END IF;
    IF v_player_id ~ '[\x00-\x20\x7F:]' THEN
        RAISE EXCEPTION 'player_id contains invalid whitespace, control characters, or colons' USING ERRCODE = '22023';
    END IF;
    IF v_player_id ~ '^[=+\-@]' THEN
        RAISE EXCEPTION 'player_id cannot begin with a formula prefix' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM public.fc27_roster AS r
        WHERE r.player_id = v_player_id
    ) THEN
        RAISE EXCEPTION 'unknown canonical roster player_id' USING ERRCODE = '23503';
    END IF;

    IF p_roster_revision IS NULL THEN
        RAISE EXCEPTION 'roster_revision is required' USING ERRCODE = '22023';
    END IF;
    v_revision := pg_catalog.btrim(p_roster_revision);
    IF v_revision !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$' THEN
        RAISE EXCEPTION 'roster_revision must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$' USING ERRCODE = '22023';
    END IF;

    IF p_reason_code IS NULL THEN
        RAISE EXCEPTION 'reason_code is required' USING ERRCODE = '22023';
    END IF;
    v_reason := pg_catalog.upper(pg_catalog.btrim(p_reason_code));
    IF v_reason NOT IN ('ROSTER_UPSERT', 'ROSTER_RENAME', 'TOKEN_REDEEM') THEN
        RAISE EXCEPTION 'reason_code must be ROSTER_UPSERT, ROSTER_RENAME, or TOKEN_REDEEM' USING ERRCODE = '22023';
    END IF;

    v_requested_at := COALESCE(p_requested_at, pg_catalog.clock_timestamp());
    v_idempotency_key := pg_catalog.format('roster-refresh:%s:%s:v1', v_player_id, v_revision);
    IF pg_catalog.char_length(v_idempotency_key) < 1 OR pg_catalog.char_length(v_idempotency_key) > 128 THEN
        RAISE EXCEPTION 'idempotency_key exceeds 128 character bound' USING ERRCODE = '22023';
    END IF;

    v_payload := pg_catalog.jsonb_build_object(
        'schema_version', 'fc27-roster-refresh-v1',
        'event_type', 'refresh_roster_messages',
        'player_id', v_player_id,
        'roster_revision', v_revision,
        'reason_code', v_reason,
        'requested_at', pg_catalog.to_char(v_requested_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
    );

    INSERT INTO public.fc27_discord_jobs (
        job_type,
        payload,
        state,
        attempt_count,
        idempotency_key,
        available_at
    ) VALUES (
        'refresh_roster_messages',
        v_payload,
        'pending',
        0,
        v_idempotency_key,
        v_requested_at
    )
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING job_id INTO v_job_id;

    IF v_job_id IS NULL THEN
        SELECT j.job_type, j.payload->>'player_id'
          INTO v_existing_job_type, v_existing_player_id
        FROM public.fc27_discord_jobs AS j
        WHERE j.idempotency_key = v_idempotency_key;

        IF v_existing_job_type IS DISTINCT FROM 'refresh_roster_messages'
           OR v_existing_player_id IS DISTINCT FROM v_player_id THEN
            RAISE EXCEPTION 'idempotency_key collision with incompatible job' USING ERRCODE = '23505';
        END IF;
    END IF;

    RETURN v_job_id;
END;
$$;

ALTER FUNCTION public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz)
    OWNER TO fc27_owner;

REVOKE ALL ON FUNCTION public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz)
    FROM PUBLIC;
