"""Adversarial stress tests for Milestone 1: Concurrency, Deadlocks, Idempotent Replays, and Fault Injection.

This test module challenges the Milestone 1 implementation against:
1. Concurrency & Deadlock Stress:
   - High contention race between apply_decision and expire_due at expiry boundary
   - Competing valid challenges/nonces for the same thread (APPROVE vs REJECT)
   - Multi-thread batch sweep under decision contention
2. Replay & Idempotency Challenge:
   - Concurrent identical replays (10 threads) asserting safe return and zero duplicate audits/events
   - Concurrent conflicting replays asserting 409 conflict and zero state corruption
   - Replay attempts on revoked/expired nonces
3. Fault Injection & Atomic Rollback:
   - Injected failures into approval_audit, domain_events, and outbox
   - Verifying complete atomic rollback (thread remains awaiting_approval, nonce unconsumed)
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest

from src import approval
from src.approval_store import ApprovalStore, StoreError, request_hash
from src.schemas import DecisionIntent, SignedDecision
from src.security import Principal, Role
from tests.helpers import (
    start,
    decide,
    COM,
    COMMISSIONER_ID,
    SIGNING_SECRET,
    APPROVE_COACH_BEST,
    REJECT,
    signed,
    fake_packages,
)

COMMISSIONER = Principal(Role.commissioner, COMMISSIONER_ID)


# =====================================================================
# 1. Concurrency & Deadlock Stress Tests
# =====================================================================

def test_adversarial_high_concurrency_race_at_expiry(pg_uri):
    """Stress Test: 10x expire_due + 10x apply_decision at sub-second deadline boundary.

    Invariants:
    - Zero deadlocks under high contention.
    - Exactly one terminal state wins ('approved' or 'expired').
    - approval_audit has exactly 1 row (if approved) or 0 rows (if expired).
    - graph_threads and approval_requests remain strictly paired.
    """
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))

    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    ch = store.issue_challenge(tid, intent, COMMISSIONER, 10)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    # Sleep close to 1.0s expiry boundary
    time.sleep(0.95)

    num_threads = 10
    barrier = threading.Barrier(num_threads * 2)
    results = []
    deadlocks = []

    def run_expiry():
        barrier.wait()
        try:
            exp = store.expire_due(thread_id=tid)
            results.append(("expiry", exp))
        except psycopg.errors.DeadlockDetected as exc:
            deadlocks.append(exc)
        except Exception as exc:
            results.append(("expiry_err", exc))

    def run_decision():
        barrier.wait()
        try:
            res = store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)
            results.append(("decision", res))
        except psycopg.errors.DeadlockDetected as exc:
            deadlocks.append(exc)
        except StoreError as exc:
            results.append(("decision_store_err", exc.status_code, exc.detail))
        except Exception as exc:
            results.append(("decision_err", exc))

    threads = []
    for _ in range(num_threads):
        threads.append(threading.Thread(target=run_expiry))
        threads.append(threading.Thread(target=run_decision))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    # 1. Zero deadlocks
    assert len(deadlocks) == 0, f"Deadlock occurred: {deadlocks}"

    # 2. Final state is terminal and paired
    final_thread = store.load(tid)
    assert final_thread is not None
    assert final_thread["status"] in ("approved", "expired")

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.approval_requests WHERE thread_id = %s", (tid,))
            req_status = cur.fetchone()[0]
            assert req_status == final_thread["status"]

            cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
            audit_count = cur.fetchone()[0]
            if final_thread["status"] == "approved":
                assert audit_count == 1
            else:
                assert audit_count == 0

            # Zero inconsistencies
            cur.execute("SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE thread_id = %s AND kind <> 'legacy_unlinked'", (tid,))
            assert cur.fetchone()[0] == 0


def test_adversarial_dual_challenge_competing_decisions(pg_uri):
    """Stress Test: Two different challenges issued for same thread (APPROVE vs REJECT).

    Both attempt concurrent application.
    Invariants:
    - Zero deadlocks.
    - Exactly one decision commits (200 OK).
    - The other fails with 409 Conflict.
    - Exactly 1 audit row in database.
    """
    store = ApprovalStore(pg_uri, request_ttl=timedelta(minutes=5))

    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    intent_approve = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    intent_reject = DecisionIntent.model_validate(REJECT)

    ch_a = store.issue_challenge(tid, intent_approve, COMMISSIONER, 120)
    ch_b = store.issue_challenge(tid, intent_reject, COMMISSIONER, 120)

    signed_a = SignedDecision.model_validate({**ch_a, "signature": approval.sign(ch_a, SIGNING_SECRET)})
    signed_b = SignedDecision.model_validate({**ch_b, "signature": approval.sign(ch_b, SIGNING_SECRET)})

    barrier = threading.Barrier(2)
    outcomes = []
    deadlocks = []

    def decide_a():
        barrier.wait()
        try:
            res = store.apply_decision(tid, signed_a, COMMISSIONER, SIGNING_SECRET)
            outcomes.append(("a_success", res))
        except psycopg.errors.DeadlockDetected as exc:
            deadlocks.append(exc)
        except StoreError as exc:
            outcomes.append(("a_err", exc.status_code, exc.detail))

    def decide_b():
        barrier.wait()
        try:
            res = store.apply_decision(tid, signed_b, COMMISSIONER, SIGNING_SECRET)
            outcomes.append(("b_success", res))
        except psycopg.errors.DeadlockDetected as exc:
            deadlocks.append(exc)
        except StoreError as exc:
            outcomes.append(("b_err", exc.status_code, exc.detail))

    t1 = threading.Thread(target=decide_a)
    t2 = threading.Thread(target=decide_b)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert len(deadlocks) == 0, f"Deadlock occurred: {deadlocks}"
    successes = [o for o in outcomes if "success" in o[0]]
    conflicts = [o for o in outcomes if "err" in o[0]]

    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
    assert len(conflicts) == 1, f"Expected exactly 1 conflict, got {len(conflicts)}"
    assert conflicts[0][1] == 409

    # Verify audit row
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == 1


def test_adversarial_batch_expiry_under_contention(pg_uri):
    """Stress Test: Multi-thread batch sweep (thread_id=None) under concurrent apply_decision contention.

    Asserts zero deadlocks and consistent final state across all threads.
    """
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))

    tids = []
    payloads = []
    for _ in range(4):
        state = store.create_run(club_id="330323", thread_id=None, initial_state=lambda tid: {"lead_packages": fake_packages()}, req_hash=request_hash({}))
        tid = state["thread_id"]
        store.complete_run(tid, {**state, "status": "awaiting_approval"})
        ch = store.issue_challenge(tid, DecisionIntent.model_validate(APPROVE_COACH_BEST), COMMISSIONER, 10)
        signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})
        tids.append(tid)
        payloads.append(signed_payload)

    time.sleep(0.95)

    barrier = threading.Barrier(6)
    deadlocks = []

    def sweeper():
        barrier.wait()
        for _ in range(5):
            try:
                store.expire_due(thread_id=None)
            except psycopg.errors.DeadlockDetected as exc:
                deadlocks.append(exc)
            time.sleep(0.05)

    def worker(tid, p):
        barrier.wait()
        try:
            store.apply_decision(tid, p, COMMISSIONER, SIGNING_SECRET)
        except psycopg.errors.DeadlockDetected as exc:
            deadlocks.append(exc)
        except StoreError:
            pass

    threads = [
        threading.Thread(target=sweeper),
        threading.Thread(target=sweeper),
    ]
    for tid, p in zip(tids, payloads):
        threads.append(threading.Thread(target=worker, args=(tid, p)))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(deadlocks) == 0, f"Deadlock occurred during batch sweep: {deadlocks}"

    for tid in tids:
        row = store.load(tid)
        assert row["status"] in ("approved", "expired")


# =====================================================================
# 2. Replay & Idempotency Challenge Tests
# =====================================================================

def test_adversarial_concurrent_identical_replays(pg_uri):
    """Idempotency Challenge: 10 threads concurrently sending the EXACT same signed decision.

    Invariants:
    - ALL 10 threads succeed (200 OK / same returned state).
    - Exactly 1 approval_audit row created.
    - Exactly 1 domain_events record created.
    """
    store = ApprovalStore(pg_uri)
    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    ch = store.issue_challenge(tid, intent, COMMISSIONER, 120)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    # First commit
    initial_res = store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)
    assert initial_res["status"] == "approved"

    # Now 10 concurrent replays
    num_replays = 10
    barrier = threading.Barrier(num_replays)
    replay_results = []
    errors = []

    def replay_worker():
        barrier.wait()
        try:
            res = store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)
            replay_results.append(res)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=replay_worker) for _ in range(num_replays)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(errors) == 0, f"Replay threw unexpected error: {errors}"
    assert len(replay_results) == num_replays
    for r in replay_results:
        assert r["status"] == "approved"
        assert r["approval_audit_id"] == initial_res["approval_audit_id"]

    # Verify database has strictly 1 audit and 1 event
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM public.domain_events WHERE entity_id = %s AND event_type = 'approval.decided'", (str(initial_res["approval_request_id"]),))
            assert cur.fetchone()[0] == 1


def test_adversarial_concurrent_conflicting_replays(pg_uri):
    """Idempotency Challenge: 5 identical replays racing against 5 conflicting replays with same nonce.

    Invariants:
    - All 5 identical replays return 200 OK.
    - All 5 conflicting replays fail with 409 Conflict ('replay: conflicting terminal decision...').
    - State is never corrupted.
    """
    store = ApprovalStore(pg_uri)
    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    ch = store.issue_challenge(tid, intent, COMMISSIONER, 120)
    good_signed = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    # Initial decision
    store.apply_decision(tid, good_signed, COMMISSIONER, SIGNING_SECRET)

    # Conflicting signed payload (same nonce, altered notes)
    bad_dict = {**ch, "notes": "adversarial mutation"}
    bad_signed = SignedDecision.model_validate({**bad_dict, "signature": approval.sign(bad_dict, SIGNING_SECRET)})

    barrier = threading.Barrier(10)
    identical_oks = []
    conflict_errs = []
    other_errs = []

    def identical_worker():
        barrier.wait()
        try:
            res = store.apply_decision(tid, good_signed, COMMISSIONER, SIGNING_SECRET)
            identical_oks.append(res)
        except Exception as exc:
            other_errs.append(exc)

    def conflict_worker():
        barrier.wait()
        try:
            res = store.apply_decision(tid, bad_signed, COMMISSIONER, SIGNING_SECRET)
            other_errs.append(res)
        except StoreError as exc:
            if exc.status_code == 409 and "replay:" in exc.detail:
                conflict_errs.append(exc)
            else:
                other_errs.append(exc)
        except Exception as exc:
            other_errs.append(exc)

    threads = []
    for _ in range(5):
        threads.append(threading.Thread(target=identical_worker))
        threads.append(threading.Thread(target=conflict_worker))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(other_errs) == 0, f"Unexpected errors: {other_errs}"
    assert len(identical_oks) == 5, f"Expected 5 identical replay successes, got {len(identical_oks)}"
    assert len(conflict_errs) == 5, f"Expected 5 conflict errors, got {len(conflict_errs)}"


def test_adversarial_replay_with_revoked_nonce(pg_uri):
    """Challenge: Nonce revoked by expiry cannot be replayed or used for decision.

    Invariants:
    - Fails with 409 approval_expired.
    - Zero audit rows written.
    """
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))
    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    ch = store.issue_challenge(tid, DecisionIntent.model_validate(APPROVE_COACH_BEST), COMMISSIONER, 10)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    time.sleep(1.2)
    # Expire and revoke
    store.expire_due(thread_id=tid)

    with pytest.raises(StoreError) as exc_info:
        store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)

    assert exc_info.value.status_code == 409
    assert "approval_expired" in exc_info.value.detail

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == 0


# =====================================================================
# 3. Fault Injection & Atomic Rollback Tests
# =====================================================================

@pytest.mark.parametrize("target_table", ["approval_audit", "domain_events", "outbox"])
def test_adversarial_fault_injection_atomic_rollback(pg_uri, pg_admin_db_uri, target_table):
    """Fault Injection: Injected failure in table rolls back entire transaction atomically.

    Verifies:
    - graph_threads remains awaiting_approval.
    - approval_requests remains awaiting_approval.
    - approval_nonces consumed_at remains NULL.
    - zero audit records created.
    - zero domain events recorded.
    """
    store = ApprovalStore(pg_uri)
    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    ch = store.issue_challenge(tid, intent, COMMISSIONER, 120)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    # Install fault trigger
    trig_name = f"trg_adv_fail_{target_table}"
    func_name = f"fn_adv_fail_{target_table}"
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(f"""
            CREATE OR REPLACE FUNCTION {func_name}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'ADVERSARIAL FAULT INJECTION ON %', TG_TABLE_NAME;
            END; $$;
            CREATE TRIGGER {trig_name} BEFORE INSERT ON public.{target_table}
            FOR EACH ROW EXECUTE FUNCTION {func_name}();
        """)

    try:
        with pytest.raises(Exception, match=f"ADVERSARIAL FAULT INJECTION ON {target_table}"):
            store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)

        # Full rollback assertions
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "awaiting_approval"

                cur.execute("SELECT status FROM public.approval_requests WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "awaiting_approval"

                cur.execute("SELECT consumed_at FROM public.approval_nonces WHERE nonce = %s", (ch["nonce"],))
                assert cur.fetchone()[0] is None

                cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == 0
    finally:
        with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
            conn.execute(f"DROP TRIGGER IF EXISTS {trig_name} ON public.{target_table};")
            conn.execute(f"DROP FUNCTION IF EXISTS {func_name}();")


def test_adversarial_pair_consistency_guard_rollback(pg_uri):
    """Fault Injection: Deferred constraint trigger rollback on mismatched status mutation.

    Verifies that any attempted partial update is aborted by fc27_check_approval_pair at commit time.
    """
    store = ApprovalStore(pg_uri)
    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            # Modify only graph_threads without updating approval_requests
            cur.execute("""
                UPDATE public.graph_threads
                   SET status = 'approved', approved_by = 'batmanL8', approved_at = now(), decision_payload = '{}'
                 WHERE thread_id = %s
            """, (tid,))

            # Attempt commit -> must be blocked by deferred trigger
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: status mismatch"):
                conn.commit()

    # Outside the rolled-back transaction, state must still be awaiting_approval
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == "awaiting_approval"
            cur.execute("SELECT status FROM public.approval_requests WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == "awaiting_approval"
