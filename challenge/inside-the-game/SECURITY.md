# Security Policy

## Scope

MatchPulse Live ingests **synthetic** match events only and produces
evidence-backed, localized match insights. It exposes a small HTTP API, a
WebSocket stream, and a static demo page.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through the repository's
GitHub Security Advisories page (`Security` tab → `Report a vulnerability`)
rather than opening a public issue. Include the affected component, the
reproduction steps, and the impact you observed.

We aim to acknowledge a report within 5 business days.

## Security design

- **Synthetic-only enforcement.** `MatchEvent.source` is constrained to the
  literal `synthetic` at the schema boundary, and `CHALLENGE_SYNTHETIC_ONLY`
  must remain enabled for ingestion to be accepted. The service returns
  `503` when the safety gate is turned off.
- **No real match data.** No real league, club, player, fixture, broadcast,
  commentary, music, badge, or crest is stored, served, or downloaded.
- **No secrets in the repository.** Foundry access is supplied at runtime
  through environment variables. No API key, connection string, publish
  profile, or private key is committed. `.env` is ignored; `.env.example`
  contains placeholders only.
- **Optional model enrichment is guarded.** Microsoft Foundry is a constrained
  narrative copy editor. It cannot create match state, alter evidence, or add
  unsupported numbers. A timeout, invalid JSON, service error, unsupported
  claim, or schema mismatch falls back to the deterministic pipeline.
- **Secret hygiene in logs.** Credential values are never written to logs or
  returned in API responses. Fallback warnings carry only an exception type.
- **Azure identity preferred.** In Azure, managed identity via
  `DefaultAzureCredential` is preferred over a stored API key.
- **Container hardening.** The published image runs as a non-root user, the
  Compose stack uses a read-only root filesystem, a bounded `/tmp`, dropped
  capabilities, and `no-new-privileges`.

## Out of scope

- Findings that require an already-compromised host or an existing
  administrative credential.
- Denial of service caused by deliberately flooding a local instance.
- The synthetic-data restriction itself: it is an intentional design
  constraint, not a vulnerability.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |
