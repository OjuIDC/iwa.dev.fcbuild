# FC27 Private GHCR Digest-Pinned Runtime Promotion and Rollback

> **Status**: Active operational runbook and policy for promoting verified, signed, private GitHub Container Registry (`ghcr.io`) images into the local `fc27-full-stack` runtime by immutable SHA-256 digest.

---

## 1. Approved Private GHCR Image References

All runtime promotions from GitHub Container Registry (`ghcr.io`) MUST pin exact verified OCI SHA-256 digests published from trusted `main` commits after `ci`, `security-scan`, and `container-images` gates succeed.

| Service | Private GHCR Repository | Approved Immutable Digest (`main` @ `299a7376b4cb96f2d7cae419d3d3c787bfb33c97`) | Full Digest-Pinned Reference |
| :--- | :--- | :--- | :--- |
| `fc27-discord-bot` | `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot` | `sha256:b8b571088b9a490fa4a1a517c60e1f43305ad863f3ecea9fc5df636541e7e084` | `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot@sha256:b8b571088b9a490fa4a1a517c60e1f43305ad863f3ecea9fc5df636541e7e084` |
| `fc27-community-worker` | `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker` | `sha256:8628acc26c9c73d9360a5e47d66c10c988e03d8273b5f15bb1f7cd3e1dc1fb79` | `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker@sha256:8628acc26c9c73d9360a5e47d66c10c988e03d8273b5f15bb1f7cd3e1dc1fb79` |

### Non-Negotiable Digest and Tag Policy

- **Immutable digests only**: Never deploy by `:main`, `:latest`, branch name, or semantic-version tag.
- **Private visibility**: Both GHCR packages remain `private` (`GHCR_PACKAGE_VISIBILITY=PRIVATE`); unauthenticated pulls are rejected (`HTTP 401`).
- **Zero embedded credentials**: Registry tokens or PATs must never appear in `docker-compose*.yml`, `.env`, `.local-data/*.env`, or image references. Authenticate locally using the operator's GitHub CLI session (`read:packages` scope) piped to the OS Docker credential helper (`gh auth token --user iwa-ojuidc | docker login ghcr.io -u iwa-ojuidc --password-stdin`).
- **Single-replica boundary**: Run at most **one replica** (`replicas=1`) of `fc27-discord-bot` and **one replica** (`replicas=1`) of `fc27-community-worker`.

---

## 2. Pre-Promotion Verification Gates

Before replacing any running container, every candidate GHCR digest must pass:

1. **Authenticated Pull & Digest Match**:
   - Pull by exact `@sha256:<64-hex>` digest and verify `docker image inspect <ref> --format "{{json .RepoDigests}}"` matches the expected digest.
2. **Sigstore Cosign Keyless OIDC Signature Verification**:
   - Fulcio issuer: `sigstore.dev` / `sigstore-intermediate`
   - OIDC issuer: `https://token.actions.githubusercontent.com`
   - Certificate SAN workflow identity: `https://github.com/iwa-ojuidc/iwa.dev.fcbuild/.github/workflows/container-images.yml@refs/heads/main`
   - Source repository: `iwa-ojuidc/iwa.dev.fcbuild`
   - Source commit: `299a7376b4cb96f2d7cae419d3d3c787bfb33c97`
   - Rekor transparency log entry present (`logIndex=3692446213` for `fc27-discord-bot`; `logIndex=3692446060` for `fc27-community-worker`).
3. **SLSA v1 Provenance & SPDX-2.3 SBOM Verification**:
   - Provenance predicate type `https://slsa.dev/provenance/v1` tied to commit `299a7376b4cb96f2d7cae419d3d3c787bfb33c97`, builder `https://github.com/iwa-ojuidc/iwa.dev.fcbuild/actions/runs/41959064030/attempts/1`, platform `linux/amd64`, and pinned base image `python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e`.
   - SPDX-2.3 SBOM (`https://spdx.dev/Document`) attached to the OCI index (`159` packages for `fc27-discord-bot`, `141` packages for `fc27-community-worker`).
4. **Offline Pre-Deployment Validation (`--network none --read-only`)**:
   - Verify non-root execution (`10001:65534`), `--status` readiness output, zero embedded `.env` or secret files, and clean `SIGTERM` exit (`0`).

---

## 3. Tracked GHCR Compose Overlay vs. Local-Build Mode

The repository provides a tracked deployment overlay (`docker-compose.ghcr.yml`) containing only immutable GHCR image references and zero credentials, while preserving local-build definitions in `docker-compose.community-worker.yml` and `docker-compose.discord.yml` for local development.

### Deploy or Update Using the Digest-Pinned GHCR Overlay

```powershell
docker compose `
  --env-file .env `
  --env-file .local-data/polling.env `
  --env-file .local-data/discord.env `
  -f docker-compose.yml `
  -f docker-compose.community-worker.yml `
  -f docker-compose.discord.yml `
  -f docker-compose.ghcr.yml `
  --profile fc27-polling `
  --profile fc27-discord `
  up -d
```

### Promote One Service at a Time (Controlled Sequential Promotion)

Always promote **one service at a time** (`fc27-discord-bot` first, `fc27-community-worker` second after verifying `next_poll_eligible` and persisted polling state in PostgreSQL):

```powershell
# 1. Promote Discord bot only (preserving PostgreSQL, Redis, and polling worker)
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
  up -d --no-deps --no-build fc27-discord-bot

# 2. Promote community polling worker only after Discord observation and schedule gate pass
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
  up -d --no-deps --no-build fc27-community-worker
```

### Local-Build Mode (Development Default)

Omitting `-f docker-compose.ghcr.yml` retains the local `Dockerfile.community-worker` and `Dockerfile.discord` build definitions in `docker-compose.community-worker.yml` and `docker-compose.discord.yml`.

---

## 4. Preserved Runtime Security & Schedule Invariants

Applying `docker-compose.ghcr.yml` over `docker-compose.community-worker.yml` and `docker-compose.discord.yml` preserves all runtime security and rate-limit invariants:

- **Community Polling Worker (`fc27-community-worker`)**:
  - `FC27_COMMUNITY_POLL_INTERVAL_SECONDS=43200` (12-hour interval, maximum 1 provider request per cycle, 0 retries)
  - `command: ["--worker", "--confirm-live-polling"]`
  - `healthcheck: ["CMD", "python", "scripts/fc27_polling_worker.py", "--status"]`
  - `user: "10001:65534"`, `read_only: true`, `cap_drop: ["ALL"]`, `no-new-privileges: true`
  - Persisted schedule recovery from PostgreSQL (`recover_polling_state_from_postgres`) suppresses startup polling whenever `now_utc < next_poll_eligible`.
- **Discord Analytics Bot (`fc27-discord-bot`)**:
  - `command: ["--run"]`
  - `healthcheck: ["CMD", "python", "scripts/fc27_discord_bot.py", "--status"]`
  - `user: "10001:65534"`, `read_only: true`, `cap_drop: ["ALL"]`, `no-new-privileges: true`
  - Zero provider access (`FC27_COMMUNITY_POLLING_ENABLED=false`, `FC27_COMMUNITY_PROVIDER_ENABLED=false`, `DISCORD_PROVIDER_REQUESTS=0`) and read-only PostgreSQL access (`DISCORD_DATABASE_WRITE_ACCESS=NO`).

---

## 5. Rollback Procedure

Before promoting a new GHCR digest, tag the currently running local image IDs as immutable local rollback references (recorded in ignored `.local-artifacts/promotion/fc27-ghcr-rollback.json`):

- `fc27-rollback/discord-bot:20261105T032827Z` -> `sha256:1dc09af85ae8a8d52a4c16ee83610e725c723df259b376e950c7ff95a5a3b4b1`
- `fc27-rollback/community-worker:20261105T032827Z` -> `sha256:907158e590c9d177adf3c1ef60040377a74ad303b84456778daa5df7717c821b`

To roll back a single service without touching PostgreSQL, Redis, or the other service:

1. Point the local override (`.local-data/compose/ghcr-promotion.yml`) for that service to its `fc27-rollback/<service>:<timestamp>` tag (or omit the GHCR overlay to use the preserved local build image).
2. Recreate only the target service with `--no-deps --no-build`:
   ```powershell
   docker compose -p fc27-full-stack `
     --env-file .env `
     --env-file .local-data/polling.env `
     --env-file .local-data/discord.env `
     -f docker-compose.yml `
     -f docker-compose.community-worker.yml `
     -f docker-compose.discord.yml `
     --profile fc27-polling `
     --profile fc27-discord `
     up -d --no-deps --no-build <fc27-discord-bot|fc27-community-worker>
   ```
3. Verify one healthy replica (`docker ps`, `docker inspect`) and confirm zero unexpected provider requests or database writes.
