-- Migration 0025: Read-only FC27 operations status snapshot and deduplicated alerting.
-- Additive, idempotent migration. Contiguous after Migration 0024.
--
-- 1. Creates `public.fc27_operator_alerts` table for durable deduplicated alert lifecycle.
-- 2. Adds stored procedures for atomic alert recording, deduplication, suppression, and resolution.
-- 3. Adds `public.fc27_get_operations_status_snapshot()` returning sanitized operational health snapshot.
-- 4. Preserves fc27_owner ownership, SECURITY DEFINER, fixed safe search_path, PUBLIC revocation,
--    and narrow runtime grants without broadening table permissions.

CREATE TABLE IF NOT EXISTS public.fc27_operator_alerts (
    alert_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_type TEXT NOT NULL,
    component TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('WARNING', 'CRITICAL')),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    summary TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_notified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    acknowledged_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    notification_count INT NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_fc27_operator_alerts_active
    ON public.fc27_operator_alerts (alert_type, component)
    WHERE status IN ('OPEN', 'ACKNOWLEDGED');

CREATE INDEX IF NOT EXISTS idx_fc27_operator_alerts_status
    ON public.fc27_operator_alerts (status, opened_at DESC);

ALTER TABLE public.fc27_operator_alerts OWNER TO fc27_owner;
REVOKE ALL ON public.fc27_operator_alerts FROM PUBLIC;

-- Narrow SECURITY DEFINER procedure to atomically record, update, or suppress alerts
CREATE OR REPLACE FUNCTION public.fc27_record_operator_alert(
    p_alert_type TEXT,
    p_component TEXT,
    p_severity TEXT,
    p_summary TEXT,
    p_details JSONB DEFAULT '{}'::jsonb,
    p_suppression_seconds INT DEFAULT 1800
)
RETURNS TABLE (
    alert_id UUID,
    should_notify BOOLEAN,
    is_new BOOLEAN,
    notification_count INT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_rec RECORD;
    v_new_id UUID;
BEGIN
    IF p_alert_type IS NULL OR length(trim(p_alert_type)) = 0 THEN
        RAISE EXCEPTION 'p_alert_type must not be empty';
    END IF;
    IF p_component IS NULL OR length(trim(p_component)) = 0 THEN
        RAISE EXCEPTION 'p_component must not be empty';
    END IF;
    IF p_severity NOT IN ('WARNING', 'CRITICAL') THEN
        RAISE EXCEPTION 'p_severity must be WARNING or CRITICAL';
    END IF;

    -- Concurrency safety via transactional advisory lock
    PERFORM pg_advisory_xact_lock(hashtext('fc27_operator_alert'), hashtext(p_alert_type || ':' || p_component));

    SELECT a.alert_id, a.last_notified_at, a.notification_count, a.status
      INTO v_rec
      FROM public.fc27_operator_alerts a
     WHERE a.alert_type = p_alert_type
       AND a.component = p_component
       AND a.status IN ('OPEN', 'ACKNOWLEDGED')
     FOR UPDATE;

    IF FOUND THEN
        -- Existing active alert: evaluate suppression window
        IF (clock_timestamp() - v_rec.last_notified_at) >= (p_suppression_seconds || ' seconds')::interval THEN
            UPDATE public.fc27_operator_alerts
               SET last_notified_at = clock_timestamp(),
                   notification_count = v_rec.notification_count + 1,
                   severity = p_severity,
                   summary = p_summary,
                   details = p_details
             WHERE public.fc27_operator_alerts.alert_id = v_rec.alert_id;
            RETURN QUERY SELECT v_rec.alert_id, TRUE, FALSE, v_rec.notification_count + 1;
        ELSE
            -- Suppressed duplicate notification
            UPDATE public.fc27_operator_alerts
               SET severity = p_severity,
                   summary = p_summary,
                   details = p_details
             WHERE public.fc27_operator_alerts.alert_id = v_rec.alert_id;
            RETURN QUERY SELECT v_rec.alert_id, FALSE, FALSE, v_rec.notification_count;
        END IF;
    ELSE
        -- Brand new active alert
        v_new_id := gen_random_uuid();
        INSERT INTO public.fc27_operator_alerts (
            alert_id, alert_type, component, severity, status, summary, details, opened_at, last_notified_at, notification_count
        ) VALUES (
            v_new_id, p_alert_type, p_component, p_severity, 'OPEN', p_summary, p_details, clock_timestamp(), clock_timestamp(), 1
        );
        RETURN QUERY SELECT v_new_id, TRUE, TRUE, 1;
    END IF;
END;
$$;

ALTER FUNCTION public.fc27_record_operator_alert(TEXT, TEXT, TEXT, TEXT, JSONB, INT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_record_operator_alert(TEXT, TEXT, TEXT, TEXT, JSONB, INT) FROM PUBLIC;

-- Narrow SECURITY DEFINER procedure to resolve active operator alerts
CREATE OR REPLACE FUNCTION public.fc27_resolve_operator_alert(
    p_alert_type TEXT,
    p_component TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_id UUID;
BEGIN
    IF p_alert_type IS NULL OR length(trim(p_alert_type)) = 0 THEN
        RAISE EXCEPTION 'p_alert_type must not be empty';
    END IF;
    IF p_component IS NULL OR length(trim(p_component)) = 0 THEN
        RAISE EXCEPTION 'p_component must not be empty';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext('fc27_operator_alert'), hashtext(p_alert_type || ':' || p_component));

    SELECT a.alert_id
      INTO v_id
      FROM public.fc27_operator_alerts a
     WHERE a.alert_type = p_alert_type
       AND a.component = p_component
       AND a.status IN ('OPEN', 'ACKNOWLEDGED')
     FOR UPDATE;

    IF FOUND THEN
        UPDATE public.fc27_operator_alerts
           SET status = 'RESOLVED',
               resolved_at = clock_timestamp()
         WHERE public.fc27_operator_alerts.alert_id = v_id;
        RETURN TRUE;
    END IF;

    RETURN FALSE;
END;
$$;

ALTER FUNCTION public.fc27_resolve_operator_alert(TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_resolve_operator_alert(TEXT, TEXT) FROM PUBLIC;

-- Narrow SECURITY DEFINER procedure to query active operator alerts
CREATE OR REPLACE FUNCTION public.fc27_get_active_operator_alerts()
RETURNS TABLE (
    alert_id UUID,
    alert_type TEXT,
    component TEXT,
    severity TEXT,
    status TEXT,
    summary TEXT,
    details JSONB,
    opened_at TIMESTAMPTZ,
    last_notified_at TIMESTAMPTZ,
    notification_count INT
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT a.alert_id,
           a.alert_type,
           a.component,
           a.severity,
           a.status,
           a.summary,
           a.details,
           a.opened_at,
           a.last_notified_at,
           a.notification_count
      FROM public.fc27_operator_alerts a
     WHERE a.status IN ('OPEN', 'ACKNOWLEDGED')
     ORDER BY a.opened_at ASC;
$$;

ALTER FUNCTION public.fc27_get_active_operator_alerts() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_get_active_operator_alerts() FROM PUBLIC;

-- Narrow SECURITY DEFINER procedure to safely extract operational health snapshot
CREATE OR REPLACE FUNCTION public.fc27_get_operations_status_snapshot()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_db_clock TIMESTAMPTZ := clock_timestamp();
    v_last_cycle_id TEXT;
    v_last_cycle_start TIMESTAMPTZ;
    v_last_cycle_completion TIMESTAMPTZ;
    v_last_cycle_outcome TEXT;
    v_match_types_attempted JSONB;
    v_per_type_outcome JSONB;
    v_provider_attempts_24h INT := 0;
    v_oldest_unresolved_reservation_sec NUMERIC;
    v_single_flight_busy BOOLEAN := FALSE;

    v_new_matches_last_cycle INT := 0;
    v_existing_duplicate_matches INT := 0;
    v_quarantined_matches INT := 0;
    v_replay_conflicts INT := 0;
    v_export_batch_state TEXT := 'N/A';
    v_last_persisted_match_at TIMESTAMPTZ;
    v_latest_source_match_at TIMESTAMPTZ;

    v_jobs_pending INT := 0;
    v_jobs_claimed INT := 0;
    v_jobs_completed INT := 0;
    v_jobs_failed INT := 0;
    v_jobs_dead_lettered INT := 0;
    v_oldest_pending_job_age_sec NUMERIC;
    v_formal_result_delivery_count INT := 0;
    v_team_summary_delivery_count INT := 0;
    v_failed_destination_delivery_count INT := 0;
    v_last_successful_delivery_at TIMESTAMPTZ;

    v_worker_heartbeat_at TIMESTAMPTZ;
    v_bot_heartbeat_at TIMESTAMPTZ;
    v_current_migration TEXT;
    v_grants_policy INT;
    v_active_backfill_count INT := 0;
BEGIN
    -- 1. POLLING METRICS
    IF to_regclass('public.fc27_provider_request_attempts') IS NOT NULL THEN
        SELECT cycle_id,
               min(attempted_at),
               max(finalized_at),
               jsonb_agg(DISTINCT match_type ORDER BY match_type),
               jsonb_object_agg(match_type, outcome_category)
          INTO v_last_cycle_id,
               v_last_cycle_start,
               v_last_cycle_completion,
               v_match_types_attempted,
               v_per_type_outcome
          FROM public.fc27_provider_request_attempts
         WHERE cycle_id = (
             SELECT cycle_id
               FROM public.fc27_provider_request_attempts
              ORDER BY attempted_at DESC
              LIMIT 1
         )
         GROUP BY cycle_id;

        IF v_last_cycle_id IS NOT NULL THEN
            IF EXISTS (
                SELECT 1 FROM public.fc27_provider_request_attempts
                 WHERE cycle_id = v_last_cycle_id AND outcome_category != 'SUCCEEDED'
            ) THEN
                v_last_cycle_outcome := 'PARTIAL_OR_FAILED';
            ELSE
                v_last_cycle_outcome := 'SUCCEEDED';
            END IF;
        ELSE
            v_last_cycle_outcome := 'NO_CYCLES_RECORDED';
            v_match_types_attempted := '[]'::jsonb;
            v_per_type_outcome := '{}'::jsonb;
        END IF;

        -- Rolling 24h attempts
        SELECT count(*)
          INTO v_provider_attempts_24h
          FROM public.fc27_provider_request_attempts
         WHERE attempted_at >= v_db_clock - interval '24 hours';

        -- Oldest unresolved reservation
        SELECT EXTRACT(EPOCH FROM (v_db_clock - min(attempted_at)))
          INTO v_oldest_unresolved_reservation_sec
          FROM public.fc27_provider_request_attempts
         WHERE outcome_category = 'RESERVED'
           AND finalized_at IS NULL;
    ELSE
        v_last_cycle_outcome := 'NO_CYCLES_RECORDED';
        v_match_types_attempted := '[]'::jsonb;
        v_per_type_outcome := '{}'::jsonb;
    END IF;

    -- Single-flight lock-state check (read-only without acquiring)
    SELECT EXISTS (
        SELECT 1 FROM pg_locks
         WHERE locktype = 'advisory'
           AND classid = hashtext('fc27_poll:fc-clubs-api-community-v1')
           AND objid = hashtext('330323:common-gen5:fc27:poll')
    ) INTO v_single_flight_busy;

    -- 2. INGESTION METRICS
    IF to_regclass('public.fc27_matches') IS NOT NULL THEN
        IF v_last_cycle_start IS NOT NULL THEN
            SELECT count(*)
              INTO v_new_matches_last_cycle
              FROM public.fc27_matches
             WHERE created_at >= v_last_cycle_start;
        END IF;

        SELECT max(created_at), max(played_at)
          INTO v_last_persisted_match_at, v_latest_source_match_at
          FROM public.fc27_matches;
    END IF;

    IF to_regclass('public.fc27_quarantine_records') IS NOT NULL THEN
        SELECT count(*) FILTER (WHERE review_status = 'PENDING'),
               count(*) FILTER (WHERE anomaly_category = 'REPLAY_CONFLICT')
          INTO v_quarantined_matches, v_replay_conflicts
          FROM public.fc27_quarantine_records;
    END IF;

    IF to_regclass('public.fc27_export_batches') IS NOT NULL THEN
        SELECT COALESCE(export_id, 'NONE'),
               COALESCE(total_matches - accepted_matches, 0)
          INTO v_export_batch_state,
               v_existing_duplicate_matches
          FROM public.fc27_export_batches
         ORDER BY applied_at DESC NULLS LAST, created_at DESC
         LIMIT 1;
    END IF;

    -- 3. DISCORD DELIVERY METRICS
    IF to_regclass('public.fc27_discord_jobs') IS NOT NULL THEN
        SELECT count(*) FILTER (WHERE state = 'pending'),
               count(*) FILTER (WHERE state = 'claimed'),
               count(*) FILTER (WHERE state = 'completed'),
               count(*) FILTER (WHERE state = 'failed'),
               count(*) FILTER (WHERE state = 'dead_letter'),
               EXTRACT(EPOCH FROM (v_db_clock - min(created_at))) FILTER (WHERE state = 'pending')
          INTO v_jobs_pending,
               v_jobs_claimed,
               v_jobs_completed,
               v_jobs_failed,
               v_jobs_dead_lettered,
               v_oldest_pending_job_age_sec
          FROM public.fc27_discord_jobs;
    END IF;

    IF to_regclass('public.fc27_match_discord_deliveries') IS NOT NULL THEN
        SELECT count(*) FILTER (WHERE message_kind = 'FORMAL_RESULT' AND delivery_state = 'DELIVERED'),
               count(*) FILTER (WHERE message_kind = 'TEAM_CHAT_SUMMARY' AND delivery_state = 'DELIVERED'),
               count(*) FILTER (WHERE delivery_state = 'FAILED'),
               max(delivered_at) FILTER (WHERE delivery_state = 'DELIVERED')
          INTO v_formal_result_delivery_count,
               v_team_summary_delivery_count,
               v_failed_destination_delivery_count,
               v_last_successful_delivery_at
          FROM public.fc27_match_discord_deliveries;
    END IF;

    -- 4. RUNTIME METRICS
    IF to_regclass('public.service_heartbeats') IS NOT NULL THEN
        SELECT last_heartbeat INTO v_worker_heartbeat_at
          FROM public.service_heartbeats
         WHERE service_name IN ('fc27-community-worker', 'community-worker', 'worker')
         ORDER BY last_heartbeat DESC LIMIT 1;

        SELECT last_heartbeat INTO v_bot_heartbeat_at
          FROM public.service_heartbeats
         WHERE service_name IN ('fc27-discord-bot', 'discord-bot', 'bot')
         ORDER BY last_heartbeat DESC LIMIT 1;
    END IF;

    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        SELECT max(version) INTO v_current_migration FROM public.schema_migrations;
    END IF;

    IF to_regclass('public.schema_policy_versions') IS NOT NULL THEN
        SELECT max(version) INTO v_grants_policy FROM public.schema_policy_versions WHERE policy = 'grants';
    END IF;

    IF to_regclass('public.fc27_discord_backfill_requests') IS NOT NULL THEN
        SELECT count(*) INTO v_active_backfill_count
          FROM public.fc27_discord_backfill_requests
         WHERE status IN ('PLANNED', 'CONFIRMED', 'RUNNING');
    END IF;

    RETURN jsonb_build_object(
        'database_clock', v_db_clock,
        'snapshot_generated_at', v_db_clock,
        'polling', jsonb_build_object(
            'last_cycle_id_sanitized', CASE
                WHEN v_last_cycle_id IS NOT NULL THEN 'cyc_' || substr(md5(v_last_cycle_id), 1, 8)
                ELSE NULL
            END,
            'last_cycle_start_at', v_last_cycle_start,
            'last_cycle_completed_at', v_last_cycle_completion,
            'last_cycle_outcome', v_last_cycle_outcome,
            'match_types_attempted', COALESCE(v_match_types_attempted, '[]'::jsonb),
            'per_type_outcome', COALESCE(v_per_type_outcome, '{}'::jsonb),
            'next_expected_poll_at', CASE
                WHEN v_last_cycle_start IS NOT NULL THEN v_last_cycle_start + interval '14400 seconds'
                ELSE NULL
            END,
            'polling_interval_seconds', 14400,
            'single_flight_state', CASE WHEN v_single_flight_busy THEN 'busy' ELSE 'free' END,
            'active_lease_age_seconds', CASE WHEN v_single_flight_busy THEN 0 ELSE NULL END,
            'rolling_provider_attempts_used', v_provider_attempts_24h,
            'provider_attempt_limit', 18,
            'remaining_provider_capacity', GREATEST(0, 18 - v_provider_attempts_24h),
            'oldest_unresolved_reservation_age_seconds', v_oldest_unresolved_reservation_sec
        ),
        'ingestion', jsonb_build_object(
            'new_matches_in_last_cycle', v_new_matches_last_cycle,
            'existing_duplicate_matches', v_existing_duplicate_matches,
            'quarantined_matches', v_quarantined_matches,
            'replay_conflicts', v_replay_conflicts,
            'export_batch_state', v_export_batch_state,
            'last_persisted_match_at', v_last_persisted_match_at,
            'latest_source_match_at', v_latest_source_match_at,
            'data_freshness_age_seconds', CASE
                WHEN v_latest_source_match_at IS NOT NULL THEN EXTRACT(EPOCH FROM (v_db_clock - v_latest_source_match_at))
                ELSE NULL
            END
        ),
        'discord_delivery', jsonb_build_object(
            'jobs_pending', v_jobs_pending,
            'jobs_claimed', v_jobs_claimed,
            'jobs_completed', v_jobs_completed,
            'jobs_failed', v_jobs_failed,
            'jobs_dead_lettered', v_jobs_dead_lettered,
            'oldest_pending_job_age_seconds', v_oldest_pending_job_age_sec,
            'formal_result_delivery_count', v_formal_result_delivery_count,
            'team_summary_delivery_count', v_team_summary_delivery_count,
            'failed_destination_delivery_count', v_failed_destination_delivery_count,
            'last_successful_delivery_time', v_last_successful_delivery_at
        ),
        'runtime', jsonb_build_object(
            'worker_heartbeat_age_seconds', CASE
                WHEN v_worker_heartbeat_at IS NOT NULL THEN EXTRACT(EPOCH FROM (v_db_clock - v_worker_heartbeat_at))
                ELSE NULL
            END,
            'bot_heartbeat_age_seconds', CASE
                WHEN v_bot_heartbeat_at IS NOT NULL THEN EXTRACT(EPOCH FROM (v_db_clock - v_bot_heartbeat_at))
                ELSE NULL
            END,
            'current_migration_version', v_current_migration,
            'grants_policy_version', v_grants_policy,
            'active_backfill_request_count', v_active_backfill_count
        )
    );
END;
$$;

ALTER FUNCTION public.fc27_get_operations_status_snapshot() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_get_operations_status_snapshot() FROM PUBLIC;
