-- Migration 0026: Durable PostgreSQL liveness heartbeats for FC27 runtimes.
-- Additive, idempotent migration. Contiguous after Migration 0025.
--
-- 1. Creates `public.fc27_service_heartbeats`, a dedicated one-row-per-service
--    liveness table for the fixed reviewed service keys:
--        fc27-community-worker
--        fc27-discord-bot
--    The legacy `public.service_heartbeats` table is intentionally NOT reused:
--    it is a dispatch-foundation table whose pre-existing fc27_dispatch grants
--    include direct INSERT/UPDATE, which would violate the "no runtime role gets
--    direct heartbeat-table write" contract for these new heartbeats.
-- 2. Adds the bounded SECURITY DEFINER function
--    `public.fc27_record_service_heartbeat(text, text, text)`:
--        - fixed service-key and runtime-status allowlists
--        - SHA-256 lowercase 64-hex instance digest validation
--        - authoritative observed time from clock_timestamp()
--        - callers can never supply observed_at
--        - upsert retains started_at for the same instance, resets it on a new one
--        - one row per service, bounded update_count, no history growth
-- 3. Adds the SECURITY DEFINER read helper
--    `public.fc27_get_service_heartbeat_status()` returning database-clock ages.
-- 4. Preserves fc27_owner ownership, fixed safe search_path, PUBLIC revocation.
--    Runtime EXECUTE grants are declared in migrations/post/grants.sql (policy 27).
--
-- No heartbeat rows are inserted by this migration. No provider request. No
-- Discord message. No alert-state evaluation. No backfill behaviour change.

CREATE TABLE IF NOT EXISTS public.fc27_service_heartbeats (
    service_key TEXT PRIMARY KEY,
    instance_digest TEXT NOT NULL,
    runtime_status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    update_count BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fc27_service_heartbeats_service_key_chk
        CHECK (service_key IN ('fc27-community-worker', 'fc27-discord-bot')),
    CONSTRAINT fc27_service_heartbeats_runtime_status_chk
        CHECK (runtime_status IN ('READY', 'STARTING', 'DEGRADED', 'STOPPING')),
    CONSTRAINT fc27_service_heartbeats_instance_digest_chk
        CHECK (instance_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fc27_service_heartbeats_update_count_chk
        CHECK (update_count >= 1),
    CONSTRAINT fc27_service_heartbeats_seen_order_chk
        CHECK (last_seen_at >= started_at)
);

ALTER TABLE public.fc27_service_heartbeats OWNER TO fc27_owner;
REVOKE ALL ON public.fc27_service_heartbeats FROM PUBLIC;

COMMENT ON TABLE public.fc27_service_heartbeats IS
    'Durable per-service liveness heartbeats. One row per reviewed service key. Written only through public.fc27_record_service_heartbeat(); no direct runtime table grants.';

-- Bounded SECURITY DEFINER writer. Fixed allowlist, no dynamic SQL, no
-- caller-supplied timestamp. Uses clock_timestamp() as the authoritative
-- observed time so application-local clocks are never trusted.
CREATE OR REPLACE FUNCTION public.fc27_record_service_heartbeat(
    p_service_key TEXT,
    p_instance_digest TEXT,
    p_runtime_status TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_prev_digest TEXT;
    v_prev_started_at TIMESTAMPTZ;
    v_prev_update_count BIGINT;
    v_instance_changed BOOLEAN;
    v_started_at TIMESTAMPTZ;
    v_last_seen_at TIMESTAMPTZ;
    v_update_count BIGINT;
BEGIN
    IF p_service_key IS NULL
       OR p_service_key NOT IN ('fc27-community-worker', 'fc27-discord-bot') THEN
        RAISE EXCEPTION 'invalid heartbeat service key'
            USING ERRCODE = '22023';
    END IF;

    IF p_runtime_status IS NULL
       OR p_runtime_status NOT IN ('READY', 'STARTING', 'DEGRADED', 'STOPPING') THEN
        RAISE EXCEPTION 'invalid heartbeat runtime status'
            USING ERRCODE = '22023';
    END IF;

    IF p_instance_digest IS NULL
       OR p_instance_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid heartbeat instance digest'
            USING ERRCODE = '22023';
    END IF;

    SELECT instance_digest, started_at, update_count
      INTO v_prev_digest, v_prev_started_at, v_prev_update_count
      FROM public.fc27_service_heartbeats
     WHERE service_key = p_service_key
       FOR UPDATE;

    IF NOT FOUND THEN
        INSERT INTO public.fc27_service_heartbeats (
            service_key, instance_digest, runtime_status,
            started_at, last_seen_at, update_count, created_at
        )
        VALUES (
            p_service_key, p_instance_digest, p_runtime_status,
            v_now, v_now, 1, v_now
        )
        ON CONFLICT (service_key) DO UPDATE
           SET instance_digest = EXCLUDED.instance_digest,
               runtime_status = EXCLUDED.runtime_status,
               started_at = EXCLUDED.started_at,
               last_seen_at = EXCLUDED.last_seen_at,
               update_count = 1;
        v_instance_changed := TRUE;
    ELSIF v_prev_digest = p_instance_digest THEN
        UPDATE public.fc27_service_heartbeats
           SET runtime_status = p_runtime_status,
               last_seen_at = v_now,
               update_count = v_prev_update_count + 1
         WHERE service_key = p_service_key;
        v_instance_changed := FALSE;
    ELSE
        UPDATE public.fc27_service_heartbeats
           SET instance_digest = p_instance_digest,
               runtime_status = p_runtime_status,
               started_at = v_now,
               last_seen_at = v_now,
               update_count = 1
         WHERE service_key = p_service_key;
        v_instance_changed := TRUE;
    END IF;

    SELECT started_at, last_seen_at, update_count
      INTO v_started_at, v_last_seen_at, v_update_count
      FROM public.fc27_service_heartbeats
     WHERE service_key = p_service_key;

    RETURN jsonb_build_object(
        'service_key', p_service_key,
        'runtime_status', p_runtime_status,
        'instance_changed', v_instance_changed,
        'started_at', v_started_at,
        'last_seen_at', v_last_seen_at,
        'update_count', v_update_count
    );
END;
$$;

ALTER FUNCTION public.fc27_record_service_heartbeat(TEXT, TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_record_service_heartbeat(TEXT, TEXT, TEXT) FROM PUBLIC;

-- Read helper used by the read-only operations status path. Returns only
-- database-clock ages and safe fields; never instance_digest, IDs, or secrets.
CREATE OR REPLACE FUNCTION public.fc27_get_service_heartbeat_status()
RETURNS JSONB
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    WITH clock AS (
        SELECT clock_timestamp() AS now_ts
    ),
    observed AS (
        SELECT
            h.service_key,
            h.runtime_status,
            EXTRACT(EPOCH FROM (c.now_ts - h.last_seen_at)) AS heartbeat_age_seconds,
            EXTRACT(EPOCH FROM (c.now_ts - h.started_at)) AS started_age_seconds
        FROM public.fc27_service_heartbeats h
        CROSS JOIN clock c
    )
    SELECT jsonb_build_object(
        'database_clock', (SELECT now_ts FROM clock),
        'worker', COALESCE(
            (
                SELECT jsonb_build_object(
                    'known', TRUE,
                    'runtime_status', runtime_status,
                    'heartbeat_age_seconds', GREATEST(heartbeat_age_seconds, 0),
                    'started_age_seconds', GREATEST(started_age_seconds, 0)
                )
                FROM observed
                WHERE service_key = 'fc27-community-worker'
            ),
            jsonb_build_object('known', FALSE)
        ),
        'bot', COALESCE(
            (
                SELECT jsonb_build_object(
                    'known', TRUE,
                    'runtime_status', runtime_status,
                    'heartbeat_age_seconds', GREATEST(heartbeat_age_seconds, 0),
                    'started_age_seconds', GREATEST(started_age_seconds, 0)
                )
                FROM observed
                WHERE service_key = 'fc27-discord-bot'
            ),
            jsonb_build_object('known', FALSE)
        )
    );
$$;

ALTER FUNCTION public.fc27_get_service_heartbeat_status() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_get_service_heartbeat_status() FROM PUBLIC;
