# Dispatch Assignment: Milestone 4 Schema & Migration Explorer

**Agent**: `explorer_m4_schema_1`  
**Archetype**: `teamwork_preview_explorer`  
**Role**: Milestone 4 Schema & Migration Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_schema_1`  
**Timestamp**: 2026-10-03T20:08:00Z  

---

## 1. Context & Scope

You are exploring the database DDL and migration requirements for Milestone 4: Activation Gates & Atomic Approval-to-Dispatch Creation.

### Mandatory Reading
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 8: Activation gates and atomic approval-to-dispatch creation, lines 919–1040)
4. `migrations/0003_dispatch_foundation.sql` (inspect end of file)
5. `migrations/post/grants.sql`
6. `src/db_migrate.py`

---

## 2. Exploration Objectives

Analyze and design the exact DDL additions for Section E of `migrations/0003_dispatch_foundation.sql` and `migrations/post/grants.sql`:
1. `public.activation_gate_log` table:
   - Columns: `id bigserial PRIMARY KEY`, `gate_id text CHECK (gate_id IN ('G-ID','G-ATOMIC','G-OUTBOX'))`, `status text CHECK (status IN ('passed','failed'))`, `evidence_ref text NOT NULL CHECK (char_length(evidence_ref) BETWEEN 1 AND 500)`, `code_revision text NOT NULL`, `deployment_id text NOT NULL`, `fc_version text NOT NULL CHECK (fc_version = '27')`, `platform text NOT NULL CHECK (platform = 'common-gen5')`, `nonce text NOT NULL`, `signature_sha256 text NOT NULL`, `recorded_by text NOT NULL`, `recorded_at timestamptz NOT NULL DEFAULT now()`.
   - Immutability / append-only trigger using `public.fc27_reject_mutation()`.
2. `public.activation_gates` view:
   - Compute latest state per gate (`DISTINCT ON (gate_id)`).
3. `public.service_heartbeats` table:
   - `service_name text PRIMARY KEY`, `last_heartbeat timestamptz NOT NULL DEFAULT now()`, `details jsonb NOT NULL DEFAULT '{}'::jsonb`.
4. Role grants in `migrations/post/grants.sql`:
   - `fc27_app` grants: `SELECT, INSERT` on `activation_gate_log`, `SELECT` on `activation_gates` view, `SELECT` on `service_heartbeats`.
   - `fc27_dispatch` grants: `SELECT, INSERT, UPDATE` on `service_heartbeats`.
   - Policy version bump (version 5) in `grants.sql` and `GRANTS_POLICY` in `src/db_migrate.py`.
5. Check event source function `fc27_check_event_source()`:
   - Verify `activation_gate_log` is recognized as a valid source table for `activation.gate_recorded` domain events.

---

## 3. Output Requirements

Write `schema_analysis.md` and `handoff.md` in your working directory.
Include exact, copy-paste ready SQL snippets, constraint definitions, grant statements, and verification commands.
Do NOT modify production source code in `src/` or `migrations/`.
Notify orchestrator via `send_message` when done.


## 2026-10-03T20:13:59Z

**Sender**: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b (parent)
**Context**: Milestone 4 Schema Exploration
**Content**: Note that you do not need to run commands to check the grants hash or schema. You can read files directly using `view_file`:
- `migrations/post/grants.sql` is currently at version 4.
- `src/db_migrate.py:21` has `GRANTS_POLICY = GrantsPolicy(version=4, sha256="9488d48e4ca1ebea64fa7fedd44cc3c02712b0fa2d9bafac424dc97b80711495")`.
- When appending Section E grants, bump policy version to 5.
**Action**: Please proceed with your analysis using `view_file` and write `schema_analysis.md` and `handoff.md`.
