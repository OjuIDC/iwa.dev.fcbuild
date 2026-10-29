> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide for the current repository state.
> Status: Non-authoritative historical record.
> Operational status: DO NOT USE
> Original path: PHASE2_VICTORY_AUDIT_REQUEST.md
> Archived on: 2026-10-29
> Superseded by: docs/ARCHITECTURE.md
> Reason: Point-in-time Phase 2 audit request prompt superseded by current architecture docs
> Warning: Do not execute commands in this archived document.

# Phase 2 Independent Victory Audit Request

**Status**: REQUEST ONLY. No verdict is claimed. Every rubric row below is `UNVERIFIED` until an independent auditor fills it from commands they ran themselves.

## 1. Audit target

- Branch: `fix/phase2-identity-victory-remediation`
- Base: `main` @ `056d73b`
- Pre-remediation HEAD: `170e6a05966750e8ee53cdb9967e8605c31f6578`
- Remediation commits (in order, all on top of `170e6a0`):
  1. `7f230bc` chore(repo): untrack transient agent runtime state and ignore it
  2. `200d34a` fix(identity): enforce strict WebAuthn proof and request boundaries
  3. `fe56921` fix(approval): make evidence consumption and decision commit atomic
  4. `0bec9c0` fix(identity): harden sessions bootstrap player proof and TOTP replay
  5. `d3fde93` chore(db): move identity schema to migrations and restrict runtime roles
  6. `a6d951e` test(identity): add Phase 2 remediation regression and concurrency coverage
  7. docs commit(s) following `a6d951e` (see detached audit manifest)
- **Exact audit-target SHA**: this file cannot contain the SHA of the commit that contains it. The authoritative target is the branch tip recorded in the detached audit manifest produced after the final commit (not committed). The auditor MUST run `git rev-parse HEAD` and confirm it equals the manifest SHA before starting.

## 2. Rules for the auditor

1. Audit committed content only (`git show <sha>:<path>` or a clean checkout of the target). Uncommitted or untracked files are out of scope.
2. Require `git status --short` to be empty before auditing.
3. Re-run every critical command yourself; do not accept any result quoted in this repository (including `PHASE2_REMEDIATION_REPORT.md`, `PHASE2_TEST_TRACEABILITY.md`, and `PHASE2_LOCAL_MERGE_GATE_REPORT.md`).
4. Any later change to source, migrations, tests, configuration, or documentation invalidates this audit; request a new one against the new HEAD.
5. Do not enable dispatch or start any dispatch worker. Do not merge.

## 3. Validation commands to re-run (real PostgreSQL, Python 3.11)

```
python -m compileall -q src tests host_agent voice
python -m pytest tests/identity
python -m pytest tests -k "webauthn or evidence or decision or concurrency or replay"
python -m pytest tests -k "totp or recovery or bootstrap"
python -m pytest tests -k "migration or grants or db_role or runtime_ddl"
python -m pytest tests -k "dispatch_disabled or dormancy or zero_delivery"
python -m pytest
docker compose config
python -m pytest tests/test_compose_isolation.py
```
plus clean-database migration, upgrade-from-Phase-1 migration, and the dormancy SQL in `PHASE2_LOCAL_MERGE_GATE_REPORT.md` section 12. Record command, collected/passed/failed/skipped/xfailed/warnings, and exit code for each. Skips caused by a missing database are failures for this audit.

## 4. Local validation results

Host-side (Docker/PostgreSQL) validation: **PENDING - no results exist yet.** Only static review was performed; see `PHASE2_LOCAL_MERGE_GATE_REPORT.md` for the open findings, which include Critical/High items that must be resolved and re-audited first.

## 5. 20-point rubric (auditor fills Status)

| # | Item | Success criterion | Status |
|---|---|---|---|
| 1 | Commissioner step-up requires WebAuthn user verification | `REQUIRED` in options, `require_user_verification=True`, `user_verified` enforced on every privileged path (decision, step-up, player, bootstrap) | UNVERIFIED |
| 2 | Decision evidence bound to user, session, credential, challenge, decision, request, thread, package, version, hash | All bindings mandatory (no optional `expected_*`), hash recomputed server-side | UNVERIFIED |
| 3 | Evidence consumption and decision commit in one transaction | Single connection/transaction incl. counter, challenge, audit, events, outbox | UNVERIFIED |
| 4 | Exactly one concurrent decision wins | Real PostgreSQL race tests; losers leave evidence unconsumed | UNVERIFIED |
| 5 | Legacy/service principals cannot make WebAuthn decisions or choose club | Legacy token pinned to configured club | UNVERIFIED |
| 6 | Clients cannot request elevated sessions | Rotation cannot raise auth level | UNVERIFIED |
| 7 | Bootstrap creates no unproven passkey authority | UV assertion required at completion | UNVERIFIED |
| 8 | Every player mutation requires persisted, bound proof | No self-issued proof; proof bound to decision/assignment/session | UNVERIFIED |
| 9 | TOTP replay prevention atomic | `FOR UPDATE` + monotonic step | UNVERIFIED |
| 10 | No runtime DDL; schema migration-owned | Zero CREATE/ALTER in request paths; `fc27_app` lacks CREATE on public | UNVERIFIED |
| 11 | Human invitations cannot assign machine roles | Enforced in app and database | UNVERIFIED |
| 12 | Security request models strict | `extra="forbid"`, discriminated union, no raw-body detection | UNVERIFIED |
| 13 | Migrations ordered, unique, immutable, checksummed; grants policy version bumped | Clean + upgrade run succeed | UNVERIFIED |
| 14 | SECURITY DEFINER hardening | Fixed search_path, non-login owner, no PUBLIC EXECUTE | UNVERIFIED |
| 15 | Parameterized SQL, no swallowed security exceptions | No `except Exception: pass` on security mutations | UNVERIFIED |
| 16 | Clean, reviewable, linear history preserving earlier commits | No rewrite since `170e6a0` | UNVERIFIED |
| 17 | Concurrency tests are genuine | Separate connections, real PostgreSQL, no mocks | UNVERIFIED |
| 18 | Documentation matches code | No stale paths, test names, counts, or pre-filled PASS | UNVERIFIED |
| 19 | Dormancy verified from PostgreSQL, processes, containers | Zero dispatch/outbox-published/delivery/ack/tracking/completed rows; no worker | UNVERIFIED |
| 20 | No Phase 3 / dashboard / delivery code; no secrets committed | Scope and secret scans clean | UNVERIFIED |
