-- Migration 0021: Durable FC27 community-provider request attempt ledger.
-- Records sanitized attempt metadata only; provider payloads and headers are never stored.
CREATE TABLE IF NOT EXISTS public.fc27_provider_request_attempts (
    attempt_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    club_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    match_type TEXT NOT NULL CHECK (match_type IN ('leagueMatch', 'playoffMatch', 'friendlyMatch')),
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome_category TEXT NOT NULL DEFAULT 'ATTEMPTED'
        CHECK (outcome_category IN ('ATTEMPTED', 'SUCCEEDED', 'PROVIDER_BLOCKED', 'SCHEMA_DRIFT', 'TRANSIENT_FAILURE', 'PERSISTENCE_FAILURE'))
);
CREATE INDEX IF NOT EXISTS idx_fc27_provider_request_attempts_window
    ON public.fc27_provider_request_attempts (provider_id, club_id, platform, attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_fc27_provider_request_attempts_cycle
    ON public.fc27_provider_request_attempts (cycle_id, attempted_at);
REVOKE ALL ON public.fc27_provider_request_attempts FROM PUBLIC;
