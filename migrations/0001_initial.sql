-- 0001: baseline schema as created by the pre-migration graph_api.ensure_schema().
-- Idempotent so it can be applied to databases created by older releases.
CREATE TABLE IF NOT EXISTS graph_threads (
    thread_id  text PRIMARY KEY,
    state      jsonb NOT NULL,
    status     text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approval_audit (
    id           bigserial PRIMARY KEY,
    thread_id    text NOT NULL REFERENCES graph_threads(thread_id),
    commissioner text NOT NULL,
    decision     text NOT NULL CHECK (decision IN ('APPROVE','REJECT')),
    notes        text NOT NULL DEFAULT '',
    owner        text NOT NULL,
    selection    text NOT NULL,
    candidate_id text NOT NULL,
    player_id    text NOT NULL,
    decided_at   timestamptz NOT NULL DEFAULT now()
);
