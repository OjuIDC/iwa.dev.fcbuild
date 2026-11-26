-- Migration 0027: Destination-aware Discord operations alert dispatch
-- Additive, idempotent schema migration expanding message_kind for operations alerts,
-- creating durable alert delivery tracking, and providing narrow stored procedures.

-- 1. Extend destination message_kind check constraint to permit OPERATIONS_ALERT
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fc27_discord_dest_kind_chk'
          AND conrelid = 'public.fc27_discord_destinations'::regclass
    ) THEN
        ALTER TABLE public.fc27_discord_destinations
            DROP CONSTRAINT fc27_discord_dest_kind_chk;
    END IF;

    ALTER TABLE public.fc27_discord_destinations
        ADD CONSTRAINT fc27_discord_dest_kind_chk
        CHECK (message_kind IN ('FORMAL_RESULT', 'TEAM_CHAT_SUMMARY', 'OPERATIONS_ALERT'));
END;
$$;

-- 2. Durable operations alert delivery table
CREATE TABLE IF NOT EXISTS public.fc27_operator_alert_deliveries (
    delivery_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alert_id               UUID NOT NULL REFERENCES public.fc27_operator_alerts(alert_id) ON DELETE CASCADE,
    lifecycle_event        TEXT NOT NULL,
    destination_key        TEXT NOT NULL REFERENCES public.fc27_discord_destinations(destination_key),
    delivery_state         TEXT NOT NULL DEFAULT 'PENDING',
    attempt_count          INT NOT NULL DEFAULT 0,
    claim_owner            TEXT,
    claimed_at             TIMESTAMPTZ,
    delivered_at           TIMESTAMPTZ,
    failed_at              TIMESTAMPTZ,
    discord_message_id     TEXT,
    discord_channel_id     TEXT,
    marker_digest          TEXT NOT NULL,
    last_error_category    TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fc27_alert_deliv_event_chk CHECK (lifecycle_event IN ('OPEN', 'RESOLVED')),
    CONSTRAINT fc27_alert_deliv_state_chk CHECK (delivery_state IN ('PENDING', 'CLAIMED', 'DELIVERED', 'FAILED', 'DEAD_LETTER')),
    CONSTRAINT fc27_alert_deliv_attempt_chk CHECK (attempt_count >= 0),
    CONSTRAINT fc27_alert_deliv_channel_chk CHECK (discord_channel_id IS NULL OR discord_channel_id ~ '^[0-9]{1,30}$'),
    CONSTRAINT fc27_alert_deliv_msg_chk CHECK (discord_message_id IS NULL OR discord_message_id ~ '^[0-9]{1,30}$'),
    CONSTRAINT uq_fc27_alert_deliv_lifecycle UNIQUE (alert_id, lifecycle_event, destination_key)
);

CREATE INDEX IF NOT EXISTS idx_fc27_alert_deliv_dispatch
    ON public.fc27_operator_alert_deliveries (destination_key, delivery_state, created_at);

CREATE INDEX IF NOT EXISTS idx_fc27_alert_deliv_marker
    ON public.fc27_operator_alert_deliveries (destination_key, marker_digest);

-- 3. Procedure: reserve operator alert delivery (idempotent, 1 row per alert/event/destination)
CREATE OR REPLACE FUNCTION public.fc27_reserve_operator_alert_delivery(
    p_alert_id UUID,
    p_lifecycle_event TEXT,
    p_destination_key TEXT,
    p_marker_digest TEXT
)
RETURNS TABLE (
    delivery_id BIGINT,
    is_new BOOLEAN,
    delivery_state TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_dest RECORD;
    v_existing RECORD;
    v_new_id BIGINT;
BEGIN
    IF p_alert_id IS NULL THEN
        RAISE EXCEPTION 'p_alert_id must not be null';
    END IF;
    IF p_lifecycle_event NOT IN ('OPEN', 'RESOLVED') THEN
        RAISE EXCEPTION 'p_lifecycle_event must be OPEN or RESOLVED';
    END IF;
    IF p_destination_key IS NULL OR length(trim(p_destination_key)) = 0 THEN
        RAISE EXCEPTION 'p_destination_key must not be empty';
    END IF;
    IF p_marker_digest IS NULL OR length(trim(p_marker_digest)) = 0 THEN
        RAISE EXCEPTION 'p_marker_digest must not be empty';
    END IF;

    -- Validate destination in registry
    SELECT destination_key, enabled, message_kind
      INTO v_dest
      FROM public.fc27_discord_destinations
     WHERE destination_key = p_destination_key;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Destination key % not found in registry', p_destination_key;
    END IF;
    IF NOT v_dest.enabled THEN
        RAISE EXCEPTION 'Destination % is disabled', p_destination_key;
    END IF;
    IF v_dest.message_kind <> 'OPERATIONS_ALERT' THEN
        RAISE EXCEPTION 'Destination % message_kind % does not match OPERATIONS_ALERT',
            p_destination_key, v_dest.message_kind;
    END IF;

    -- Check for existing delivery
    SELECT d.delivery_id, d.delivery_state
      INTO v_existing
      FROM public.fc27_operator_alert_deliveries d
     WHERE d.alert_id = p_alert_id
       AND d.lifecycle_event = p_lifecycle_event
       AND d.destination_key = p_destination_key;

    IF FOUND THEN
        RETURN QUERY SELECT v_existing.delivery_id, FALSE, v_existing.delivery_state;
        RETURN;
    END IF;

    INSERT INTO public.fc27_operator_alert_deliveries (
        alert_id,
        lifecycle_event,
        destination_key,
        delivery_state,
        attempt_count,
        marker_digest,
        created_at,
        updated_at
    ) VALUES (
        p_alert_id,
        p_lifecycle_event,
        p_destination_key,
        'PENDING',
        0,
        p_marker_digest,
        clock_timestamp(),
        clock_timestamp()
    )
    ON CONFLICT (alert_id, lifecycle_event, destination_key) DO NOTHING
    RETURNING fc27_operator_alert_deliveries.delivery_id INTO v_new_id;

    IF v_new_id IS NOT NULL THEN
        RETURN QUERY SELECT v_new_id, TRUE, 'PENDING'::TEXT;
    ELSE
        SELECT d.delivery_id, d.delivery_state
          INTO v_existing
          FROM public.fc27_operator_alert_deliveries d
         WHERE d.alert_id = p_alert_id
           AND d.lifecycle_event = p_lifecycle_event
           AND d.destination_key = p_destination_key;
        RETURN QUERY SELECT v_existing.delivery_id, FALSE, v_existing.delivery_state;
    END IF;
END;
$$;

ALTER FUNCTION public.fc27_reserve_operator_alert_delivery(UUID, TEXT, TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_reserve_operator_alert_delivery(UUID, TEXT, TEXT, TEXT) FROM PUBLIC;

-- 4. Procedure: claim batch of pending/retryable operator alert deliveries
CREATE OR REPLACE FUNCTION public.fc27_claim_operator_alert_deliveries(
    p_destination_key TEXT,
    p_claim_owner TEXT,
    p_batch_size INT DEFAULT 5,
    p_lease_seconds INT DEFAULT 300,
    p_max_attempts INT DEFAULT 3
)
RETURNS TABLE (
    delivery_id BIGINT,
    alert_id UUID,
    lifecycle_event TEXT,
    destination_key TEXT,
    attempt_count INT,
    marker_digest TEXT,
    guild_id TEXT,
    channel_id TEXT,
    alert_type TEXT,
    component TEXT,
    severity TEXT,
    summary TEXT,
    details JSONB,
    opened_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    notification_count INT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF p_destination_key IS NULL OR length(trim(p_destination_key)) = 0 THEN
        RAISE EXCEPTION 'p_destination_key must not be empty';
    END IF;
    IF p_claim_owner IS NULL OR length(trim(p_claim_owner)) = 0 THEN
        RAISE EXCEPTION 'p_claim_owner must not be empty';
    END IF;

    RETURN QUERY
    WITH eligible AS (
        SELECT d.delivery_id
          FROM public.fc27_operator_alert_deliveries d
          JOIN public.fc27_discord_destinations dest ON dest.destination_key = d.destination_key
         WHERE d.destination_key = p_destination_key
           AND dest.enabled = TRUE
           AND dest.message_kind = 'OPERATIONS_ALERT'
           AND (
               d.delivery_state = 'PENDING'
               OR (
                   d.delivery_state = 'CLAIMED'
                   AND d.claimed_at < clock_timestamp() - (p_lease_seconds || ' seconds')::interval
                   AND d.attempt_count < p_max_attempts
               )
               OR (
                   d.delivery_state = 'FAILED'
                   AND d.attempt_count < p_max_attempts
               )
           )
         ORDER BY d.created_at ASC
         LIMIT p_batch_size
         FOR UPDATE OF d SKIP LOCKED
    ),
    claimed AS (
        UPDATE public.fc27_operator_alert_deliveries d
           SET delivery_state = 'CLAIMED',
               claim_owner = p_claim_owner,
               claimed_at = clock_timestamp(),
               attempt_count = d.attempt_count + 1,
               updated_at = clock_timestamp()
          FROM eligible e
         WHERE d.delivery_id = e.delivery_id
        RETURNING d.*
    )
    SELECT c.delivery_id,
           c.alert_id,
           c.lifecycle_event,
           c.destination_key,
           c.attempt_count,
           c.marker_digest,
           dest.guild_id,
           dest.channel_id,
           a.alert_type,
           a.component,
           a.severity,
           a.summary,
           a.details,
           a.opened_at,
           a.resolved_at,
           a.notification_count
      FROM claimed c
      JOIN public.fc27_operator_alerts a ON a.alert_id = c.alert_id
      JOIN public.fc27_discord_destinations dest ON dest.destination_key = c.destination_key;
END;
$$;

ALTER FUNCTION public.fc27_claim_operator_alert_deliveries(TEXT, TEXT, INT, INT, INT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_claim_operator_alert_deliveries(TEXT, TEXT, INT, INT, INT) FROM PUBLIC;

-- 5. Procedure: finalize delivered operator alert delivery
CREATE OR REPLACE FUNCTION public.fc27_finalize_operator_alert_delivery(
    p_delivery_id BIGINT,
    p_claim_owner TEXT,
    p_discord_message_id TEXT,
    p_discord_channel_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_updated INT;
BEGIN
    IF p_discord_message_id IS NULL OR length(trim(p_discord_message_id)) = 0 THEN
        RAISE EXCEPTION 'p_discord_message_id must not be empty';
    END IF;
    IF p_discord_channel_id IS NULL OR length(trim(p_discord_channel_id)) = 0 THEN
        RAISE EXCEPTION 'p_discord_channel_id must not be empty';
    END IF;

    UPDATE public.fc27_operator_alert_deliveries
       SET delivery_state = 'DELIVERED',
           delivered_at = clock_timestamp(),
           discord_message_id = p_discord_message_id,
           discord_channel_id = p_discord_channel_id,
           updated_at = clock_timestamp()
     WHERE delivery_id = p_delivery_id
       AND (claim_owner = p_claim_owner OR delivery_state = 'CLAIMED');

    GET DIAGNOSTICS v_updated = ROW_COUNT;
    RETURN v_updated > 0;
END;
$$;

ALTER FUNCTION public.fc27_finalize_operator_alert_delivery(BIGINT, TEXT, TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_finalize_operator_alert_delivery(BIGINT, TEXT, TEXT, TEXT) FROM PUBLIC;

-- 6. Procedure: record failure or dead-letter for alert delivery
CREATE OR REPLACE FUNCTION public.fc27_fail_operator_alert_delivery(
    p_delivery_id BIGINT,
    p_claim_owner TEXT,
    p_error_category TEXT,
    p_max_attempts INT DEFAULT 3
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_new_state TEXT;
BEGIN
    UPDATE public.fc27_operator_alert_deliveries
       SET delivery_state = CASE
               WHEN attempt_count >= p_max_attempts THEN 'DEAD_LETTER'
               ELSE 'FAILED'
           END,
           failed_at = clock_timestamp(),
           last_error_category = p_error_category,
           updated_at = clock_timestamp()
     WHERE delivery_id = p_delivery_id
       AND (claim_owner = p_claim_owner OR delivery_state = 'CLAIMED')
    RETURNING delivery_state INTO v_new_state;

    RETURN v_new_state;
END;
$$;

ALTER FUNCTION public.fc27_fail_operator_alert_delivery(BIGINT, TEXT, TEXT, INT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_fail_operator_alert_delivery(BIGINT, TEXT, TEXT, INT) FROM PUBLIC;

-- 7. Procedure: reconcile previously delivered marker (reconciles ambiguous send before retry)
CREATE OR REPLACE FUNCTION public.fc27_reconcile_operator_alert_delivery_by_marker(
    p_destination_key TEXT,
    p_marker_digest TEXT,
    p_discord_message_id TEXT,
    p_discord_channel_id TEXT
)
RETURNS TABLE (
    reconciled BOOLEAN,
    delivery_id BIGINT,
    delivery_state TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_del RECORD;
BEGIN
    IF p_destination_key IS NULL OR length(trim(p_destination_key)) = 0 THEN
        RAISE EXCEPTION 'p_destination_key must not be empty';
    END IF;
    IF p_marker_digest IS NULL OR length(trim(p_marker_digest)) = 0 THEN
        RAISE EXCEPTION 'p_marker_digest must not be empty';
    END IF;
    IF p_discord_message_id IS NULL OR length(trim(p_discord_message_id)) = 0 THEN
        RAISE EXCEPTION 'p_discord_message_id must not be empty';
    END IF;
    IF p_discord_channel_id IS NULL OR length(trim(p_discord_channel_id)) = 0 THEN
        RAISE EXCEPTION 'p_discord_channel_id must not be empty';
    END IF;

    SELECT d.delivery_id, d.delivery_state
      INTO v_del
      FROM public.fc27_operator_alert_deliveries d
     WHERE d.destination_key = p_destination_key
       AND d.marker_digest = p_marker_digest
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT FALSE, NULL::BIGINT, NULL::TEXT;
        RETURN;
    END IF;

    IF v_del.delivery_state = 'DELIVERED' THEN
        RETURN QUERY SELECT FALSE, v_del.delivery_id, 'DELIVERED'::TEXT;
        RETURN;
    END IF;

    UPDATE public.fc27_operator_alert_deliveries
       SET delivery_state = 'DELIVERED',
           delivered_at = clock_timestamp(),
           discord_message_id = p_discord_message_id,
           discord_channel_id = p_discord_channel_id,
           updated_at = clock_timestamp()
     WHERE public.fc27_operator_alert_deliveries.delivery_id = v_del.delivery_id;

    RETURN QUERY SELECT TRUE, v_del.delivery_id, 'DELIVERED'::TEXT;
END;
$$;

ALTER FUNCTION public.fc27_reconcile_operator_alert_delivery_by_marker(TEXT, TEXT, TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_reconcile_operator_alert_delivery_by_marker(TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;

-- 8. Procedure: bounded delivery summary
CREATE OR REPLACE FUNCTION public.fc27_get_operator_alert_delivery_summary()
RETURNS TABLE (
    pending_count BIGINT,
    claimed_count BIGINT,
    delivered_count BIGINT,
    failed_count BIGINT,
    dead_letter_count BIGINT
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT count(*) FILTER (WHERE delivery_state = 'PENDING') AS pending_count,
           count(*) FILTER (WHERE delivery_state = 'CLAIMED') AS claimed_count,
           count(*) FILTER (WHERE delivery_state = 'DELIVERED') AS delivered_count,
           count(*) FILTER (WHERE delivery_state = 'FAILED') AS failed_count,
           count(*) FILTER (WHERE delivery_state = 'DEAD_LETTER') AS dead_letter_count
      FROM public.fc27_operator_alert_deliveries;
$$;

ALTER FUNCTION public.fc27_get_operator_alert_delivery_summary() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_get_operator_alert_delivery_summary() FROM PUBLIC;

-- 9. Update fc27_resolve_operator_alert to return TABLE (was_resolved BOOLEAN, alert_id UUID)
DROP FUNCTION IF EXISTS public.fc27_resolve_operator_alert(TEXT, TEXT);
CREATE OR REPLACE FUNCTION public.fc27_resolve_operator_alert(
    p_alert_type TEXT,
    p_component TEXT
)
RETURNS TABLE (
    was_resolved BOOLEAN,
    alert_id UUID
)
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
        RETURN QUERY SELECT TRUE, v_id;
        RETURN;
    END IF;

    RETURN QUERY SELECT FALSE, NULL::UUID;
END;
$$;

ALTER FUNCTION public.fc27_resolve_operator_alert(TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_resolve_operator_alert(TEXT, TEXT) FROM PUBLIC;
