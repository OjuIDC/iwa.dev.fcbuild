-- 0010_outbox_claim_ownership.sql: Phase 3 Package 2 Outbox Claim Ownership

ALTER TABLE public.outbox ADD COLUMN IF NOT EXISTS locked_by text;

CREATE INDEX IF NOT EXISTS outbox_claim_idx
    ON public.outbox (published_at, locked_until, attempts)
    WHERE published_at IS NULL;
