-- 0002: immutable runs, atomic terminal decisions, persisted single-use nonces,
--       per-club/day run sequences, and a database-level terminal-state guard.

-- graph_threads -----------------------------------------------------------
ALTER TABLE graph_threads
    ADD COLUMN IF NOT EXISTS club_id          text,
    ADD COLUMN IF NOT EXISTS request_hash     text,
    ADD COLUMN IF NOT EXISTS package_version  text,
    ADD COLUMN IF NOT EXISTS created_at       timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS approved_by      text,
    ADD COLUMN IF NOT EXISTS approved_at      timestamptz,
    ADD COLUMN IF NOT EXISTS decision_payload jsonb;

-- Legacy rows: normalise unknown statuses and backfill terminal metadata so the
-- new constraints validate. Legacy decisions are explicitly marked unverified.
UPDATE graph_threads SET status = 'failed'
 WHERE status NOT IN ('running','awaiting_approval','approved','rejected','failed');

UPDATE graph_threads g
   SET approved_by = 'legacy-unverified',
       approved_at = COALESCE(g.updated_at, now()),
       decision_payload = jsonb_build_object('legacy', true, 'note', 'decided before migration 0002; identity not verified')
 WHERE g.status IN ('approved','rejected') AND g.approved_by IS NULL;

-- Legacy awaiting_approval rows have no package_version and therefore can never
-- be approved under the new rules (the conditional update requires a match).

ALTER TABLE graph_threads
    ADD CONSTRAINT graph_threads_status_chk
        CHECK (status IN ('running','awaiting_approval','approved','rejected','failed')),
    ADD CONSTRAINT graph_threads_terminal_fields_chk
        CHECK ((status IN ('approved','rejected'))
               = (approved_by IS NOT NULL AND approved_at IS NOT NULL AND decision_payload IS NOT NULL)),
    ADD CONSTRAINT graph_threads_thread_id_len_chk
        CHECK (char_length(thread_id) BETWEEN 1 AND 128);

-- Terminal-state guard: approved/rejected rows can never change or be deleted,
-- and only awaiting_approval rows can become terminal.
CREATE OR REPLACE FUNCTION fc27_guard_thread_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('approved','rejected') THEN
            RAISE EXCEPTION 'thread % is terminal and cannot be deleted', OLD.thread_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status IN ('approved','rejected') THEN
        RAISE EXCEPTION 'thread % is terminal (%) and cannot be modified', OLD.thread_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status IN ('approved','rejected') AND OLD.status <> 'awaiting_approval' THEN
        RAISE EXCEPTION 'thread % must be awaiting_approval before a decision (was %)', OLD.thread_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'awaiting_approval' AND OLD.status NOT IN ('running','awaiting_approval') THEN
        RAISE EXCEPTION 'thread % cannot return to awaiting_approval from %', OLD.thread_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.thread_id <> OLD.thread_id THEN
        RAISE EXCEPTION 'thread_id is immutable' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS graph_threads_guard ON graph_threads;
CREATE TRIGGER graph_threads_guard
    BEFORE UPDATE OR DELETE ON graph_threads
    FOR EACH ROW EXECUTE FUNCTION fc27_guard_thread_transition();

-- approval_audit ----------------------------------------------------------
ALTER TABLE approval_audit
    ADD COLUMN IF NOT EXISTS approved_by     text,
    ADD COLUMN IF NOT EXISTS nonce           text,
    ADD COLUMN IF NOT EXISTS package_version text,
    ADD COLUMN IF NOT EXISTS signature_sha256 text;

UPDATE approval_audit SET approved_by = 'legacy-unverified' WHERE approved_by IS NULL;
ALTER TABLE approval_audit ALTER COLUMN approved_by SET NOT NULL;
-- Rejections need not name a candidate.
ALTER TABLE approval_audit ALTER COLUMN owner DROP NOT NULL,
                           ALTER COLUMN selection DROP NOT NULL,
                           ALTER COLUMN candidate_id DROP NOT NULL,
                           ALTER COLUMN player_id DROP NOT NULL;
-- The legacy free-text commissioner column is no longer trusted; keep it for history.
ALTER TABLE approval_audit ALTER COLUMN commissioner DROP NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM approval_audit GROUP BY thread_id HAVING count(*) > 1) THEN
        RAISE EXCEPTION 'approval_audit contains more than one decision for a thread; see MIGRATION_GUIDE.md "Duplicate legacy decisions" before applying 0002';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS approval_audit_one_decision_per_thread ON approval_audit(thread_id);

-- approval_nonces -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approval_nonces (
    nonce           text PRIMARY KEY CHECK (char_length(nonce) >= 32),
    thread_id       text NOT NULL REFERENCES graph_threads(thread_id),
    payload_sha256  text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    package_version text NOT NULL,
    issued_at       timestamptz NOT NULL,
    expires_at      timestamptz NOT NULL,
    consumed_at     timestamptz,
    created_by      text NOT NULL,
    CHECK (expires_at > issued_at)
);
CREATE INDEX IF NOT EXISTS approval_nonces_thread_idx ON approval_nonces(thread_id);

ALTER TABLE approval_audit
    ADD CONSTRAINT approval_audit_nonce_fk FOREIGN KEY (nonce) REFERENCES approval_nonces(nonce);
CREATE UNIQUE INDEX IF NOT EXISTS approval_audit_nonce_unique ON approval_audit(nonce) WHERE nonce IS NOT NULL;

-- run sequences (thread_id convention fc27-<clubId>-run-<YYYYMMDD>-<seq>) -----
CREATE TABLE IF NOT EXISTS run_sequences (
    club_id  text NOT NULL CHECK (club_id ~ '^[0-9]+$'),
    run_date date NOT NULL,
    last_seq integer NOT NULL CHECK (last_seq > 0),
    PRIMARY KEY (club_id, run_date)
);
