> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide. See `TEST_RESULTS.md and docs/SECURITY_BOUNDARIES.md` for the current implementation.
> Archived on `2026-10-29` from `PHASE2_LOCAL_MERGE_GATE_REPORT.md`.
>
> Warning: Do not execute commands in this archived document.

# Phase 2 Local Merge-Gate Report (PROVISIONAL)

Generated 2026-10-05. This report is evidence preparation, not a victory claim. Nothing below says a command passed unless it is listed with an exit code in section 8-12 as actually run.

## 1-4. Repository identity
- Branch: `fix/phase2-identity-victory-remediation`
- Base SHA: `056d73b` (main)
- Previous HEAD: `170e6a05966750e8ee53cdb9967e8605c31f6578`
- New committed HEAD: the commit that contains this file cannot name itself. Remediation tip before docs: `a6d951e`; final tip is recorded in the detached audit manifest (not committed). Verify with `git rev-parse HEAD`.

## 5. Ordered remediation commits
1. `7f230bc` chore(repo): untrack transient agent runtime state and ignore it
2. `200d34a` fix(identity): enforce strict WebAuthn proof and request boundaries
3. `fe56921` fix(approval): make evidence consumption and decision commit atomic
4. `0bec9c0` fix(identity): harden sessions bootstrap player proof and TOTP replay
5. `d3fde93` chore(db): move identity schema to migrations and restrict runtime roles
6. `a6d951e` test(identity): add Phase 2 remediation regression and concurrency coverage
7. docs(identity) commit(s) after `a6d951e`

Commits 2-5 were split by file; because `store.py`, `graph_api.py` etc. contain interleaved changes, individual intermediate commits are not guaranteed to pass tests on their own. Only the tip is a validation target. Commit 1 untracks `.agents/teamwork/` (already tracked in `170e6a0`) without rewriting history.

## 6. File classification (working tree at start: 48 tracked changes, 20 untracked non-agent files, 579 untracked + tracked agent files)
- A production implementation: src/approval_store.py, src/graph_api.py, src/dispatch/gates.py, src/schemas.py, src/db_migrate.py, src/identity/{crypto,deps,policy,services,store,tokens,totp,webauthn}.py, src/identity/routers/{delegations,invitations,passkeys,player,recovery,sessions,step_up,totp,users}.py
- B migration: migrations/0007_decision_evidence.sql, 0008_r2_remediation.sql, 0009_r3_evidence_binding.sql, migrations/post/grants.sql
- C test: tests/conftest.py, tests/test_db_migrate_roles.py, tests/test_graph_api_v1.py, 13 changed/added tests/identity files, 6 untracked tests/adversarial files, and 14 further untracked tests/identity files (full list in commit `a6d951e`)
- D documentation: the 8 PHASE2_*.md files plus this report (stale claims corrected; see section 14)
- E transient agent state (excluded, untracked from index, now gitignored): `.agents/teamwork/**` (~600 files)
- F excluded: `ORIGINAL_REQUEST.md` working-tree change (+250 lines of pasted session prompt material appended at 2026-10-04T16:40:34Z; copied prompt content, not a maintained requirement doc; no secrets found). It is a tracked file with an uncommitted modification and was deliberately NOT committed or reverted (no checkout/reset allowed). Local backup of all diffs: device scratch `gate1/` (backup.tgz sha256 a735bb58...).
- G manual review: none outstanding.

## 7. Blocker-to-code traceability (static; claims unverified by execution)
See `PHASE2_TEST_TRACEABILITY.md` (corrected). Static test mapping found tests present for: strict proof union, UV on step-up (real-library test only at tests/adversarial/test_r6_empirical_security_boundary_challenges.py), evidence binding (mostly mock-connection tests; real-DB lifecycle in test_step_up.py), atomic commit and single winner (real PostgreSQL threaded tests in tests/adversarial/test_r3_r4_empirical_challenger.py, tests/identity/test_r4_concurrency_race.py), bootstrap, TOTP atomic replay, player proofs (mocked store), runtime DDL, invitation roles, dormancy (weak).

## 8. Migration results
NOT RUN (no Python 3.11/PostgreSQL/Docker in the device shell). Static review only: 0001-0009 unique/contiguous; 0001-0006 unchanged vs HEAD; grants policy bumped 8 -> 9 and matches `db_migrate.py`; runner enforces per-file sha256. Pending host run via `phase2-host-validation.ps1`.

## 9-11. Focused tests, full suite, skips/xfails
NOT RUN. Static scan: no `xfail`/`skipif` markers in scope; but every Postgres-dependent test is skipped if `TEST_POSTGRES_URI` is unset (tests/conftest.py:53) and conftest has a silent fallback URI (conftest.py:30-35). Five skips in tests/adversarial/test_adversarial_phase2.py on connection errors. Any such skip is blocking.

## 12. Dormancy
Static: `DISPATCH_ENABLED` default false (graph_api.py:109,138; compose pins "false"); `DISPATCH_WORKER_ENABLED` default false in code only (identity/services.py:116), absent from compose and config.example.env; no dispatch-worker/outbox-relay service in any compose file; no Phase 3/dashboard/delivery code in the diff. Database/process/container evidence: PENDING host run. SQL executed by the host script:
```sql
SELECT 'dispatches_total', count(*) FROM public.dispatches
UNION ALL SELECT 'dispatches_delivered_ack_tracking_completed', count(*) FROM public.dispatches WHERE dispatch_status IN ('queued','running','delivered','acknowledged','tracking','completed')
UNION ALL SELECT 'outbox_published', count(*) FROM public.outbox WHERE published_at IS NOT NULL
UNION ALL SELECT 'dispatch_domain_events', count(*) FROM public.domain_events WHERE event_type LIKE 'dispatch.%'
UNION ALL SELECT 'delivery_attempts', count(*) FROM public.dispatch_delivery_attempts
UNION ALL SELECT 'acknowledgments', count(*) FROM public.dispatch_acknowledgments
UNION ALL SELECT 'player_observations', count(*) FROM public.player_observations
UNION ALL SELECT 'service_heartbeats', count(*) FROM public.service_heartbeats;
```
(No agent-execution/personalization/tracker table exists in the migrations.)

## 13. Working-tree cleanliness
See final `git status --short` in the delivery message. Known residual: ` M ORIGINAL_REQUEST.md` (deliberately uncommitted), so the tree is NOT clean and the verdict is blocked on that decision.

## 14. Remaining discrepancies (from parallel static audits; none fixed in this pass because they cannot be regression-tested without the host DB)
Critical
- C1 `src/identity/recovery.py:27,74-80` hard-coded default bootstrap token `bootstrap_secret_token_123` (unauthenticated bootstrap on unconfigured deployment). [confirmed by grep]
High
- H1 bootstrap completion mints `step_up_passkey` session without a UV assertion; registration uses `require_user_verification=False` (`store.py:1701-1795`, `routers/recovery.py:204-230`, `webauthn.py:192`).
- H2 club not checked against thread club in decision/challenge paths (`graph_api.py:1010-1070`, `approval_store.py:656-800`, `routers/step_up.py:199-214,329-346`).
- H3 player decision evidence consumed with optional/unbound `expected_*` (`routers/player.py:520-540`, `store.py:2594-2623`).
- H4 player proof consumption and decision recording in separate transactions (`routers/player.py:470-585`).
- H5 `idempotency_key` lookup unscoped, can leak another player's decision (`store.py:2898-2905`).
- H6 `/action-tokens` has no `authorize()` (`routers/step_up.py:440-478`).
- H7 player self-issues `player_decision` token as sufficient proof (`routers/player.py:388-515`).
- H8 step-up-required actions depend on a session level that only the unproven bootstrap path can reach; `graph_api.py:1052-1062` hard-codes STEP_UP_PASSKEY.
Medium/Low: swallowed exceptions and test-only branches in `step_up.py` (hasattr/`thread_default`/`uuid4()` fallbacks), `delegations.py:117-127`, `totp.py:276-292`; `MagicMock` type check in `tokens.py:224-233`; legacy token club from header (`deps.py:130-152`); flat player proof model with raw dict (`routers/player.py:112-135`); WebAuthn registration/login UV `preferred`; 0007 edited after staging and duplicated by 0009 (reset any DB that applied earlier 0007); grants.sql executed before policy-row check (`db_migrate.py:176-182`); machine-role invitation guard app-layer only; trigger functions lack pinned search_path; mock-based "atomicity" tests; vacuous dormancy tests; `test_r5_negative_matrix.py:528,533` duplicate test; DISPATCH_WORKER_ENABLED absent from compose/config.example.env.
Docs: stale items corrected in commit docs(identity) (status claims, nonexistent `test_r4_action_tokens.py`, class names, runbook routes/`--apply`, event name, startup-error wording). Still unverified: per-file pass counts and test counts in PHASE2_REMEDIATION_REPORT.md, "14-field" and "23 models" counts.

## 15. Independent audit status
PENDING: FINAL COMMITTED HEAD HAS NOT YET RECEIVED THE REQUIRED INDEPENDENT VICTORY AUDIT
