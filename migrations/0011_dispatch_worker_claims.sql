-- 0011_dispatch_worker_claims.sql: Phase 3 Package 3 Dispatch Worker Claim Ownership and Recovery

CREATE TABLE IF NOT EXISTS public.dispatch_worker_claims (
    dispatch_id uuid PRIMARY KEY REFERENCES public.dispatches(dispatch_id) ON DELETE CASCADE,
    worker_id text NOT NULL,
    locked_until timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dispatch_worker_claims_recovery_idx
    ON public.dispatch_worker_claims (locked_until, next_attempt_at);
