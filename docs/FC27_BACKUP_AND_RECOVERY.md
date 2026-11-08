# FC27 Backup and Recovery (`src/fc27_backup_restore.py`)

## Overview & Current Scope Classification (`PARTIAL_FC27_DOMAIN_TABLES_ONLY`)

`src/fc27_backup_restore.py`, `scripts/fc27_backup.py`, and `scripts/fc27_restore_verify.py` provide local PostgreSQL backup creation, retention enforcement, SHA-256 integrity verification, and isolated restore verification for the **11 backed-up FC27 domain, migration-ledger, and manual Match Results tables** (`FC27_DOMAIN_TABLES`).

> **Scope & Historical Recovery Notice**:
> - Current executable backup coverage in `src/fc27_backup_restore.py` is a **partial domain-table backup** (`PARTIAL_FC27_DOMAIN_TABLES_ONLY`); it covers the 5 normalized provider/import domain tables, the 2 migration/policy ledger tables (`schema_migrations`, `schema_policy_versions`), and the 4 manual Match Results tables (`fc27_manual_matches`, `fc27_manual_match_players`, `fc27_manual_match_events`, `fc27_manual_match_audit`), while identity, approval/authority, dispatch/outbox, and LangGraph checkpoint tables remain omitted (`full_required_database_state_covered = False`).
> - `fc27_match_team_stats` and `fc27_manual_discord_linked_matches` in `FC27BackupTableCounts` are **derived manifest row-count aliases** (`FC27_DERIVED_MANIFEST_COUNT_ALIASES`), **not** separate physical PostgreSQL tables.
> - **Historical recovery status**: No authoritative historical backup has yet been verified (`HISTORICAL_RECOVERY_VERIFIED=NO`).

## Current Backup Inclusions & Manifest (`FC27_DOMAIN_TABLES`)

Each backup writes two untracked local files under `.local-data/backups/`:
1. `<backup_id>.dump` — Custom-format `pg_dump -Fc` archive of the **11** backed-up tables (`FC27_DOMAIN_TABLES`):
   - `public.fc27_clubs` (club metadata)
   - `public.fc27_export_batches` (polling reconstruction, ingest, and export batch history)
   - `public.fc27_matches` (FC27 provider-derived matches and team-level aggregates)
   - `public.fc27_player_match_stats` (player match statistics)
   - `public.fc27_quarantine_records` (validation quarantine records)
   - `public.schema_migrations` (applied numbered migration ledger)
   - `public.schema_policy_versions` (applied grants policy version ledger)
   - `public.fc27_manual_matches` (manual Match Results fixtures, scores, team stats, and Discord message/channel linkage)
   - `public.fc27_manual_match_players` (manual Match Results player stats)
   - `public.fc27_manual_match_events` (manual Match Results goal/assist events)
   - `public.fc27_manual_match_audit` (append-only manual Match Results audit trail)
2. `<backup_id>.manifest.json` — `FC27BackupManifest` containing:
   - `backup_id`, `created_at_utc`, `migration_version` (`0001_THROUGH_0013`)
   - `archive_filename`, `archive_size_bytes`, `archive_sha256`
   - `table_counts` (`FC27BackupTableCounts`: row counts for all 11 physical tables in `FC27_DOMAIN_TABLES` plus derived `fc27_match_team_stats` and `fc27_manual_discord_linked_matches` counts)
   - `record_hashes` (`FC27BackupRecordHashes`: `matches_sha256`, `player_stats_sha256`, `combined_records_sha256`, `match_record_hashes`, `manual_matches_sha256`, `manual_players_sha256`, `manual_events_sha256`, `manual_audit_sha256`)

## Full Required Database State & Current Backup Omissions (`FC27_BACKUP_OMITTED_TABLES_BY_DOMAIN`)

The target full-state backup scope requires complete coverage of the FC27 PostgreSQL `public` schema. The following domains are covered or **omitted** from the current 11-table `FC27_DOMAIN_TABLES` archive and tracked for a dedicated backup-remediation PR (`SEPARATE_BACKUP_REMEDIATION_PR`):

| Domain | Current Executable Backup Status | Tables / Scope |
|---|---|---|
| **Schema & Migration Ledger** | **Included** (`FC27_DOMAIN_TABLES`) | `public.schema_migrations`, `public.schema_policy_versions` |
| **FC27 Provider Matches, Player Stats, Polling Reconstruction & Ingest/Export** | **Included** (`FC27_DOMAIN_TABLES`) | `public.fc27_clubs`, `public.fc27_export_batches`, `public.fc27_matches`, `public.fc27_player_match_stats`, `public.fc27_quarantine_records` |
| **Manual Match Results & Audit/Discord Linkage** | **Included** (`FC27_DOMAIN_TABLES`) | `public.fc27_manual_matches`, `public.fc27_manual_match_players`, `public.fc27_manual_match_events`, `public.fc27_manual_match_audit` |
| **Identity State** | Omitted | `public.fc27_users`, `public.fc27_user_roles`, `public.fc27_invitations`, `public.fc27_sessions`, `public.fc27_webauthn_credentials`, `public.fc27_totp_credentials`, `public.fc27_recovery_codes`, `public.fc27_action_tokens`, `public.fc27_challenges`, `public.fc27_bootstrap_locks`, `public.fc27_credentials` |
| **Approval, Nonces & Authority Audit** | Omitted | `public.graph_threads`, `public.run_sequences`, `public.approval_requests`, `public.approval_nonces`, `public.approval_audit`, `public.fc27_decision_evidence`, `public.authority_policies`, `public.authority_delegations`, `public.fc27_delegations`, `public.player_assignments`, `public.player_decision_requests`, `public.player_decisions`, `public.approval_escalations`, `public.authority_conflicts`, `public.idempotency_keys` |
| **Dispatch & Outbox State** | Omitted | `public.domain_events`, `public.outbox`, `public.dispatches`, `public.dispatch_plans`, `public.dispatch_transition_rules`, `public.player_observations`, `public.dispatch_assessments`, `public.dispatch_delivery_attempts`, `public.dispatch_acknowledgments`, `public.dispatch_plan_decisions`, `public.dispatch_comments`, `public.analyst_notes`, `public.players`, `public.activation_gates`, `public.activation_gate_log`, `public.service_heartbeats`, `public.dispatch_worker_claims` |
| **LangGraph `PostgresSaver` Checkpoints (when enabled)** | Omitted | `public.checkpoints`, `public.checkpoint_blobs`, `public.checkpoint_writes`, `public.checkpoint_migrations` |
| **Operational Backup Controls** | Incomplete | Complete full-stack restoration ownership/procedure, archive encryption-at-rest, NAS transfer automation, and backup failure alerting |

`fc27_credentials`, `fc27_delegations`, and `activation_gates` in the scope column are compatibility views, not physical tables; their definitions are also omitted from the archive and tracked separately as `FC27_BACKUP_OMITTED_VIEWS`. Manual Match Results tables (`fc27_manual_matches`, `fc27_manual_match_players`, `fc27_manual_match_events`, `fc27_manual_match_audit`) and migration ledger tables (`schema_migrations`, `schema_policy_versions`), previously tracked as `Pending feature migration` / omitted prior to Migration `0013`, are now `INCLUDED` in `FC27_DOMAIN_TABLES` (`FC27_PENDING_FUTURE_BACKUP_DOMAINS = ()`).

## Retention Policy

`enforce_backup_retention(...)` retains the **7 most recent** backup `.dump` + `.manifest.json` pairs (`BACKUP_RETENTION_COUNT = 7`) and prunes older pairs automatically.

## Restore Verification

`verify_fc27_backup_restore(...)` (`scripts/fc27_restore_verify.py`):
1. Verifies the `.dump` archive SHA-256 against `<backup_id>.manifest.json` (fails closed with `FC27BackupIntegrityError` on any byte mismatch).
2. Creates a disposable temporary database, applies canonical migrations `0001` through `0013` via `src.db_migrate`, restores the 11-table domain archive via `pg_restore`, and drops the temporary database in a `finally` block.
3. Verifies all 11 restored domain table counts (plus derived `fc27_match_team_stats` and `fc27_manual_discord_linked_matches`) and canonical record hashes against the manifest.
4. Executes `Search -> Scout -> Analyst` over the restored records and verifies `MODERATE_CONFIDENCE` readback when >= 5 provider matches are present.
