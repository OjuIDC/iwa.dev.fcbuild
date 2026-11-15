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
