-- Migration 0017: Durable Reconciliation Token Inventory
-- Forward-only, idempotent, zero-rewriting of existing migrations 0001-0016.
--
-- Roster reconciliation hands operators short-lived `T1..Tn` tokens. The binding
-- between a token and an internal player id must survive until it expires: it is
-- issued by one process (the long-running Discord bot, or a CLI listing) and
-- consumed by another (a CLI write). Persisting the inventory keeps the binding
-- stable even when the unresolved-player ordering changes between listing and use,
-- and lets the write path fail closed on unknown or expired tokens instead of
-- re-deriving a token from the current ordering and silently retargeting it.

CREATE TABLE IF NOT EXISTS public.fc27_roster_reconciliation_tokens (
    actor_discord_user_id     text NOT NULL,
    token                     text NOT NULL,
    player_id                 text NOT NULL,
    issued_at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at                timestamptz NOT NULL,

    CONSTRAINT fc27_roster_reconciliation_tokens_pk
        PRIMARY KEY (actor_discord_user_id, token),
    CONSTRAINT fc27_roster_reconciliation_tokens_actor_chk
        CHECK (
            actor_discord_user_id = ''
            OR actor_discord_user_id ~ '^[0-9]{15,20}$'
        ),
    CONSTRAINT fc27_roster_reconciliation_tokens_token_chk
        CHECK (token ~ '^T[1-9][0-9]{0,2}$'),
    CONSTRAINT fc27_roster_reconciliation_tokens_player_id_chk
        CHECK (
            length(btrim(player_id)) BETWEEN 1 AND 64
            AND player_id !~ '[\x00-\x1F\x7F]'
        ),
    CONSTRAINT fc27_roster_reconciliation_tokens_expiry_chk
        CHECK (expires_at > issued_at)
);

ALTER TABLE public.fc27_roster_reconciliation_tokens OWNER TO fc27_owner;
REVOKE ALL ON public.fc27_roster_reconciliation_tokens FROM PUBLIC;

CREATE INDEX IF NOT EXISTS idx_fc27_roster_reconciliation_tokens_expiry
    ON public.fc27_roster_reconciliation_tokens (expires_at);
