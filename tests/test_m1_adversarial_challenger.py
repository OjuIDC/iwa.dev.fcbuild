"""Adversarial Challenge Test Suite for Milestone 1 Phase 1.

Targets:
1. Section 1A Authority Attacks:
   - Challenge timeout approval: expired requests NEVER transition to approved under any circumstances.
   - Challenge unauthorized actors: player, delegate, agent, and unsupported authority bases fail closed.
   - Verify zero automated decisions can be recorded.
2. Schema & Trigger Tamper Attacks:
   - Attempt direct SQL mutation of package_version while awaiting.
   - Attempt direct SQL UPDATE or DELETE on terminal rows in approval_requests.
   - Attempt direct SQL UPDATE or DELETE on terminal rows in graph_threads.
   - Attempt committing unlinked or mismatched thread/request pairs (deferred constraint trigger).
   - Verify public.fc27_approval_inconsistencies view accurately classifies all inconsistency kinds.
"""
from __future__ import annotations

import time
import uuid
from datetime import timedelta

import psycopg
import pytest

from src import approval
from src.approval_store import ApprovalStore, StoreError, request_hash
from src.schemas import DecisionIntent, SignedDecision
from src.security import Principal, Role
from tests.helpers import (
    APPROVE_COACH_BEST,
    COM,
    COMMISSIONER_ID,
    REJECT,
    SIGNING_SECRET,
    SVC,
    fake_packages,
    signed,
    start,
)

COMMISSIONER = Principal(Role.commissioner, COMMISSIONER_ID)


# ==============================================================================
# SECTION 1A: AUTHORITY ATTACKS
# ==============================================================================

def test_attack_expired_request_cannot_transition_to_approved_via_sql(pg_uri, pg_admin_db_uri):
    """Attack 1.1: Direct SQL update of an expired approval_request to 'approved' MUST be blocked."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))
    initial_state = lambda t: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    # Wait for expiry
    time.sleep(1.2)
    expired_tids = store.expire_due(thread_id=tid)
    assert tid in expired_tids

    # Verify both records are expired
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_request_id, status FROM public.approval_requests WHERE thread_id = %s", (tid,))
            req_id, req_status = cur.fetchone()
            assert req_status == "expired"

            # Attempt direct SQL mutation to 'approved' on approval_requests
            with pytest.raises(psycopg.errors.CheckViolation, match="approval_request is terminal and frozen"):
                cur.execute(
                    "UPDATE public.approval_requests SET status = 'approved', decided_at = now() WHERE approval_request_id = %s",
                    (req_id,),
                )

            # Attempt direct SQL mutation to 'approved' on graph_threads
            with pytest.raises(psycopg.errors.CheckViolation, match="terminal.*cannot be modified"):
                cur.execute(
                    """UPDATE public.graph_threads
                          SET status = 'approved', approved_by = 'batmanL8', approved_at = now(), decision_payload = '{}'
                        WHERE thread_id = %s""",
                    (tid,),
                )


def test_attack_expired_request_cannot_be_approved_via_store_or_api(pg_uri, client):
    """Attack 1.2: apply_decision and issue_challenge must reject expired requests with 409."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))

    # Create run and wait
    initial_state = lambda t: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    # Issue challenge before expiry
    intent = DecisionIntent.model_validate(APPROVE_COACH_BEST)
    ch = store.issue_challenge(tid, intent, COMMISSIONER, 10)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})

    # Sleep past request expiry
    time.sleep(1.2)

    # Attempt apply_decision after expiry
    with pytest.raises(StoreError) as exc_info:
        store.apply_decision(tid, signed_payload, COMMISSIONER, SIGNING_SECRET)
    assert exc_info.value.status_code == 409
    assert "approval_expired" in exc_info.value.detail

    # Attempt new issue_challenge after expiry
    with pytest.raises(StoreError) as exc_info2:
        store.issue_challenge(tid, intent, COMMISSIONER, 10)
    assert exc_info2.value.status_code == 409
    assert "approval_expired" in exc_info2.value.detail

    # Verify state in DB remains strictly expired, NEVER approved
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM public.approval_requests WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == "expired"
            cur.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == "expired"
            cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == 0


def test_attack_timeout_never_approves_in_background(pg_uri, pg_admin_db_uri):
    """Attack 1.3: Background expiry daemon must never record an APPROVE decision or emit dispatch rows."""
    store = ApprovalStore(pg_uri, request_ttl=timedelta(seconds=1))

    initial_state = lambda t: {"lead_packages": fake_packages()}
    state = store.create_run(club_id="330323", thread_id=None, initial_state=initial_state, req_hash=request_hash({}))
    tid = state["thread_id"]
    store.complete_run(tid, {**state, "status": "awaiting_approval"})

    time.sleep(1.2)
    expired = store.expire_due()
    assert tid in expired

    # Check database state: zero approvals, zero audits, zero dispatch outbox
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, expired_at, decided_at FROM public.approval_requests WHERE thread_id = %s", (tid,))
            r_st, r_exp, r_dec = cur.fetchone()
            assert r_st == "expired"
            assert r_exp is not None
            assert r_dec is None

            cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,))
            assert cur.fetchone()[0] == 0

    with psycopg.connect(pg_admin_db_uri) as admin_conn:
        with admin_conn.cursor() as admin_cur:
            admin_cur.execute(
                """SELECT count(*) FROM public.domain_events
                    WHERE entity_id IN (SELECT approval_request_id::text FROM public.approval_requests WHERE thread_id = %s)
                      AND event_type = 'approval.decided'""",
                (tid,),
            )
            assert admin_cur.fetchone()[0] == 0

            admin_cur.execute("SELECT count(*) FROM public.outbox WHERE channel = 'fc27:dispatch'")
            assert admin_cur.fetchone()[0] == 0


@pytest.mark.parametrize("unauthorized_actor", ["player", "delegate", "agent:player_agent_1", "coach", "system", "bot"])
def test_attack_unauthorized_actors_fail_closed(client, pg_uri, unauthorized_actor):
    """Attack 1.4: Decision submission by any actor other than commissioner must fail closed (403/422)."""
    tid = start(client)
    store = ApprovalStore(pg_uri)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    signed_payload = SignedDecision.model_validate(signed(ch))

    # Case A: actor_type parameter
    with pytest.raises(StoreError) as exc_info:
        store.apply_decision(
            tid,
            signed_payload,
            COMMISSIONER,
            SIGNING_SECRET,
            actor_type=unauthorized_actor,
        )
    assert exc_info.value.status_code in (403, 422)

    # Case B: actor parameter claiming to be an agent
    if "agent" in unauthorized_actor:
        with pytest.raises(StoreError) as exc_info2:
            store.apply_decision(
                tid,
                signed_payload,
                COMMISSIONER,
                SIGNING_SECRET,
                actor=unauthorized_actor,
            )
        assert exc_info2.value.status_code in (403, 422)


@pytest.mark.parametrize("unauthorized_basis", [
    "affected_player_fallback",
    "explicit_delegate",
    "affected_player_consent",
    "unauthorized_fallback",
    "unknown_basis",
])
def test_attack_unauthorized_authority_bases_fail_closed(client, pg_uri, unauthorized_basis):
    """Attack 1.5: Decision submission with non-primary authority basis must fail closed (403/422)."""
    tid = start(client)
    store = ApprovalStore(pg_uri)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    signed_payload = SignedDecision.model_validate(signed(ch))

    with pytest.raises(StoreError) as exc_info:
        store.apply_decision(
            tid,
            signed_payload,
            COMMISSIONER,
            SIGNING_SECRET,
            actor_type="commissioner",
            authority_basis=unauthorized_basis,
        )
    assert exc_info.value.status_code in (403, 422)


def test_attack_unauthorized_token_and_endpoints(client):
    """Attack 1.6: Service token cannot decide, invalid tokens 401, future player routes 404."""
    tid = start(client)

    # Service token cannot challenge or decide
    assert client.post(f"/threads/{tid}/decision-challenge", headers=SVC, json=APPROVE_COACH_BEST).status_code == 403
    com_ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    assert client.post(f"/threads/{tid}/decision", headers=SVC, json=signed(com_ch)).status_code == 403

    # Invalid token is rejected outright
    bad_token = {"X-API-Key": "invalid-token-1234567890123456789012345"}
    assert client.post(f"/threads/{tid}/decision-challenge", headers=bad_token, json=APPROVE_COACH_BEST).status_code == 401

    # Future player endpoints fail closed with 404 in Phase 1
    for future_route in (
        "/v1/player-decisions",
        "/v1/player-assignments/p1/decision",
        "/v1/authority-delegations",
        "/v1/approval-escalations",
        "/v1/authority-conflicts/resolve",
    ):
        assert client.post(future_route, headers=COM, json={}).status_code in (403, 404)


# ==============================================================================
# SCHEMA & TRIGGER TAMPER ATTACKS
# ==============================================================================

def test_attack_package_version_tamper_on_graph_threads_blocked(client, pg_uri):
    """Attack 2.1: Direct SQL mutation of package_version on graph_threads in awaiting_approval must raise CheckViolation."""
    tid = start(client)
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="package_version is immutable"):
                cur.execute(
                    "UPDATE public.graph_threads SET package_version = %s WHERE thread_id = %s",
                    ("e" * 64, tid),
                )


def test_attack_package_version_tamper_on_approval_requests_blocked(client, pg_uri):
    """Attack 2.2: Direct SQL mutation of package_version on approval_requests must raise CheckViolation."""
    tid = start(client)
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_request_id FROM public.graph_threads WHERE thread_id = %s", (tid,))
            req_id = cur.fetchone()[0]

            with pytest.raises(psycopg.errors.CheckViolation, match="package_version is immutable"):
                cur.execute(
                    "UPDATE public.approval_requests SET package_version = %s WHERE approval_request_id = %s",
                    ("9" * 64, req_id),
                )


def test_attack_approval_requests_terminal_row_frozen(client, pg_uri, pg_admin_db_uri):
    """Attack 2.3: Terminal approval_requests rows are completely frozen against UPDATE and DELETE (even as admin)."""
    tid = start(client)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=APPROVE_COACH_BEST).json()
    assert client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch)).status_code == 200

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approval_request_id FROM public.graph_threads WHERE thread_id = %s", (tid,))
            req_id = cur.fetchone()[0]

            # Attempt UPDATE
            with pytest.raises(psycopg.errors.CheckViolation, match="approval_request is terminal and frozen"):
                cur.execute(
                    "UPDATE public.approval_requests SET status = 'awaiting_approval' WHERE approval_request_id = %s",
                    (req_id,),
                )

    # Attempt DELETE as admin
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as admin_conn:
        with admin_conn.cursor() as admin_cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="approval_request is terminal and frozen"):
                admin_cur.execute(
                    "DELETE FROM public.approval_requests WHERE approval_request_id = %s",
                    (req_id,),
                )


def test_attack_graph_threads_terminal_row_frozen(client, pg_uri, pg_admin_db_uri):
    """Attack 2.4: Terminal graph_threads rows are completely frozen against UPDATE and DELETE (even as admin)."""
    tid = start(client)
    ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM, json=REJECT).json()
    assert client.post(f"/threads/{tid}/decision", headers=COM, json=signed(ch)).status_code == 200

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Attempt UPDATE
            with pytest.raises(psycopg.errors.CheckViolation, match="terminal.*cannot be modified"):
                cur.execute(
                    "UPDATE public.graph_threads SET status = 'awaiting_approval' WHERE thread_id = %s",
                    (tid,),
                )

    # Attempt DELETE as admin
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as admin_conn:
        with admin_conn.cursor() as admin_cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="terminal and cannot be deleted"):
                admin_cur.execute(
                    "DELETE FROM public.graph_threads WHERE thread_id = %s",
                    (tid,),
                )


def test_attack_twin_pair_consistency_triggers_block_mismatch(pg_uri, client):
    """Attack 2.5: Constraint trigger fc27_check_approval_pair blocks unlinked or mismatched thread/request pairs."""
    tid = start(client)

    # 1. Status mismatch from graph_threads side
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.graph_threads
                      SET status = 'approved', approved_by = 'batmanL8', approved_at = now(), decision_payload = '{}'
                    WHERE thread_id = %s""",
                (tid,),
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: status mismatch"):
                conn.commit()

    # 2. Status mismatch from approval_requests side
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.approval_requests SET status = 'approved', decided_at = now() WHERE thread_id = %s",
                (tid,),
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: status mismatch"):
                conn.commit()

    # 3. Unlinked awaiting thread with package_version
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
                   VALUES ('fc27-330323-run-20261001-unlinked', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, NULL)""",
                ("1" * 64,),
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: awaiting thread requires link"):
                conn.commit()

    # 4. Unlinked request pointing to thread that does not link back
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            # Create a running thread (no link)
            cur.execute(
                """INSERT INTO public.graph_threads (thread_id, state, status, club_id)
                   VALUES ('fc27-330323-run-20261001-running', '{"status":"running"}', 'running', '330323')"""
            )
            # Create request pointing to it
            cur.execute(
                """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, expires_at)
                   VALUES (gen_random_uuid(), 'fc27-330323-run-20261001-running', %s, 'awaiting_approval', now() + interval '1h')""",
                ("2" * 64,),
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="approval pair: thread .* does not link to approval_request"):
                conn.commit()


@pytest.mark.allow_inconsistency
def test_attack_inconsistencies_view_classifies_all_corruptions(pg_uri, pg_admin_db_uri):
    """Attack 2.6: fc27_approval_inconsistencies view detects and accurately classifies corrupt fixtures."""
    req1 = str(uuid.uuid4())
    req2 = str(uuid.uuid4())
    req3 = str(uuid.uuid4())

    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET session_replication_role = 'replica'")
            try:
                # 1. status_mismatch
                cur.execute(
                    """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
                       VALUES ('incon-th-1', '{"status":"running"}', 'running', '330323', %s, %s)""",
                    ("a" * 64, req1),
                )
                cur.execute(
                    """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, expires_at)
                       VALUES (%s, 'incon-th-1', %s, 'awaiting_approval', now() + interval '1h')""",
                    (req1, "a" * 64),
                )

                # 2. package_mismatch
                cur.execute(
                    """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
                       VALUES ('incon-th-2', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, %s)""",
                    ("b" * 64, req2),
                )
                cur.execute(
                    """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, expires_at)
                       VALUES (%s, 'incon-th-2', %s, 'awaiting_approval', now() + interval '1h')""",
                    (req2, "c" * 64),
                )

                # 3. unlinked_request
                cur.execute(
                    """INSERT INTO public.graph_threads (thread_id, state, status, club_id, package_version, approval_request_id)
                       VALUES ('incon-th-3', '{"status":"awaiting_approval"}', 'awaiting_approval', '330323', %s, NULL)""",
                    ("d" * 64,),
                )
                cur.execute(
                    """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, expires_at)
                       VALUES (%s, 'incon-th-3', %s, 'awaiting_approval', now() + interval '1h')""",
                    (req3, "d" * 64),
                )

                # 4. missing_link (already covered in incon-th-3 because package_version is not null and approval_request_id is null)

            finally:
                cur.execute("SET session_replication_role = 'origin'")

    # Query view and assert all kinds are identified
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kind, thread_id, approval_request_id FROM public.fc27_approval_inconsistencies WHERE thread_id LIKE 'incon-th-%' ORDER BY thread_id"
            )
            rows = cur.fetchall()
            kinds_by_thread = {r[1]: r[0] for r in rows}

            assert kinds_by_thread.get("incon-th-1") == "status_mismatch"
            assert kinds_by_thread.get("incon-th-2") == "package_mismatch"
            assert "unlinked_request" in [r[0] for r in rows if r[1] == "incon-th-3"]
            assert "missing_link" in [r[0] for r in rows if r[1] == "incon-th-3"]
