-- Migration 0016: Canonical Player Roster + Legacy Match Migration & Metadata Columns
-- Forward-only, idempotent, zero-rewriting of existing migrations 0001-0015.

-- 1. Create canonical Discord roster table mapping internal player IDs to display names.
CREATE TABLE IF NOT EXISTS public.fc27_roster (
    player_id                   text PRIMARY KEY,
    display_name                text NOT NULL,
    display_name_normalized     text NOT NULL,
    position                    text NULL,
    discord_user_id             text NULL,
    active                      boolean NOT NULL DEFAULT TRUE,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fc27_roster_player_id_chk
        CHECK (
            length(btrim(player_id)) BETWEEN 1 AND 64
            AND player_id !~ '[\x00-\x1F\x7F]'
        ),
    CONSTRAINT fc27_roster_display_name_chk
        CHECK (
            length(btrim(display_name)) BETWEEN 1 AND 40
            AND display_name !~ '[\x00-\x1F\x7F]'
        ),
    CONSTRAINT fc27_roster_display_name_norm_chk
        CHECK (
            display_name_normalized = lower(btrim(display_name))
            AND length(btrim(display_name_normalized)) BETWEEN 1 AND 40
        ),
    CONSTRAINT fc27_roster_position_chk
        CHECK (
            position IS NULL
            OR (
                length(btrim(position)) BETWEEN 1 AND 16
                AND position !~ '[\x00-\x1F\x7F]'
            )
        ),
    CONSTRAINT fc27_roster_discord_user_id_chk
        CHECK (
            discord_user_id IS NULL
            OR discord_user_id ~ '^[0-9]{15,20}$'
        )
);

ALTER TABLE public.fc27_roster OWNER TO fc27_owner;
REVOKE ALL ON public.fc27_roster FROM PUBLIC;

CREATE INDEX IF NOT EXISTS idx_fc27_roster_active_name
    ON public.fc27_roster (active, display_name_normalized);

CREATE INDEX IF NOT EXISTS idx_fc27_roster_discord_user_id
    ON public.fc27_roster (discord_user_id)
    WHERE discord_user_id IS NOT NULL;

-- 2. Extend public.fc27_manual_matches with legacy_ref, competition, formation, interaction_id.
ALTER TABLE public.fc27_manual_matches
    ADD COLUMN IF NOT EXISTS legacy_ref text NULL,
    ADD COLUMN IF NOT EXISTS competition text NOT NULL DEFAULT 'League Match',
    ADD COLUMN IF NOT EXISTS formation text NULL,
    ADD COLUMN IF NOT EXISTS interaction_id text NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_matches_legacy_ref_chk'
          AND conrelid = 'public.fc27_manual_matches'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_matches
            ADD CONSTRAINT fc27_manual_matches_legacy_ref_chk
            CHECK (
                legacy_ref IS NULL
                OR (length(btrim(legacy_ref)) BETWEEN 1 AND 64 AND legacy_ref !~ '[\x00-\x1F\x7F]')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_matches_competition_chk'
          AND conrelid = 'public.fc27_manual_matches'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_matches
            ADD CONSTRAINT fc27_manual_matches_competition_chk
            CHECK (
                length(btrim(competition)) BETWEEN 1 AND 48
                AND competition !~ '[\x00-\x1F\x7F]'
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_matches_formation_chk'
          AND conrelid = 'public.fc27_manual_matches'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_matches
            ADD CONSTRAINT fc27_manual_matches_formation_chk
            CHECK (
                formation IS NULL
                OR (length(btrim(formation)) BETWEEN 1 AND 24 AND formation !~ '[\x00-\x1F\x7F]')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_matches_interaction_id_chk'
          AND conrelid = 'public.fc27_manual_matches'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_matches
            ADD CONSTRAINT fc27_manual_matches_interaction_id_chk
            CHECK (
                interaction_id IS NULL
                OR (length(btrim(interaction_id)) BETWEEN 1 AND 64 AND interaction_id !~ '[\x00-\x1F\x7F]')
            );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fc27_manual_matches_legacy_ref
    ON public.fc27_manual_matches (legacy_ref)
    WHERE legacy_ref IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fc27_manual_matches_interaction_id
    ON public.fc27_manual_matches (interaction_id)
    WHERE interaction_id IS NOT NULL;

-- 3. Extend public.fc27_manual_match_players for roster player_id binding and detailed stats.
ALTER TABLE public.fc27_manual_match_players
    ADD COLUMN IF NOT EXISTS player_id text NULL,
    ADD COLUMN IF NOT EXISTS position text NULL,
    ADD COLUMN IF NOT EXISTS passes_completed integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS passes_attempted integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS saves integer NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_match_players_player_id_chk'
          AND conrelid = 'public.fc27_manual_match_players'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_match_players
            ADD CONSTRAINT fc27_manual_match_players_player_id_chk
            CHECK (
                player_id IS NULL
                OR (length(btrim(player_id)) BETWEEN 1 AND 64 AND player_id !~ '[\x00-\x1F\x7F]')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_match_players_position_chk'
          AND conrelid = 'public.fc27_manual_match_players'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_match_players
            ADD CONSTRAINT fc27_manual_match_players_position_chk
            CHECK (
                position IS NULL
                OR (length(btrim(position)) BETWEEN 1 AND 16 AND position !~ '[\x00-\x1F\x7F]')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_match_players_passes_completed_chk'
          AND conrelid = 'public.fc27_manual_match_players'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_match_players
            ADD CONSTRAINT fc27_manual_match_players_passes_completed_chk
            CHECK (passes_completed BETWEEN 0 AND 500);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_match_players_passes_attempted_chk'
          AND conrelid = 'public.fc27_manual_match_players'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_match_players
            ADD CONSTRAINT fc27_manual_match_players_passes_attempted_chk
            CHECK (passes_attempted BETWEEN 0 AND 500);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fc27_manual_match_players_saves_chk'
          AND conrelid = 'public.fc27_manual_match_players'::regclass
    ) THEN
        ALTER TABLE public.fc27_manual_match_players
            ADD CONSTRAINT fc27_manual_match_players_saves_chk
            CHECK (saves BETWEEN 0 AND 200);
    END IF;
END $$;

-- Replace blanket unique(match_id, normalized_display_label) so multiple unresolved
-- ('Unnamed Player') provider entries can coexist per match while preserving label
-- uniqueness for manual entries (player_id IS NULL) and player_id uniqueness when set.
ALTER TABLE public.fc27_manual_match_players
    DROP CONSTRAINT IF EXISTS fc27_manual_match_players_match_label_uk;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fc27_manual_match_players_match_label
    ON public.fc27_manual_match_players (match_id, normalized_display_label)
    WHERE player_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fc27_manual_match_players_match_pid
    ON public.fc27_manual_match_players (match_id, player_id)
    WHERE player_id IS NOT NULL;

-- 4. Idempotently migrate existing mref_ecf_* provider matches and player stats into
--    numeric match IDs in public.fc27_manual_matches / fc27_manual_match_players without
--    renumbering any existing manual match rows.
WITH candidate_matches AS (
    SELECT
        m.match_id AS src_match_uuid,
        m.club_id,
        m.platform,
        m.game_version,
        m.external_match_id,
        CASE
            WHEN btrim(m.external_match_id) LIKE 'mref_ecf_%'
             AND length(btrim(m.external_match_id)) <= 64
                THEN btrim(m.external_match_id)
            ELSE 'mref_ecf_' || substr(
                encode(
                    sha256(
                        convert_to(
                            'fc27-ecf-' || COALESCE(NULLIF(btrim(m.club_id), ''), '330323') || '-match-v1:' || btrim(m.external_match_id),
                            'UTF8'
                        )
                    ),
                    'hex'
                ),
                1,
                12
            )
        END AS resolved_legacy_ref,
        m.played_at,
        m.goals_for,
        m.goals_against,
        COALESCE(
            NULLIF(
                left(
                    btrim(
                        regexp_replace(
                            regexp_replace(replace(m.opponent_name, '_', ' '), '[\x00-\x1F\x7F]', '', 'g'),
                            '\s+',
                            ' ',
                            'g'
                        )
                    ),
                    80
                ),
                ''
            ),
            'Opponent'
        ) AS clean_opponent_name,
        CASE split_part(COALESCE(eb.supplier_reference, ''), ':', 2)
            WHEN 'playoffMatch' THEN 'Playoff Match'
            WHEN 'friendlyMatch' THEN 'Friendly Match'
            ELSE 'League Match'
        END AS competition_label,
        CASE
            WHEN m.formation IS NULL OR upper(btrim(m.formation)) IN ('UNKNOWN', 'NONE', 'NULL', 'N/A', '') THEN NULL
            ELSE left(btrim(m.formation), 24)
        END AS clean_formation,
        (
            SELECT CASE WHEN COUNT(p.stat_id) > 0 THEN LEAST(COALESCE(SUM(p.shots), 0), 200) ELSE NULL END
            FROM public.fc27_player_match_stats p
            WHERE p.match_id = m.match_id
        ) AS agg_shots,
        (
            SELECT CASE WHEN COUNT(p.stat_id) > 0 THEN LEAST(COALESCE(SUM(p.passes_completed), 0), 2000) ELSE NULL END
            FROM public.fc27_player_match_stats p
            WHERE p.match_id = m.match_id
        ) AS agg_passes,
        (
            SELECT CASE
                WHEN COALESCE(SUM(p.passes_attempted), 0) > 0
                THEN LEAST(100, GREATEST(0, ROUND((COALESCE(SUM(p.passes_completed), 0)::numeric * 100.0) / SUM(p.passes_attempted))::integer))
                ELSE NULL
            END
            FROM public.fc27_player_match_stats p
            WHERE p.match_id = m.match_id
        ) AS agg_pass_accuracy,
        (
            SELECT CASE WHEN COUNT(p.stat_id) > 0 THEN LEAST(COALESCE(SUM(p.tackles_won), 0), 500) ELSE NULL END
            FROM public.fc27_player_match_stats p
            WHERE p.match_id = m.match_id
        ) AS agg_tackles,
        m.created_at
    FROM public.fc27_matches m
    LEFT JOIN public.fc27_export_batches eb
        ON eb.export_id = m.export_id
    WHERE length(btrim(m.external_match_id)) > 0
      AND m.data_quality_status <> 'REJECTED'
      AND NOT EXISTS (
          SELECT 1
          FROM public.fc27_manual_matches mm
          WHERE mm.legacy_ref = (
              CASE
                  WHEN btrim(m.external_match_id) LIKE 'mref_ecf_%'
                   AND length(btrim(m.external_match_id)) <= 64
                      THEN btrim(m.external_match_id)
                  ELSE 'mref_ecf_' || substr(
                      encode(
                          sha256(
                              convert_to(
                                  'fc27-ecf-' || COALESCE(NULLIF(btrim(m.club_id), ''), '330323') || '-match-v1:' || btrim(m.external_match_id),
                                  'UTF8'
                              )
                          ),
                          'hex'
                      ),
                      1,
                      12
                  )
              END
          )
      )
    ORDER BY m.played_at ASC, m.created_at ASC, m.external_match_id ASC
)
INSERT INTO public.fc27_manual_matches (
    club_id,
    platform,
    game_version,
    opponent,
    opponent_normalized,
    match_date,
    match_time,
    kickoff_at,
    home_score,
    away_score,
    status,
    shots,
    passes,
    pass_accuracy,
    tackles,
    legacy_ref,
    competition,
    formation,
    source_classification,
    created_by,
    updated_by,
    created_at,
    updated_at
)
SELECT
    cm.club_id,
    cm.platform,
    cm.game_version,
    cm.clean_opponent_name,
    lower(btrim(cm.clean_opponent_name)),
    to_char(COALESCE(cm.played_at, cm.created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD'),
    to_char(COALESCE(cm.played_at, cm.created_at) AT TIME ZONE 'UTC', 'HH24:MI UTC'),
    COALESCE(cm.played_at, cm.created_at),
    LEAST(GREATEST(cm.goals_for, 0), 30),
    LEAST(GREATEST(cm.goals_against, 0), 30),
    'FULL_TIME',
    cm.agg_shots,
    cm.agg_passes,
    cm.agg_pass_accuracy,
    cm.agg_tackles,
    cm.resolved_legacy_ref,
    cm.competition_label,
    cm.clean_formation,
    'MANUAL_MATCH_RESULT',
    '0',
    '0',
    COALESCE(cm.played_at, cm.created_at),
    COALESCE(cm.played_at, cm.created_at)
FROM candidate_matches cm
ON CONFLICT (legacy_ref) WHERE legacy_ref IS NOT NULL DO NOTHING;

INSERT INTO public.fc27_manual_match_players (
    match_id,
    normalized_display_label,
    safe_display_label,
    player_id,
    position,
    goals,
    assists,
    tackles,
    passes,
    passes_completed,
    passes_attempted,
    pass_accuracy,
    shots,
    saves,
    rating,
    source_classification,
    created_at,
    updated_at
)
SELECT
    mm.match_id,
    lower(btrim(COALESCE(r.display_name, 'Unnamed Player'))),
    COALESCE(r.display_name, 'Unnamed Player'),
    p.local_player_reference,
    CASE
        WHEN p.position IS NULL OR upper(btrim(p.position)) IN ('UNKNOWN', 'NONE', 'NULL', 'N/A', '') THEN NULL
        ELSE left(btrim(p.position), 16)
    END,
    LEAST(GREATEST(p.goals, 0), 30),
    LEAST(GREATEST(p.assists, 0), 30),
    LEAST(GREATEST(p.tackles_won, 0), 200),
    LEAST(GREATEST(p.passes_completed, 0), 1000),
    LEAST(GREATEST(p.passes_completed, 0), 500),
    LEAST(GREATEST(p.passes_attempted, 0), 500),
    CASE
        WHEN p.passes_attempted > 0
        THEN LEAST(100, GREATEST(0, ROUND((p.passes_completed::numeric * 100.0) / p.passes_attempted)::integer))
        ELSE NULL
    END,
    LEAST(GREATEST(p.shots, 0), 100),
    LEAST(GREATEST(p.saves, 0), 200),
    LEAST(GREATEST(p.provider_rating, 0.00), 10.00),
    'MANUAL_MATCH_RESULT',
    mm.created_at,
    mm.updated_at
FROM public.fc27_matches m
JOIN public.fc27_manual_matches mm
    ON mm.legacy_ref = (
        CASE
            WHEN btrim(m.external_match_id) LIKE 'mref_ecf_%'
             AND length(btrim(m.external_match_id)) <= 64
                THEN btrim(m.external_match_id)
            ELSE 'mref_ecf_' || substr(
                encode(
                    sha256(
                        convert_to(
                            'fc27-ecf-' || COALESCE(NULLIF(btrim(m.club_id), ''), '330323') || '-match-v1:' || btrim(m.external_match_id),
                            'UTF8'
                        )
                    ),
                    'hex'
                ),
                1,
                12
            )
        END
    )
JOIN public.fc27_player_match_stats p
    ON p.match_id = m.match_id
LEFT JOIN public.fc27_roster r
    ON r.player_id = p.local_player_reference
   AND r.active = TRUE
ON CONFLICT (match_id, player_id) WHERE player_id IS NOT NULL DO NOTHING;
