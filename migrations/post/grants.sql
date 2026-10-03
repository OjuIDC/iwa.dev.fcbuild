-- grants-policy-version: 2
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
    END IF;
    IF to_regclass('public.domain_events_event_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.domain_events_event_id_seq TO fc27_app;
    END IF;

    IF to_regclass('public.outbox') IS NOT NULL THEN
        GRANT INSERT ON public.outbox TO fc27_app;
    END IF;
    IF to_regclass('public.outbox_outbox_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.outbox_outbox_id_seq TO fc27_app;
    END IF;
END $$;
