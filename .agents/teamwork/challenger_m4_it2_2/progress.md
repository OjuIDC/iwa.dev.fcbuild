# Progress — challenger_m4_it2_2

- Last visited: 2026-10-03T21:05:45Z
- Status: Adversarial Analysis Complete
- Active Step: Authoring challenge_report.md and handoff.md
- Analysis Summary:
  - Nonce & Gate Protocol: Verified replayed nonces fail with 409, G-OUTBOX challenge/record fails with 409 (C-3), and correlation ID is properly isolated from GATE_SIGNED_FIELDS (C-4, C-5).
  - Atomic Rollback & Partition Locks: Verified candidates lacking objective or unverified player IDs fail with 409 not_dispatchable rolling back the entire transaction. Verified advisory lock `pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))` guarantees serialization and single active dispatch. Verified 10 parallel duplicate replays return identical winner dispatch_id with status 200.
  - Phase 1 Invariants: Verified DISPATCH_ENABLED=false invariant in /health/ready and gating logic.
  - Verdict: APPROVE.
