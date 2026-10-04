# Milestone 4: Database Schema & Migration Exploration Report

**Agent**: `explorer_m4_schema_1`  
**Role**: Milestone 4 Schema & Migration Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_schema_1`  
**Timestamp**: 2026-10-03T20:15:00Z  

---

## 1. Executive Summary

This report delivers the complete, authoritative database schema and migration design for **Milestone 4: Activation Gates & Atomic Approval-to-Dispatch Creation**.

Milestone 4 requires:
1. **Section E of `migrations/0003_dispatch_foundation.sql`**:
   - `public.activation_gate_log` table (append-only audit log for manual gate challenges).
   - Append-only immutability trigger using `public.fc27_reject_mutation()`.
   - `public.activation_gates` view projecting latest recorded status per `gate_id`.
   - `public.service_heartbeats` table registering liveness heartbeats for background services.
   - Event source validation in `public.fc27_check_event_source()` recognizing `activation.gate_recorded` sourced from `activation_gate_log`.
2. **Post-migration Grants in `migrations/post/grants.sql`**:
   - Policy version bump from `4` to `5`.
   - `fc27_app` grants: `SELECT, INSERT` on `activation_gate_log`, `USAGE` on sequence `activation_gate_log_id_seq`, `SELECT` on view `activation_gates`, `SELECT` on `service_heartbeats`.
   - `fc27_dispatch` grants: `SELECT, INSERT, UPDATE` on `service_heartbeats` (no access to `activation_gate_log` or `activation_gates`).
3. **Migration Runner Alignment in `src/db_migrate.py`**:
   - `GRANTS_POLICY` updated to `version=5` with exact SHA-256 digest of `grants.sql`.

---

## 2. DDL Specification for Section E (`0003_dispatch_foundation.sql`)

Append Section E to `migrations/0003_dispatch_foundation.sql` following Section D (`fc27_dispatch_transition`):

```sql
-- ==============================================================================
-- Section E: Activation Gates, Service Heartbeats & Event Source Alignment
-- ==============================================================================

-- 1. Activation Gate Log (append-only)
CREATE TABLE IF NOT EXISTS public.activation_gate_log (
    id bigserial PRIMARY KEY,
    gate_id text NOT NULL CHECK (gate_id IN ('G-ID', 'G-ATOMIC', 'G-OUTBOX')),
    status text NOT NULL CHECK (status IN ('passed', 'failed')),
    evidence_ref text NOT NULL CHECK (char_length(evidence_ref) BETWEEN 1 AND 500),
    code_revision text NOT NULL,
    deployment_id text NOT NULL,
    fc_version text NOT NULL CHECK (fc_version = '27'),
    platform text NOT NULL CHECK (platform = 'common-gen5'),
    nonce text NOT NULL,
    signature_sha256 text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

-- Immutability trigger: prevent UPDATE and DELETE on activation_gate_log
CREATE TRIGGER fc27_activation_gate_log_immutable
    BEFORE UPDATE OR DELETE ON public.activation_gate_log
    FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

-- Indexes for activation_gate_log
CREATE INDEX IF NOT EXISTS activation_gate_log_gate_recorded_idx
    ON public.activation_gate_log (gate_id, recorded_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS activation_gate_log_deployment_idx
    ON public.activation_gate_log (code_revision, deployment_id, gate_id, recorded_at DESC);


-- 2. Activation Gates View (latest record per gate_id)
CREATE OR REPLACE VIEW public.activation_gates AS
SELECT DISTINCT ON (gate_id)
    id,
    gate_id,
    status,
    evidence_ref,
    code_revision,
    deployment_id,
    fc_version,
    platform,
    nonce,
    signature_sha256,
    recorded_by,
    recorded_at
FROM public.activation_gate_log
ORDER BY gate_id, recorded_at DESC, id DESC;


-- 3. Service Heartbeats Table
CREATE TABLE IF NOT EXISTS public.service_heartbeats (
    service_name text PRIMARY KEY,
    last_heartbeat timestamptz NOT NULL DEFAULT now(),
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS service_heartbeats_last_idx
    ON public.service_heartbeats (service_name, last_heartbeat DESC);


-- 4. Update Event Source Trigger to validate activation.gate_recorded
CREATE OR REPLACE FUNCTION public.fc27_check_event_source()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Transition-only event types (C-10)
    IF (NEW.event_type LIKE 'dispatch.%' AND NEW.event_type != 'dispatch.created') 
       OR NEW.event_type LIKE 'plan.%' 
       OR NEW.event_type LIKE 'tracking.%' THEN
        IF current_user != 'fc27_owner' OR session_user NOT IN ('fc27_app', 'fc27_dispatch') THEN
            RAISE EXCEPTION 'transition-only event type not allowed outside transition function';
        END IF;
    END IF;

    -- Sourced event validation
    IF NEW.event_type = 'approval.decided' THEN
        IF NEW.source_table IS DISTINCT FROM 'approval_audit' THEN
            RAISE EXCEPTION 'approval.decided requires source_table = approval_audit';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'approval.decided requires a source_id';
        END IF;
        -- Entity ownership check (entity_id matches approval_request_id or thread_id)
        IF NEW.entity_type = 'approval_request' THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.approval_audit 
                 WHERE id = NEW.source_id::bigint 
                   AND approval_request_id = NEW.entity_id::uuid
            ) THEN
                RAISE EXCEPTION 'source row not found or entity mismatch for approval.decided';
            END IF;
        ELSE
            IF NOT EXISTS (
                SELECT 1 FROM public.approval_audit 
                 WHERE id = NEW.source_id::bigint 
                   AND thread_id = NEW.entity_id
            ) THEN
                RAISE EXCEPTION 'source row not found or entity mismatch for approval.decided';
            END IF;
        END IF;

    ELSIF NEW.event_type IN ('approval.created', 'approval.expired') THEN
        IF NEW.source_table IS DISTINCT FROM 'approval_requests' THEN
            RAISE EXCEPTION 'event_type requires source_table = approval_requests';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.approval_requests 
             WHERE approval_request_id = NEW.source_id::uuid 
               AND approval_request_id = NEW.entity_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found or entity mismatch for approval request event';
        END IF;

    ELSIF NEW.event_type = 'dispatch.created' THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatches' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatches';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;

    ELSIF NEW.event_type = 'dispatch.acknowledged' THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_acknowledgments' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_acknowledgments';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;

    ELSIF NEW.event_type IN ('tracking.updated', 'tracking.completed') THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_assessments' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_assessments';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;

    ELSIF NEW.event_type = 'activation.gate_recorded' THEN
        IF NEW.source_table IS DISTINCT FROM 'activation_gate_log' THEN
            RAISE EXCEPTION 'activation.gate_recorded requires source_table = activation_gate_log';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'activation.gate_recorded requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.activation_gate_log
             WHERE id = NEW.source_id::bigint
               AND gate_id = NEW.entity_id
        ) THEN
            RAISE EXCEPTION 'source row not found or entity mismatch for activation.gate_recorded';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;
```

---

## 3. Post-Migration Grants Specification (`migrations/post/grants.sql`)

Update `migrations/post/grants.sql`:
1. Bump line 1 header from version 4 to 5:
   ```sql
   -- grants-policy-version: 5
   ```
2. Insert Milestone 4 grants before `END $$;`:
   ```sql
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
   ```

### Privilege Boundary Matrix

| Database Object | `fc27_app` | `fc27_dispatch` | Notes |
|---|---|---|---|
| `public.activation_gate_log` | `SELECT, INSERT` | None | Append-only; immutability trigger blocks UPDATE/DELETE. |
| `public.activation_gate_log_id_seq` | `USAGE` | None | Mandatory for inserting into table with `bigserial` PK. |
| `public.activation_gates` (view) | `SELECT` | None | Read-only projection of latest gate states. |
| `public.service_heartbeats` | `SELECT` | `SELECT, INSERT, UPDATE` | Worker daemon records heartbeats via UPDATE. |

---

## 4. Migration Runner Alignment (`src/db_migrate.py`)

In `src/db_migrate.py`:
1. Update `GRANTS_POLICY`:
   ```python
   GRANTS_POLICY = GrantsPolicy(version=5, sha256="<computed_sha256_of_grants.sql>")
   ```
2. The SHA-256 digest is computed as:
   `hashlib.sha256(Path("migrations/post/grants.sql").read_text(encoding="utf-8").encode()).hexdigest()`
3. Both the header `-- grants-policy-version: 5` and the exact file hash are validated on `migrate()` execution and stored in `public.schema_policy_versions`.

---

## 5. Nonce & Domain Event Alignment

### 5.1 Nonce Table Alignment
`public.approval_nonces` already possesses the required constraints established in Section B (`0003_dispatch_foundation.sql` lines 680–720):
- `gate_id text NULL` column exists.
- `CHECK (subject_type IN (..., 'activation_gate'))`.
- `CHECK ((subject_type = 'activation_gate' AND gate_id IS NOT NULL AND thread_id IS NULL AND dispatch_id IS NULL AND user_id IS NULL))`.
- `fc27_app` already has `GRANT SELECT, INSERT, UPDATE ON public.approval_nonces TO fc27_app;`.
- No further alterations to `approval_nonces` are necessary.

### 5.2 Domain Event Alignment
`public.domain_events` already has `'activation_gate_log'` present in its column check:
`source_table text CHECK (source_table IN (..., 'activation_gate_log'))`.
`visibility text CHECK (visibility IN ('staff','player_owner','audit_only','ops'))` allows `'audit_only'`, which is the mandated visibility for `activation.gate_recorded` events.

---

## 6. Verification and Negative Tests Matrix

When Milestone 4 tests are executed:
1. **Append-Only Guard Test**:
   - `UPDATE public.activation_gate_log SET status = 'failed' WHERE id = 1` -> raises `CheckViolation` (`activation_gate_log is append-only`).
   - `DELETE FROM public.activation_gate_log WHERE id = 1` -> raises `CheckViolation` (`activation_gate_log is append-only`).
2. **Event Source Guard Test**:
   - `record_event(..., event_type='activation.gate_recorded', source_table='dispatches', ...)` -> raises exception (`activation.gate_recorded requires source_table = activation_gate_log`).
   - `record_event(..., event_type='activation.gate_recorded', source_table='activation_gate_log', source_id='999999', ...)` -> raises exception (`source row not found or entity mismatch for activation.gate_recorded`).
   - `record_event(..., event_type='activation.gate_recorded', source_table='activation_gate_log', source_id=str(log_id), entity_id='G-ATOMIC', ...)` (when log is for G-ID) -> raises exception (entity mismatch).
3. **Role Privilege Boundaries Test**:
   - Connecting as `fc27_dispatch`: attempting `SELECT FROM activation_gate_log` raises `InsufficientPrivilege`.
   - Connecting as `fc27_app`: attempting `UPDATE service_heartbeats` raises `InsufficientPrivilege`.
   - Connecting as `fc27_dispatch`: inserting/updating `service_heartbeats` succeeds.
4. **Grants Policy Verification Test**:
   - `test_grants_policy_constant_matches_file` passes with version 5.
   - `test_grants_policy_recorded` records policy version 5 in `public.schema_policy_versions`.
