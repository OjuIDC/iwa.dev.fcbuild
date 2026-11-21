-- grants-policy-version: 22
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
        GRANT SELECT ON public.schema_migrations TO fc27_app, fc27_dispatch;
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
        -- CRITICAL: NO UPDATE, NO DELETE on dispatches for any runtime role!
        GRANT SELECT, INSERT ON public.dispatches TO fc27_app;
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
        GRANT SELECT ON public.activation_gate_log TO fc27_dispatch;
    END IF;

    IF to_regclass('public.activation_gate_log_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.activation_gate_log_id_seq TO fc27_app;
    END IF;

    IF to_regclass('public.activation_gates') IS NOT NULL THEN
        GRANT SELECT ON public.activation_gates TO fc27_app, fc27_dispatch;
    END IF;

    IF to_regclass('public.service_heartbeats') IS NOT NULL THEN
        GRANT SELECT ON public.service_heartbeats TO fc27_app;
        GRANT SELECT, INSERT, UPDATE ON public.service_heartbeats TO fc27_dispatch;
    END IF;

    IF to_regclass('public.dispatch_worker_claims') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.dispatch_worker_claims TO fc27_dispatch, fc27_app;
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

    -- Migration 0012: FC 27 PostgreSQL Performance Storage and Quarantine Grants
    IF to_regclass('public.fc27_clubs') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_clubs FROM PUBLIC, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_clubs FROM fc27_relay';
        END IF;
        GRANT SELECT, INSERT, UPDATE (club_name, identity_state, source_classification, updated_at)
            ON public.fc27_clubs TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_export_batches') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_export_batches FROM PUBLIC, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_export_batches FROM fc27_relay';
        END IF;
        GRANT SELECT, INSERT, UPDATE (accepted_matches, quarantined_matches, applied_at)
            ON public.fc27_export_batches TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_matches') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_matches FROM PUBLIC, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_matches FROM fc27_relay';
        END IF;
        GRANT SELECT, INSERT ON public.fc27_matches TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_player_match_stats') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_player_match_stats FROM PUBLIC, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_match_stats FROM fc27_relay';
        END IF;
        GRANT SELECT, INSERT, UPDATE (alias_visibility_approved) ON public.fc27_player_match_stats TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_player_match_stats_stat_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_player_match_stats_stat_id_seq FROM PUBLIC, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_match_stats_stat_id_seq FROM fc27_relay';
        END IF;
        GRANT USAGE, SELECT ON SEQUENCE public.fc27_player_match_stats_stat_id_seq TO fc27_app;
    END IF;

    IF to_regclass('public.fc27_quarantine_records') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_quarantine_records FROM PUBLIC, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_quarantine_records FROM fc27_relay';
        END IF;
        GRANT SELECT, INSERT, UPDATE (review_status, reviewed_by, reviewed_at, review_notes)
            ON public.fc27_quarantine_records TO fc27_app;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_is_valid_diagnostic_paths') THEN
        REVOKE ALL ON FUNCTION public.fc27_is_valid_diagnostic_paths(jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.fc27_is_valid_diagnostic_paths(jsonb)
            TO fc27_app, fc27_migrator;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_purge_expired_quarantine') THEN
        REVOKE ALL ON FUNCTION public.fc27_purge_expired_quarantine(timestamptz) FROM PUBLIC, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_purge_expired_quarantine(timestamptz) FROM fc27_relay';
        END IF;
        GRANT EXECUTE ON FUNCTION public.fc27_purge_expired_quarantine(timestamptz) TO fc27_app;
    END IF;

    -- =========================================================================
    -- Migration 0013: Manual Match Results Domain & Dedicated Writer Role
    -- =========================================================================
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fc27_match_results_writer';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM fc27_match_results_writer';
        EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM fc27_match_results_writer';
        EXECUTE 'GRANT USAGE ON SCHEMA public TO fc27_match_results_writer';
    END IF;

    IF to_regclass('public.fc27_manual_matches') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_manual_matches FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_manual_matches FROM fc27_relay';
        END IF;
        GRANT SELECT ON public.fc27_manual_matches TO fc27_app;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON public.fc27_manual_matches TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_matches_match_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_manual_matches_match_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_manual_matches_match_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_manual_matches_match_id_seq TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_players') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_manual_match_players FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_manual_match_players FROM fc27_relay';
        END IF;
        GRANT SELECT ON public.fc27_manual_match_players TO fc27_app;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_manual_match_players TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_players_player_stat_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_manual_match_players_player_stat_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_manual_match_players_player_stat_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_manual_match_players_player_stat_id_seq TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_events') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_manual_match_events FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_manual_match_events FROM fc27_relay';
        END IF;
        GRANT SELECT ON public.fc27_manual_match_events TO fc27_app;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_manual_match_events TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_events_event_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_manual_match_events_event_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_manual_match_events_event_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_manual_match_events_event_id_seq TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_audit') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_manual_match_audit FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_manual_match_audit FROM fc27_relay';
        END IF;
        GRANT SELECT ON public.fc27_manual_match_audit TO fc27_app;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT ON public.fc27_manual_match_audit TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_audit_audit_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_manual_match_audit_audit_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_manual_match_audit_audit_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_manual_match_audit_audit_id_seq TO fc27_match_results_writer';
        END IF;
    END IF;

    -- Canonical Player Roster table (0016)
    IF to_regclass('public.fc27_roster') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_roster FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_roster FROM fc27_relay';
        END IF;
        GRANT SELECT ON public.fc27_roster TO fc27_app;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON public.fc27_roster TO fc27_match_results_writer';
        END IF;
    END IF;

    -- =========================================================================
    -- Migration 0014: Player Development, Linking, Build Catalog, Suggestions,
    -- and Discord Job Queue Roles (Grants Policy Version 17)
    -- =========================================================================
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fc27_poll_writer';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM fc27_poll_writer';
        EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM fc27_poll_writer';
        EXECUTE 'GRANT USAGE ON SCHEMA public TO fc27_poll_writer';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fc27_player_dev_writer';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM fc27_player_dev_writer';
        EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM fc27_player_dev_writer';
        EXECUTE 'GRANT USAGE ON SCHEMA public TO fc27_player_dev_writer';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fc27_discord_reader';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM fc27_discord_reader';
        EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM fc27_discord_reader';
        EXECUTE 'GRANT USAGE ON SCHEMA public TO fc27_discord_reader';
    END IF;

    -- Provider-derived tables (0012) grants for Stage C roles
    IF to_regclass('public.fc27_clubs') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE (club_name, identity_state, source_classification, updated_at) ON public.fc27_clubs TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_clubs TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_clubs TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_export_batches') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE (accepted_matches, quarantined_matches, applied_at) ON public.fc27_export_batches TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_export_batches TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_export_batches TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_matches') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT ON public.fc27_matches TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_matches TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_matches TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_match_stats') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT ON public.fc27_player_match_stats TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_player_match_stats TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_player_match_stats TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_match_stats_stat_id_seq') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_player_match_stats_stat_id_seq TO fc27_poll_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_quarantine_records') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT ON public.fc27_quarantine_records TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_quarantine_records TO fc27_discord_reader';
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_is_valid_diagnostic_paths') THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_valid_diagnostic_paths(jsonb) TO fc27_poll_writer';
        END IF;
    END IF;

    -- Manual Match Results tables (0013) read-only access for fc27_discord_reader
    IF to_regclass('public.fc27_manual_matches') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_manual_matches TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_players') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_manual_match_players TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_events') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_manual_match_events TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_manual_match_audit') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_manual_match_audit TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_roster') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_roster TO fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_roster TO fc27_player_dev_writer';
        END IF;
    END IF;

    -- Migration 0014 validation helper functions
    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_is_valid_catalog_positions') THEN
        REVOKE ALL ON FUNCTION public.fc27_is_valid_catalog_positions(jsonb) FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_valid_catalog_positions(jsonb) TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_valid_catalog_positions(jsonb) TO fc27_discord_reader';
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_is_safe_bounded_jsonb') THEN
        REVOKE ALL ON FUNCTION public.fc27_is_safe_bounded_jsonb(jsonb, text, boolean, integer) FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_safe_bounded_jsonb(jsonb, text, boolean, integer) TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_safe_bounded_jsonb(jsonb, text, boolean, integer) TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_safe_bounded_jsonb(jsonb, text, boolean, integer) TO fc27_discord_reader';
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_is_valid_suggestion_objective') THEN
        REVOKE ALL ON FUNCTION public.fc27_is_valid_suggestion_objective(jsonb) FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_valid_suggestion_objective(jsonb) TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_valid_suggestion_objective(jsonb) TO fc27_discord_reader';
        END IF;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'fc27_is_valid_match_id_array') THEN
        REVOKE ALL ON FUNCTION public.fc27_is_valid_match_id_array(text[]) FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_valid_match_id_array(text[]) TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_is_valid_match_id_array(text[]) TO fc27_discord_reader';
        END IF;
    END IF;

    -- 1. public.fc27_player_alias_fingerprints
    IF to_regclass('public.fc27_player_alias_fingerprints') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_player_alias_fingerprints FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_fingerprints FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_fingerprints FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE (last_seen_at, updated_at) ON public.fc27_player_alias_fingerprints TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_player_alias_fingerprints TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_player_alias_fingerprints TO fc27_discord_reader';
        END IF;
    END IF;

    -- 2. public.fc27_player_links
    IF to_regclass('public.fc27_player_links') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_player_links FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_links FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_links FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON public.fc27_player_links TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_player_links TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_links_link_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_player_links_link_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_links_link_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_links_link_id_seq FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_player_links_link_id_seq TO fc27_player_dev_writer';
        END IF;
    END IF;

    -- 3. public.fc27_build_catalog
    IF to_regclass('public.fc27_build_catalog') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_build_catalog FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_build_catalog FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_build_catalog FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON public.fc27_build_catalog TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_build_catalog TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_build_catalog_catalog_row_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_build_catalog_catalog_row_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_build_catalog_catalog_row_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_build_catalog_catalog_row_id_seq FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_build_catalog_catalog_row_id_seq TO fc27_player_dev_writer';
        END IF;
    END IF;

    -- 4. public.fc27_build_catalog_audit (append-only)
    IF to_regclass('public.fc27_build_catalog_audit') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_build_catalog_audit FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_build_catalog_audit FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_build_catalog_audit FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT ON public.fc27_build_catalog_audit TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_build_catalog_audit TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_build_catalog_audit_audit_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_build_catalog_audit_audit_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_build_catalog_audit_audit_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_build_catalog_audit_audit_id_seq FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_build_catalog_audit_audit_id_seq TO fc27_player_dev_writer';
        END IF;
    END IF;

    -- 5. public.fc27_player_suggestions
    IF to_regclass('public.fc27_player_suggestions') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_player_suggestions FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_suggestions FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_suggestions FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON public.fc27_player_suggestions TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_player_suggestions TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_suggestions_suggestion_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_player_suggestions_suggestion_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_suggestions_suggestion_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_suggestions_suggestion_id_seq FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_player_suggestions_suggestion_id_seq TO fc27_player_dev_writer';
        END IF;
    END IF;

    -- 6. public.fc27_suggestion_assessments (append-only)
    IF to_regclass('public.fc27_suggestion_assessments') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_suggestion_assessments FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_suggestion_assessments FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_suggestion_assessments FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT ON public.fc27_suggestion_assessments TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_suggestion_assessments TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_suggestion_assessments_assessment_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_suggestion_assessments_assessment_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_suggestion_assessments_assessment_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_suggestion_assessments_assessment_id_seq FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_suggestion_assessments_assessment_id_seq TO fc27_player_dev_writer';
        END IF;
    END IF;

    -- 7. public.fc27_discord_jobs
    IF to_regclass('public.fc27_discord_jobs') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_discord_jobs FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_discord_jobs FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_discord_jobs FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT ON public.fc27_discord_jobs TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE (state, claim_owner, claimed_at, lease_expires_at, attempt_count, last_error_category, available_at, completed_at) ON public.fc27_discord_jobs TO fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_discord_jobs TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_discord_jobs_job_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_discord_jobs_job_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_discord_jobs_job_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_discord_jobs_job_id_seq FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_discord_jobs_job_id_seq TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_discord_jobs_job_id_seq TO fc27_player_dev_writer';
        END IF;
    END IF;

    -- Durable reconciliation token inventory (0017)
    IF to_regclass('public.fc27_roster_reconciliation_tokens') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_roster_reconciliation_tokens FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_roster_reconciliation_tokens FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_roster_reconciliation_tokens FROM fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_roster_reconciliation_tokens FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON public.fc27_roster_reconciliation_tokens TO fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.fc27_roster_reconciliation_tokens TO fc27_match_results_writer';
        END IF;
    END IF;

    -- Least-privilege roster-refresh enqueue SECURITY DEFINER function (0018)
    IF to_regprocedure('public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz)
            FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz) FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz) FROM fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz) FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz) FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz) TO fc27_match_results_writer';
        END IF;
    END IF;

    -- Protected provider alias candidates & observation evidence (0019)
    IF to_regclass('public.fc27_player_alias_candidates') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_player_alias_candidates FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidates FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidates FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidates FROM fc27_poll_writer';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE (public_alias, last_seen_at, observation_count, distinct_match_count, verification_status, conflict_status, updated_at) ON public.fc27_player_alias_candidates TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidates FROM fc27_discord_reader';
            EXECUTE 'GRANT SELECT (candidate_id, player_id, public_alias, public_alias_normalized, source_classification, first_seen_at, last_seen_at, observation_count, distinct_match_count, verification_status, conflict_status, promoted_at, created_at, updated_at) ON public.fc27_player_alias_candidates TO fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidates FROM fc27_match_results_writer';
            EXECUTE 'GRANT SELECT (candidate_id, player_id, public_alias, public_alias_normalized, source_classification, first_seen_at, last_seen_at, observation_count, distinct_match_count, verification_status, conflict_status, promoted_at, created_at, updated_at), UPDATE (verification_status, conflict_status, promoted_at, updated_at) ON public.fc27_player_alias_candidates TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_alias_candidates_candidate_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_player_alias_candidates_candidate_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_candidates_candidate_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_candidates_candidate_id_seq FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_candidates_candidate_id_seq FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_candidates_candidate_id_seq FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_player_alias_candidates_candidate_id_seq TO fc27_poll_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_alias_candidate_observations') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_player_alias_candidate_observations FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidate_observations FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidate_observations FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidate_observations FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidate_observations FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_candidate_observations FROM fc27_poll_writer';
            EXECUTE 'GRANT SELECT, INSERT ON public.fc27_player_alias_candidate_observations TO fc27_poll_writer';
        END IF;
    END IF;

    -- Alias auto-promotion audit & review exceptions (0020)
    IF to_regclass('public.fc27_player_alias_promotion_audit') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_player_alias_promotion_audit FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_promotion_audit FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_promotion_audit FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_promotion_audit FROM fc27_poll_writer';
            EXECUTE 'GRANT SELECT ON public.fc27_player_alias_promotion_audit TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_promotion_audit FROM fc27_discord_reader';
            EXECUTE 'GRANT SELECT ON public.fc27_player_alias_promotion_audit TO fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_promotion_audit FROM fc27_match_results_writer';
            EXECUTE 'GRANT SELECT ON public.fc27_player_alias_promotion_audit TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_alias_promotion_audit_audit_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_player_alias_promotion_audit_audit_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_promotion_audit_audit_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_promotion_audit_audit_id_seq FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_promotion_audit_audit_id_seq FROM fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_promotion_audit_audit_id_seq FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_promotion_audit_audit_id_seq FROM fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_alias_review_exceptions') IS NOT NULL THEN
        REVOKE ALL ON public.fc27_player_alias_review_exceptions FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_review_exceptions FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_review_exceptions FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_review_exceptions FROM fc27_poll_writer';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE (exception_state, observation_count, distinct_match_count, last_detected_at, resolved_at, updated_at) ON public.fc27_player_alias_review_exceptions TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_review_exceptions FROM fc27_match_results_writer';
            EXECUTE 'GRANT SELECT, UPDATE (exception_state, resolved_at, resolved_observation_count, resolved_distinct_match_count, resolved_last_seen_at, resolved_candidate_state_digest, updated_at) ON public.fc27_player_alias_review_exceptions TO fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON public.fc27_player_alias_review_exceptions FROM fc27_discord_reader';
            EXECUTE 'GRANT SELECT ON public.fc27_player_alias_review_exceptions TO fc27_discord_reader';
        END IF;
    END IF;

    IF to_regclass('public.fc27_player_alias_review_exceptions_exception_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_player_alias_review_exceptions_exception_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_review_exceptions_exception_id_seq FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_review_exceptions_exception_id_seq FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_review_exceptions_exception_id_seq FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_review_exceptions_exception_id_seq FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE public.fc27_player_alias_review_exceptions_exception_id_seq FROM fc27_poll_writer';
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE public.fc27_player_alias_review_exceptions_exception_id_seq TO fc27_poll_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_evaluate_alias_candidates_for_promotion(integer, integer)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_evaluate_alias_candidates_for_promotion(integer, integer)
            FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_evaluate_alias_candidates_for_promotion(integer, integer) FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_evaluate_alias_candidates_for_promotion(integer, integer) FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_evaluate_alias_candidates_for_promotion(integer, integer) FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_evaluate_alias_candidates_for_promotion(integer, integer) FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_evaluate_alias_candidates_for_promotion(integer, integer) TO fc27_poll_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_auto_promote_stable_alias_candidate(bigint, text, integer, boolean)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_auto_promote_stable_alias_candidate(bigint, text, integer, boolean)
            FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_auto_promote_stable_alias_candidate(bigint, text, integer, boolean) FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_auto_promote_stable_alias_candidate(bigint, text, integer, boolean) FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_auto_promote_stable_alias_candidate(bigint, text, integer, boolean) FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_auto_promote_stable_alias_candidate(bigint, text, integer, boolean) FROM fc27_match_results_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_auto_promote_stable_alias_candidate(bigint, text, integer, boolean) TO fc27_poll_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_record_manual_alias_promotion_audit(bigint, text, text, text, integer, integer, text, text, text, bigint, timestamptz)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_record_manual_alias_promotion_audit(bigint, text, text, text, integer, integer, text, text, text, bigint, timestamptz)
            FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_record_manual_alias_promotion_audit(bigint, text, text, text, integer, integer, text, text, text, bigint, timestamptz) FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_record_manual_alias_promotion_audit(bigint, text, text, text, integer, integer, text, text, text, bigint, timestamptz) FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_record_manual_alias_promotion_audit(bigint, text, text, text, integer, integer, text, text, text, bigint, timestamptz) FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_record_manual_alias_promotion_audit(bigint, text, text, text, integer, integer, text, text, text, bigint, timestamptz) FROM fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_record_manual_alias_promotion_audit(bigint, text, text, text, integer, integer, text, text, text, bigint, timestamptz) TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_purge_resolved_alias_review_exceptions(integer, timestamptz)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_purge_resolved_alias_review_exceptions(integer, timestamptz)
            FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_purge_resolved_alias_review_exceptions(integer, timestamptz) FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_purge_resolved_alias_review_exceptions(integer, timestamptz) FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_purge_resolved_alias_review_exceptions(integer, timestamptz) FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_purge_resolved_alias_review_exceptions(integer, timestamptz) FROM fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_purge_resolved_alias_review_exceptions(integer, timestamptz) TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_normalize_alias_comparison_key(text)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_normalize_alias_comparison_key(text)
            FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_normalize_alias_comparison_key(text) FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_normalize_alias_comparison_key(text) FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.fc27_normalize_alias_comparison_key(text) FROM fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_normalize_alias_comparison_key(text) TO fc27_poll_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_normalize_alias_comparison_key(text) TO fc27_match_results_writer';
        END IF;
    END IF;

    -- Migration 0021 additions (Policy 22)
    IF to_regclass('public.fc27_discord_destinations') IS NOT NULL THEN
        REVOKE ALL ON TABLE public.fc27_discord_destinations FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON TABLE public.fc27_discord_destinations FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON TABLE public.fc27_discord_destinations FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON TABLE public.fc27_discord_destinations FROM fc27_poll_writer';
        END IF;
        GRANT SELECT ON TABLE public.fc27_discord_destinations TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON TABLE public.fc27_discord_destinations TO fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE public.fc27_discord_destinations TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_discord_destinations_destination_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_discord_destinations_destination_id_seq FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT USAGE ON SEQUENCE public.fc27_discord_destinations_destination_id_seq TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_match_discord_deliveries') IS NOT NULL THEN
        REVOKE ALL ON TABLE public.fc27_match_discord_deliveries FROM PUBLIC, fc27_app;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_relay') THEN
            EXECUTE 'REVOKE ALL ON TABLE public.fc27_match_discord_deliveries FROM fc27_relay';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_player_dev_writer') THEN
            EXECUTE 'REVOKE ALL ON TABLE public.fc27_match_discord_deliveries FROM fc27_player_dev_writer';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
            EXECUTE 'REVOKE ALL ON TABLE public.fc27_match_discord_deliveries FROM fc27_poll_writer';
        END IF;
        GRANT SELECT, INSERT, UPDATE ON TABLE public.fc27_match_discord_deliveries TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT SELECT ON TABLE public.fc27_match_discord_deliveries TO fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE public.fc27_match_discord_deliveries TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regclass('public.fc27_match_discord_deliveries_delivery_id_seq') IS NOT NULL THEN
        REVOKE ALL ON SEQUENCE public.fc27_match_discord_deliveries_delivery_id_seq FROM PUBLIC, fc27_app;
        GRANT USAGE ON SEQUENCE public.fc27_match_discord_deliveries_delivery_id_seq TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT USAGE ON SEQUENCE public.fc27_match_discord_deliveries_delivery_id_seq TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_reserve_discord_delivery(text, text, text, bigint, text)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_reserve_discord_delivery(text, text, text, bigint, text)
            FROM PUBLIC, fc27_app;
        GRANT EXECUTE ON FUNCTION public.fc27_reserve_discord_delivery(text, text, text, bigint, text) TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_reserve_discord_delivery(text, text, text, bigint, text) TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_record_discord_delivery_success(bigint, text, text)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_record_discord_delivery_success(bigint, text, text)
            FROM PUBLIC, fc27_app;
        GRANT EXECUTE ON FUNCTION public.fc27_record_discord_delivery_success(bigint, text, text) TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_record_discord_delivery_success(bigint, text, text) TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_record_discord_delivery_failure(bigint, text, text)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_record_discord_delivery_failure(bigint, text, text)
            FROM PUBLIC, fc27_app;
        GRANT EXECUTE ON FUNCTION public.fc27_record_discord_delivery_failure(bigint, text, text) TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_record_discord_delivery_failure(bigint, text, text) TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_get_deliveries_for_refresh(text)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_get_deliveries_for_refresh(text)
            FROM PUBLIC, fc27_app;
        GRANT EXECUTE ON FUNCTION public.fc27_get_deliveries_for_refresh(text) TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_discord_reader') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_get_deliveries_for_refresh(text) TO fc27_discord_reader';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_get_deliveries_for_refresh(text) TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_record_delivery_refresh_state(bigint, text, text)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_record_delivery_refresh_state(bigint, text, text)
            FROM PUBLIC, fc27_app;
        GRANT EXECUTE ON FUNCTION public.fc27_record_delivery_refresh_state(bigint, text, text) TO fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_record_delivery_refresh_state(bigint, text, text) TO fc27_match_results_writer';
        END IF;
    END IF;

    IF to_regprocedure('public.fc27_upsert_discord_destination(text, text, text, text, boolean, boolean, boolean)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.fc27_upsert_discord_destination(text, text, text, text, boolean, boolean, boolean)
            FROM PUBLIC, fc27_app, fc27_dispatch;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_match_results_writer') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION public.fc27_upsert_discord_destination(text, text, text, text, boolean, boolean, boolean) TO fc27_match_results_writer';
        END IF;
    END IF;
    IF to_regclass('public.fc27_provider_request_attempts') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fc27_poll_writer') THEN
        REVOKE ALL ON public.fc27_provider_request_attempts FROM fc27_poll_writer;
        GRANT SELECT, INSERT ON public.fc27_provider_request_attempts TO fc27_poll_writer;
        GRANT UPDATE (outcome_category) ON public.fc27_provider_request_attempts TO fc27_poll_writer;
    END IF;
END $$;
