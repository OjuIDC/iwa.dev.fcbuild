# Documentation Archive Policy

## Purpose & Scope

`docs/archive/` stores historical documentation, superseded architectural specifications, completed phase implementation reports, historical test snapshots, and replaced execution prompts for the `iwa.dev.fcbuild` repository.

## Non-Negotiable Archive Rules

1. **Historical Documentation Only**: This archive is exclusively for historical documentation, superseded plans, and replaced reports.
2. **No Active Code or Tests**:
   - The archive is **not** for active source code (`src/`, `scripts/`, `host_agent/`, `voice/`).
   - The archive is **not** for active tests (`tests/`).
   - The archive is **not** for database migrations (`migrations/`).
3. **No Secrets or Real Data**: The archive is **not** for secrets, credentials, `.local-artifacts/`, `.local-data/`, or real East Coasters club/player data.
4. **Non-Authoritative Status**:
   - All archived documents are **non-authoritative** (`OPERATIONAL_STATUS: DO NOT USE`).
   - Archived instructions, commands, feature-flag settings, and schema snippets must **never** be followed operationally.
   - Current authoritative documentation must not cite archived files as active operational authority.
5. **Required Provenance & Replacement Metadata**:
   - Every archived Markdown file must carry a top-of-file notice stating its archived status, archive date, original path, and current authoritative replacement path (plus a command-execution warning when historical shell commands appear).
   - Every archived file must be registered in [`docs/archive/ARCHIVE_MANIFEST.md`](ARCHIVE_MANIFEST.md).
6. **Git History Preservation**:
   - Archival moves use `git mv <previous_path> <archive_path>` so full Git blame and commit history remain intact.
   - Archive filenames remain human-understandable and preserve original dates or milestone identifiers where known.

## Archive Directory Categories

Only populated categories are created:

- `docs/archive/architecture/` — Superseded phase-specific security, evidence-binding, runbook, transaction, and WebAuthn specifications whose active rules have been consolidated into current authoritative guides (`docs/SECURITY_BOUNDARIES.md`, `docs/OPERATIONS.md`, `docs/FC27_APPROVAL_FLOW.md`).
- `docs/archive/plans/` — Superseded or draft architectural design specifications (`2026-10-02-player-dispatch-design.md`).
- `docs/archive/prompts/` — Historical execution prompts and audit request rubrics (`ORIGINAL_REQUEST.md`, `PHASE2_VICTORY_AUDIT_REQUEST.md`).
- `docs/archive/reports/` — Completed milestone implementation reports, merge-gate reports, and pre-2.0.0 bundle/archive audits.
- `docs/archive/test-results/` — Historical dated test-result and traceability snapshots superseded by current commit-bound verification in [`TEST_RESULTS.md`](../../TEST_RESULTS.md) and [`docs/TESTING.md`](../TESTING.md).
