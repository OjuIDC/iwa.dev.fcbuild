-- =============================================================================
-- Migration 0014: FC 27 Player Development, Linking, Build Catalog, Suggestion
-- Tracking, and Discord Job Queue Foundation
-- =============================================================================
-- Authoritative Engine: PostgreSQL 16
--
-- Creates seven purpose-specific tables owned by fc27_owner:
--   1. public.fc27_player_alias_fingerprints
--   2. public.fc27_player_links
--   3. public.fc27_build_catalog
--   4. public.fc27_build_catalog_audit
--   5. public.fc27_player_suggestions
--   6. public.fc27_suggestion_assessments
--   7. public.fc27_discord_jobs
--
-- Data-Integrity & Privacy Invariants:
--   - Zero columns named gamertag or player_name; zero raw EA player-ID columns.
--   - Player identity uses deterministic pseudonyms matching ^plr_ecf_[0-9a-f]{12}$.
--   - Alias fingerprints store only lowercase 64-hex HMAC-SHA256 digests; the
--     HMAC key is never stored in PostgreSQL.
--   - Collision policy on fc27_player_alias_fingerprints: (pseudonym) is PRIMARY
--     KEY and (gamertag_hmac) is UNIQUE; a conflicting mapping for an existing
--     pseudonym or gamertag_hmac fails closed with unique_violation (23505).
--   - Discord user, channel, message, and thread IDs use lossless numeric
--     snowflake strings (^[1-9][0-9]{0,19}$); no Discord usernames or display
--     names are stored.
--   - Build catalog definitions are versioned ((build_key, version) UNIQUE,
--     version >= 1) and are never auto-seeded by migrations.
--   - fc27_build_catalog_audit and fc27_suggestion_assessments are enforced
--     append-only by both BEFORE UPDATE OR DELETE database triggers (ERRCODE
--     42501) and least-privilege table grants (SELECT, INSERT only).
--   - fc27_discord_jobs is an independent, idempotent queue with zero foreign
--     dependencies on historical dispatch or outbox tables.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Validation Helper Functions (owned by fc27_owner, IMMUTABLE, safe search_path)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.fc27_is_valid_catalog_positions(p_positions jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_elem jsonb;
    v_pos text;
BEGIN
    IF p_positions IS NULL OR pg_catalog.jsonb_typeof(p_positions) <> 'array' THEN
        RETURN false;
    END IF;

    IF pg_catalog.jsonb_array_length(p_positions) < 1
       OR pg_catalog.jsonb_array_length(p_positions) > 16
       OR pg_catalog.octet_length(p_positions::text) > 512
    THEN
        RETURN false;
    END IF;

    FOR v_elem IN SELECT pg_catalog.jsonb_array_elements(p_positions)
    LOOP
        IF pg_catalog.jsonb_typeof(v_elem) <> 'string' THEN
            RETURN false;
        END IF;
        v_pos := v_elem #>> '{}';
        IF v_pos IS NULL
           OR v_pos NOT IN (
               'GK', 'CB', 'LB', 'RB', 'LWB', 'RWB',
               'CDM', 'CM', 'CAM', 'LM', 'RM',
               'LW', 'RW', 'ST', 'CF'
           )
        THEN
            RETURN false;
        END IF;
    END LOOP;

    RETURN true;
END;
$$;

ALTER FUNCTION public.fc27_is_valid_catalog_positions(jsonb) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_is_valid_catalog_positions(jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.fc27_is_safe_bounded_jsonb(
    p_doc jsonb,
    p_expected_type text,
    p_require_non_empty boolean,
    p_max_bytes integer
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_serialized text;
BEGIN
    IF p_doc IS NULL OR pg_catalog.jsonb_typeof(p_doc) <> p_expected_type THEN
        RETURN false;
    END IF;

    v_serialized := p_doc::text;
    IF pg_catalog.octet_length(v_serialized) > p_max_bytes THEN
        RETURN false;
    END IF;

    IF p_require_non_empty THEN
        IF p_expected_type = 'array' AND pg_catalog.jsonb_array_length(p_doc) < 1 THEN
            RETURN false;
        END IF;
        IF p_expected_type = 'object' AND p_doc = '{}'::jsonb THEN
            RETURN false;
        END IF;
    END IF;

    IF v_serialized ~ '[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]' THEN
        RETURN false;
    END IF;

    IF v_serialized ~* '(https?://|www\.|@(everyone|here)|gamertag|gamer_tag|player_name|playername|proname|persona_id|personaid|nucleus_id|nucleusid|blaze_id|blazeid|account_id|raw_response|raw_payload|model_name|prompt_version|llm_|password|secret|bearer)' THEN
        RETURN false;
    END IF;

    RETURN true;
END;
$$;

ALTER FUNCTION public.fc27_is_safe_bounded_jsonb(jsonb, text, boolean, integer) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_is_safe_bounded_jsonb(jsonb, text, boolean, integer) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.fc27_is_valid_suggestion_objective(p_obj jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_key text;
    v_metric text;
    v_direction text;
    v_min_text text;
    v_max_text text;
    v_min integer;
    v_max integer;
BEGIN
    IF NOT public.fc27_is_safe_bounded_jsonb(p_obj, 'object', true, 2048) THEN
        RETURN false;
    END IF;

    IF NOT (
        p_obj ? 'metric'
        AND p_obj ? 'direction'
        AND p_obj ? 'target_delta'
        AND p_obj ? 'min_matches'
        AND p_obj ? 'max_matches'
    ) THEN
        RETURN false;
    END IF;

    FOR v_key IN SELECT pg_catalog.jsonb_object_keys(p_obj)
    LOOP
        IF v_key NOT IN ('metric', 'direction', 'target_delta', 'min_matches', 'max_matches') THEN
            RETURN false;
        END IF;
    END LOOP;

    IF pg_catalog.jsonb_typeof(p_obj -> 'metric') <> 'string'
       OR pg_catalog.jsonb_typeof(p_obj -> 'direction') <> 'string'
       OR pg_catalog.jsonb_typeof(p_obj -> 'target_delta') <> 'number'
       OR pg_catalog.jsonb_typeof(p_obj -> 'min_matches') <> 'number'
       OR pg_catalog.jsonb_typeof(p_obj -> 'max_matches') <> 'number'
    THEN
        RETURN false;
    END IF;

    v_metric := p_obj ->> 'metric';
    v_direction := p_obj ->> 'direction';
    v_min_text := p_obj ->> 'min_matches';
    v_max_text := p_obj ->> 'max_matches';

    IF v_metric IS NULL OR v_metric !~ '^[a-z][a-z0-9_]{1,63}$' THEN
        RETURN false;
    END IF;

    IF v_direction IS NULL OR v_direction NOT IN ('increase', 'decrease', 'maintain', 'at_least', 'at_most') THEN
        RETURN false;
    END IF;

    IF v_min_text IS NULL OR v_min_text !~ '^[0-9]{1,2}$'
       OR v_max_text IS NULL OR v_max_text !~ '^[0-9]{1,2}$'
    THEN
        RETURN false;
    END IF;

    v_min := v_min_text::integer;
    v_max := v_max_text::integer;

    IF v_min < 1 OR v_max < 1 OR v_max > 50 OR v_min > v_max THEN
        RETURN false;
    END IF;

    RETURN true;
END;
$$;

ALTER FUNCTION public.fc27_is_valid_suggestion_objective(jsonb) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_is_valid_suggestion_objective(jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.fc27_is_valid_match_id_array(p_ids text[])
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_id text;
    v_len integer;
BEGIN
    IF p_ids IS NULL OR pg_catalog.array_ndims(p_ids) <> 1 THEN
        RETURN false;
    END IF;

    v_len := pg_catalog.cardinality(p_ids);
    IF v_len IS NULL OR v_len < 1 OR v_len > 50 THEN
        RETURN false;
    END IF;

    FOREACH v_id IN ARRAY p_ids
    LOOP
        IF v_id IS NULL
           OR pg_catalog.char_length(pg_catalog.btrim(v_id)) < 1
           OR pg_catalog.char_length(v_id) > 64
           OR v_id !~ '^[a-zA-Z0-9_:-]{1,64}$'
        THEN
            RETURN false;
        END IF;
    END LOOP;

    RETURN true;
END;
$$;

ALTER FUNCTION public.fc27_is_valid_match_id_array(text[]) OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_is_valid_match_id_array(text[]) FROM PUBLIC;

-- -----------------------------------------------------------------------------
-- 1. public.fc27_player_alias_fingerprints
-- -----------------------------------------------------------------------------
-- Collision policy:
--   - `pseudonym` is the PRIMARY KEY (`plr_ecf_<12 lowercase hex>`).
--   - `gamertag_hmac` has a UNIQUE constraint (`^[0-9a-f]{64}$`).
--   - Re-observing the same `(pseudonym, gamertag_hmac)` updates `last_seen_at`
--     and `updated_at`. Any attempt to bind an existing `pseudonym` to a
--     different `gamertag_hmac` or an existing `gamertag_hmac` to a different
--     `pseudonym` fails closed at the database constraint boundary (`23505`).
--   - Neither plaintext gamertags, raw EA player IDs, nor HMAC keys are stored.
CREATE TABLE IF NOT EXISTS public.fc27_player_alias_fingerprints (
    pseudonym       text PRIMARY KEY,
    gamertag_hmac   text NOT NULL,
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fc27_player_alias_fingerprints_pseudonym_chk
        CHECK (pseudonym ~ '^plr_ecf_[0-9a-f]{12}$'),
    CONSTRAINT fc27_player_alias_fingerprints_hmac_chk
        CHECK (gamertag_hmac ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fc27_player_alias_fingerprints_hmac_uk
        UNIQUE (gamertag_hmac),
    CONSTRAINT fc27_player_alias_fingerprints_timestamps_chk
        CHECK (
            updated_at >= created_at
            AND last_seen_at >= created_at
            AND created_at >= '2024-01-01 00:00:00+00'::timestamptz
        )
);

ALTER TABLE public.fc27_player_alias_fingerprints OWNER TO fc27_owner;

CREATE INDEX IF NOT EXISTS idx_fc27_player_alias_fingerprints_last_seen
    ON public.fc27_player_alias_fingerprints (last_seen_at DESC, pseudonym ASC);

-- -----------------------------------------------------------------------------
-- 2. public.fc27_player_links
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_player_links (
    link_id         bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    pseudonym       text NOT NULL,
    discord_user_id text NOT NULL,
    status          text NOT NULL,
    requested_by    text NOT NULL,
    confirmed_by    text NULL,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    confirmed_at    timestamptz NULL,
    revoked_at      timestamptz NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fc27_player_links_pseudonym_chk
        CHECK (pseudonym ~ '^plr_ecf_[0-9a-f]{12}$'),
    CONSTRAINT fc27_player_links_discord_user_id_chk
        CHECK (discord_user_id ~ '^[1-9][0-9]{0,19}$'),
    CONSTRAINT fc27_player_links_status_chk
        CHECK (status IN ('pending', 'confirmed', 'revoked')),
    CONSTRAINT fc27_player_links_requested_by_chk
        CHECK (requested_by ~ '^[1-9][0-9]{0,19}$'),
    CONSTRAINT fc27_player_links_confirmed_by_chk
        CHECK (confirmed_by IS NULL OR confirmed_by ~ '^[1-9][0-9]{0,19}$'),
    CONSTRAINT fc27_player_links_lifecycle_chk
        CHECK (
            (
                status = 'pending'
                AND confirmed_by IS NULL
                AND confirmed_at IS NULL
                AND revoked_at IS NULL
            )
            OR (
                status = 'confirmed'
                AND confirmed_by IS NOT NULL
                AND confirmed_at IS NOT NULL
                AND confirmed_at >= requested_at
                AND revoked_at IS NULL
            )
            OR (
                status = 'revoked'
                AND revoked_at IS NOT NULL
                AND revoked_at >= requested_at
                AND (
                    (confirmed_by IS NULL AND confirmed_at IS NULL)
                    OR (
                        confirmed_by IS NOT NULL
                        AND confirmed_at IS NOT NULL
                        AND confirmed_at >= requested_at
                        AND revoked_at >= confirmed_at
                    )
                )
            )
        ),
    CONSTRAINT fc27_player_links_timestamps_chk
        CHECK (
            updated_at >= created_at
            AND updated_at >= requested_at
        )
);

ALTER TABLE public.fc27_player_links OWNER TO fc27_owner;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fc27_player_links_confirmed_pseudonym
    ON public.fc27_player_links (pseudonym)
    WHERE status = 'confirmed';

CREATE UNIQUE INDEX IF NOT EXISTS uq_fc27_player_links_confirmed_discord_user
    ON public.fc27_player_links (discord_user_id)
    WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS idx_fc27_player_links_pseudonym_status
    ON public.fc27_player_links (pseudonym, status, updated_at DESC, link_id DESC);

CREATE INDEX IF NOT EXISTS idx_fc27_player_links_discord_user_status
    ON public.fc27_player_links (discord_user_id, status, updated_at DESC, link_id DESC);

-- -----------------------------------------------------------------------------
-- 3. public.fc27_build_catalog
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_build_catalog (
    catalog_row_id          bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    build_key               text NOT NULL,
    name                    text NOT NULL,
    positions               jsonb NOT NULL,
    archetype               text NOT NULL,
    attribute_priorities    jsonb NOT NULL,
    playstyles              jsonb NOT NULL,
    match_rules             jsonb NOT NULL,
    game_patch              text NOT NULL,
    notes                   text NULL,
    version                 integer NOT NULL,
    status                  text NOT NULL,
    created_by              text NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fc27_build_catalog_key_version_uk
        UNIQUE (build_key, version),
    CONSTRAINT fc27_build_catalog_build_key_chk
        CHECK (build_key ~ '^[a-z0-9][a-z0-9_-]{1,63}$'),
    CONSTRAINT fc27_build_catalog_name_chk
        CHECK (
            char_length(btrim(name)) BETWEEN 1 AND 80
            AND name !~ '[\x00-\x1F\x7F]'
            AND name !~* '(https?://|www\.|@(everyone|here)|gamertag|gamer_tag|persona_id|nucleus_id)'
        ),
    CONSTRAINT fc27_build_catalog_positions_chk
        CHECK (public.fc27_is_valid_catalog_positions(positions)),
    CONSTRAINT fc27_build_catalog_archetype_chk
        CHECK (
            char_length(btrim(archetype)) BETWEEN 1 AND 64
            AND archetype !~ '[\x00-\x1F\x7F]'
            AND archetype !~* '(https?://|www\.|@(everyone|here)|gamertag|gamer_tag|persona_id|nucleus_id)'
        ),
    CONSTRAINT fc27_build_catalog_attribute_priorities_chk
        CHECK (
            public.fc27_is_safe_bounded_jsonb(attribute_priorities, 'array', true, 4096)
            OR public.fc27_is_safe_bounded_jsonb(attribute_priorities, 'object', true, 4096)
        ),
    CONSTRAINT fc27_build_catalog_playstyles_chk
        CHECK (
            public.fc27_is_safe_bounded_jsonb(playstyles, 'array', false, 4096)
            OR public.fc27_is_safe_bounded_jsonb(playstyles, 'object', false, 4096)
        ),
    CONSTRAINT fc27_build_catalog_match_rules_chk
        CHECK (public.fc27_is_safe_bounded_jsonb(match_rules, 'object', false, 4096)),
    CONSTRAINT fc27_build_catalog_game_patch_chk
        CHECK (
            char_length(btrim(game_patch)) BETWEEN 1 AND 32
            AND game_patch !~ '[\x00-\x1F\x7F]'
            AND game_patch !~* '(https?://|www\.|@(everyone|here))'
        ),
    CONSTRAINT fc27_build_catalog_notes_chk
        CHECK (
            notes IS NULL
            OR (
                char_length(btrim(notes)) BETWEEN 1 AND 500
                AND notes !~ '[\x00-\x1F\x7F]'
                AND notes !~* '(https?://|www\.|@(everyone|here)|gamertag|gamer_tag|persona_id|nucleus_id|blaze_id|account_id)'
            )
        ),
    CONSTRAINT fc27_build_catalog_version_chk
        CHECK (version >= 1),
    CONSTRAINT fc27_build_catalog_status_chk
        CHECK (status IN ('active', 'retired')),
    CONSTRAINT fc27_build_catalog_created_by_chk
        CHECK (created_by ~ '^[1-9][0-9]{0,19}$')
);

ALTER TABLE public.fc27_build_catalog OWNER TO fc27_owner;

CREATE INDEX IF NOT EXISTS idx_fc27_build_catalog_status_key_version
    ON public.fc27_build_catalog (status, build_key ASC, version DESC);

-- -----------------------------------------------------------------------------
-- 4. public.fc27_build_catalog_audit (Append-Only Audit Trail)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_build_catalog_audit (
    audit_id                bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    catalog_row_id          bigint NOT NULL,
    build_key               text NOT NULL,
    version                 integer NOT NULL,
    action                  text NOT NULL,
    actor_discord_user_id   text NOT NULL,
    snapshot                jsonb NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fc27_build_catalog_audit_row_fk
        FOREIGN KEY (catalog_row_id)
        REFERENCES public.fc27_build_catalog (catalog_row_id)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_build_catalog_audit_key_version_fk
        FOREIGN KEY (build_key, version)
        REFERENCES public.fc27_build_catalog (build_key, version)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_build_catalog_audit_build_key_chk
        CHECK (build_key ~ '^[a-z0-9][a-z0-9_-]{1,63}$'),
    CONSTRAINT fc27_build_catalog_audit_version_chk
        CHECK (version >= 1),
    CONSTRAINT fc27_build_catalog_audit_action_chk
        CHECK (action IN ('create', 'update', 'retire')),
    CONSTRAINT fc27_build_catalog_audit_actor_chk
        CHECK (actor_discord_user_id ~ '^[1-9][0-9]{0,19}$'),
    CONSTRAINT fc27_build_catalog_audit_snapshot_chk
        CHECK (public.fc27_is_safe_bounded_jsonb(snapshot, 'object', true, 16384))
);

ALTER TABLE public.fc27_build_catalog_audit OWNER TO fc27_owner;

CREATE INDEX IF NOT EXISTS idx_fc27_build_catalog_audit_key_created
    ON public.fc27_build_catalog_audit (build_key, version DESC, created_at DESC, audit_id DESC);

CREATE OR REPLACE FUNCTION public.fc27_forbid_build_catalog_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'fc27_build_catalog_audit is append-only; % is prohibited', TG_OP
        USING ERRCODE = '42501';
END;
$$;

ALTER FUNCTION public.fc27_forbid_build_catalog_audit_mutation() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_forbid_build_catalog_audit_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_fc27_build_catalog_audit_immutable ON public.fc27_build_catalog_audit;
CREATE TRIGGER trg_fc27_build_catalog_audit_immutable
BEFORE UPDATE OR DELETE ON public.fc27_build_catalog_audit
FOR EACH ROW
EXECUTE FUNCTION public.fc27_forbid_build_catalog_audit_mutation();

-- -----------------------------------------------------------------------------
-- 5. public.fc27_player_suggestions
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_player_suggestions (
    suggestion_id       bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    pseudonym           text NOT NULL,
    kind                text NOT NULL,
    objective           jsonb NOT NULL,
    content             jsonb NOT NULL,
    catalog_build_key   text NULL,
    catalog_version     integer NULL,
    baseline            jsonb NOT NULL,
    baseline_match_ids  text[] NOT NULL,
    status              text NOT NULL,
    outcome             text NULL,
    outcome_value       numeric(10, 4) NULL,
    discord_channel_id  text NULL,
    discord_message_id  text NULL,
    discord_thread_id   text NULL,
    suggested_at        timestamptz NOT NULL DEFAULT now(),
    acknowledged_at     timestamptz NULL,
    tracking_started_at timestamptz NULL,
    closed_at           timestamptz NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fc27_player_suggestions_catalog_fk
        FOREIGN KEY (catalog_build_key, catalog_version)
        REFERENCES public.fc27_build_catalog (build_key, version)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_player_suggestions_pseudonym_chk
        CHECK (pseudonym ~ '^plr_ecf_[0-9a-f]{12}$'),
    CONSTRAINT fc27_player_suggestions_kind_chk
        CHECK (kind IN ('build', 'training')),
    CONSTRAINT fc27_player_suggestions_catalog_consistency_chk
        CHECK (
            (catalog_build_key IS NULL) = (catalog_version IS NULL)
            AND (catalog_version IS NULL OR catalog_version >= 1)
            AND (kind = 'build' OR (catalog_build_key IS NULL AND catalog_version IS NULL))
        ),
    CONSTRAINT fc27_player_suggestions_objective_chk
        CHECK (public.fc27_is_valid_suggestion_objective(objective)),
    CONSTRAINT fc27_player_suggestions_content_chk
        CHECK (public.fc27_is_safe_bounded_jsonb(content, 'object', true, 8192)),
    CONSTRAINT fc27_player_suggestions_baseline_chk
        CHECK (public.fc27_is_safe_bounded_jsonb(baseline, 'object', true, 8192)),
    CONSTRAINT fc27_player_suggestions_baseline_matches_chk
        CHECK (public.fc27_is_valid_match_id_array(baseline_match_ids)),
    CONSTRAINT fc27_player_suggestions_status_chk
        CHECK (status IN ('suggested', 'acknowledged', 'tracking', 'outcome')),
    CONSTRAINT fc27_player_suggestions_outcome_chk
        CHECK (
            outcome IS NULL
            OR outcome IN ('target_met', 'improving', 'no_change', 'regressed', 'insufficient_data')
        ),
    CONSTRAINT fc27_player_suggestions_channel_snowflake_chk
        CHECK (discord_channel_id IS NULL OR discord_channel_id ~ '^[1-9][0-9]{0,19}$'),
    CONSTRAINT fc27_player_suggestions_message_snowflake_chk
        CHECK (discord_message_id IS NULL OR discord_message_id ~ '^[1-9][0-9]{0,19}$'),
    CONSTRAINT fc27_player_suggestions_thread_snowflake_chk
        CHECK (discord_thread_id IS NULL OR discord_thread_id ~ '^[1-9][0-9]{0,19}$'),
    CONSTRAINT fc27_player_suggestions_lifecycle_chk
        CHECK (
            (
                status = 'suggested'
                AND acknowledged_at IS NULL
                AND tracking_started_at IS NULL
                AND closed_at IS NULL
                AND outcome IS NULL
                AND outcome_value IS NULL
            )
            OR (
                status = 'acknowledged'
                AND acknowledged_at IS NOT NULL
                AND acknowledged_at >= suggested_at
                AND tracking_started_at IS NULL
                AND closed_at IS NULL
                AND outcome IS NULL
                AND outcome_value IS NULL
            )
            OR (
                status = 'tracking'
                AND acknowledged_at IS NOT NULL
                AND acknowledged_at >= suggested_at
                AND tracking_started_at IS NOT NULL
                AND tracking_started_at >= acknowledged_at
                AND closed_at IS NULL
                AND outcome IS NULL
                AND outcome_value IS NULL
            )
            OR (
                status = 'outcome'
                AND closed_at IS NOT NULL
                AND closed_at >= suggested_at
                AND (acknowledged_at IS NULL OR acknowledged_at >= suggested_at)
                AND (
                    tracking_started_at IS NULL
                    OR (acknowledged_at IS NOT NULL AND tracking_started_at >= acknowledged_at AND closed_at >= tracking_started_at)
                )
                AND outcome IS NOT NULL
                AND (outcome = 'insufficient_data' OR outcome_value IS NOT NULL)
            )
        ),
    CONSTRAINT fc27_player_suggestions_timestamps_chk
        CHECK (
            updated_at >= created_at
            AND updated_at >= suggested_at
        )
);

ALTER TABLE public.fc27_player_suggestions OWNER TO fc27_owner;

CREATE INDEX IF NOT EXISTS idx_fc27_player_suggestions_open_pseudonym_status
    ON public.fc27_player_suggestions (pseudonym, status, suggested_at DESC, suggestion_id DESC)
    WHERE status IN ('suggested', 'acknowledged', 'tracking');

CREATE INDEX IF NOT EXISTS idx_fc27_player_suggestions_pseudonym_status
    ON public.fc27_player_suggestions (pseudonym, status, updated_at DESC, suggestion_id DESC);

-- -----------------------------------------------------------------------------
-- 6. public.fc27_suggestion_assessments (Append-Only Measurements)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_suggestion_assessments (
    assessment_id       bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    suggestion_id       bigint NOT NULL,
    calculated_values   jsonb NOT NULL,
    match_ids           text[] NOT NULL,
    assessment_time     timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fc27_suggestion_assessments_suggestion_fk
        FOREIGN KEY (suggestion_id)
        REFERENCES public.fc27_player_suggestions (suggestion_id)
        ON DELETE RESTRICT,
    CONSTRAINT fc27_suggestion_assessments_calc_values_chk
        CHECK (public.fc27_is_safe_bounded_jsonb(calculated_values, 'object', true, 8192)),
    CONSTRAINT fc27_suggestion_assessments_match_ids_chk
        CHECK (public.fc27_is_valid_match_id_array(match_ids))
);

ALTER TABLE public.fc27_suggestion_assessments OWNER TO fc27_owner;

CREATE INDEX IF NOT EXISTS idx_fc27_suggestion_assessments_suggestion_time
    ON public.fc27_suggestion_assessments (suggestion_id, assessment_time DESC, assessment_id DESC);

CREATE OR REPLACE FUNCTION public.fc27_forbid_suggestion_assessments_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION 'fc27_suggestion_assessments is append-only; % is prohibited', TG_OP
        USING ERRCODE = '42501';
END;
$$;

ALTER FUNCTION public.fc27_forbid_suggestion_assessments_mutation() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_forbid_suggestion_assessments_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_fc27_suggestion_assessments_immutable ON public.fc27_suggestion_assessments;
CREATE TRIGGER trg_fc27_suggestion_assessments_immutable
BEFORE UPDATE OR DELETE ON public.fc27_suggestion_assessments
FOR EACH ROW
EXECUTE FUNCTION public.fc27_forbid_suggestion_assessments_mutation();

-- -----------------------------------------------------------------------------
-- 7. public.fc27_discord_jobs (Independent Durable Discord Job Queue)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fc27_discord_jobs (
    job_id                  bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    job_type                text NOT NULL,
    payload                 jsonb NOT NULL,
    state                   text NOT NULL DEFAULT 'pending',
    claim_owner             text NULL,
    claimed_at              timestamptz NULL,
    lease_expires_at        timestamptz NULL,
    attempt_count           integer NOT NULL DEFAULT 0,
    last_error_category     text NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    available_at            timestamptz NOT NULL DEFAULT now(),
    completed_at            timestamptz NULL,
    idempotency_key         text NOT NULL,

    CONSTRAINT fc27_discord_jobs_idempotency_key_uk
        UNIQUE (idempotency_key),
    CONSTRAINT fc27_discord_jobs_idempotency_key_chk
        CHECK (
            char_length(btrim(idempotency_key)) BETWEEN 1 AND 128
            AND idempotency_key !~ '[\x00-\x1F\x7F]'
        ),
    CONSTRAINT fc27_discord_jobs_job_type_chk
        CHECK (job_type IN ('post_new_results', 'evaluate_suggestions')),
    CONSTRAINT fc27_discord_jobs_payload_chk
        CHECK (public.fc27_is_safe_bounded_jsonb(payload, 'object', true, 16384)),
    CONSTRAINT fc27_discord_jobs_state_chk
        CHECK (state IN ('pending', 'claimed', 'completed', 'failed')),
    CONSTRAINT fc27_discord_jobs_claim_owner_chk
        CHECK (
            claim_owner IS NULL
            OR (
                char_length(btrim(claim_owner)) BETWEEN 1 AND 128
                AND claim_owner !~ '[\x00-\x1F\x7F]'
            )
        ),
    CONSTRAINT fc27_discord_jobs_attempt_count_chk
        CHECK (attempt_count BETWEEN 0 AND 10),
    CONSTRAINT fc27_discord_jobs_error_category_chk
        CHECK (
            last_error_category IS NULL
            OR last_error_category ~ '^[A-Z0-9_]{1,64}$'
        ),
    CONSTRAINT fc27_discord_jobs_state_lifecycle_chk
        CHECK (
            (
                state = 'pending'
                AND claim_owner IS NULL
                AND claimed_at IS NULL
                AND lease_expires_at IS NULL
                AND completed_at IS NULL
            )
            OR (
                state = 'claimed'
                AND claim_owner IS NOT NULL
                AND claimed_at IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at > claimed_at
                AND completed_at IS NULL
                AND attempt_count >= 1
            )
            OR (
                state = 'completed'
                AND completed_at IS NOT NULL
                AND completed_at >= created_at
                AND lease_expires_at IS NULL
                AND attempt_count >= 1
            )
            OR (
                state = 'failed'
                AND claim_owner IS NULL
                AND claimed_at IS NULL
                AND lease_expires_at IS NULL
                AND completed_at IS NULL
                AND attempt_count >= 1
                AND last_error_category IS NOT NULL
            )
        )
);

ALTER TABLE public.fc27_discord_jobs OWNER TO fc27_owner;

CREATE INDEX IF NOT EXISTS idx_fc27_discord_jobs_pending_available
    ON public.fc27_discord_jobs (available_at ASC, job_id ASC)
    WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS idx_fc27_discord_jobs_claimed_lease
    ON public.fc27_discord_jobs (lease_expires_at ASC, job_id ASC)
    WHERE state = 'claimed';

CREATE OR REPLACE FUNCTION public.fc27_enforce_discord_job_claim_lease()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF NEW.state = 'claimed' THEN
        IF TG_OP = 'INSERT'
           OR OLD.state <> 'claimed'
           OR NEW.lease_expires_at IS DISTINCT FROM OLD.lease_expires_at
        THEN
            IF NEW.lease_expires_at <= pg_catalog.now() THEN
                RAISE EXCEPTION 'claimed fc27_discord_jobs row requires an unexpired lease_expires_at > now()'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

ALTER FUNCTION public.fc27_enforce_discord_job_claim_lease() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_enforce_discord_job_claim_lease() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_fc27_discord_jobs_claim_lease ON public.fc27_discord_jobs;
CREATE TRIGGER trg_fc27_discord_jobs_claim_lease
BEFORE INSERT OR UPDATE ON public.fc27_discord_jobs
FOR EACH ROW
EXECUTE FUNCTION public.fc27_enforce_discord_job_claim_lease();
