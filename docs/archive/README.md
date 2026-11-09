# Historical Documentation & Script Archive

> **Operational status: DO NOT USE**
>
> All files under `docs/archive/` and `scripts/archive/` are **non-authoritative** historical records retained solely for audit provenance and architectural history. Do not execute commands, SQL snippets, or deployment steps found in archived files.

For current operational, architectural, provider, security, and testing documentation, start at [`docs/README.md`](../README.md) and root [`README.md`](../../README.md).

---

## Archive Manifests

- **Human-Readable Manifest**: [`docs/archive/ARCHIVE_MANIFEST.md`](ARCHIVE_MANIFEST.md)
- **Machine-Readable Manifest**: [`config/fc27_archive_manifest.json`](../../config/fc27_archive_manifest.json) (`fc27-archive-manifest-v1`)
- **Removed-Items Manifest**: [`config/fc27_removed_items_manifest.json`](../../config/fc27_removed_items_manifest.json)

---

## Archived Categories & Complete Index

### 1. Providers (`docs/archive/providers/`)
Historical provider notes and superseded early transport wiring:
- [`docs/archive/providers/mcp_ws.md`](providers/mcp_ws.md) — Historical v2.0.0 MCP proxy and EA retry note (superseded by [`docs/FC27_PROVIDER_INTEGRATION.md`](../FC27_PROVIDER_INTEGRATION.md) and [`docs/SECURITY_BOUNDARIES.md`](../SECURITY_BOUNDARIES.md)).

### 2. Phases (`docs/archive/phases/`)
Historical implementation phase notes for non-primary subsystems:
- [`docs/archive/phases/installer.md`](phases/installer.md) — Historical v2.0.0 Windows host-agent installer phase note (superseded by [`docs/DEVELOPMENT.md`](../DEVELOPMENT.md)).
- [`docs/archive/phases/watcher_ocr_rundown.md`](phases/watcher_ocr_rundown.md) — Historical v2.0.0 watcher, OCR, and post-match rundown phase note (superseded by [`docs/FC27_COMPONENT_STATUS.md`](../FC27_COMPONENT_STATUS.md)).

### 3. Experiments (`docs/archive/experiments/` & `scripts/archive/experiments/`)
Unimplemented or superseded experimental integration documents and scripts:
- [`docs/archive/experiments/WINDHOVER_INTEGRATION_GAP.md`](experiments/WINDHOVER_INTEGRATION_GAP.md) — Historical 2026-10-01 unimplemented Windhover contract gap note (superseded by [`docs/FC27_COMPONENT_STATUS.md`](../FC27_COMPONENT_STATUS.md)).
- [`scripts/archive/experiments/matchpulse_conformance.py`](../../scripts/archive/experiments/matchpulse_conformance.py) — Superseded placeholder MatchPulse conformance script (replaced by [`scripts/check_matchpulse_boundary.py`](../../scripts/check_matchpulse_boundary.py)).

### 4. Architecture (`docs/archive/architecture/`)
Superseded Phase 2 root specifications consolidated into current `docs/`:
- [`docs/archive/architecture/PHASE2_EVIDENCE_BINDING.md`](architecture/PHASE2_EVIDENCE_BINDING.md) — Superseded by [`docs/FC27_APPROVAL_FLOW.md`](../FC27_APPROVAL_FLOW.md).
- [`docs/archive/architecture/PHASE2_RUNBOOKS.md`](architecture/PHASE2_RUNBOOKS.md) — Superseded by [`docs/OPERATIONS.md`](../OPERATIONS.md).
- [`docs/archive/architecture/PHASE2_SECURITY_INVARIANTS.md`](architecture/PHASE2_SECURITY_INVARIANTS.md) — Superseded by [`docs/SECURITY_BOUNDARIES.md`](../SECURITY_BOUNDARIES.md).
- [`docs/archive/architecture/PHASE2_TRANSACTION_MODEL.md`](architecture/PHASE2_TRANSACTION_MODEL.md) — Superseded by [`docs/DATABASE_SCHEMA.md`](../DATABASE_SCHEMA.md).
- [`docs/archive/architecture/PHASE2_WEBAUTHN_POLICY.md`](architecture/PHASE2_WEBAUTHN_POLICY.md) — Superseded by [`docs/FC27_APPROVAL_FLOW.md`](../FC27_APPROVAL_FLOW.md).

### 5. Plans (`docs/archive/plans/`)
Historical point-in-time design plans:
- [`docs/archive/plans/2026-10-02-player-dispatch-design.md`](plans/2026-10-02-player-dispatch-design.md) — Superseded by [`docs/DISPATCH_ACTIVATION.md`](../DISPATCH_ACTIVATION.md).

### 6. Prompts (`docs/archive/prompts/`)
Historical audit and task prompt artifacts:
- [`docs/archive/prompts/ORIGINAL_REQUEST.md`](prompts/ORIGINAL_REQUEST.md) — Superseded by [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md).
- [`docs/archive/prompts/PHASE2_VICTORY_AUDIT_REQUEST.md`](prompts/PHASE2_VICTORY_AUDIT_REQUEST.md) — Superseded by [`docs/TESTING.md`](../TESTING.md).

### 7. Reports (`docs/archive/reports/`)
Point-in-time audit and release gate reports:
- [`docs/archive/reports/BUNDLE_AUDIT.md`](reports/BUNDLE_AUDIT.md) — Superseded by [`docs/TESTING.md`](../TESTING.md).
- [`docs/archive/reports/FINAL_ARCHIVE_AUDIT.md`](reports/FINAL_ARCHIVE_AUDIT.md) — Superseded by [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md).
- [`docs/archive/reports/HACKATHON_ALIGNMENT.md`](reports/HACKATHON_ALIGNMENT.md) — Superseded by [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md).
- [`docs/archive/reports/IMPLEMENTATION_REPORT.md`](reports/IMPLEMENTATION_REPORT.md) — Superseded by [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md).
- [`docs/archive/reports/PHASE2_LOCAL_MERGE_GATE_REPORT.md`](reports/PHASE2_LOCAL_MERGE_GATE_REPORT.md) — Superseded by [`docs/TESTING.md`](../TESTING.md).
- [`docs/archive/reports/PHASE2_REMEDIATION_REPORT.md`](reports/PHASE2_REMEDIATION_REPORT.md) — Superseded by [`docs/SECURITY_BOUNDARIES.md`](../SECURITY_BOUNDARIES.md).
- [`docs/archive/reports/SECURITY_CHANGES.md`](reports/SECURITY_CHANGES.md) — Superseded by [`docs/SECURITY_BOUNDARIES.md`](../SECURITY_BOUNDARIES.md).
- [`docs/archive/reports/TYPED_GRAPH_VOICE_AUDIT.md`](reports/TYPED_GRAPH_VOICE_AUDIT.md) — Superseded by [`docs/FC27_COMPONENT_STATUS.md`](../FC27_COMPONENT_STATUS.md).

### 8. Test Results (`docs/archive/test-results/`)
Historical test-execution snapshots and traceability matrices:
- [`docs/archive/test-results/PHASE2_TEST_TRACEABILITY.md`](test-results/PHASE2_TEST_TRACEABILITY.md) — Superseded by [`docs/TESTING.md`](../TESTING.md).
- [`docs/archive/test-results/TEST_RESULTS_2026-10-03_PHASE1.md`](test-results/TEST_RESULTS_2026-10-03_PHASE1.md) — Superseded by [`TEST_RESULTS.md`](../../TEST_RESULTS.md).
