-- Migration 0022: Durable FC27 community-provider request budget and attempt ledger.
-- Records sanitized attempt metadata only; provider payloads and headers are never stored.

CREATE TABLE IF NOT EXISTS public.fc27_provider_request_attempts (
    attempt_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    club_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    match_type TEXT NOT NULL CHECK (match_type IN ('leagueMatch', 'playoffMatch', 'friendlyMatch')),
    invocation_source TEXT NOT NULL DEFAULT 'SCHEDULED_POLL'
        CHECK (invocation_source IN ('SCHEDULED_POLL', 'MANUAL_POLL', 'BACKFILL_CLI')),
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome_category TEXT NOT NULL DEFAULT 'RESERVED'
        CHECK (outcome_category IN (
            'RESERVED', 'ATTEMPTED', 'SUCCEEDED', 'FAILED', 'ABANDONED',
            'PROVIDER_BLOCKED', 'SCHEMA_DRIFT', 'TRANSIENT_FAILURE', 'PERSISTENCE_FAILURE',
            'AUTHENTICATION_FAILED', 'AUTHORIZATION_FAILED', 'RATE_LIMITED', 'PRIVACY_REJECTED'
        )),
    finalized_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_fc27_provider_request_attempts_window
    ON public.fc27_provider_request_attempts (provider_id, club_id, platform, attempted_at DESC);

CREATE INDEX IF NOT EXISTS idx_fc27_provider_request_attempts_cycle
    ON public.fc27_provider_request_attempts (cycle_id, attempted_at);

REVOKE ALL ON public.fc27_provider_request_attempts FROM PUBLIC;

-- Narrow SECURITY DEFINER procedure to atomically reserve a request budget slot.
-- Enforces rolling 24-hour daily cap and per-cycle cap with transaction-level advisory locking for concurrent final-slot safety.
CREATE OR REPLACE FUNCTION public.fc27_reserve_provider_request(
    p_attempt_id TEXT,
    p_cycle_id TEXT,
    p_provider_id TEXT,
    p_club_id TEXT,
    p_platform TEXT,
    p_match_type TEXT,
    p_max_requests_per_cycle INT,
    p_max_requests_per_24_hours INT,
    p_invocation_source TEXT DEFAULT 'SCHEDULED_POLL'
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_recent_count INT;
    v_cycle_count INT;
    v_lock_key TEXT;
    v_inv_source TEXT;
BEGIN
    IF p_attempt_id IS NULL OR p_attempt_id = '' THEN
        RAISE EXCEPTION 'p_attempt_id must not be empty';
    END IF;
    IF p_cycle_id IS NULL OR p_cycle_id = '' THEN
        RAISE EXCEPTION 'p_cycle_id must not be empty';
    END IF;
    IF p_match_type NOT IN ('leagueMatch', 'playoffMatch', 'friendlyMatch') THEN
        RAISE EXCEPTION 'Invalid match_type: %', p_match_type;
    END IF;
    v_inv_source := COALESCE(NULLIF(TRIM(p_invocation_source), ''), 'SCHEDULED_POLL');
    IF v_inv_source NOT IN ('SCHEDULED_POLL', 'MANUAL_POLL', 'BACKFILL_CLI') THEN
        RAISE EXCEPTION 'Invalid invocation_source: %', p_invocation_source;
    END IF;

    -- Concurrency-safe final-slot locking per provider/club/platform
    v_lock_key := p_provider_id || ':' || p_club_id || ':' || p_platform;
    PERFORM pg_advisory_xact_lock(hashtext('fc27_provider_budget'), hashtext(v_lock_key));

    -- Database-clock rolling 24-hour count and cycle count
    SELECT count(*) FILTER (WHERE attempted_at >= now() - interval '24 hours'),
           count(*) FILTER (WHERE cycle_id = p_cycle_id)
      INTO v_recent_count, v_cycle_count
      FROM public.fc27_provider_request_attempts
     WHERE provider_id = p_provider_id
       AND club_id = p_club_id
       AND platform = p_platform;

    IF v_recent_count >= p_max_requests_per_24_hours OR v_cycle_count >= p_max_requests_per_cycle THEN
        RETURN FALSE;
    END IF;

    INSERT INTO public.fc27_provider_request_attempts (
        attempt_id,
        cycle_id,
        provider_id,
        club_id,
        platform,
        match_type,
        invocation_source,
        attempted_at,
        outcome_category
    ) VALUES (
        p_attempt_id,
        p_cycle_id,
        p_provider_id,
        p_club_id,
        p_platform,
        p_match_type,
        v_inv_source,
        now(),
        'RESERVED'
    );

    RETURN TRUE;
END;
$$;

ALTER FUNCTION public.fc27_reserve_provider_request(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INT, INT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_reserve_provider_request(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INT, INT, TEXT) FROM PUBLIC;

-- Narrow SECURITY DEFINER procedure to finalize attempt outcome.
CREATE OR REPLACE FUNCTION public.fc27_finalize_provider_request(
    p_attempt_id TEXT,
    p_outcome_category TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_current_outcome TEXT;
BEGIN
    IF p_attempt_id IS NULL OR p_attempt_id = '' THEN
        RAISE EXCEPTION 'p_attempt_id must not be empty';
    END IF;
    IF p_outcome_category NOT IN (
        'RESERVED', 'ATTEMPTED', 'SUCCEEDED', 'FAILED', 'ABANDONED',
        'PROVIDER_BLOCKED', 'SCHEMA_DRIFT', 'TRANSIENT_FAILURE', 'PERSISTENCE_FAILURE',
        'AUTHENTICATION_FAILED', 'AUTHORIZATION_FAILED', 'RATE_LIMITED', 'PRIVACY_REJECTED'
    ) THEN
        RAISE EXCEPTION 'Invalid outcome_category: %', p_outcome_category;
    END IF;

    SELECT outcome_category INTO v_current_outcome
      FROM public.fc27_provider_request_attempts
     WHERE attempt_id = p_attempt_id;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    -- Finalization is idempotent: already in requested final state
    IF v_current_outcome = p_outcome_category THEN
        RETURN TRUE;
    END IF;

    -- Invalid transition: terminal outcome cannot be reverted to RESERVED
    IF v_current_outcome != 'RESERVED' AND p_outcome_category = 'RESERVED' THEN
        RAISE EXCEPTION 'Invalid state transition from % to RESERVED', v_current_outcome;
    END IF;

    UPDATE public.fc27_provider_request_attempts
       SET outcome_category = p_outcome_category,
           finalized_at = now()
     WHERE attempt_id = p_attempt_id;

    RETURN TRUE;
END;
$$;

ALTER FUNCTION public.fc27_finalize_provider_request(TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_finalize_provider_request(TEXT, TEXT) FROM PUBLIC;
