# FC27 Host Rebuild, Protected State Export/Import, and Recovery Runbook

> **Status**: Development-verified operational recovery runbook for host rebuilds, Windows reinstalls, Docker Desktop resets, repository reclones, container recreation, and immutable GHCR image promotions. Contains zero real secrets, passwords, tokens, credential URIs, numeric Discord IDs, or raw player aliases.

---

## 1. Recovery Inventory and Security Boundaries

The tracked manifest [`ops/fc27-protected-state-manifest.json`](../ops/fc27-protected-state-manifest.json) defines every non-Git operational asset required to rebuild the `fc27-full-stack` runtime cleanly:

| Category | Paths | Notes |
| :--- | :--- | :--- |
| **PostgreSQL & Redis Data** | Docker volumes `fc27-full-stack_postgres_data`, `fc27-full-stack_redis_data`; logical dumps `.local-data/backups/*.dump` + `.manifest.json` | Logical `pg_dump --format=custom` archives remain portable across PostgreSQL minor/host upgrades and WSL2/Docker Desktop resets. |
| **Protected Env Files** | `.env`, `.local-data/discord.env`, `.local-data/polling.env`, optional `.local-data/worker.env` | Contains numeric Discord guild/channel/operator role IDs and runtime feature flags (`FC27_ALIAS_AUTO_PROMOTION_ENABLED=false` by default). |
| **Least-Privilege Secrets (18 files)** | `secrets/postgres_password`, `secrets/postgres_admin_uri`, `secrets/redis_password`, `secrets/fc27_migrator_password`, `secrets/fc27_migrator_uri`, `secrets/fc27_app_password`, `secrets/fc27_app_uri`, `secrets/fc27_dispatch_password`, `secrets/fc27_relay_password`, `secrets/fc27_match_results_writer_password`, `secrets/fc27_poll_writer_password`, `secrets/fc27_poll_writer_uri`, `secrets/fc27_player_dev_writer_password`, `secrets/fc27_player_dev_writer_uri`, `secrets/fc27_discord_reader_password`, `secrets/fc27_discord_reader_uri`, `secrets/fc27_alias_hmac_key`, `secrets/discord_bot_token` (plus optional tracked writer URI `secrets/fc27_match_results_writer_uri`) | Mounted via Docker Compose secrets to isolated services only. Never committed to Git or included in Docker image build contexts (`.dockerignore`). |
| **Local Compose Overrides & Metadata** | `docker-compose.override.yml`, `docker-compose.local.yml`, `.local-data/compose/ghcr-promotion.yml`, `.local-data/runtime-deployment.json` | Pinned overrides must use immutable `@sha256:...` digests and embed zero credentials. |

---

## 2. Critical Continuity & Portability Rules

### `secrets/fc27_alias_hmac_key` Continuity Invariant
- `fc27-community-worker` computes `source_tag_hmac` using HMAC-SHA256 keyed by `secrets/fc27_alias_hmac_key` (at least 32 non-whitespace bytes).
- **Never regenerate `fc27_alias_hmac_key` after `public.fc27_player_alias_candidates` contains rows unless intentionally rotating all candidate hashes under maintenance.** A different key breaks correlation between newly polled matches and existing `source_tag_hmac` candidate records.
- `src.fc27_protected_state` records a non-secret domain-separated verification digest (`alias_hmac_key_fingerprint_sha256 = HMAC-SHA256(key, "fc27:alias-hmac-key-fingerprint:v1")`) inside `bundle-manifest.json` and `.local-data/runtime-deployment.json`, allowing `verify-postgres-volume` and `readiness` to detect accidental key replacement before starting the polling worker.

### Logical `pg_dump` / `pg_restore` vs. Raw Docker Volume Copies
- **Prefer logical backups (`scripts/fc27_backup.py` -> `.dump` + `.manifest.json`)** for OS reinstalls, Windows/WSL2 rebuilds, PostgreSQL minor version upgrades, and cross-machine transfers. Logical backups include `schema_migrations`, `schema_policy_versions`, `fc27_roster`, `fc27_player_alias_candidates`, `fc27_player_alias_candidate_observations`, `fc27_player_alias_promotion_audit`, `fc27_player_alias_review_exceptions`, `fc27_discord_jobs`, `fc27_provider_request_attempts`, and all match/stats tables (`EXPECTED_MIGRATION_VERSION = "0001_THROUGH_0027"`).
- **Docker volume snapshots (`fc27-full-stack_postgres_data`)** are fast for local container recreation or Docker Desktop restarts on the same host engine, provided PostgreSQL is stopped cleanly (`docker compose stop postgres`) before copying the backing ext4/VHDX volume data.

---

## 3. Pre-Rebuild Backup & Export Procedure (Before Host Changes)

1. Verify current repository commit, container health, and clean Git status:
   ```powershell
   git status --short
   docker ps --filter "label=com.docker.compose.project=fc27-full-stack"
   ```
2. Capture a verified logical PostgreSQL custom-format backup (`fc27_backup_*.dump` + `.manifest.json`):
   ```powershell
   python scripts/fc27_backup.py --backup-dir .local-data/backups --retention-count 7
   ```
3. Verify the logical backup archive cleanly restores into an ephemeral database:
   ```powershell
   python scripts/fc27_restore_verify.py --backup-dir .local-data/backups
   ```
4. Export the allowlisted protected configuration, secrets, local overrides, and metadata **outside the Git repository working tree** (for example, to encrypted BitLocker/VeraCrypt storage or an offline removable volume):
   ```powershell
   python -m src.fc27_protected_state export --output E:\fc27-offline-vault\fc27-protected-state.zip
   ```
   - Generates `E:\fc27-offline-vault\fc27-protected-state.zip` and `E:\fc27-offline-vault\fc27-protected-state.zip.sha256` with restrictive Windows ACLs.
   - Fails closed if exported inside the Git working tree, if any required file is missing/placeholder, if any symlink/NTFS junction is encountered, or if any numeric Discord ID is malformed.
5. Copy the latest `.local-data/backups/fc27_backup_*.dump` and `.manifest.json` alongside `fc27-protected-state.zip` in encrypted offline storage.

---

## 4. Complete 27-Step Host Rebuild & Recovery Runbook

1. Install Windows security updates, PowerShell 7, Git, Python 3.11+, and Docker Desktop (WSL2 backend).
2. Enable BitLocker or mount the encrypted offline recovery volume containing `fc27-protected-state.zip`, `fc27-protected-state.zip.sha256`, and the latest `.dump` + `.manifest.json`.
3. Obtain a fresh checkout of the repository into a non-synced local NTFS directory and verify the commit hash:
   ```powershell
   gh repo clone OjuIDC/iwa.dev.fcbuild C:\Users\<operator>\dev\iwa.dev.fcbuild
   Set-Location C:\Users\<operator>\dev\iwa.dev.fcbuild
   git rev-parse HEAD
   ```
4. Create and activate `.venv` and install project dependencies matching `pyproject.toml`.
5. Run a read-only dry-run import first to verify bundle SHA-256, `bundle-manifest.json` member hashes, archive safety (no traversal, symlinks, or unallowlisted files), and numeric Discord IDs:
   ```powershell
   python -m src.fc27_protected_state import `
     --input E:\fc27-offline-vault\fc27-protected-state.zip `
     --dry-run
   ```
6. Import protected env files, 18 secret files, local Compose overrides, and runtime deployment metadata into the fresh workspace:
   ```powershell
   python -m src.fc27_protected_state import `
     --input E:\fc27-offline-vault\fc27-protected-state.zip
   ```
7. Confirm restrictive file permissions on `.env`, `.local-data/`, `.local-data/*.env`, and `secrets/*`:
   - **Windows**: inheritance disabled, accessible only to the current user SID, `BUILTIN\Administrators`, and `NT AUTHORITY\SYSTEM`:
     ```powershell
     icacls .env .local-data secrets secrets\*
     ```
   - **Linux / POSIX hosts**: shipped `fc27-discord-bot` and `fc27-community-worker` containers execute as non-root `10001:65534`. Keep offline recovery ZIP bundles/sidecars strictly `0600`, and ensure runtime secret/env files are readable by container UID `10001` (either owned by `10001:65534` with `0600`/`0700`, or readable by shared container GID `65534` with `0640`/`0750` and zero world permissions — unless using rootless Docker user-namespace UID mapping that maps the host operator UID to `10001`).
   ```bash
   sudo chown -R 10001:65534 secrets .local-data .env
   chmod 0700 secrets .local-data .local-data/compose
   chmod 0600 .env .local-data/*.env secrets/*
   ```
8. Verify `git status --short` shows zero untracked secrets or staged local configuration leaks.
9. Confirm auto-promotion defaults remain disabled unless separately authorized for Stage 2:
   - `FC27_ALIAS_AUTO_PROMOTION_ENABLED=false`
   - `FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE=true`
   - `FC27_ALIAS_REVIEW_NOTIFICATION_ENABLED=false`
10. Authenticate to GitHub Container Registry if using immutable GHCR digests:
    ```powershell
    gh auth token | docker login ghcr.io -u <github-user> --password-stdin
    ```
11. Start only `postgres` and `redis` first (never start worker or bot before database readiness passes):
    ```powershell
    docker compose -p fc27-full-stack `
      --env-file .env `
      --env-file .local-data/polling.env `
      --env-file .local-data/discord.env `
      -f docker-compose.yml `
      up -d postgres redis
    ```
12. Wait for healthy PostgreSQL and Redis containers (`docker compose -p fc27-full-stack ps`).
13. **Path A — Preserved Docker Volume**: If `fc27-full-stack_postgres_data` already contains production data, verify it non-destructively:
    ```powershell
    python -m src.fc27_protected_state verify-postgres-volume
    ```
14. **Path B — Fresh Empty Host / Restore from Logical Dump**: Bootstrap roles if starting from a brand-new cluster (`--confirm` only on an intentionally fresh cluster after inspecting the dry-run plan):
    ```powershell
    docker compose -p fc27-full-stack --env-file .env --profile bootstrap run --rm db-bootstrap
    docker compose -p fc27-full-stack --env-file .env --profile bootstrap run --rm db-bootstrap --confirm
    ```
15. On **Path B**, apply schema migrations cleanly (`0001` through `0027` and Grants Policy `28`):
    ```powershell
    docker compose -p fc27-full-stack --env-file .env run --rm db-migrate
    ```
16. On **Path B**, restore the logical custom dump into the PostgreSQL database after copying the verified `.dump` into `.local-data/backups/` and run `pg_restore` / `scripts/fc27_restore_verify.py`.
17. On **Path A or Path B**, re-run `db-migrate` cleanly to confirm idempotent Migration `0027` and Grants Policy `28` postconditions:
    ```powershell
    docker compose -p fc27-full-stack --env-file .env run --rm db-migrate
    ```
18. Verify PostgreSQL volume & `fc27_alias_hmac_key` continuity read-only (when running from the host against `secrets/postgres_admin_uri` whose hostname is `postgres` on the internal Compose network, `src.fc27_protected_state` automatically falls back cleanly to a host-mapped loopback port or an ephemeral stdin-authenticated read-only `psql` check inside the running Compose `postgres` container without exposing credentials in command arguments):
    ```powershell
    python -m src.fc27_protected_state verify-postgres-volume --db-uri (Get-Content secrets/postgres_admin_uri -Raw).Trim()
    ```
19. Verify read-only SQL counts across `schema_migrations`, `schema_policy_versions`, `fc27_roster`, `fc27_matches`, `fc27_player_alias_candidates`, `fc27_player_alias_promotion_audit`, `fc27_player_alias_review_exceptions`, and `fc27_discord_jobs`.
20. Execute the 23-point sanitized pre-start readiness gate and require `"status": "READY"` (`overall_ready: true`):
    ```powershell
    python -m src.fc27_protected_state readiness --db-uri (Get-Content secrets/postgres_admin_uri -Raw).Trim()
    ```
21. Verify Compose merged configuration resolution fails closed cleanly with zero credential leakage (`--quiet` validation):
    ```powershell
    docker compose -p fc27-full-stack `
      --env-file .env `
      --env-file .local-data/polling.env `
      --env-file .local-data/discord.env `
      -f docker-compose.yml `
      -f docker-compose.community-worker.yml `
      -f docker-compose.discord.yml `
      -f docker-compose.ghcr.yml `
      config --quiet
    ```
22. Start `fc27-discord-bot` first (single replica, `--no-deps`) while keeping `fc27-community-worker` stopped until Discord bot health stabilizes:
    ```powershell
    docker compose -p fc27-full-stack `
      --env-file .env `
      --env-file .local-data/polling.env `
      --env-file .local-data/discord.env `
      -f docker-compose.yml `
      -f docker-compose.community-worker.yml `
      -f docker-compose.discord.yml `
      -f docker-compose.ghcr.yml `
      --profile fc27-discord `
      up -d --no-deps fc27-discord-bot
    ```
23. Verify `fc27-discord-bot` `--status` readiness cleanly inside the container:
    ```powershell
    docker exec fc27-full-stack-fc27-discord-bot-1 python scripts/fc27_discord_bot.py --status
    ```
24. Start `fc27-community-worker` (single replica, `--no-deps`, 12-hour polling interval preserved):
    ```powershell
    docker compose -p fc27-full-stack `
      --env-file .env `
      --env-file .local-data/polling.env `
      --env-file .local-data/discord.env `
      -f docker-compose.yml `
      -f docker-compose.community-worker.yml `
      -f docker-compose.discord.yml `
      -f docker-compose.ghcr.yml `
      --profile fc27-polling `
      --profile fc27-discord `
      up -d --no-deps fc27-community-worker
    ```
25. Verify `fc27-community-worker` `--status` cleanly without issuing any provider HTTP call:
    ```powershell
    docker exec fc27-full-stack-fc27-community-worker-1 python scripts/fc27_polling_worker.py --status
    ```
26. Run post-start health verification (`0` provider calls executed):
    ```powershell
    python -m src.fc27_protected_state verify-post-start --compose-project fc27-full-stack --db-uri (Get-Content secrets/fc27_discord_reader_uri -Raw).Trim()
    ```
27. Monitor container restart counts (`RestartCount == 0`) and confirm `fc27_discord_jobs` and `next_poll_eligible` schedules are intact.

---

## 5. Auto-Promotion Stage 2 Rollout & Immediate Rollback Controls

Auto-promotion remains **disabled by default** in code, `.env` defaults, and `ops/fc27-protected-state-manifest.json`. Do not enable in production until explicitly authorized in Stage 2:

1. **Shadow Mode Observation**: Keep `FC27_ALIAS_AUTO_PROMOTION_ENABLED=false` and `FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE=true`. Logs and health summaries count `candidates_would_promote` and exception classifications without mutating `public.fc27_roster`.
2. **Active Promotion Mode (Bounded)**: Only when authorized, set in `.local-data/polling.env`:
   - `FC27_ALIAS_AUTO_PROMOTION_ENABLED=true`
   - `FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE=false`
   - `FC27_ALIAS_AUTO_PROMOTION_MIN_DISTINCT_MATCHES=2`
   - `FC27_ALIAS_AUTO_PROMOTION_MAX_PER_POLL=10`
   - `FC27_ALIAS_AUTO_PROMOTION_REFRESH_MESSAGES=true`
   - `FC27_ALIAS_AUTO_PROMOTION_POLICY_VERSION=stable-v1`
3. **Instant Safe Rollback**: Set `FC27_ALIAS_AUTO_PROMOTION_ENABLED=false` and recreate `fc27-community-worker`. Already-committed audit rows (`fc27_player_alias_promotion_audit`) remain intact, and operators retain `/setplayer`, `/renameplayer`, and `/rejectplayer` authority to supersede or revert individual entries at any time.
