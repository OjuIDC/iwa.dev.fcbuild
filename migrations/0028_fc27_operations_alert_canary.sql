-- Migration 0028: Controlled FC27 operations alert canary contract
-- Additive, idempotent schema migration adding narrow canary procedures,
-- constrained INFO severity, and isolating synthetic canaries from general procedures.

-- 1. Severity check constraint allowing INFO only for OPERATIONS_CANARY with operations-alert-dispatch
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fc27_operator_alerts_severity_check'
          AND conrelid = 'public.fc27_operator_alerts'::regclass
    ) THEN
        ALTER TABLE public.fc27_operator_alerts
            DROP CONSTRAINT fc27_operator_alerts_severity_check;
    END IF;

    ALTER TABLE public.fc27_operator_alerts
        ADD CONSTRAINT fc27_operator_alerts_severity_check
        CHECK (
            (severity IN ('WARNING', 'CRITICAL') AND alert_type <> 'OPERATIONS_CANARY')
            OR
            (severity = 'INFO' AND alert_type = 'OPERATIONS_CANARY' AND component = 'operations-alert-dispatch')
        );
END;
$$;

-- 2. Restrict general record_operator_alert procedure so it cannot create OPERATIONS_CANARY
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
    IF p_alert_type = 'OPERATIONS_CANARY' THEN
        RAISE EXCEPTION 'OPERATIONS_CANARY cannot be recorded through general alert procedure';
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

-- 3. Restrict general resolve_operator_alert procedure so it cannot resolve OPERATIONS_CANARY
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
    IF p_alert_type = 'OPERATIONS_CANARY' THEN
        RAISE EXCEPTION 'OPERATIONS_CANARY cannot be resolved through general alert procedure';
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
    ELSE
        RETURN QUERY SELECT FALSE, NULL::UUID;
    END IF;
END;
$$;

ALTER FUNCTION public.fc27_resolve_operator_alert(TEXT, TEXT) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_resolve_operator_alert(TEXT, TEXT) FROM PUBLIC;

-- 4. Narrow procedure to open an operations alert canary and reserve its OPEN delivery
CREATE OR REPLACE FUNCTION public.fc27_open_operations_alert_canary()
RETURNS TABLE (
    is_new BOOLEAN,
    lifecycle_status TEXT,
    delivery_id BIGINT,
    delivery_state TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_dest RECORD;
    v_rec RECORD;
    v_new_alert_id UUID;
    v_target_alert_id UUID;
    v_is_new_alert BOOLEAN := FALSE;
    v_canonical TEXT;
    v_tag_hash TEXT;
    v_marker_tag TEXT;
    v_marker_digest TEXT;
    v_del RECORD;
BEGIN
    -- Validate destination in registry
    SELECT destination_key, enabled, message_kind
      INTO v_dest
      FROM public.fc27_discord_destinations
     WHERE destination_key = 'fc27-operations';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Destination fc27-operations not found in registry';
    END IF;
    IF NOT v_dest.enabled THEN
        RAISE EXCEPTION 'Destination fc27-operations is disabled';
    END IF;
    IF v_dest.message_kind <> 'OPERATIONS_ALERT' THEN
        RAISE EXCEPTION 'Destination fc27-operations message_kind must be OPERATIONS_ALERT';
    END IF;

    -- Concurrency safety via transactional advisory lock
    PERFORM pg_advisory_xact_lock(hashtext('fc27_operator_alert'), hashtext('OPERATIONS_CANARY:operations-alert-dispatch'));

    -- Check for existing active canary alert
    SELECT a.alert_id, a.status
      INTO v_rec
      FROM public.fc27_operator_alerts a
     WHERE a.alert_type = 'OPERATIONS_CANARY'
       AND a.component = 'operations-alert-dispatch'
       AND a.status IN ('OPEN', 'ACKNOWLEDGED')
     FOR UPDATE;

    IF FOUND THEN
        v_target_alert_id := v_rec.alert_id;
        v_is_new_alert := FALSE;
    ELSE
        v_new_alert_id := gen_random_uuid();
        v_target_alert_id := v_new_alert_id;
        v_is_new_alert := TRUE;

        INSERT INTO public.fc27_operator_alerts (
            alert_id,
            alert_type,
            component,
            severity,
            status,
            summary,
            details,
            opened_at,
            last_notified_at,
            notification_count
        ) VALUES (
            v_new_alert_id,
            'OPERATIONS_CANARY',
            'operations-alert-dispatch',
            'INFO',
            'OPEN',
            'FC27 operations alert delivery canary',
            '{"recommended_action": "No action required; controlled verification only"}'::jsonb,
            clock_timestamp(),
            clock_timestamp(),
            1
        );
    END IF;

    -- Compute deterministic marker tag and digest
    v_canonical := v_target_alert_id::text || ':OPEN:OPERATIONS_CANARY:operations-alert-dispatch:fc27-operations:OPERATIONS_ALERT';
    v_tag_hash := substr(encode(sha256(v_canonical::bytea), 'hex'), 1, 16);
    v_marker_tag := 'fc27-ops-' || v_tag_hash;
    v_marker_digest := encode(sha256(v_marker_tag::bytea), 'hex');

    -- Reserve delivery row idempotently
    SELECT r.delivery_id, r.delivery_state
      INTO v_del
      FROM public.fc27_reserve_operator_alert_delivery(
          v_target_alert_id,
          'OPEN',
          'fc27-operations',
          v_marker_digest
      ) r;

    RETURN QUERY
    SELECT v_is_new_alert,
           'OPEN'::TEXT,
           v_del.delivery_id,
           v_del.delivery_state;
END;
$$;

ALTER FUNCTION public.fc27_open_operations_alert_canary() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_open_operations_alert_canary() FROM PUBLIC;

-- 5. Narrow procedure to resolve an active operations alert canary and reserve its RESOLVED delivery
CREATE OR REPLACE FUNCTION public.fc27_resolve_operations_alert_canary()
RETURNS TABLE (
    was_resolved BOOLEAN,
    lifecycle_status TEXT,
    delivery_id BIGINT,
    delivery_state TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_dest RECORD;
    v_rec RECORD;
    v_last_resolved RECORD;
    v_canonical TEXT;
    v_tag_hash TEXT;
    v_marker_tag TEXT;
    v_marker_digest TEXT;
    v_del RECORD;
BEGIN
    -- Validate destination in registry
    SELECT destination_key, enabled, message_kind
      INTO v_dest
      FROM public.fc27_discord_destinations
     WHERE destination_key = 'fc27-operations';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Destination fc27-operations not found in registry';
    END IF;
    IF NOT v_dest.enabled THEN
        RAISE EXCEPTION 'Destination fc27-operations is disabled';
    END IF;
    IF v_dest.message_kind <> 'OPERATIONS_ALERT' THEN
        RAISE EXCEPTION 'Destination fc27-operations message_kind must be OPERATIONS_ALERT';
    END IF;

    -- Concurrency safety via transactional advisory lock
    PERFORM pg_advisory_xact_lock(hashtext('fc27_operator_alert'), hashtext('OPERATIONS_CANARY:operations-alert-dispatch'));

    -- Check for existing active canary alert
    SELECT a.alert_id, a.status
      INTO v_rec
      FROM public.fc27_operator_alerts a
     WHERE a.alert_type = 'OPERATIONS_CANARY'
       AND a.component = 'operations-alert-dispatch'
       AND a.status IN ('OPEN', 'ACKNOWLEDGED')
     FOR UPDATE;

    IF FOUND THEN
        -- Transition active canary to RESOLVED
        UPDATE public.fc27_operator_alerts
           SET status = 'RESOLVED',
               resolved_at = clock_timestamp()
         WHERE alert_id = v_rec.alert_id;

        -- Compute deterministic marker tag and digest for RESOLVED event
        v_canonical := v_rec.alert_id::text || ':RESOLVED:OPERATIONS_CANARY:operations-alert-dispatch:fc27-operations:OPERATIONS_ALERT';
        v_tag_hash := substr(encode(sha256(v_canonical::bytea), 'hex'), 1, 16);
        v_marker_tag := 'fc27-ops-' || v_tag_hash;
        v_marker_digest := encode(sha256(v_marker_tag::bytea), 'hex');

        -- Reserve RESOLVED delivery row idempotently
        SELECT r.delivery_id, r.delivery_state
          INTO v_del
          FROM public.fc27_reserve_operator_alert_delivery(
              v_rec.alert_id,
              'RESOLVED',
              'fc27-operations',
              v_marker_digest
          ) r;

        RETURN QUERY
        SELECT TRUE,
               'RESOLVED'::TEXT,
               v_del.delivery_id,
               v_del.delivery_state;
    ELSE
        -- No active canary. Return idempotent result if a previously resolved canary exists
        SELECT a.alert_id, d.delivery_id, d.delivery_state
          INTO v_last_resolved
          FROM public.fc27_operator_alerts a
          LEFT JOIN public.fc27_operator_alert_deliveries d
            ON d.alert_id = a.alert_id
           AND d.lifecycle_event = 'RESOLVED'
           AND d.destination_key = 'fc27-operations'
         WHERE a.alert_type = 'OPERATIONS_CANARY'
           AND a.component = 'operations-alert-dispatch'
           AND a.status = 'RESOLVED'
         ORDER BY a.resolved_at DESC NULLS LAST
         LIMIT 1;

        IF FOUND THEN
            RETURN QUERY
            SELECT FALSE,
                   'RESOLVED'::TEXT,
                   v_last_resolved.delivery_id,
                   v_last_resolved.delivery_state;
        ELSE
            RETURN QUERY
            SELECT FALSE,
                   'NOT_FOUND'::TEXT,
                   NULL::BIGINT,
                   NULL::TEXT;
        END IF;
    END IF;
END;
$$;

ALTER FUNCTION public.fc27_resolve_operations_alert_canary() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_resolve_operations_alert_canary() FROM PUBLIC;

