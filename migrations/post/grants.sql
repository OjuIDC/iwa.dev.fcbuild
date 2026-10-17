-- grants-policy-version: 12
DO $$
BEGIN
    IF to_regclass('public.graph_threads') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.graph_threads TO fc27_app;
    END IF;
    IF to_regclass('public.approval_nonces') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.approval_nonces TO fc27_app;
    END IF;
    IF to_regclass('public.run_sequences') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.run_sequences TO fc27_app;
    END IF;
    IF to_regclass('public.approval_audit') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.approval_audit TO fc27_app;
    END IF;
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        GRANT SELECT ON public.schema_migrations TO fc27_app;
    END IF;
    IF to_regclass('public.checkpoints') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.checkpoints TO fc27_app;
    END IF;
    IF to_regclass('public.checkpoint_blobs') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.checkpoint_blobs TO fc27_app;
    END IF;
    IF to_regclass('public.checkpoint_writes') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.checkpoint_writes TO fc27_app;
    END IF;
    IF to_regclass('public.checkpoint_migrations') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.checkpoint_migrations TO fc27_app;
    END IF;

    IF to_regclass('public.approval_audit_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.approval_audit_id_seq TO fc27_app;
    END IF;

    IF to_regclass('public.domain_events') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.domain_events TO fc27_app;
        GRANT SELECT ON public.domain_events TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'GRANT SELECT ON public.domain_events TO fc27_relay';
        END IF;
    END IF;
    IF to_regclass('public.domain_events_event_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.domain_events_event_id_seq TO fc27_app;
    END IF;

    IF to_regclass('public.outbox') IS NOT NULL THEN
        GRANT INSERT ON public.outbox TO fc27_app;
        GRANT SELECT ON public.outbox TO fc27_dispatch;
        GRANT UPDATE (published_at, attempts, last_error, locked_until, locked_by) ON public.outbox TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'GRANT SELECT ON public.outbox TO fc27_relay';
            EXECUTE 'GRANT UPDATE (published_at, attempts, last_error, locked_until, locked_by) ON public.outbox TO fc27_relay';
        END IF;
    END IF;
    IF to_regclass('public.outbox_outbox_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.outbox_outbox_id_seq TO fc27_app;
    END IF;

    -- Milestone 1 additions
    IF to_regclass('public.approval_requests') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.approval_requests TO fc27_app;
    END IF;
    IF to_regclass('public.fc27_approval_inconsistencies') IS NOT NULL THEN
        GRANT SELECT ON public.fc27_approval_inconsistencies TO fc27_app;
    END IF;

    -- Section 1A compatibility additions
    IF to_regclass('public.authority_policies') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.authority_policies TO fc27_app;
    END IF;
    IF to_regclass('public.authority_delegations') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.authority_delegations TO fc27_app;
    END IF;
    IF to_regclass('public.fc27_delegations') IS NOT NULL THEN
        GRANT SELECT ON public.fc27_delegations TO fc27_app;
    END IF;
    IF to_regclass('public.player_assignments') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.player_assignments TO fc27_app;
    END IF;
    IF to_regclass('public.player_decision_requests') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.player_decision_requests TO fc27_app;
    END IF;
    IF to_regclass('public.player_decisions') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.player_decisions TO fc27_app;
    END IF;
    IF to_regclass('public.approval_escalations') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.approval_escalations TO fc27_app;
    END IF;
    IF to_regclass('public.authority_conflicts') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.authority_conflicts TO fc27_app;
    END IF;

    -- Milestone 3 Part C & D Grants
    IF to_regclass('public.players') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.players TO fc27_app;
        GRANT SELECT ON public.players TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatches') IS NOT NULL THEN
        -- UPDATE required for SELECT FOR UPDATE in supersession logic (dispatch/creation.py)
        GRANT SELECT, INSERT, UPDATE ON public.dispatches TO fc27_app;
        GRANT SELECT ON public.dispatches TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_transition_rules') IS NOT NULL THEN
        GRANT SELECT ON public.dispatch_transition_rules TO fc27_app, fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_plans') IS NOT NULL THEN
        GRANT SELECT ON public.dispatch_plans TO fc27_app;
        GRANT SELECT, INSERT ON public.dispatch_plans TO fc27_dispatch;
    END IF;

    IF to_regclass('public.player_observations') IS NOT NULL THEN
        GRANT SELECT ON public.player_observations TO fc27_app;
        GRANT SELECT, INSERT ON public.player_observations TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_assessments') IS NOT NULL THEN
        GRANT SELECT ON public.dispatch_assessments TO fc27_app;
        GRANT SELECT, INSERT ON public.dispatch_assessments TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_delivery_attempts') IS NOT NULL THEN
        GRANT SELECT ON public.dispatch_delivery_attempts TO fc27_app;
        GRANT SELECT, INSERT ON public.dispatch_delivery_attempts TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_acknowledgments') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.dispatch_acknowledgments TO fc27_app;
        GRANT SELECT ON public.dispatch_acknowledgments TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_plan_decisions') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.dispatch_plan_decisions TO fc27_app;
        GRANT SELECT ON public.dispatch_plan_decisions TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_comments') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.dispatch_comments TO fc27_app;
        GRANT SELECT ON public.dispatch_comments TO fc27_dispatch;
    END IF;

    IF to_regclass('public.analyst_notes') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.analyst_notes TO fc27_app;
        GRANT SELECT ON public.analyst_notes TO fc27_dispatch;
    END IF;

    IF to_regclass('public.idempotency_keys') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.idempotency_keys TO fc27_app;
    END IF;

    -- Function Execution Grants
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_dispatch_transition') THEN
        REVOKE ALL ON FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.fc27_dispatch_transition(uuid, text, integer, text, text, uuid, jsonb) 
            TO fc27_app, fc27_dispatch;
    END IF;

    -- Milestone 4 additions
    IF to_regclass('public.activation_gate_log') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.activation_gate_log TO fc27_app;
    END IF;

    IF to_regclass('public.activation_gate_log_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.activation_gate_log_id_seq TO fc27_app;
    END IF;

    IF to_regclass('public.activation_gates') IS NOT NULL THEN
        GRANT SELECT ON public.activation_gates TO fc27_app;
    END IF;

    IF to_regclass('public.service_heartbeats') IS NOT NULL THEN
        GRANT SELECT ON public.service_heartbeats TO fc27_app;
        GRANT SELECT, INSERT, UPDATE ON public.service_heartbeats TO fc27_dispatch;
    END IF;

    -- Phase 2 Milestone 1: Identity Data Foundation Grants
    IF to_regclass('public.fc27_users') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_users TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_user_roles') IS NOT NULL THEN
        GRANT SELECT, INSERT, DELETE ON public.fc27_user_roles TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_invitations') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_invitations TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_sessions') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_sessions TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_webauthn_credentials') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_webauthn_credentials TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_totp_credentials') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_totp_credentials TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_recovery_codes') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_recovery_codes TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_action_tokens') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_action_tokens TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_challenges') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_challenges TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_bootstrap_locks') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.fc27_bootstrap_locks TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_credentials') IS NOT NULL THEN
        GRANT SELECT ON public.fc27_credentials TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_decision_evidence') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.fc27_decision_evidence TO fc27_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_decision_evidence TO fc27_migrator;
    END IF;
END $$;
