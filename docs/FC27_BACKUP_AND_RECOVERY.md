# FC27 Backup and Recovery (`src/fc27_backup_restore.py`)

## Overview

`src/fc27_backup_restore.py`, `scripts/fc27_backup.py`, and `scripts/fc27_restore_verify.py` provide local PostgreSQL backup creation, retention enforcement, SHA-256 integrity verification, and isolated restore verification for East Coasters FC27 domain data.

## Backup Scope & Manifest

Each backup writes two untracked local files under `.local-data/backups/`:
1. `<backup_id>.dump` — Custom-format `pg_dump -Fc` archive of the 6 FC27 domain tables:
   - `public.fc27_clubs`
   - `public.fc27_export_batches`
   - `public.fc27_matches`
   - `public.fc27_match_team_stats`
   - `public.fc27_player_match_stats`
   - `public.fc27_quarantine_records`
2. `<backup_id>.manifest.json` — `FC27BackupManifest` containing:
   - `backup_id`, `created_at_utc`, `migration_version` (`0001_through_0012`)
   - `archive_filename`, `archive_size_bytes`, `archive_sha256`
   - `table_counts` (`FC27BackupTableCounts`)
   - `record_hashes` (`FC27BackupRecordHashes`: `matches_sha256`, `player_stats_sha256`, `combined_records_sha256`, `match_record_hashes`)

## Retention Policy

`enforce_backup_retention(...)` retains the **7 most recent** backup `.dump` + `.manifest.json` pairs (`BACKUP_RETENTION_COUNT = 7`) and prunes older pairs automatically.

## Restore Verification

`verify_fc27_backup_restore(...)` (`scripts/fc27_restore_verify.py`):
1. Verifies the `.dump` archive SHA-256 against `<backup_id>.manifest.json` (fails closed with `FC27BackupIntegrityError` on any byte mismatch).
2. Creates a disposable temporary database, restores the archive via `pg_restore`, and drops the temporary database in a `finally` block.
3. Verifies all 6 table counts and canonical record hashes against the manifest.
4. Executes `Search -> Scout -> Analyst` over the restored records and verifies `MODERATE_CONFIDENCE` readback.
