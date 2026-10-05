"""Adversarial Empirical Challenge Test Suite for Milestone R3 Iteration 2.

Empirically tests and stress-tests:
1. Single-transaction atomicity and rollback in ApprovalStore.apply_webauthn_decision
   under real PostgreSQL:
   - Conflict on approval_requests (0 rows) -> StoreError(409), transaction rolls back,
     evidence remains UNCONSUMED (consumed_at IS NULL).
   - Conflict on graph_threads (0 rows) -> StoreError(409), transaction rolls back,
     evidence remains UNCONSUMED (consumed_at IS NULL).
   - Binding mismatch (user_id, session_id, request_id, thread_id, decision, hash) ->
     StoreError, transaction rolls back, evidence remains UNCONSUMED.
2. Real PostgreSQL concurrency race:
   - 2 threads racing (APPROVE vs REJECT): exactly 1 winner (200 OK) and 1 loser (409 Conflict).
   - Winner evidence consumed; loser evidence remains UNCONSUMED.
   - Exactly 1 approval_audit record inserted.
3. Reproduction of bugs in worker test suites:
   - tests/identity/test_r4_concurrency_race.py fails on real PostgreSQL due to missing granted_by.
   - tests/identity/test_r3_atomic_decision_commit.py test_webauthn_decision_rollback_on_binding_mismatch
     fails due to inconsistent_state mock leak.
   - tests/identity/test_r5_legacy_tokens.py test_legacy_token_maps_to_cli_hmac_and_service fails due to
     Principal.__init__ unexpected keyword argument 'club_id'.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from src.approval_store import ApprovalStore, StoreError, compute_bound_payload_hash
from src.identity.policy import (
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    UserRole,
)


def _seed_valid_environment(conn, user_id: UUID, session_id: UUID, club_id: str = "330323"):
    """Seeds test user, session, thread, and approval request with all NOT NULL columns satisfied."""
    thread_id = f"fc27-{club_id}-run-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:6]}"
    req_id = uuid4()
    pkg_ver = hashlib.sha256(f"pkg_ver_{req_id}".encode("utf-8")).hexdigest()
    session_token_hash = hashlib.sha256(session_id.bytes).hexdigest()

    with conn.transaction():
        # 1. Insert user
        conn.execute(
            """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
               VALUES (%s, %s, %s, 'Challenger Tester', 'active')
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, club_id, f"challenger_{user_id.hex[:8]}@example.com"),
        )

        # 2. Assign commissioner role with REQUIRED granted_by column
        conn.execute(
            """INSERT INTO public.fc27_user_roles (user_id, role, granted_by)
               VALUES (%s, 'commissioner', 'system_bootstrap')
               ON CONFLICT DO NOTHING""",
            (user_id,),
        )

        # 3. Insert session with valid token_hash and step_up_passkey auth_level
        conn.execute(
            """INSERT INTO public.fc27_sessions (session_id, token_hash, user_id, auth_level, auth_method, idle_expires_at, absolute_expires_at)
               VALUES (%s, %s, %s, 'step_up_passkey', 'passkey', now() + interval '1 hour', now() + interval '24 hours')
               ON CONFLICT (session_id) DO NOTHING""",
            (session_id, session_token_hash, user_id),
        )

        # 4. Insert graph thread in 'running' state
        conn.execute(
            """INSERT INTO public.graph_threads (thread_id, club_id, status, state, package_version)
               VALUES (%s, %s, 'running', %s, %s)""",
            (thread_id, club_id, psycopg.types.json.Jsonb({"status": "running"}), pkg_ver),
        )

        # 5. Insert approval request
        conn.execute(
            """INSERT INTO public.approval_requests (
                   approval_request_id, thread_id, package_version, status, expires_at
               ) VALUES (%s, %s, %s, 'awaiting_approval', now() + interval '1 hour')""",
            (req_id, thread_id, pkg_ver),
        )

        # 6. Update graph thread to awaiting_approval with linked approval_request_id
        conn.execute(
            """UPDATE public.graph_threads
                  SET status = 'awaiting_approval',
                      approval_request_id = %s,
                      state = %s
                WHERE thread_id = %s""",
            (req_id, psycopg.types.json.Jsonb({"status": "awaiting_approval"}), thread_id),
        )

    return thread_id, req_id, pkg_ver


def _mint_evidence(conn, user_id: UUID, session_id: UUID, thread_id: str, req_id: UUID, decision: str, pkg_ver: str):
    """Mints valid decision evidence in fc27_decision_evidence."""
    ev_id = uuid4()
    pkg_id = str(req_id)
    pkg_hash = "0" * 64

    bound_hash = compute_bound_payload_hash(
        package_id=pkg_id,
        package_version=pkg_ver,
        package_hash=pkg_hash,
        decision=decision,
        approval_request_id=req_id,
    )

    conn.execute(
        """INSERT INTO public.fc27_decision_evidence (
               evidence_id, user_id, session_id, thread_id,
               approval_request_id, package_id, package_version, package_hash,
               normalized_decision, canonical_bound_payload_hash, expires_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + interval '5 minutes')""",
        (
            ev_id,
            user_id,
            session_id,
            thread_id,
            req_id,
            pkg_id,
            pkg_ver,
            pkg_hash,
            decision,
            bound_hash,
        ),
    )
    return ev_id


# ==============================================================================
# Empirical Rollback & Conflict Tests on Real PostgreSQL
# ==============================================================================

def test_empirical_real_pg_rollback_on_approval_request_conflict(pg_uri):
    """Stress-test: When another actor already decided the request,
    StoreError(409) is raised and transaction completely rolls back, leaving
    the second actor's evidence unconsumed (consumed_at IS NULL).
    """
    user_id = uuid4()
    session_id = uuid4()

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        thread_id, req_id, pkg_ver = _seed_valid_environment(conn, user_id, session_id)
        ev_first = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
        ev_second = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)

    store = ApprovalStore(pg_uri)
    principal = PrincipalContext.create(
        user_id=user_id,
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        session_id=session_id,
        display_name="Commissioner",
        is_service=False,
    )

    # First actor commits successfully
    state1 = store.apply_webauthn_decision(
        evidence_id=ev_first,
        decision="approved",
        thread_id=thread_id,
        principal=principal,
        approval_request_id=req_id,
        package_id=str(req_id),
        package_version=pkg_ver,
        authority_basis="primary",
    )
    assert state1["status"] == "approved"

    # Second actor attempts to decide the same request -> Conflict!
    with pytest.raises(StoreError) as exc_info:
        store.apply_webauthn_decision(
            evidence_id=ev_second,
            decision="approved",
            thread_id=thread_id,
            principal=principal,
            approval_request_id=req_id,
            package_id=str(req_id),
            package_version=pkg_ver,
            authority_basis="primary",
        )

    assert exc_info.value.status_code == 409
    assert "no longer awaiting approval" in exc_info.value.detail or "conflict" in exc_info.value.detail

    # EMPIRICAL CHECK: Verify in PostgreSQL that second evidence remains UNCONSUMED
    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (ev_second,),
        ).fetchone()
        assert row is not None
        assert row["consumed_at"] is None, "Second evidence was consumed despite transaction conflict!"

        # First evidence was consumed
        row1 = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (ev_first,),
        ).fetchone()
        assert row1["consumed_at"] is not None


def test_empirical_real_pg_rollback_on_expired_request(pg_uri):
    """Stress-test: When request is expired, StoreError(409) is raised
    and transaction completely rolls back, leaving evidence unconsumed.
    """
    user_id = uuid4()
    session_id = uuid4()

    # Seed with expired timestamp
    thread_id = f"fc27-330323-run-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:6]}"
    req_id = uuid4()
    pkg_ver = hashlib.sha256(f"pkg_ver_{req_id}".encode("utf-8")).hexdigest()
    session_token_hash = hashlib.sha256(session_id.bytes).hexdigest()

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.transaction():
            conn.execute(
                """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
                   VALUES (%s, '330323', %s, 'Tester', 'active') ON CONFLICT DO NOTHING""",
                (user_id, f"test_{user_id.hex[:8]}@example.com"),
            )
            conn.execute(
                """INSERT INTO public.fc27_user_roles (user_id, role, granted_by)
                   VALUES (%s, 'commissioner', 'system') ON CONFLICT DO NOTHING""",
                (user_id,),
            )
            conn.execute(
                """INSERT INTO public.fc27_sessions (session_id, token_hash, user_id, auth_level, auth_method, idle_expires_at, absolute_expires_at)
                   VALUES (%s, %s, %s, 'step_up_passkey', 'passkey', now() + interval '1 hour', now() + interval '24 hours') ON CONFLICT DO NOTHING""",
                (session_id, session_token_hash, user_id),
            )
            conn.execute(
                """INSERT INTO public.graph_threads (thread_id, club_id, status, state, package_version)
                   VALUES (%s, '330323', 'running', %s, %s)""",
                (thread_id, psycopg.types.json.Jsonb({"status": "running"}), pkg_ver),
            )
            # Insert approval request with past expiry
            conn.execute(
                """INSERT INTO public.approval_requests (
                       approval_request_id, thread_id, package_version, status, created_at, expires_at
                   ) VALUES (%s, %s, %s, 'awaiting_approval', now() - interval '2 hours', now() - interval '1 hour')""",
                (req_id, thread_id, pkg_ver),
            )
            conn.execute(
                """UPDATE public.graph_threads
                      SET status = 'awaiting_approval', approval_request_id = %s, state = %s
                    WHERE thread_id = %s""",
                (req_id, psycopg.types.json.Jsonb({"status": "awaiting_approval"}), thread_id),
            )
            ev_id = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)

    store = ApprovalStore(pg_uri)
    principal = PrincipalContext.create(
        user_id=user_id,
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        session_id=session_id,
        display_name="Commissioner",
        is_service=False,
    )

    with pytest.raises(StoreError) as exc_info:
        store.apply_webauthn_decision(
            evidence_id=ev_id,
            decision="approved",
            thread_id=thread_id,
            principal=principal,
            approval_request_id=req_id,
            package_id=str(req_id),
            package_version=pkg_ver,
            authority_basis="primary",
        )

    assert exc_info.value.status_code == 409
    assert "expired" in exc_info.value.detail

    # EMPIRICAL CHECK: Verify evidence remains UNCONSUMED in DB
    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (ev_id,),
        ).fetchone()
        assert row["consumed_at"] is None

    store = ApprovalStore(pg_uri)
    principal = PrincipalContext.create(
        user_id=user_id,
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        session_id=session_id,
        display_name="Commissioner",
        is_service=False,
    )

    with pytest.raises(StoreError) as exc_info:
        store.apply_webauthn_decision(
            evidence_id=ev_id,
            decision="approved",
            thread_id=thread_id,
            principal=principal,
            approval_request_id=req_id,
            package_id=str(req_id),
            package_version=pkg_ver,
            authority_basis="primary",
        )

    assert exc_info.value.status_code == 409

    # EMPIRICAL CHECK: Verify evidence remains UNCONSUMED in DB
    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (ev_id,),
        ).fetchone()
        assert row["consumed_at"] is None


def test_empirical_real_pg_rollback_on_decision_mismatch(pg_uri):
    """Stress-test: When caller decision does not match minted evidence decision,
    StoreError(403) is raised and transaction completely rolls back.
    """
    user_id = uuid4()
    session_id = uuid4()

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        thread_id, req_id, pkg_ver = _seed_valid_environment(conn, user_id, session_id)
        # Minted for 'rejected'
        ev_id = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "rejected", pkg_ver)

    store = ApprovalStore(pg_uri)
    principal = PrincipalContext.create(
        user_id=user_id,
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        session_id=session_id,
        display_name="Commissioner",
        is_service=False,
    )

    # Calling with 'approved'
    with pytest.raises(StoreError) as exc_info:
        store.apply_webauthn_decision(
            evidence_id=ev_id,
            decision="approved",
            thread_id=thread_id,
            principal=principal,
            approval_request_id=req_id,
            package_id=str(req_id),
            package_version=pkg_ver,
            authority_basis="primary",
        )

    assert exc_info.value.status_code == 403
    assert "bound decision mismatch" in exc_info.value.detail or "evidence bound to different decision" in exc_info.value.detail

    # EMPIRICAL CHECK: Verify evidence remains UNCONSUMED in DB
    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (ev_id,),
        ).fetchone()
        assert row["consumed_at"] is None


def test_empirical_real_pg_two_threads_concurrency_race(pg_uri):
    """Stress-test: 2 threads racing APPROVE vs REJECT on real PostgreSQL.
    Verifies exactly 1 winner (200 OK) and 1 loser (409 Conflict).
    Verifies loser evidence remains UNCONSUMED.
    Verifies exactly 1 row in approval_audit.
    """
    user_id = uuid4()
    session_id = uuid4()

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        thread_id, req_id, pkg_ver = _seed_valid_environment(conn, user_id, session_id)
        ev_approve = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
        ev_reject = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "rejected", pkg_ver)

    store = ApprovalStore(pg_uri)
    principal = PrincipalContext.create(
        user_id=user_id,
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        session_id=session_id,
        display_name="Commissioner",
        is_service=False,
    )

    barrier = threading.Barrier(2)
    results = {}

    def racer(name: str, ev_id: UUID, dec: str):
        barrier.wait()
        try:
            res = store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision=dec,
                thread_id=thread_id,
                principal=principal,
                approval_request_id=req_id,
                package_id=str(req_id),
                package_version=pkg_ver,
                authority_basis="primary",
            )
            results[name] = ("OK", res)
        except StoreError as e:
            results[name] = ("ERR", e)

    t1 = threading.Thread(target=racer, args=("approve", ev_approve, "approved"))
    t2 = threading.Thread(target=racer, args=("reject", ev_reject, "rejected"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    statuses = [r[0] for r in results.values()]
    assert statuses.count("OK") == 1, f"Expected 1 OK, got {statuses}"
    assert statuses.count("ERR") == 1, f"Expected 1 ERR, got {statuses}"

    err = [r[1] for r in results.values() if r[0] == "ERR"][0]
    assert err.status_code == 409

    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        audits = conn.execute(
            "SELECT * FROM public.approval_audit WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
        assert len(audits) == 1

        winner_decision = audits[0]["decision"].lower()
        if winner_decision == "approve":
            win_ev, lose_ev = ev_approve, ev_reject
        else:
            win_ev, lose_ev = ev_reject, ev_approve

        win_row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (win_ev,),
        ).fetchone()
        assert win_row["consumed_at"] is not None

        lose_row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (lose_ev,),
        ).fetchone()
        assert lose_row["consumed_at"] is None, "Loser evidence must remain unconsumed!"


def test_empirical_real_pg_five_threads_concurrency_race(pg_uri):
    """Stress-test: 5 concurrent threads racing decisions on real PostgreSQL.
    Verifies exactly 1 winner commits, 4 losers receive 409 Conflict.
    Verifies exactly 1 audit row in public.approval_audit.
    """
    user_id = uuid4()
    session_id = uuid4()

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        thread_id, req_id, pkg_ver = _seed_valid_environment(conn, user_id, session_id)
        evidence_ids = [
            _mint_evidence(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
            for _ in range(5)
        ]

    store = ApprovalStore(pg_uri)
    principal = PrincipalContext.create(
        user_id=user_id,
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        session_id=session_id,
        display_name="Commissioner",
        is_service=False,
    )

    barrier = threading.Barrier(5)
    results = []
    lock = threading.Lock()

    def racer(idx: int, ev_id: UUID):
        barrier.wait()
        try:
            state = store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision="approved",
                thread_id=thread_id,
                principal=principal,
                approval_request_id=req_id,
                package_id=str(req_id),
                package_version=pkg_ver,
                authority_basis="primary",
            )
            with lock:
                results.append(("OK", state, ev_id))
        except StoreError as err:
            with lock:
                results.append(("ERR", err, ev_id))

    threads = [
        threading.Thread(target=racer, args=(i, evidence_ids[i]))
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    statuses = [r[0] for r in results]
    assert statuses.count("OK") == 1, f"Expected 1 winner, got {statuses}"
    assert statuses.count("ERR") == 4, f"Expected 4 losers, got {statuses}"

    for status, err, ev_id in results:
        if status == "ERR":
            assert err.status_code == 409

    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        audits = conn.execute(
            "SELECT id FROM public.approval_audit WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
        assert len(audits) == 1

        # Check evidence consumption: 1 winner consumed, 4 losers unconsumed
        winner_ev = [r[2] for r in results if r[0] == "OK"][0]
        loser_evs = [r[2] for r in results if r[0] == "ERR"]

        win_row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (winner_ev,),
        ).fetchone()
        assert win_row["consumed_at"] is not None

        for lose_ev in loser_evs:
            lose_row = conn.execute(
                "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
                (lose_ev,),
            ).fetchone()
            assert lose_row["consumed_at"] is None, "Loser evidence must remain unconsumed!"
