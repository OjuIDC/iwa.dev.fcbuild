# FC27 Backup and Recovery (`src/fc27_backup_restore.py`)

## Overview & Current Scope Classification (`PARTIAL_FC27_DOMAIN_TABLES_ONLY`)

`src/fc27_backup_restore.py`, `scripts/fc27_backup.py`, and `scripts/fc27_restore_verify.py` provide local PostgreSQL backup creation, retention enforcement, SHA-256 integrity verification, and isolated restore verification for the **5 normalized East Coasters FC27 domain tables** (`FC27_DOMAIN_TABLES`).

> **Scope & Historical Recovery Notice**:
> - Current executable backup coverage in `src/fc27_backup_restore.py` is a **partial domain-table backup** (`PARTIAL_FC27_DOMAIN_TABLES_ONLY`); it does **not** yet archive the full required database state across migration ledger, identity, approval/authority, dispatch/outbox, and LangGraph checkpoint tables.
> - `fc27_match_team_stats` in `FC27BackupTableCounts` is a **derived manifest row-count alias** equal to `public.fc27_matches` (`FC27_DERIVED_MANIFEST_COUNT_ALIASES = {"fc27_match_team_stats": "fc27_matches"}`), **not** a physical PostgreSQL table created by migrations `0001` through `0012`.
> - **Historical recovery status**: No authoritative historical backup has yet been verified (`HISTORICAL_RECOVERY_VERIFIED=NO`).
> - **Future Match Results**: Manual Match Results and Match Results audit/Discord linkage backup requirements are **pending the future Match Results feature migration** (not yet implemented).

## Current Backup Inclusions & Manifest (`FC27_DOMAIN_TABLES`)

Each backup writes two untracked local files under `.local-data/backups/`:
1. `<backup_id>.dump` — Custom-format `pg_dump -Fc` archive of the **5** normalized FC27 domain tables (`FC27_DOMAIN_TABLES`):
   - `public.fc27_clubs` (club metadata)
   - `public.fc27_export_batches` (polling reconstruction, ingest, and export batch history)
   - `public.fc27_matches` (FC27 provider-derived matches and team-level aggregates)
   - `public.fc27_player_match_stats` (player match statistics)
   - `public.fc27_quarantine_records` (validation quarantine records)
2. `<backup_id>.manifest.json` — `FC27BackupManifest` containing:
   - `backup_id`, `created_at_utc`, `migration_version` (`0001_THROUGH_0012`)
   - `archive_filename`, `archive_size_bytes`, `archive_sha256`
   - `table_counts` (`FC27BackupTableCounts`: row counts for the 5 physical tables in `FC27_DOMAIN_TABLES` plus the derived `fc27_match_team_stats` alias mirroring `fc27_matches`)
   - `record_hashes` (`FC27BackupRecordHashes`: `matches_sha256`, `player_stats_sha256`, `combined_records_sha256`, `match_record_hashes`)

## Full Required Database State & Current Backup Omissions (`FC27_BACKUP_OMITTED_TABLES_BY_DOMAIN`)

The target full-state backup scope requires complete coverage of the FC27 PostgreSQL `public` schema. The following domains are **omitted** from the current 5-table `FC27_DOMAIN_TABLES` archive and tracked for a dedicated backup-remediation PR (`SEPARATE_BACKUP_REMEDIATION_PR`):

| Domain | Current Executable Backup Status | Tables / Scope |
|---|---|---|
| **Schema & Migration Ledger** | Omitted (`metadata checked only`) | `public.schema_migrations`, `public.schema_policy_versions` |
| **FC27 Provider Matches, Player Stats, Polling Reconstruction & Ingest/Export** | **Included** (`FC27_DOMAIN_TABLES`) | `public.fc27_clubs`, `public.fc27_export_batches`, `public.fc27_matches`, `public.fc27_player_match_stats`, `public.fc27_quarantine_records` |
| **Identity State** | Omitted | `public.fc27_users`, `public.fc27_user_roles`, `public.fc27_invitations`, `public.fc27_sessions`, `public.fc27_webauthn_credentials`, `public.fc27_totp_credentials`, `public.fc27_recovery_codes`, `public.fc27_action_tokens`, `public.fc27_challenges`, `public.fc27_bootstrap_locks`, `public.fc27_credentials` |
| **Approval, Nonces & Authority Audit** | Omitted | `public.graph_threads`, `public.run_sequences`, `public.approval_requests`, `public.approval_nonces`, `public.approval_audit`, `public.fc27_decision_evidence`, `public.authority_policies`, `public.authority_delegations`, `public.fc27_delegations`, `public.player_assignments`, `public.player_decision_requests`, `public.player_decisions`, `public.approval_escalations`, `public.authority_conflicts`, `public.idempotency_keys` |
| **Dispatch & Outbox State** | Omitted | `public.domain_events`, `public.outbox`, `public.dispatches`, `public.dispatch_plans`, `public.dispatch_transition_rules`, `public.player_observations`, `public.dispatch_assessments`, `public.dispatch_delivery_attempts`, `public.dispatch_acknowledgments`, `public.dispatch_plan_decisions`, `public.dispatch_comments`, `public.analyst_notes`, `public.players`, `public.activation_gates`, `public.activation_gate_log`, `public.service_heartbeats`, `public.dispatch_worker_claims` |
| **LangGraph `PostgresSaver` Checkpoints (when enabled)** | Omitted | `public.checkpoints`, `public.checkpoint_blobs`, `public.checkpoint_writes`, `public.checkpoint_migrations` |
| **Future Manual Match Results & Audit/Discord Linkage** | **Pending feature migration** (`NOT_IMPLEMENTED`) | Future manual Match Results and Match Results audit/Discord linkage tables (not created by migrations `0001`–`0012`) |
| **Operational Backup Controls** | Incomplete | Complete full-stack restoration ownership/procedure, archive encryption-at-rest, NAS transfer automation, and backup failure alerting |

`fc27_credentials`, `fc27_delegations`, and `activation_gates` in the scope column are compatibility views, not physical tables; their definitions are also omitted from the archive and tracked separately as `FC27_BACKUP_OMITTED_VIEWS`.

## Retention Policy

`enforce_backup_retention(...)` retains the **7 most recent** backup `.dump` + `.manifest.json` pairs (`BACKUP_RETENTION_COUNT = 7`) and prunes older pairs automatically.

## Restore Verification

`verify_fc27_backup_restore(...)` (`scripts/fc27_restore_verify.py`):
1. Verifies the `.dump` archive SHA-256 against `<backup_id>.manifest.json` (fails closed with `FC27BackupIntegrityError` on any byte mismatch).
2. Creates a disposable temporary database, applies canonical migrations `0001` through `0012` via `src.db_migrate`, restores the 5-table domain archive via `pg_restore`, and drops the temporary database in a `finally` block.
3. Verifies all 5 restored domain table counts (plus the derived `fc27_match_team_stats` alias) and canonical record hashes against the manifest.
4. Executes `Search -> Scout -> Analyst` over the restored records and verifies `MODERATE_CONFIDENCE` readback.
