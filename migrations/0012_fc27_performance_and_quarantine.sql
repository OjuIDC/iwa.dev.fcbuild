-- Migration 0012: Normalized FC 27 Performance Storage and Sanitized Quarantine Metadata
-- Authoritative Engine: PostgreSQL 16
-- Zero raw payload retention, zero personal/account/session fields, least-privilege role separation.

-- Helper function: validate that diagnostic_paths is a JSONB array of bounded field-path strings only (no raw values/objects/emails).
CREATE OR REPLACE FUNCTION public.fc27_is_valid_diagnostic_paths(p_paths jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_elem jsonb;
    v_text text;
BEGIN
    IF p_paths IS NULL OR pg_catalog.jsonb_typeof(p_paths) <> 'array' THEN
        RETURN false;
    END IF;

    IF pg_catalog.jsonb_array_length(p_paths) > 100 THEN
        RETURN false;
    END IF;

    IF pg_catalog.octet_length(p_paths::text) > 4096 THEN
        RETURN false;
    END IF;

    FOR v_elem IN SELECT pg_catalog.jsonb_array_elements(p_paths)
    LOOP
        IF pg_catalog.jsonb_typeof(v_elem) <> 'string' THEN
            RETURN false;
        END IF;
        v_text := v_elem #>> '{}';
        IF v_text IS NULL
           OR pg_catalog.char_length(pg_catalog.btrim(v_text)) < 1
           OR pg_catalog.char_length(v_text) > 128
           OR pg_catalog.strpos(v_text, '@') > 0
           OR v_text !~ '^[a-zA-Z0-9_.\[\]-]{1,128}$'
        THEN
            RETURN false;
        END IF;
    END LOOP;

    RETURN true;
END;
$$;

-- 2.1 public.fc27_clubs
CREATE TABLE public.fc27_clubs (
    club_id text NOT NULL,
    platform text NOT NULL DEFAULT 'common-gen5',
    game_version text NOT NULL DEFAULT 'FC27',
    club_name text NOT NULL,
    identity_state text NOT NULL DEFAULT 'PROVISIONAL',
    manager_authority_status text NOT NULL DEFAULT 'UNVERIFIED',
    source_classification text NOT NULL DEFAULT 'USER_SUPPLIED_EXPORT',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (club_id, platform, game_version),
    CONSTRAINT fc27_clubs_club_id_check CHECK (club_id ~ '^[0-9]{1,20}$'),
    CONSTRAINT fc27_clubs_platform_check CHECK (platform IN ('common-gen5', 'nx')),
    CONSTRAINT fc27_clubs_game_version_check CHECK (game_version = 'FC27'),
    CONSTRAINT fc27_clubs_club_name_check CHECK (
        char_length(btrim(club_name)) BETWEEN 1 AND 128
        AND left(btrim(club_name), 1) NOT IN ('=', '+', '-', '@')
    ),
    CONSTRAINT fc27_clubs_identity_state_check CHECK (
        identity_state IN (
            'UNVERIFIED',
            'SYNTHETIC_ONLY',
            'PROVISIONAL',
            'VERIFIED',
            'CONFLICTED',
            'RETIRED'
        )
    ),
    -- Club identity can never self-grant manager authority
    CONSTRAINT fc27_clubs_manager_authority_check CHECK (
        manager_authority_status = 'UNVERIFIED'
    ),
    CONSTRAINT fc27_clubs_source_classification_check CHECK (
        source_classification IN (
            'SYNTHETIC',
            'COMMUNITY_RESEARCH_DERIVED',
            'AUTHORIZED_CAPTURE',
            'USER_SUPPLIED_EXPORT'
        )
    )
);

-- 2.2 public.fc27_export_batches
CREATE TABLE public.fc27_export_batches (
    export_id text PRIMARY KEY,
    club_id text NOT NULL,
    platform text NOT NULL DEFAULT 'common-gen5',
    game_version text NOT NULL DEFAULT 'FC27',
    contract_version text NOT NULL DEFAULT '1.0.0',
    schema_version text NOT NULL DEFAULT '1.0.0',
    source_classification text NOT NULL DEFAULT 'USER_SUPPLIED_EXPORT',
    supplier_role text NOT NULL,
    supplier_reference text NOT NULL,
    consent_version text NOT NULL DEFAULT '1.0.0',
    consented_categories jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    total_matches integer NOT NULL DEFAULT 0,
    accepted_matches integer NOT NULL DEFAULT 0,
    quarantined_matches integer NOT NULL DEFAULT 0,
    applied_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fc27_export_batches_club_fk FOREIGN KEY (club_id, platform, game_version)
        REFERENCES public.fc27_clubs (club_id, platform, game_version)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_export_batches_export_id_check CHECK (
        char_length(btrim(export_id)) BETWEEN 1 AND 128
        AND position('/' in export_id) = 0
        AND position('\' in export_id) = 0
    ),
    CONSTRAINT fc27_export_batches_versions_check CHECK (
        char_length(btrim(contract_version)) BETWEEN 1 AND 32
        AND char_length(btrim(schema_version)) BETWEEN 1 AND 32
        AND char_length(btrim(consent_version)) BETWEEN 1 AND 32
    ),
    CONSTRAINT fc27_export_batches_source_classification_check CHECK (
        source_classification IN (
            'SYNTHETIC',
            'COMMUNITY_RESEARCH_DERIVED',
            'AUTHORIZED_CAPTURE',
            'USER_SUPPLIED_EXPORT'
        )
    ),
    CONSTRAINT fc27_export_batches_supplier_role_check CHECK (
        supplier_role IN ('CLUB_MANAGER', 'CLUB_ADMIN', 'DATA_STEWARD')
    ),
    CONSTRAINT fc27_export_batches_supplier_reference_check CHECK (
        char_length(btrim(supplier_reference)) BETWEEN 1 AND 128
        AND position('@' in supplier_reference) = 0
        AND position('/' in supplier_reference) = 0
        AND position('\' in supplier_reference) = 0
        AND supplier_reference !~* '(password|token|secret|bearer|session|cookie|account_id|persona_id|nucleus_id|gamertag|gamer_tag)'
    ),
    CONSTRAINT fc27_export_batches_consented_categories_check CHECK (
        jsonb_typeof(consented_categories) = 'array'
        AND jsonb_array_length(consented_categories) BETWEEN 1 AND 10
    ),
    CONSTRAINT fc27_export_batches_payload_sha256_check CHECK (
        payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT fc27_export_batches_counts_check CHECK (
        total_matches >= 0
        AND accepted_matches >= 0
        AND quarantined_matches >= 0
        AND (accepted_matches + quarantined_matches) <= total_matches
    )
);

-- 2.3 public.fc27_matches
CREATE TABLE public.fc27_matches (
    match_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id text NOT NULL,
    platform text NOT NULL DEFAULT 'common-gen5',
    game_version text NOT NULL DEFAULT 'FC27',
    external_match_id text NOT NULL,
    export_id text NOT NULL,
    played_at timestamptz NOT NULL,
    result text NOT NULL,
    goals_for integer NOT NULL,
    goals_against integer NOT NULL,
    opponent_name text NOT NULL,
    formation text NOT NULL DEFAULT 'UNKNOWN',
    match_status text NOT NULL DEFAULT 'COMPLETED',
    match_hash text NOT NULL,
    source_classification text NOT NULL DEFAULT 'USER_SUPPLIED_EXPORT',
    data_quality_status text NOT NULL DEFAULT 'VERIFIED',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fc27_matches_club_platform_version_ext_match_key UNIQUE (
        club_id,
        platform,
        game_version,
        external_match_id
    ),
    CONSTRAINT fc27_matches_club_fk FOREIGN KEY (club_id, platform, game_version)
        REFERENCES public.fc27_clubs (club_id, platform, game_version)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_matches_export_batch_fk FOREIGN KEY (export_id)
        REFERENCES public.fc27_export_batches (export_id)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_matches_external_match_id_check CHECK (
        char_length(btrim(external_match_id)) BETWEEN 1 AND 64
    ),
    CONSTRAINT fc27_matches_score_nonnegative_check CHECK (
        goals_for BETWEEN 0 AND 99 AND goals_against BETWEEN 0 AND 99
    ),
    CONSTRAINT fc27_matches_result_check CHECK (
        result IN ('WIN', 'DRAW', 'LOSS')
        AND (
            (goals_for > goals_against AND result = 'WIN')
            OR (goals_for = goals_against AND result = 'DRAW')
            OR (goals_for < goals_against AND result = 'LOSS')
        )
    ),
    CONSTRAINT fc27_matches_opponent_name_check CHECK (
        char_length(btrim(opponent_name)) BETWEEN 1 AND 128
        AND left(btrim(opponent_name), 1) NOT IN ('=', '+', '-', '@')
    ),
    CONSTRAINT fc27_matches_formation_check CHECK (
        formation IN (
            'UNKNOWN',
            '4-3-3', '4-3-3(2)', '4-3-3(4)',
            '4-2-3-1', '4-2-3-1(2)',
            '4-4-2', '4-4-2(2)',
            '4-1-2-1-2', '4-1-2-1-2(2)',
            '3-5-2', '3-4-2-1',
            '5-3-2', '5-2-1-2', '5-4-1'
        )
    ),
    CONSTRAINT fc27_matches_match_status_check CHECK (
        match_status IN ('COMPLETED', 'ABANDONED', 'PARTIAL')
    ),
    CONSTRAINT fc27_matches_match_hash_check CHECK (
        match_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT fc27_matches_source_classification_check CHECK (
        source_classification IN (
            'SYNTHETIC',
            'COMMUNITY_RESEARCH_DERIVED',
            'AUTHORIZED_CAPTURE',
            'USER_SUPPLIED_EXPORT'
        )
    ),
    CONSTRAINT fc27_matches_data_quality_status_check CHECK (
        data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED', 'REJECTED')
    )
);

-- 2.4 public.fc27_player_match_stats
CREATE TABLE public.fc27_player_match_stats (
    stat_id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    match_id uuid NOT NULL,
    club_id text NOT NULL,
    platform text NOT NULL DEFAULT 'common-gen5',
    game_version text NOT NULL DEFAULT 'FC27',
    external_match_id text NOT NULL,
    local_player_reference text NOT NULL,
    display_alias text NULL,
    alias_visibility_approved boolean NOT NULL DEFAULT false,
    position text NOT NULL,
    role text NULL,
    minutes_played integer NOT NULL DEFAULT 90,
    goals integer NOT NULL DEFAULT 0,
    assists integer NOT NULL DEFAULT 0,
    passes_attempted integer NOT NULL DEFAULT 0,
    passes_completed integer NOT NULL DEFAULT 0,
    tackles_attempted integer NOT NULL DEFAULT 0,
    tackles_won integer NOT NULL DEFAULT 0,
    interceptions integer NOT NULL DEFAULT 0,
    shots integer NOT NULL DEFAULT 0,
    shots_on_target integer NOT NULL DEFAULT 0,
    saves integer NOT NULL DEFAULT 0,
    possession_losses integer NOT NULL DEFAULT 0,
    provider_rating numeric(4,2) NOT NULL,
    build_reference text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fc27_player_match_stats_match_player_key UNIQUE (
        match_id,
        local_player_reference
    ),
    CONSTRAINT fc27_player_match_stats_match_fk FOREIGN KEY (match_id)
        REFERENCES public.fc27_matches (match_id)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_player_match_stats_club_fk FOREIGN KEY (club_id, platform, game_version)
        REFERENCES public.fc27_clubs (club_id, platform, game_version)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_player_match_stats_local_player_ref_check CHECK (
        char_length(btrim(local_player_reference)) BETWEEN 1 AND 64
        AND position('@' in local_player_reference) = 0
        AND local_player_reference !~* '(account_id|persona_id|nucleus_id|gamertag|gamer_tag|password|token|secret|cookie|session)'
    ),
    CONSTRAINT fc27_player_match_stats_display_alias_check CHECK (
        display_alias IS NULL
        OR (
            char_length(btrim(display_alias)) BETWEEN 1 AND 64
            AND position('@' in display_alias) = 0
            AND left(btrim(display_alias), 1) NOT IN ('=', '+', '-', '@')
        )
    ),
    CONSTRAINT fc27_player_match_stats_position_check CHECK (
        position IN (
            'UNKNOWN',
            'GK', 'CB', 'LB', 'RB', 'LWB', 'RWB',
            'CDM', 'CM', 'CAM', 'LM', 'RM',
            'LW', 'RW', 'ST', 'CF'
        )
    ),
    CONSTRAINT fc27_player_match_stats_role_check CHECK (
        role IS NULL
        OR role IN (
            'UNKNOWN',
            'Poacher', 'Target Forward', 'Advanced Forward', 'False 9',
            'Winger', 'Inside Forward', 'Playmaker', 'Shadow Striker',
            'Box-to-Box', 'Deep Lying Playmaker', 'Holding', 'Anchor',
            'Ball Playing Defender', 'Stopper', 'Cover',
            'Fullback', 'Attacking Fullback', 'Inverted Fullback', 'Wingback',
            'Sweeper Keeper', 'Defensive Goalkeeper'
        )
    ),
    CONSTRAINT fc27_player_match_stats_counts_nonnegative_check CHECK (
        minutes_played BETWEEN 0 AND 150
        AND goals BETWEEN 0 AND 30
        AND assists BETWEEN 0 AND 30
        AND passes_attempted BETWEEN 0 AND 500
        AND passes_completed BETWEEN 0 AND 500
        AND tackles_attempted BETWEEN 0 AND 100
        AND tackles_won BETWEEN 0 AND 100
        AND interceptions BETWEEN 0 AND 100
        AND shots BETWEEN 0 AND 100
        AND shots_on_target BETWEEN 0 AND 100
        AND saves BETWEEN 0 AND 100
        AND possession_losses BETWEEN 0 AND 200
    ),
    CONSTRAINT fc27_player_match_stats_completed_le_attempted_check CHECK (
        passes_completed <= passes_attempted
        AND tackles_won <= tackles_attempted
        AND shots_on_target <= shots
    ),
    CONSTRAINT fc27_player_match_stats_rating_range_check CHECK (
        provider_rating >= 0.0 AND provider_rating <= 10.0
    ),
    CONSTRAINT fc27_player_match_stats_build_reference_check CHECK (
        build_reference IS NULL
        OR char_length(btrim(build_reference)) BETWEEN 1 AND 64
    )
);

-- 2.5 public.fc27_quarantine_records
CREATE TABLE public.fc27_quarantine_records (
    quarantine_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id text NOT NULL,
    platform text NOT NULL DEFAULT 'common-gen5',
    game_version text NOT NULL DEFAULT 'FC27',
    external_match_id text NULL,
    export_id text NULL,
    source_classification text NOT NULL DEFAULT 'USER_SUPPLIED_EXPORT',
    anomaly_category text NOT NULL,
    reason_code text NOT NULL,
    diagnostic_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
    payload_sha256 text NOT NULL,
    record_hash text NULL,
    review_status text NOT NULL DEFAULT 'PENDING_REVIEW',
    quarantined_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '30 days'),
    reviewed_by text NULL,
    reviewed_at timestamptz NULL,
    review_notes text NULL,
    CONSTRAINT fc27_quarantine_records_club_id_check CHECK (
        club_id ~ '^[0-9]{1,20}$'
    ),
    CONSTRAINT fc27_quarantine_records_platform_check CHECK (
        platform IN ('common-gen5', 'nx')
    ),
    CONSTRAINT fc27_quarantine_records_game_version_check CHECK (
        game_version = 'FC27'
    ),
    CONSTRAINT fc27_quarantine_records_ext_match_id_check CHECK (
        external_match_id IS NULL
        OR char_length(btrim(external_match_id)) BETWEEN 1 AND 64
    ),
    CONSTRAINT fc27_quarantine_records_export_id_check CHECK (
        export_id IS NULL
        OR char_length(btrim(export_id)) BETWEEN 1 AND 128
    ),
    CONSTRAINT fc27_quarantine_records_source_classification_check CHECK (
        source_classification IN (
            'SYNTHETIC',
            'COMMUNITY_RESEARCH_DERIVED',
            'AUTHORIZED_CAPTURE',
            'USER_SUPPLIED_EXPORT'
        )
    ),
    CONSTRAINT fc27_quarantine_records_anomaly_category_check CHECK (
        anomaly_category IN (
            'VALIDATION_ANOMALY',
            'SCHEMA_DRIFT',
            'OUT_OF_RANGE_OBSERVATION',
            'REPLAY_CONFLICT',
            'INTEGRITY_MISMATCH',
            'REVIEW_REQUIRED',
            'SENSITIVE_FIELD_REJECTED'
        )
    ),
    CONSTRAINT fc27_quarantine_records_reason_code_check CHECK (
        char_length(btrim(reason_code)) BETWEEN 1 AND 128
        AND reason_code ~ '^[A-Z0-9_]{1,128}$'
    ),
    CONSTRAINT fc27_quarantine_records_diagnostic_paths_check CHECK (
        public.fc27_is_valid_diagnostic_paths(diagnostic_paths)
    ),
    CONSTRAINT fc27_quarantine_records_payload_sha256_check CHECK (
        payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT fc27_quarantine_records_record_hash_check CHECK (
        record_hash IS NULL
        OR record_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT fc27_quarantine_records_review_status_check CHECK (
        review_status IN (
            'PENDING_REVIEW',
            'APPROVED_OVERRIDE',
            'REJECTED_PERMANENT',
            'RESOLVED',
            'EXPIRED'
        )
    ),
    CONSTRAINT fc27_quarantine_records_expiry_check CHECK (
        expires_at > quarantined_at
    ),
    CONSTRAINT fc27_quarantine_records_review_notes_check CHECK (
        review_notes IS NULL
        OR (
            char_length(review_notes) <= 500
            AND position('@' in review_notes) = 0
            AND review_notes !~* '(password|token|secret|bearer|session|cookie|account_id|persona_id|nucleus_id|gamertag|gamer_tag)'
        )
    ),
    CONSTRAINT fc27_quarantine_records_review_consistency_check CHECK (
        (
            review_status = 'PENDING_REVIEW'
            AND reviewed_by IS NULL
            AND reviewed_at IS NULL
        )
        OR (
            review_status IN ('APPROVED_OVERRIDE', 'REJECTED_PERMANENT', 'RESOLVED')
            AND reviewed_by IS NOT NULL
            AND char_length(btrim(reviewed_by)) BETWEEN 1 AND 128
            AND position('@' in reviewed_by) = 0
            AND reviewed_at IS NOT NULL
        )
        OR (
            review_status = 'EXPIRED'
            AND (reviewed_by IS NULL) = (reviewed_at IS NULL)
        )
    )
);

-- 2.6 Indexes
CREATE INDEX idx_fc27_matches_club_played_at
    ON public.fc27_matches (club_id, platform, game_version, played_at DESC);

CREATE INDEX idx_fc27_matches_export_id
    ON public.fc27_matches (export_id);

CREATE INDEX idx_fc27_player_match_stats_match_id
    ON public.fc27_player_match_stats (match_id);

CREATE INDEX idx_fc27_player_match_stats_club_player
    ON public.fc27_player_match_stats (club_id, platform, game_version, local_player_reference);

CREATE INDEX idx_fc27_quarantine_records_status_expires
    ON public.fc27_quarantine_records (review_status, expires_at);

CREATE INDEX idx_fc27_quarantine_records_club_quarantined_at
    ON public.fc27_quarantine_records (club_id, platform, game_version, quarantined_at DESC);

CREATE INDEX idx_fc27_export_batches_club_applied_at
    ON public.fc27_export_batches (club_id, platform, game_version, applied_at DESC);

-- Narrowly authorized SECURITY DEFINER function for expired quarantine purging
CREATE OR REPLACE FUNCTION public.fc27_purge_expired_quarantine(
    p_now timestamptz DEFAULT now()
)
RETURNS TABLE (
    purged_quarantine_id uuid,
    purged_club_id text,
    purged_platform text,
    purged_game_version text,
    purged_source_classification text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF session_user NOT IN ('fc27_app', 'fc27_owner', 'fc27_migrator') THEN
        RAISE EXCEPTION 'expired quarantine purge not authorized for role %', session_user
            USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    DELETE FROM public.fc27_quarantine_records AS q
    WHERE q.expires_at <= COALESCE(p_now, pg_catalog.now())
    RETURNING
        q.quarantine_id,
        q.club_id,
        q.platform,
        q.game_version,
        q.source_classification;
END;
$$;
