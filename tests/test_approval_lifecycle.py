"""Tests for approval lifecycle, expiry daemon, race conditions, and Section 1A authority separation."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

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
    signed_cli_hmac,
    fake_packages,
)

COMMISSIONER = Principal(Role.commissioner, COMMISSIONER_ID)


def test_complete_run_creates_linked_request(client, pg_uri, pg_admin_db_uri):
    """complete_run creates request, links thread, emits approval.created and outbox row."""
    tid = start(client)
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT approval_request_id, package_version, status FROM public.graph_threads WHERE thread_id = %s",
                (tid,),
            )
            th_link, th_pkg, th_status = cur.fetchone()
            assert th_link is not None
            assert th_status == "awaiting_approval"

            cur.execute(
                "SELECT status, package_version, expires_at FROM public.approval_requests WHERE approval_request_id = %s",
                (th_link,),
            )
            r_status, r_pkg, r_exp = cur.fetchone()
            assert r_status == "awaiting_approval"
            assert r_pkg == th_pkg

            # Event check
            cur.execute(
                "SELECT event_type, source_table, source_id FROM public.domain_events WHERE entity_id = %s AND event_type = 'approval.created'",
                (str(th_link),),
            )
            ev = cur.fetchone()
            assert ev is not None
            assert ev[0] == "approval.created"
            assert ev[1] == "approval_requests"
            assert ev[2] == str(th_link)

    # Outbox check via admin (fc27_app has INSERT only on outbox)
    with psycopg.connect(pg_admin_db_uri) as admin_conn:
        with admin_conn.cursor() as admin_cur:
            admin_cur.execute(
                """SELECT o.channel FROM public.outbox o
                     JOIN public.domain_events d ON o.domain_event_id = d.event_id
                    WHERE d.entity_id = %s""",
                (str(th_link),),
            )
            out = admin_cur.fetchone()
            assert out is not None
            assert out[0] == "fc27:events"


def test_decision_updates_request_and_thread_together(client_v1_hmac, pg_uri):
    """Decision atomically updates thread & request, writes audit with proof_type=hmac, emits event."""
    tid = start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    resp = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=signed_cli_hmac(ch))
    assert resp.status_code == 200

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT g.status, r.status, a.proof_type, a.correlation_id, a.approval_request_id, a.id
                     FROM public.graph_threads g
                     JOIN public.approval_requests r ON g.approval_request_id = r.approval_request_id
                     JOIN public.approval_audit a ON a.thread_id = g.thread_id
                    WHERE g.thread_id = %s""",
                (tid,),
            )
            g_st, r_st, proof, corr_id, req_id, audit_id = cur.fetchone()
            assert g_st == "approved"
            assert r_st == "approved"
            assert proof == "hmac"
            assert corr_id is not None
            assert req_id is not None

            # Sourced domain event
            cur.execute(
                "SELECT event_type, source_table, source_id FROM public.domain_events WHERE entity_id = %s AND event_type = 'approval.decided'",
                (str(req_id),),
            )
            ev = cur.fetchone()
            assert ev is not None
            assert ev[1] == "approval_audit"
            assert ev[2] == str(audit_id)


def test_approve_writes_no_dispatch_key(client_v1_hmac, pg_uri):
    """AC-44: Decision state never contains a dispatch key."""
    tid = start(client_v1_hmac)
    resp = decide(client_v1_hmac, tid, APPROVE_COACH_BEST, proof_type="cli_hmac")
    assert resp.status_code == 200
    dispatch_block = resp.json().get("dispatch", {})
    assert dispatch_block.get("dispatch_id") is None
    assert dispatch_block.get("enabled") is False
    assert dispatch_block.get("created") is False

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM public.graph_threads WHERE thread_id = %s", (tid,))
            state = cur.fetchone()[0]
            assert "dispatch" not in state


def test_request_revision_lifecycle(client_v1_hmac, pg_uri):
    """Spec §7.3: REQUEST_REVISION decision is terminal and immutable."""
    tid = start(client_v1_hmac)
    revision_intent = {"decision": "REQUEST_REVISION", "notes": "Please revise the tactical formation."}
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=revision_intent).json()
    resp = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=signed_cli_hmac(ch))
    assert resp.status_code == 200

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT g.status, r.status, a.decision, a.notes
                     FROM public.graph_threads g
                     JOIN public.approval_requests r ON g.approval_request_id = r.approval_request_id
                     JOIN public.approval_audit a ON a.thread_id = g.thread_id
                    WHERE g.thread_id = %s""",
                (tid,),
            )
            g_st, r_st, dec, notes = cur.fetchone()
            assert g_st == "revision_requested"
            assert r_st == "revision_requested"
            assert dec == "REQUEST_REVISION"
            assert notes == "Please revise the tactical formation."


def test_expired_request_blocks_prior_nonce(pg_uri):
    """AC-43: A nonce issued before deadline returns 409 approval_expired after deadline."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))

    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    ch = store.issue_challenge(tid, intent, COMMISSIONER, 10)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    # Sleep past deadline
    time.sleep(1.2)

    with pytest.raises(StoreError) as exc_info:
        store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)
    assert exc_info.value.status_code == 409
    assert "approval_expired" in exc_info.value.detail

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.approval_requests WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == "expired"
            cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == "expired"
            cur.execute("SELECT revoked_at FROM public.approval_nonces WHERE nonce = %s", (ch["nonce"],))
            assert cur.fetchone()[0] is not None


def test_expired_request_blocks_new_challenge(pg_uri):
    """AC-43: issue_challenge returns 409 approval_expired once past expires_at."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))

    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    time.sleep(1.2)

    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    with pytest.raises(StoreError) as exc_info:
        store.issue_challenge(tid, intent, COMMISSIONER, 10)
    assert exc_info.value.status_code == 409
    assert "approval_expired" in exc_info.value.detail


def test_expire_due_is_idempotent(pg_uri):
    """expire_due processes due requests once and is safe to call repeatedly."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))

    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    time.sleep(1.2)

    expired1 = store.expire_due()
    assert tid in expired1

    expired2 = store.expire_due()
    assert expired2 == []


def test_expiry_and_decision_race_has_single_outcome(pg_uri):
    """Review Focus 2: 4x expire_due + 4x apply_decision at deadline produces exactly one winner and no deadlock."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))

    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    ch = store.issue_challenge(tid, intent, COMMISSIONER, 10)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    # Ensure the request is expired before racing expiry workers with decisions.
    time.sleep(1.2)

    barrier = threading.Barrier(8)
    results = []

    def run_expiry():
        barrier.wait()
        try:
            exp = store.expire_due(thread_id=tid)
            results.append(("expiry", exp))
        except Exception as exc:
            results.append(("expiry_err", exc))

    def run_decision():
        barrier.wait()
        try:
            res = store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)
            results.append(("decision", res))
        except StoreError as exc:
            results.append(("decision_store_err", exc.status_code, exc.detail))
        except Exception as exc:
            results.append(("decision_err", exc))

    threads = []
    for _ in range(4):
        threads.append(threading.Thread(target=run_expiry))
        threads.append(threading.Thread(target=run_decision))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Verify final state is terminal
    row = store.load(tid)
    assert row is not None
    assert row["status"] in ("approved", "expired")

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
            audits = cur.fetchone()[0]
            if row["status"] == "approved":
                assert audits == 1
            else:
                assert audits == 0


def test_inbox_query_never_shows_terminal_threads(pg_uri, client_v1_hmac):
    """AC-55: pending_approvals inbox query never shows terminal or expired threads."""
    store = ApprovalStore(pg_uri)

    # 1. Thread that gets approved
    tid_approved = start(client_v1_hmac)
    decide(client_v1_hmac, tid_approved, APPROVE_COACH_BEST, proof_type="cli_hmac")

    # 2. Thread that gets rejected
    tid_rejected = start(client_v1_hmac)
    decide(client_v1_hmac, tid_rejected, REJECT, proof_type="cli_hmac")

    # 3. Thread that is active awaiting_approval
    tid_active = start(client_v1_hmac)

    inbox = store.pending_approvals()
    inbox_tids = [r["thread_id"] for r in inbox]

    assert tid_active in inbox_tids
    assert tid_approved not in inbox_tids
    assert tid_rejected not in inbox_tids


@pytest.mark.parametrize("table", ["approval_audit", "domain_events", "outbox"])
def test_failed_decision_rolls_back_everything(pg_uri, pg_admin_db_uri, table):
    """AC-1 fault injection: failure on any insert rolls back request, thread, audit, events, and nonce."""
    store = ApprovalStore(pg_uri)
    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    ch = store.issue_challenge(tid, intent, COMMISSIONER, 120)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    # Inject failure trigger via admin
    trigger_func = f"""
    CREATE OR REPLACE FUNCTION fail_txn_{table}() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'fault injection on %', TG_TABLE_NAME;
    END; $$;
    CREATE TRIGGER trg_fail_{table} BEFORE INSERT ON public.{table}
    FOR EACH ROW EXECUTE FUNCTION fail_txn_{table}();
    """
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(trigger_func)

    try:
        with pytest.raises(Exception, match=f"fault injection on {table}"):
            store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)

        # Assert complete rollback
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "awaiting_approval"
                cur.execute("SELECT status FROM public.approval_requests WHERE thread_id = %s", (tid,))
                assert cur.fetchone()[0] == "awaiting_approval"
                cur.execute("SELECT consumed_at FROM public.approval_nonces WHERE nonce = %s", (ch["nonce"],))
                assert cur.fetchone()[0] is None
    finally:
        with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
            conn.execute(f"DROP TRIGGER IF EXISTS trg_fail_{table} ON public.{table};")
            conn.execute(f"DROP FUNCTION IF EXISTS fail_txn_{table}();")


def test_safe_idempotent_decision_replay(client_v1_hmac, pg_uri):
    """Repeating identical signed decision safely returns committed state; conflicting replay returns 409."""
    tid = start(client_v1_hmac)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    good_signed = signed_cli_hmac(ch)
    r1 = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=good_signed)
    assert r1.status_code == 200

    # Idempotent replay: must succeed and return same terminal status
    r2 = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=good_signed)
    assert r2.status_code == 200
    assert r2.json()["status"] == "approved"

    # Conflicting replay: same nonce with modified intent
    conflict_payload = {**good_signed, "notes": "conflicting modification"}
    # Note: re-sign with conflict
    conflict_payload["signature"] = approval.sign(
        {k: v for k, v in conflict_payload.items() if k != "signature"}, SIGNING_SECRET
    )
    r3 = client_v1_hmac.post(f"/threads/{tid}/decision", headers=COM, json=conflict_payload)
    assert r3.status_code == 409


def test_timeout_inactivity_cannot_approve(pg_uri, pg_admin_db_uri):
    """Section 1A.1: Inactivity and timeout can NEVER write an approval or accept a recommendation."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))
    initial_state = lambda tid: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    time.sleep(1.2)
    store.expire_due()

    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.approval_requests WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == "expired"
            cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s AND decision = 'APPROVE'", (tid,))
            assert cur.fetchone()[0] == 0

    with psycopg.connect(pg_admin_db_uri) as admin_conn:
        with admin_conn.cursor() as admin_cur:
            admin_cur.execute("SELECT count(*) FROM public.outbox WHERE channel = 'fc27:dispatch'")
            assert admin_cur.fetchone()[0] == 0


def test_unsupported_actor_types_fail_closed_in_phase1(client_v1_hmac, pg_uri):
    """Section 1A.8 & Agent C: Unsupported actor types (player, delegate, agent) fail closed in Phase 1."""
    tid = start(client_v1_hmac)
    store = ApprovalStore(pg_uri)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    signed_payload = SignedDecision.model_validate(signed(ch))

    for bad_actor in ("player", "delegate", "agent", "coach", "system"):
        with pytest.raises(StoreError) as exc_info:
            store.apply_decision(
                tid,
                signed_payload,
                COMMISSIONER,
                SIGNING_SECRET,
                actor_type=bad_actor,
                authority_basis="primary",
            )
        assert exc_info.value.status_code in (403, 422)


def test_unsupported_authority_bases_fail_closed_in_phase1(client_v1_hmac, pg_uri):
    """Section 1A.8 & Agent B: Unsupported authority bases fail closed in Phase 1."""
    tid = start(client_v1_hmac)
    store = ApprovalStore(pg_uri)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    signed_payload = SignedDecision.model_validate(signed(ch))

    for bad_basis in ("affected_player_fallback", "explicit_delegate", "affected_player_consent"):
        with pytest.raises(StoreError) as exc_info:
            store.apply_decision(
                tid,
                signed_payload,
                COMMISSIONER,
                SIGNING_SECRET,
                actor_type="commissioner",
                authority_basis=bad_basis,
            )
        assert exc_info.value.status_code in (403, 422)


def test_schema_distinguishes_club_authorization_and_player_consent(pg_uri):
    """Section 1A.3 & Agent A: Schema separates club authorization from player consent."""
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT conname, pg_get_constraintdef(oid)
                     FROM pg_constraint
                    WHERE conrelid = 'public.approval_audit'::regclass
                      AND conname LIKE '%decision%'"""
            )
            rows = cur.fetchall()
            assert any("REQUEST_REVISION" in r[1] for r in rows)

            cur.execute(
                """SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'approval_audit'
                      AND column_name IN ('actor_type', 'actor_id', 'authority_basis', 'decision_scope')"""
            )
            found_cols = {r[0] for r in cur.fetchall()}
            assert {"actor_type", "actor_id", "authority_basis", "decision_scope"}.issubset(found_cols)


def test_player_agent_cannot_be_decision_actor(client_v1_hmac, pg_uri):
    """Section 1A.2: A player agent cannot be recorded as decision actor."""
    tid = start(client_v1_hmac)
    store = ApprovalStore(pg_uri)
    ch = client_v1_hmac.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    signed_payload = SignedDecision.model_validate(signed(ch))

    with pytest.raises(StoreError) as exc_info:
        store.apply_decision(
            tid,
            signed_payload,
            COMMISSIONER,
            SIGNING_SECRET,
            actor="agent:player_agent_1",
        )
    assert exc_info.value.status_code in (403, 422)


def test_no_player_decision_endpoints_active_in_phase1(client):
    """Section 1A.9: Future player-decision endpoints are not active in Phase 1."""
    for endpoint in (
        "/v1/player-decisions",
        "/v1/assignments/1/decision",
        "/v1/delegations",
        "/v1/escalations",
    ):
        assert client.post(endpoint, json={}).status_code == 404
