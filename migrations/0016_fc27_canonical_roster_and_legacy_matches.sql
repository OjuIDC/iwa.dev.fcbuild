-- Migration 0016: Canonical Player Roster + Manual Match Schema Extensions
-- Forward-only, idempotent, zero-rewriting of existing migrations 0001-0015.
-- Note: Provider matches stay exclusively in public.fc27_matches and
-- public.fc27_player_match_stats (LIVE_EA_CLUBS_COMMUNITY_CLIENT); manual matches
-- remain isolated in public.fc27_manual_matches / public.fc27_manual_match_players.

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

-- 4. Backfill eligible legacy matches from fc27_matches into fc27_manual_matches.
-- Exclude quarantined matches (REJECTED quality or active quarantine blocks).
-- Sanitize opponent names to remove Discord mentions (@everyone, @here, role/user mentions).
DO $$
DECLARE
    v_club_id text := '330323';
    v_platform text := 'common-gen5';
    v_game_version text := 'FC27';
    v_inserted_count integer := 0;
    v_skipped_quarantine integer := 0;
    v_skipped_duplicate integer := 0;
BEGIN
    -- Insert eligible legacy matches that don't already have a legacy_ref mapping.
    INSERT INTO public.fc27_manual_matches (
        club_id,
        platform,
        game_version,
        opponent,
        opponent_normalized,
        kickoff_at,
        match_date,
        match_time,
        home_score,
        away_score,
        status,
        motm_display_label,
        motm_normalized_label,
        possession,
        shots,
        shots_on_target,
        passes,
        pass_accuracy,
        corners,
        tackles,
        fouls,
        notes,
        discord_guild_id,
        discord_channel_id,
        discord_message_id,
        legacy_ref,
        competition,
        formation,
        source_classification,
        created_by,
        updated_by,
        revision,
        created_at,
        updated_at
    )
    SELECT
        m.club_id,
        m.platform,
        m.game_version,
        -- Sanitize opponent name: remove @everyone, @here, and Discord mention patterns.
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    COALESCE(m.opponent_name, 'Unknown Opponent'),
                    '@everyone|@here',
                    '',
                    'gi'
                ),
                '<@!?[0-9]+>',
                '',
                'g'
            ),
            '<@&[0-9]+>',
            '',
            'g'
        ),
        lower(btrim(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        COALESCE(m.opponent_name, 'Unknown Opponent'),
                        '@everyone|@here',
                        '',
                        'gi'
                    ),
                    '<@!?[0-9]+>',
                    '',
                    'g'
                ),
                '<@&[0-9]+>',
                '',
                'g'
            )
        )),
        m.played_at,
        to_char(m.played_at AT TIME ZONE 'UTC', 'YYYY-MM-DD'),
        to_char(m.played_at AT TIME ZONE 'UTC', 'HH24:MI'),
        m.goals_for,
        m.goals_against,
        'COMPLETED',
        NULL,
        NULL,
        NULL,
        NULL,
        NULL,
        NULL,
        NULL,
        NULL,
        NULL,
        m.formation,
        'Legacy import from fc27_matches',
        m.club_id,
        m.platform,
        m.discord_message_id,
        m.external_match_id,
        'League Match',
        m.formation,
        'COMMUNITY_RESEARCH_DERIVED',
        'migration_0016',
        'migration_0016',
        1,
        now(),
        now()
    FROM public.fc27_matches m
    WHERE m.club_id = v_club_id
      AND m.platform = v_platform
      AND m.game_version = v_game_version
      AND m.data_quality_status != 'REJECTED'
      AND NOT EXISTS (
          -- Skip if this match is already mapped via legacy_ref.
          SELECT 1 FROM public.fc27_manual_matches mm
          WHERE mm.legacy_ref = m.external_match_id
      )
      AND NOT EXISTS (
          -- Skip if this match, its export batch, or club-wide REPLAY_CONFLICT is actively quarantined.
          SELECT 1 FROM public.fc27_quarantine_records q
          WHERE q.club_id = m.club_id
            AND q.platform = m.platform
            AND q.game_version = m.game_version
            AND q.review_status IN ('PENDING_REVIEW', 'REJECTED_PERMANENT')
            AND (
                q.external_match_id = m.external_match_id
                OR (q.export_id IS NOT NULL AND q.export_id = m.export_id)
                OR (q.external_match_id IS NULL AND q.export_id IS NULL AND q.anomaly_category = 'REPLAY_CONFLICT')
            )
      )
    ON CONFLICT (legacy_ref) WHERE legacy_ref IS NOT NULL DO NOTHING;

    GET DIAGNOSTICS v_inserted_count = ROW_COUNT;

    -- Log summary (visible in migration logs).
    RAISE NOTICE 'Migration 0016: Backfilled % legacy matches from fc27_matches to fc27_manual_matches', v_inserted_count;
END $$;
