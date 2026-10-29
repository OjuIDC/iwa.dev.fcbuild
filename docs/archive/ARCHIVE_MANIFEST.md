# Documentation Archive Manifest

Every file stored under `docs/archive/` is listed below with its provenance, replacement path, and operational status (`DO NOT USE`).

## Summary Table

| Archived Path | Previous Path | Original Date | Source Commit | Archive Date | Decision | Superseded By | Operational Status |
|---|---|---|---|---|---|---|---|
| `docs/archive/architecture/PHASE2_EVIDENCE_BINDING.md` | `PHASE2_EVIDENCE_BINDING.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `MERGE_THEN_ARCHIVE` | `docs/FC27_APPROVAL_FLOW.md`, `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/architecture/PHASE2_RUNBOOKS.md` | `PHASE2_RUNBOOKS.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `MERGE_THEN_ARCHIVE` | `docs/OPERATIONS.md`, `MIGRATION_GUIDE.md` | `DO NOT USE` |
| `docs/archive/architecture/PHASE2_SECURITY_INVARIANTS.md` | `PHASE2_SECURITY_INVARIANTS.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `MERGE_THEN_ARCHIVE` | `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/architecture/PHASE2_TRANSACTION_MODEL.md` | `PHASE2_TRANSACTION_MODEL.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `MERGE_THEN_ARCHIVE` | `docs/FC27_APPROVAL_FLOW.md`, `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/architecture/PHASE2_WEBAUTHN_POLICY.md` | `PHASE2_WEBAUTHN_POLICY.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `MERGE_THEN_ARCHIVE` | `docs/SECURITY_BOUNDARIES.md`, `docs/FC27_APPROVAL_FLOW.md` | `DO NOT USE` |
| `docs/archive/plans/2026-10-02-player-dispatch-design.md` | `docs/superpowers/specs/2026-10-02-player-dispatch-design.md` | `2026-10-02` | `cd54a79` | `2026-10-29` | `ARCHIVE` | `docs/ARCHITECTURE.md`, `docs/FC27_DATA_MODEL.md`, `docs/DISPATCH_ACTIVATION.md` | `DO NOT USE` |
| `docs/archive/prompts/ORIGINAL_REQUEST.md` | `ORIGINAL_REQUEST.md` | `2026-10-03` | `170e6a0` | `2026-10-29` | `ARCHIVE` | `docs/ARCHITECTURE.md`, `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/prompts/PHASE2_VICTORY_AUDIT_REQUEST.md` | `PHASE2_VICTORY_AUDIT_REQUEST.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `ARCHIVE` | `docs/TESTING.md`, `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/reports/BUNDLE_AUDIT.md` | `audit/historical/BUNDLE_AUDIT.md` | `2026-10-01` | `170e6a0` | `2026-10-29` | `ARCHIVE` | `docs/ARCHITECTURE.md`, `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/reports/FINAL_ARCHIVE_AUDIT.md` | `audit/historical/FINAL_ARCHIVE_AUDIT.md` | `2026-10-01` | `170e6a0` | `2026-10-29` | `ARCHIVE` | `docs/ARCHITECTURE.md`, `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/reports/HACKATHON_ALIGNMENT.md` | `docs/HACKATHON_ALIGNMENT.md` | `2026-10-01` | `170e6a0` | `2026-10-29` | `ARCHIVE` | `README.md`, `docs/ARCHITECTURE.md` | `DO NOT USE` |
| `docs/archive/reports/IMPLEMENTATION_REPORT.md` | `IMPLEMENTATION_REPORT.md` | `2026-10-01` | `170e6a0` | `2026-10-29` | `ARCHIVE` | `docs/ARCHITECTURE.md`, `docs/FC27_COMPONENT_STATUS.md` | `DO NOT USE` |
| `docs/archive/reports/PHASE2_LOCAL_MERGE_GATE_REPORT.md` | `PHASE2_LOCAL_MERGE_GATE_REPORT.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `ARCHIVE` | `TEST_RESULTS.md`, `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/reports/PHASE2_REMEDIATION_REPORT.md` | `PHASE2_REMEDIATION_REPORT.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `ARCHIVE` | `docs/SECURITY_BOUNDARIES.md`, `docs/FC27_COMPONENT_STATUS.md` | `DO NOT USE` |
| `docs/archive/reports/SECURITY_CHANGES.md` | `SECURITY_CHANGES.md` | `2026-10-03` | `170e6a0` | `2026-10-29` | `MERGE_THEN_ARCHIVE` | `docs/SECURITY_BOUNDARIES.md` | `DO NOT USE` |
| `docs/archive/reports/TYPED_GRAPH_VOICE_AUDIT.md` | `audit/historical/TYPED_GRAPH_VOICE_AUDIT.md` | `2026-10-01` | `170e6a0` | `2026-10-29` | `ARCHIVE` | `docs/ARCHITECTURE.md`, `docs/VOICE_CAPABILITIES.md` | `DO NOT USE` |
| `docs/archive/test-results/PHASE2_TEST_TRACEABILITY.md` | `PHASE2_TEST_TRACEABILITY.md` | `2026-10-05` | `3a132dc` | `2026-10-29` | `ARCHIVE` | `docs/TESTING.md`, `TEST_RESULTS.md` | `DO NOT USE` |
| `docs/archive/test-results/TEST_RESULTS_2026-10-03_PHASE1.md` | `TEST_RESULTS.md` | `2026-10-03` | `170e6a0` | `2026-10-29` | `ARCHIVE` | `TEST_RESULTS.md`, `docs/TESTING.md` | `DO NOT USE` |

---

## Detailed Manifest Entries

### 1. `docs/archive/architecture/PHASE2_EVIDENCE_BINDING.md`
- **Previous Path**: `PHASE2_EVIDENCE_BINDING.md`
- **Title**: Phase 2 Decision Evidence Binding Specification
- **Original Purpose**: Document the 14-field `public.fc27_decision_evidence` schema and RFC-8785 canonical bound payload hash.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Merged into consolidated architecture/security references (`docs/FC27_APPROVAL_FLOW.md` and `docs/SECURITY_BOUNDARIES.md`).
- **Superseded By**: `docs/FC27_APPROVAL_FLOW.md`, `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: Historical Phase 2 R2 specification context; active specification now lives in `docs/FC27_APPROVAL_FLOW.md`.
- **Security Sensitivity**: None (public schema/hash design; no secrets).
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: None.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 2. `docs/archive/architecture/PHASE2_RUNBOOKS.md`
- **Previous Path**: `PHASE2_RUNBOOKS.md`
- **Title**: Phase 2 Operational Runbooks: Bootstrap, Key Rotation, and Least-Privilege DDL
- **Original Purpose**: Document Commissioner bootstrap ceremony, MultiFernet key rotation, and migration execution during Phase 2.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Merged into `docs/OPERATIONS.md`; contained stale references to migrations `0001`–`0009` and grants policy version `9` (current is `0001`–`0012` and grants policy version `14`).
- **Superseded By**: `docs/OPERATIONS.md`, `MIGRATION_GUIDE.md`
- **Remaining Relevance**: Consolidated into `docs/OPERATIONS.md`.
- **Security Sensitivity**: None (placeholder tokens only).
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`; command execution warning added.
- **Potentially Stale Links**: References to grants version 9 and migration 0009 as latest.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 3. `docs/archive/architecture/PHASE2_SECURITY_INVARIANTS.md`
- **Previous Path**: `PHASE2_SECURITY_INVARIANTS.md`
- **Title**: Phase 2 Security Invariants Specification
- **Original Purpose**: Specify Phase 2 dormancy, anti-impersonation, WebAuthn UV, session, and transaction invariants.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Merged into `docs/SECURITY_BOUNDARIES.md`.
- **Superseded By**: `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: Consolidated into `docs/SECURITY_BOUNDARIES.md`.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: Line number citations to earlier `policy.py` revisions.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 4. `docs/archive/architecture/PHASE2_TRANSACTION_MODEL.md`
- **Previous Path**: `PHASE2_TRANSACTION_MODEL.md`
- **Title**: Phase 2 Transactional Architecture & Concurrency Model
- **Original Purpose**: Specify deterministic lock hierarchy (`fc27_decision_evidence` -> `approval_requests` -> `graph_threads`) and single-transaction commit in `apply_webauthn_decision`.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Merged into `docs/FC27_APPROVAL_FLOW.md` and `docs/SECURITY_BOUNDARIES.md`.
- **Superseded By**: `docs/FC27_APPROVAL_FLOW.md`, `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: Consolidated into `docs/FC27_APPROVAL_FLOW.md`.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: None.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 5. `docs/archive/architecture/PHASE2_WEBAUTHN_POLICY.md`
- **Previous Path**: `PHASE2_WEBAUTHN_POLICY.md`
- **Title**: Phase 2 WebAuthn & User Verification Policy Specification
- **Original Purpose**: Document WebAuthn `UserVerificationRequirement.REQUIRED`, ceremony separation, and sign-counter clone detection.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Merged into `docs/SECURITY_BOUNDARIES.md` and `docs/FC27_APPROVAL_FLOW.md`.
- **Superseded By**: `docs/SECURITY_BOUNDARIES.md`, `docs/FC27_APPROVAL_FLOW.md`
- **Remaining Relevance**: Consolidated into `docs/SECURITY_BOUNDARIES.md`.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: None.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 6. `docs/archive/plans/2026-10-02-player-dispatch-design.md`
- **Previous Path**: `docs/superpowers/specs/2026-10-02-player-dispatch-design.md`
- **Title**: Player observer agents and approved-recommendation dispatch: architecture specification
- **Original Purpose**: Early draft (2026-10-02) design specification for player dispatch and migration `0003`.
- **Original Date**: `2026-10-02`
- **Source Commit**: `cd54a79`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Draft specification superseded by implemented migrations `0003`–`0012`, `src/dispatch/`, `src/identity/`, and current ADRs; contained superseded statements such as `graph-api keeps the owner role fc27`.
- **Superseded By**: `docs/ARCHITECTURE.md`, `docs/FC27_DATA_MODEL.md`, `docs/DISPATCH_ACTIVATION.md`
- **Remaining Relevance**: Historical design rationale only.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`; removed empty `docs/superpowers/specs/` tracked directory.
- **Potentially Stale Links**: References to `0003_player_dispatch.sql` (implemented as `0003_dispatch_foundation.sql`).
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 7. `docs/archive/prompts/ORIGINAL_REQUEST.md`
- **Previous Path**: `ORIGINAL_REQUEST.md`
- **Title**: Original User Request (Phase 1 & Phase 2 Teamwork Prompts)
- **Original Purpose**: Captured raw multi-agent execution prompts for Phase 1 and Phase 2.
- **Original Date**: `2026-10-03`
- **Source Commit**: `170e6a0`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Historical prompt transcript stored at repository root; not an operational or architectural reference.
- **Superseded By**: `docs/ARCHITECTURE.md`, `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: Historical audit provenance for Phase 1/2 requirements.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: References to temporary agent worktrees and phase-boundary checklists.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 8. `docs/archive/prompts/PHASE2_VICTORY_AUDIT_REQUEST.md`
- **Previous Path**: `PHASE2_VICTORY_AUDIT_REQUEST.md`
- **Title**: Phase 2 Independent Victory Audit Request
- **Original Purpose**: Auditor instructions and 20-point rubric for branch `fix/phase2-identity-victory-remediation`.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Historical audit request prompt for an earlier branch.
- **Superseded By**: `docs/TESTING.md`, `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: Historical Phase 2 audit rubric.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: References to `PHASE2_LOCAL_MERGE_GATE_REPORT.md` at root.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 9. `docs/archive/reports/BUNDLE_AUDIT.md`
- **Previous Path**: `audit/historical/BUNDLE_AUDIT.md`
- **Title**: Bundle audit (pre-2.0.0)
- **Original Purpose**: Audit notes on the pre-2.0.0 local-model and hierarchy archive bundle.
- **Original Date**: `2026-10-01`
- **Source Commit**: `170e6a0`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Already marked historical; consolidated from `audit/historical/` into `docs/archive/reports/`.
- **Superseded By**: `docs/ARCHITECTURE.md`, `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: None operationally.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: References to `IMPLEMENTATION_REPORT.md` and `SECURITY_CHANGES.md` at root.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 10. `docs/archive/reports/FINAL_ARCHIVE_AUDIT.md`
- **Previous Path**: `audit/historical/FINAL_ARCHIVE_AUDIT.md`
- **Title**: Final archive audit (pre-2.0.0)
- **Original Purpose**: Pre-2.0.0 archive hardening notes.
- **Original Date**: `2026-10-01`
- **Source Commit**: `170e6a0`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Already marked historical; consolidated from `audit/historical/` into `docs/archive/reports/`.
- **Superseded By**: `docs/ARCHITECTURE.md`, `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: None operationally.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: References to `IMPLEMENTATION_REPORT.md` and `SECURITY_CHANGES.md` at root.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 11. `docs/archive/reports/HACKATHON_ALIGNMENT.md`
- **Previous Path**: `docs/HACKATHON_ALIGNMENT.md`
- **Title**: Hackathon alignment review
- **Original Purpose**: Early hackathon submission pitch notes.
- **Original Date**: `2026-10-01`
- **Source Commit**: `170e6a0`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Historical hackathon memo superseded by East Coasters FC 27 platform architecture and current README.
- **Superseded By**: `README.md`, `docs/ARCHITECTURE.md`
- **Remaining Relevance**: None operationally.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: Reference to `SECURITY_CHANGES.md`.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 12. `docs/archive/reports/IMPLEMENTATION_REPORT.md`
- **Previous Path**: `IMPLEMENTATION_REPORT.md`
- **Title**: Implementation report: fc27-full-integrated-typed-voice 2.0.0
- **Original Purpose**: Milestone report for v2.0.0 (migrations `0001`–`0002`, 254 tests, pre-Phase-1/2/FC27 storage).
- **Original Date**: `2026-10-01`
- **Source Commit**: `170e6a0`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Completed historical milestone report; superseded by migrations `0003`–`0012`, Phase 2 identity, and FC 27 safe-adapter architecture.
- **Superseded By**: `docs/ARCHITECTURE.md`, `docs/FC27_COMPONENT_STATUS.md`
- **Remaining Relevance**: Historical record of v2.0.0 baseline.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: Legacy route references (`/run`, `/threads/{id}`) and v2.0.0 migration counts.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 13. `docs/archive/reports/PHASE2_LOCAL_MERGE_GATE_REPORT.md`
- **Previous Path**: `PHASE2_LOCAL_MERGE_GATE_REPORT.md`
- **Title**: Phase 2 Local Merge-Gate Report (PROVISIONAL)
- **Original Purpose**: Provisional pre-audit status snapshot on `fix/phase2-identity-victory-remediation`.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Historical provisional report whose open items were subsequently remediated and merged.
- **Superseded By**: `TEST_RESULTS.md`, `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: Historical Phase 2 remediation audit trail.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: References to `PHASE2_*.md` at root and `phase2-host-validation.ps1`.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 14. `docs/archive/reports/PHASE2_REMEDIATION_REPORT.md`
- **Previous Path**: `PHASE2_REMEDIATION_REPORT.md`
- **Title**: Phase 2 Remediation Implementation Report: Identity & Authorization Foundation
- **Original Purpose**: Report mapping Phase 2 findings R1–R15 to source files, migrations, and tests.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Completed Phase 2 milestone report; active security guarantees are documented in `docs/SECURITY_BOUNDARIES.md`.
- **Superseded By**: `docs/SECURITY_BOUNDARIES.md`, `docs/FC27_COMPONENT_STATUS.md`
- **Remaining Relevance**: Historical traceability of R1–R15 fixes.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: References to `PHASE2_LOCAL_MERGE_GATE_REPORT.md` and grants v9.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 15. `docs/archive/reports/SECURITY_CHANGES.md`
- **Previous Path**: `SECURITY_CHANGES.md`
- **Title**: Security Changes: Phase 1 Secure Data Foundation
- **Original Purpose**: Phase 1 security architecture matrix and Phase 2 handoff roadmap.
- **Original Date**: `2026-10-03`
- **Source Commit**: `170e6a0`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Phase 1 milestone report whose security controls are merged into `docs/SECURITY_BOUNDARIES.md` and whose Phase 2 handoff items and grants version (`5`) were superseded by subsequent migrations (`0004`–`0012`, grants version `14`).
- **Superseded By**: `docs/SECURITY_BOUNDARIES.md`
- **Remaining Relevance**: Consolidated into `docs/SECURITY_BOUNDARIES.md`.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`; links in `SECURITY.md` and `docs/DEPLOYMENT_AND_SECURITY.md` updated to `docs/SECURITY_BOUNDARIES.md`.
- **Potentially Stale Links**: Grants policy version 5 reference.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 16. `docs/archive/reports/TYPED_GRAPH_VOICE_AUDIT.md`
- **Previous Path**: `audit/historical/TYPED_GRAPH_VOICE_AUDIT.md`
- **Title**: Typed Graph and voice audit (pre-2.0.0)
- **Original Purpose**: Audit summary of early typed graph and voice changes.
- **Original Date**: `2026-10-01`
- **Source Commit**: `170e6a0`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Already marked historical; consolidated from `audit/historical/` into `docs/archive/reports/`.
- **Superseded By**: `docs/ARCHITECTURE.md`, `docs/VOICE_CAPABILITIES.md`
- **Remaining Relevance**: None operationally.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: References to `IMPLEMENTATION_REPORT.md` and `SECURITY_CHANGES.md` at root.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 17. `docs/archive/test-results/PHASE2_TEST_TRACEABILITY.md`
- **Previous Path**: `PHASE2_TEST_TRACEABILITY.md`
- **Title**: Phase 2 Test Traceability Matrix: Findings R1–R15 to Test Suites
- **Original Purpose**: Traceability table mapping R1–R15 to `tests/identity/` suites.
- **Original Date**: `2026-10-05`
- **Source Commit**: `3a132dc`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Historical Phase 2 test snapshot superseded by `docs/TESTING.md` and current `TEST_RESULTS.md`.
- **Superseded By**: `docs/TESTING.md`, `TEST_RESULTS.md`
- **Remaining Relevance**: Historical R1–R15 test mapping.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Moved via `git mv`.
- **Potentially Stale Links**: None.
- **Reviewer**: FC 27 Documentation Reconciliation Phase

### 18. `docs/archive/test-results/TEST_RESULTS_2026-10-03_PHASE1.md`
- **Previous Path**: `TEST_RESULTS.md`
- **Title**: Test Results & Phase 1 Acceptance Trace (2026-10-03)
- **Original Purpose**: Dated Phase 1 acceptance trace (AC-1 through AC-58) recorded on 2026-10-03.
- **Original Date**: `2026-10-03`
- **Source Commit**: `170e6a0`
- **Archive Date**: `2026-10-29`
- **Reason Archived**: Preserved as historical Phase 1 acceptance evidence while root `TEST_RESULTS.md` was updated to reflect the current 2026-10-29 commit-bound repository state.
- **Superseded By**: `TEST_RESULTS.md`, `docs/TESTING.md`
- **Remaining Relevance**: Historical Phase 1 AC-1..AC-58 trace.
- **Security Sensitivity**: None.
- **Operational Status**: `DO NOT USE`
- **Migration Notes**: Archived snapshot preserved with standard non-authoritative notice.
- **Potentially Stale Links**: References to `orchestrator_phase1/PROJECT.md`.
- **Reviewer**: FC 27 Documentation Reconciliation Phase
