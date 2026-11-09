# Archive Manifest (docs/archive/)

This manifest is the authoritative registry of all historical, point-in-time, and superseded documents and scripts archived under `docs/archive/` and `scripts/archive/`.

- **Manifest Version**: `fc27-archive-manifest-v1`
- **Machine-Readable Manifest**: [`config/fc27_archive_manifest.json`](../../config/fc27_archive_manifest.json)
- **Pre-Archive Base Commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`
- **Archive Policy**: Every archived document carries a non-authoritative header with `Operational status: DO NOT USE` and must never be cited as current operational authority.

## Archived Document Registry

| Archived Path | Previous Path | Title | Original Purpose | Archive Date | Reason | Superseded By | Operational Status | Reviewer |
|---|---|---|---|---|---|---|---|---|
| docs/archive/prompts/ORIGINAL_REQUEST.md | ORIGINAL_REQUEST.md | Original Phase 1 Hackathon Prompt | Captured initial multi-agent EA FC 27 hackathon prompt | 2026-10-29 | Point-in-time initial user request superseded by current reconciled platform docs | README.md | DO NOT USE | UNASSIGNED |
| docs/archive/prompts/PHASE2_VICTORY_AUDIT_REQUEST.md | PHASE2_VICTORY_AUDIT_REQUEST.md | Phase 2 Victory Audit Prompt | Defined Phase 2 audit acceptance prompt | 2026-10-29 | Point-in-time Phase 2 audit request prompt superseded by current architecture docs | docs/ARCHITECTURE.md | DO NOT USE | UNASSIGNED |
| docs/archive/reports/IMPLEMENTATION_REPORT.md | IMPLEMENTATION_REPORT.md | Phase 1 Implementation Report | Summarized initial Phase 1 delivery | 2026-10-29 | Point-in-time Phase 1 report superseded by current architecture and component status docs | docs/ARCHITECTURE.md | DO NOT USE | UNASSIGNED |
| docs/archive/reports/PHASE2_LOCAL_MERGE_GATE_REPORT.md | PHASE2_LOCAL_MERGE_GATE_REPORT.md | Phase 2 Local Merge Gate Report | Recorded Phase 2 merge gate verification results | 2026-10-29 | Point-in-time Phase 2 gate report superseded by current test results report | TEST_RESULTS.md | DO NOT USE | UNASSIGNED |
| docs/archive/reports/PHASE2_REMEDIATION_REPORT.md | PHASE2_REMEDIATION_REPORT.md | Phase 2 Remediation Report | Documented Phase 2 security remediation pass | 2026-10-29 | Point-in-time Phase 2 remediation report superseded by current security boundaries | docs/SECURITY_BOUNDARIES.md | DO NOT USE | UNASSIGNED |
| docs/archive/reports/SECURITY_CHANGES.md | SECURITY_CHANGES.md | Phase 1 Security Changes Log | Listed initial Phase 1 hardening diffs | 2026-10-29 | Historical Phase 1 security changelog superseded by current security boundaries | docs/SECURITY_BOUNDARIES.md | DO NOT USE | UNASSIGNED |
| docs/archive/reports/HACKATHON_ALIGNMENT.md | docs/HACKATHON_ALIGNMENT.md | Hackathon Alignment Report | Mapped initial prototype to hackathon rubric | 2026-10-29 | Historical hackathon rubric mapping superseded by current architecture documentation | docs/ARCHITECTURE.md | DO NOT USE | UNASSIGNED |
| docs/archive/reports/BUNDLE_AUDIT.md | audit/historical/BUNDLE_AUDIT.md | Historical Bundle Audit | Audited early bundle packaging | 2026-10-29 | Historical audit snapshot superseded by current security boundaries | docs/SECURITY_BOUNDARIES.md | DO NOT USE | UNASSIGNED |
| docs/archive/reports/FINAL_ARCHIVE_AUDIT.md | audit/historical/FINAL_ARCHIVE_AUDIT.md | Historical Final Archive Audit | Audited early archive bundle integrity | 2026-10-29 | Historical audit snapshot superseded by current security boundaries | docs/SECURITY_BOUNDARIES.md | DO NOT USE | UNASSIGNED |
| docs/archive/reports/TYPED_GRAPH_VOICE_AUDIT.md | audit/historical/TYPED_GRAPH_VOICE_AUDIT.md | Typed Graph and Voice Audit | Audited early typed graph and voice gateway | 2026-10-29 | Historical voice audit snapshot superseded by current component status doc | docs/FC27_COMPONENT_STATUS.md | DO NOT USE | UNASSIGNED |
| docs/archive/test-results/PHASE2_TEST_TRACEABILITY.md | PHASE2_TEST_TRACEABILITY.md | Phase 2 Test Traceability Matrix | Mapped Phase 2 requirements to tests | 2026-10-29 | Point-in-time Phase 2 traceability snapshot superseded by current testing guide | docs/TESTING.md | DO NOT USE | UNASSIGNED |
| docs/archive/test-results/TEST_RESULTS_2026-10-03_PHASE1.md | TEST_RESULTS.md | Phase 1 Test Results (2026-10-03) | Recorded 2026-10-03 Phase 1 acceptance run | 2026-10-29 | Point-in-time 2026-10-03 test execution report superseded by current TEST_RESULTS.md | TEST_RESULTS.md | DO NOT USE | UNASSIGNED |
| docs/archive/architecture/PHASE2_EVIDENCE_BINDING.md | PHASE2_EVIDENCE_BINDING.md | Phase 2 Cryptographic Evidence Binding Spec | Specified 14-field fc27_decision_evidence design | 2026-10-29 | Consolidated into current approval flow and security boundary documentation | docs/FC27_APPROVAL_FLOW.md | DO NOT USE | UNASSIGNED |
| docs/archive/architecture/PHASE2_RUNBOOKS.md | PHASE2_RUNBOOKS.md | Phase 2 Operational Runbooks | Described Phase 2 bootstrap and rotation procedures | 2026-10-29 | Consolidated into current operations runbook | docs/OPERATIONS.md | DO NOT USE | UNASSIGNED |
| docs/archive/architecture/PHASE2_SECURITY_INVARIANTS.md | PHASE2_SECURITY_INVARIANTS.md | Phase 2 Security Invariants Spec | Specified Phase 2 RBAC and anti-impersonation rules | 2026-10-29 | Consolidated into current security boundaries specification | docs/SECURITY_BOUNDARIES.md | DO NOT USE | UNASSIGNED |
| docs/archive/architecture/PHASE2_TRANSACTION_MODEL.md | PHASE2_TRANSACTION_MODEL.md | Phase 2 Transaction and Lock Hierarchy Model | Specified PostgreSQL lock ordering for decisions | 2026-10-29 | Consolidated into current approval flow and security boundaries documentation | docs/FC27_APPROVAL_FLOW.md | DO NOT USE | UNASSIGNED |
| docs/archive/architecture/PHASE2_WEBAUTHN_POLICY.md | PHASE2_WEBAUTHN_POLICY.md | Phase 2 WebAuthn Step-Up Policy | Specified FIDO2 passkey step-up requirements | 2026-10-29 | Consolidated into current security boundaries and approval flow documentation | docs/SECURITY_BOUNDARIES.md | DO NOT USE | UNASSIGNED |
| docs/archive/plans/2026-10-02-player-dispatch-design.md | docs/superpowers/specs/2026-10-02-player-dispatch-design.md | Player Dispatch and Dashboard Design Spec (v2.1) | Drafted Phase 1/2/3 dispatch design roadmap | 2026-10-29 | Historical multi-phase design spec superseded by current architecture and dispatch activation docs | docs/DISPATCH_ACTIVATION.md | DO NOT USE | UNASSIGNED |
| docs/archive/experiments/WINDHOVER_INTEGRATION_GAP.md | docs/WINDHOVER_INTEGRATION_GAP.md | Windhover Integration Gap Analysis | Documented unimplemented Windhover external consumer gap | 2026-10-31 | Historical 2026-10-01 unimplemented Windhover contract gap note; not part of the supported FC27 community-provider architecture. | docs/FC27_COMPONENT_STATUS.md | DO NOT USE | UNASSIGNED |
| docs/archive/phases/installer.md | docs/notes/installer.md | Windows Host-Agent Installer Note | Documented v2.0.0 Windows capture-agent installer | 2026-10-31 | Historical v2.0.0 Windows host-agent installer phase note; retained in archive for historical context. | docs/DEVELOPMENT.md | DO NOT USE | UNASSIGNED |
| docs/archive/phases/watcher_ocr_rundown.md | docs/notes/watcher_ocr_rundown.md | Watcher, OCR, and Post-Match Rundown Note | Documented v2.0.0 live watcher, RTSP OCR, and rundown subsystem | 2026-10-31 | Historical v2.0.0 watcher/OCR/rundown phase note; non-primary legacy subsystem outside the current FC27 community-provider path. | docs/FC27_COMPONENT_STATUS.md | DO NOT USE | UNASSIGNED |
| docs/archive/providers/mcp_ws.md | docs/notes/mcp_ws.md | MCP Retry and WebSocket Security Note | Documented v2.0.0 MCP EA retry policy and WebSocket auth | 2026-10-31 | Historical v2.0.0 PR implementation note for early MCP proxy and EA retry wiring; superseded by current security and provider documentation. | docs/FC27_PROVIDER_INTEGRATION.md | DO NOT USE | UNASSIGNED |

## Archived Script Registry (`scripts/archive/`)

| Archived Path | Previous Path | Classification | Archive Date | Reason | Replacement | Operational Status |
|---|---|---|---|---|---|---|
| `scripts/archive/experiments/matchpulse_conformance.py` | `scripts/matchpulse_conformance.py` | `SUPERSEDED_BY_CURRENT_PATH` | `2026-10-31` | Superseded placeholder MatchPulse conformance script; active repository isolation is enforced by `scripts/check_matchpulse_boundary.py`. | `scripts/check_matchpulse_boundary.py` | `DO_NOT_USE` |

## Branch-Only Historical Provider Research Records

| Record ID | Branch | Head Commit | Classification | Status | Summary | Documented In `main` | Replacement |
|---|---|---|---|---|---|---|---|
| `BRANCH-RESEARCH-001` | `feature/fc27-real-east-coasters-ingestion` | `76d20c4ef6` | `HISTORICAL_ARCHIVE` | `DO_NOT_USE` | Standards-compliant Python `urllib` HTTPS one-shot live probe against `proclubs.ea.com/api/fc/clubs/info` confirmed deterministic HTTP `403 Forbidden` without WAF/header spoofing. | `docs/FC27_PROVIDER_INTEGRATION.md` | `src/fc27_community_provider.py` |
| `BRANCH-RESEARCH-002` | `feature/fc27-browser-observed-provider` | `414b4f3d67` | `EXPERIMENTAL_NOT_ACTIVATABLE` | `EXPERIMENTAL_NOT_ACTIVATABLE` | Bounded headless Chromium browser-observed provider and official EA FC27 Clubs public page reconciliation proved HTTP/2 `ERR_HTTP2_PROTOCOL_ERROR` in headless Chromium while confirming official public page URL and `common-gen5` platform. | `docs/FC27_PROVIDER_INTEGRATION.md` | `src/fc27_community_provider.py` |
