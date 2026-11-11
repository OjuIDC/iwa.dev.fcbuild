-- =============================================================================
-- Migration 0015: FC 27 Player Development & Discord Job Follow-up Fixes
-- =============================================================================
-- Forward migration addressing PR 109/110 review follow-ups without modifying
-- the already-applied 0014 migration contents/checksum:
--   1. Enforce a composite catalog identity on fc27_build_catalog and a matching
--      composite foreign key on fc27_build_catalog_audit so immutable audit rows
--      cannot reference mismatched (catalog_row_id, build_key, version) tuples.
--   2. Harden fc27_discord_jobs claim-lease enforcement: revalidate when
--      claim_owner/claimed_at change. Historical expired-lease claimed rows are
--      preserved only via privileged administrative restore procedures (e.g.
--      controlled trigger disable/enable during restore), not by a runtime-settable
--      session GUC.
-- =============================================================================

-- 1. Composite catalog identity for immutable audit references
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'fc27_build_catalog_row_identity_uk'
           AND conrelid = 'public.fc27_build_catalog'::regclass
    ) THEN
        ALTER TABLE public.fc27_build_catalog
            ADD CONSTRAINT fc27_build_catalog_row_identity_uk
            UNIQUE (catalog_row_id, build_key, version);
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'fc27_build_catalog_audit_key_version_fk'
           AND conrelid = 'public.fc27_build_catalog_audit'::regclass
    ) THEN
        ALTER TABLE public.fc27_build_catalog_audit
            DROP CONSTRAINT fc27_build_catalog_audit_key_version_fk;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'fc27_build_catalog_audit_row_key_version_fk'
           AND conrelid = 'public.fc27_build_catalog_audit'::regclass
    ) THEN
        ALTER TABLE public.fc27_build_catalog_audit
            ADD CONSTRAINT fc27_build_catalog_audit_row_key_version_fk
            FOREIGN KEY (catalog_row_id, build_key, version)
            REFERENCES public.fc27_build_catalog (catalog_row_id, build_key, version)
            ON DELETE RESTRICT;
    END IF;
END $$;

-- 2. Hardened claim-lease enforcement (forward replacement of 0014 trigger fn)
CREATE OR REPLACE FUNCTION public.fc27_enforce_discord_job_claim_lease()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF NEW.state = 'claimed' THEN
        IF TG_OP = 'INSERT'
           OR OLD.state <> 'claimed'
           OR NEW.lease_expires_at IS DISTINCT FROM OLD.lease_expires_at
           OR NEW.claim_owner IS DISTINCT FROM OLD.claim_owner
           OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at
        THEN
            IF NEW.lease_expires_at <= pg_catalog.clock_timestamp() THEN
                RAISE EXCEPTION 'claimed fc27_discord_jobs row requires an unexpired lease_expires_at > now()'
                    USING ERRCODE = '23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

ALTER FUNCTION public.fc27_enforce_discord_job_claim_lease() OWNER TO fc27_owner;
REVOKE ALL ON FUNCTION public.fc27_enforce_discord_job_claim_lease() FROM PUBLIC;
