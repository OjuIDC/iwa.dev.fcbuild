-- 0007_decision_evidence.sql: Phase 2 Milestone R1 Decision Evidence Schema (R10 & R2)
-- Establishes public.fc27_decision_evidence with complete 14-field binding,
-- foreign keys, indexes, immutability triggers, and updates domain_events check constraint.

-- 1. Create public.fc27_decision_evidence table
CREATE TABLE IF NOT EXISTS public.fc27_decision_evidence (
    evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE RESTRICT,
    session_id uuid NOT NULL REFERENCES public.fc27_sessions(session_id) ON DELETE RESTRICT,
    thread_id text NOT NULL REFERENCES public.graph_threads(thread_id) ON DELETE RESTRICT,
    approval_request_id uuid NOT NULL REFERENCES public.approval_requests(approval_request_id) ON DELETE RESTRICT,
    package_id text NOT NULL CHECK (char_length(package_id) BETWEEN 1 AND 128),
    package_version text NOT NULL CHECK (char_length(package_version) BETWEEN 1 AND 128),
    package_hash text NOT NULL CHECK (package_hash ~ '^[0-9a-f]{64}$'),
    normalized_decision text NOT NULL CHECK (normalized_decision IN ('approved', 'rejected', 'revision_requested')),
    canonical_bound_payload_hash text NOT NULL CHECK (canonical_bound_payload_hash ~ '^[0-9a-f]{64}$'),
    issued_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NULL,
    webauthn_credential_id text NULL,
    challenge_id uuid NULL REFERENCES public.fc27_challenges(challenge_id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Backward compatibility stored generated columns
    decision text GENERATED ALWAYS AS (normalized_decision) STORED,
    bound_payload_hash text GENERATED ALWAYS AS (canonical_bound_payload_hash) STORED,
    CHECK (expires_at > issued_at)
);

-- 2. Performance and Verification Indexes
CREATE INDEX IF NOT EXISTS fc27_decision_evidence_approval_req_idx 
    ON public.fc27_decision_evidence(approval_request_id);

CREATE INDEX IF NOT EXISTS fc27_decision_evidence_user_session_idx 
    ON public.fc27_decision_evidence(user_id, session_id);

CREATE INDEX IF NOT EXISTS fc27_decision_evidence_thread_idx 
    ON public.fc27_decision_evidence(thread_id);

CREATE INDEX IF NOT EXISTS fc27_decision_evidence_package_idx 
    ON public.fc27_decision_evidence(package_id, package_version, package_hash);

CREATE INDEX IF NOT EXISTS fc27_decision_evidence_active_idx 
    ON public.fc27_decision_evidence(evidence_id, expires_at) 
    WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS fc27_decision_evidence_cred_idx 
    ON public.fc27_decision_evidence(webauthn_credential_id) 
    WHERE webauthn_credential_id IS NOT NULL;

-- 3. Permanent Immutability and Single-Use Consumption Trigger
-- Deletions strictly forbidden via fc27_reject_mutation()
DROP TRIGGER IF EXISTS fc27_decision_evidence_no_delete ON public.fc27_decision_evidence;
CREATE TRIGGER fc27_decision_evidence_no_delete
    BEFORE DELETE ON public.fc27_decision_evidence
    FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- Single-use transition guard for consumed_at; immutability for all other attributes
CREATE OR REPLACE FUNCTION public.fc27_guard_decision_evidence_mutation()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.consumed_at IS NOT NULL THEN
        RAISE check_violation USING MESSAGE = 'decision evidence is already consumed';
    END IF;
    IF NEW.consumed_at IS NULL THEN
        RAISE check_violation USING MESSAGE = 'cannot un-consume decision evidence';
    END IF;
    IF NEW.evidence_id <> OLD.evidence_id
       OR NEW.user_id <> OLD.user_id
       OR NEW.session_id <> OLD.session_id
       OR NEW.thread_id <> OLD.thread_id
       OR NEW.approval_request_id <> OLD.approval_request_id
       OR NEW.package_id <> OLD.package_id
       OR NEW.package_version <> OLD.package_version
       OR NEW.package_hash <> OLD.package_hash
       OR NEW.normalized_decision <> OLD.normalized_decision
       OR NEW.canonical_bound_payload_hash <> OLD.canonical_bound_payload_hash
       OR NEW.issued_at <> OLD.issued_at
       OR NEW.expires_at <> OLD.expires_at
       OR (NEW.webauthn_credential_id IS DISTINCT FROM OLD.webauthn_credential_id)
       OR (NEW.challenge_id IS DISTINCT FROM OLD.challenge_id)
       OR NEW.created_at <> OLD.created_at THEN
        RAISE check_violation USING MESSAGE = 'decision evidence attributes are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fc27_decision_evidence_update_guard ON public.fc27_decision_evidence;
CREATE TRIGGER fc27_decision_evidence_update_guard
    BEFORE UPDATE ON public.fc27_decision_evidence
    FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_decision_evidence_mutation();

-- 4. Expand domain_events check constraint to accept fc27_decision_evidence & player tables
ALTER TABLE public.domain_events DROP CONSTRAINT IF EXISTS domain_events_source_table_check;
ALTER TABLE public.domain_events ADD CONSTRAINT domain_events_source_table_check
CHECK (source_table IN (
    'approval_audit',
    'approval_requests',
    'dispatches',
    'dispatch_acknowledgments',
    'dispatch_assessments',
    'dispatch_plans',
    'dispatch_plan_decisions',
    'dispatch_delivery_attempts',
    'user_roles',
    'activation_gate_log',
    'fc27_users',
    'fc27_user_roles',
    'fc27_invitations',
    'fc27_sessions',
    'fc27_webauthn_credentials',
    'fc27_totp_credentials',
    'fc27_recovery_codes',
    'fc27_action_tokens',
    'fc27_challenges',
    'authority_delegations',
    'fc27_delegations',
    'fc27_bootstrap_locks',
    'fc27_decision_evidence',
    'player_assignments',
    'player_decision_requests',
    'player_decisions'
));
