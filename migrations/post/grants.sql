-- grants-policy-version: 1
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
END $$;
