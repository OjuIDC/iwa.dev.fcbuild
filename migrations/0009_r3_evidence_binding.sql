-- 0009_r3_evidence_binding.sql: Phase 2 Milestone R3 Evidence Binding Schema Remediation (R2)

-- 1. Expand normalized_decision check constraint to allow 'revision_requested'
ALTER TABLE public.fc27_decision_evidence
    DROP CONSTRAINT IF EXISTS fc27_decision_evidence_normalized_decision_check;
ALTER TABLE public.fc27_decision_evidence
    ADD CONSTRAINT fc27_decision_evidence_normalized_decision_check
    CHECK (normalized_decision IN ('approved', 'rejected', 'revision_requested'));

-- 2. Reaffirm table grants to fc27_app and fc27_migrator
GRANT SELECT, INSERT, UPDATE ON public.fc27_decision_evidence TO fc27_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_decision_evidence TO fc27_migrator;
