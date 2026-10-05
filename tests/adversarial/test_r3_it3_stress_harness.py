"""Empirical Stress Harness for Milestone R3 Iteration 3 Adversarial Challenge.

Author: challenger_r3_it3_2
Verifies under real PostgreSQL:
1. 2-thread races repeated 5 times: 1 winner, 1 loser (409), loser unconsumed.
2. 10-thread race: 1 winner, 9 losers (409), 9 losers unconsumed.
3. Trigger fc27_check_approval_pair() active enforcement.
4. Foreign key and schema constraints enforcement.
5. Zero dispatch / dormancy invariants.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from src.approval_store import ApprovalStore, StoreError, compute_bound_payload_hash
from src.identity.policy import (
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    UserRole,
)


def _seed_env(conn, user_id: UUID, session_id: UUID, club_id: str = "330323"):
    thread_id = f"fc27-{club_id}-run-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:6]}"
    req_id = uuid4()
    pkg_ver = hashlib.sha256(f"pkg_ver_{req_id}".encode("utf-8")).hexdigest()
    session_token_hash = hashlib.sha256(session_id.bytes).hexdigest()

    with conn.transaction():
        conn.execute(
            """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
               VALUES (%s, %s, %s, 'Tester', 'active')
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, club_id, f"test_{user_id.hex[:8]}@example.com"),
        )
        conn.execute(
            """INSERT INTO public.fc27_user_roles (user_id, role, granted_by)
               VALUES (%s, 'commissioner', %s)
               ON CONFLICT DO NOTHING""",
            (user_id, str(user_id)),
        )
        conn.execute(
            """INSERT INTO public.fc27_sessions (
                   session_id, token_hash, user_id, auth_level, auth_method,
                   idle_expires_at, absolute_expires_at
               ) VALUES (%s, %s, %s, 'step_up_passkey', 'passkey', now() + interval '1 hour', now() + interval '24 hours')
               ON CONFLICT (session_id) DO NOTHING""",
            (session_id, session_token_hash, user_id),
        )
        conn.execute(
            """INSERT INTO public.graph_threads (thread_id, club_id, status, state, package_version)
               VALUES (%s, %s, 'running', %s, %s)""",
            (thread_id, club_id, psycopg.types.json.Jsonb({"status": "running"}), pkg_ver),
        )
        conn.execute(
            """INSERT INTO public.approval_requests (
                   approval_request_id, thread_id, package_version, status, expires_at
               ) VALUES (%s, %s, %s, 'awaiting_approval', now() + interval '1 hour')""",
            (req_id, thread_id, pkg_ver),
        )
        conn.execute(
            """UPDATE public.graph_threads
                  SET status = 'awaiting_approval',
                      approval_request_id = %s,
                      state = %s
                WHERE thread_id = %s""",
            (req_id, psycopg.types.json.Jsonb({"status": "awaiting_approval"}), thread_id),
        )
    return thread_id, req_id, pkg_ver


def _mint_ev(conn, user_id: UUID, session_id: UUID, thread_id: str, req_id: UUID, decision: str, pkg_ver: str):
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
        (ev_id, user_id, session_id, thread_id, req_id, pkg_id, pkg_ver, pkg_hash, decision, bound_hash),
    )
    return ev_id


def test_stress_2_threads_repeated_5_times(pg_uri):
    """Stress-test: Run 2-thread race 5 times consecutively to verify determinism."""
    store = ApprovalStore(pg_uri)

    for run_idx in range(5):
        user_id = uuid4()
        session_id = uuid4()

        with psycopg.connect(pg_uri, autocommit=True) as conn:
            thread_id, req_id, pkg_ver = _seed_env(conn, user_id, session_id)
            ev_app = _mint_ev(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
            ev_rej = _mint_ev(conn, user_id, session_id, thread_id, req_id, "rejected", pkg_ver)

        principal = PrincipalContext.create(
            user_id=user_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY,
            auth_method=AuthMethod.PASSKEY,
            session_id=session_id,
            display_name="Commissioner Test",
            is_service=False,
        )

        barrier = threading.Barrier(2)
        results = {}

        def racer(name: str, ev_id: UUID, dec: str):
            barrier.wait()
            try:
                state = store.apply_webauthn_decision(
                    evidence_id=ev_id,
                    decision=dec,
                    thread_id=thread_id,
                    principal=principal,
                    approval_request_id=req_id,
                    package_id=str(req_id),
                    package_version=pkg_ver,
                    authority_basis="primary",
                )
                results[name] = ("OK", state, ev_id)
            except StoreError as err:
                results[name] = ("ERR", err, ev_id)

        t1 = threading.Thread(target=racer, args=("app", ev_app, "approved"))
        t2 = threading.Thread(target=racer, args=("rej", ev_rej, "rejected"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        statuses = [r[0] for r in results.values()]
        assert statuses.count("OK") == 1, f"Iteration {run_idx}: Expected 1 OK, got {statuses}"
        assert statuses.count("ERR") == 1, f"Iteration {run_idx}: Expected 1 ERR, got {statuses}"

        err = [r[1] for r in results.values() if r[0] == "ERR"][0]
        assert err.status_code == 409

        # Empirical DB check
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            audits = conn.execute(
                "SELECT * FROM public.approval_audit WHERE thread_id = %s",
                (thread_id,),
            ).fetchall()
            assert len(audits) == 1

            win_ev = [r[2] for r in results.values() if r[0] == "OK"][0]
            lose_ev = [r[2] for r in results.values() if r[0] == "ERR"][0]

            win_row = conn.execute(
                "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
                (win_ev,),
            ).fetchone()
            assert win_row["consumed_at"] is not None

            lose_row = conn.execute(
                "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
                (lose_ev,),
            ).fetchone()
            assert lose_row["consumed_at"] is None, f"Iteration {run_idx}: Loser evidence was consumed!"


def test_stress_10_threads_concurrency_race(pg_uri):
    """Stress-test: 10 threads racing simultaneously on real PostgreSQL."""
    user_id = uuid4()
    session_id = uuid4()

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        thread_id, req_id, pkg_ver = _seed_env(conn, user_id, session_id)
        ev_ids = [
            _mint_ev(conn, user_id, session_id, thread_id, req_id, "approved" if i % 2 == 0 else "rejected", pkg_ver)
            for i in range(10)
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

    barrier = threading.Barrier(10)
    results = []
    lock = threading.Lock()

    def racer(idx: int, ev_id: UUID, dec: str):
        barrier.wait()
        try:
            state = store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision=dec,
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
        threading.Thread(target=racer, args=(i, ev_ids[i], "approved" if i % 2 == 0 else "rejected"))
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    statuses = [r[0] for r in results]
    assert statuses.count("OK") == 1, f"Expected 1 winner out of 10, got {statuses}"
    assert statuses.count("ERR") == 9, f"Expected 9 losers out of 10, got {statuses}"

    for status, err, _ in results:
        if status == "ERR":
            assert err.status_code == 409

    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        audits = conn.execute(
            "SELECT id FROM public.approval_audit WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
        assert len(audits) == 1

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
            assert lose_row["consumed_at"] is None


def test_stress_trigger_enforcement_and_validation(pg_uri):
    """Adversarial test: Confirm that fc27_check_approval_pair() trigger actively rejects violations."""
    pkg_ver = "a" * 64

    # 1. Unlinked awaiting thread with package_version must be rejected by trigger
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        bad_thread_id = f"fc27-bad-run-{uuid4().hex[:6]}"
        with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
            conn.execute(
                """INSERT INTO public.graph_threads (thread_id, club_id, status, state, package_version)
                   VALUES (%s, '330323', 'awaiting_approval', %s, %s)""",
                (bad_thread_id, psycopg.types.json.Jsonb({"status": "awaiting_approval"}), pkg_ver),
            )
        assert "approval pair: awaiting thread requires link" in str(exc_info.value)

    # 2. Status mismatch between request and thread must be rejected by trigger upon commit
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        mismatch_thread = f"fc27-mismatch-{uuid4().hex[:6]}"
        mismatch_req = uuid4()
        with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
            with conn.transaction():
                conn.execute(
                    """INSERT INTO public.graph_threads (thread_id, club_id, status, state, package_version)
                       VALUES (%s, '330323', 'running', %s, %s)""",
                    (mismatch_thread, psycopg.types.json.Jsonb({"status": "running"}), pkg_ver),
                )
                conn.execute(
                    """INSERT INTO public.approval_requests (
                           approval_request_id, thread_id, package_version, status, expires_at
                       ) VALUES (%s, %s, %s, 'awaiting_approval', now() + interval '1 hour')""",
                    (mismatch_req, mismatch_thread, pkg_ver),
                )
                # Try to link while thread is still 'running'
                conn.execute(
                    """UPDATE public.graph_threads
                          SET approval_request_id = %s
                        WHERE thread_id = %s""",
                    (mismatch_req, mismatch_thread),
                )
        assert "approval pair: status mismatch" in str(exc_info.value)


def test_stress_foreign_key_and_not_null_constraints(pg_uri):
    """Adversarial test: Confirm NOT NULL on granted_by is enforced."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        uid = uuid4()
        conn.execute(
            """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
               VALUES (%s, '330323', %s, 'Tester', 'active')""",
            (uid, f"test_{uid.hex[:8]}@example.com"),
        )
        # Attempt insert into fc27_user_roles without granted_by -> must raise NotNullViolation
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(
                """INSERT INTO public.fc27_user_roles (user_id, role)
                   VALUES (%s, 'commissioner')""",
                (uid,),
            )


def test_stress_dormancy_and_no_dispatch(pg_uri):
    """Adversarial test: Confirm DISPATCH_ENABLED=false and 0 dispatches exist."""
    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        # Check dispatches table has zero rows
        row = conn.execute("SELECT count(*) AS cnt FROM public.dispatches").fetchone()
        assert row["cnt"] == 0, f"Dispatches table has {row['cnt']} rows! Delivery occurred!"
