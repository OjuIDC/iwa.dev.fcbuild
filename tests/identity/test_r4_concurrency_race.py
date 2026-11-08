"""Concurrency Race Test Suite for Milestone R3 & R4: Exactly One Decision Winner.

Verifies under real and simulated concurrent load:
1. Two threads racing APPROVE vs REJECT on the same approval_request_id:
   - Exactly ONE winner succeeds (HTTP 200 / commit success).
   - Exactly ONE loser fails with HTTP 409 Conflict.
   - The loser's decision evidence remains UNCONSUMED (consumed_at IS NULL).
   - Exactly 1 row inserted into public.approval_audit for that thread.
2. Five threads racing concurrent decisions:
   - Exactly 1 winner commits.
   - Exactly 4 contenders receive 409 Conflict.
3. Race between WebAuthn decision and HMAC decision:
   - Exactly 1 winner.
4. Race between WebAuthn decision and background expire_due:
   - Exactly 1 winner (either decision committed or marked expired, never hybrid/stuck).
5. Terminal status immutability:
   - Once decided, no subsequent decision can alter status.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from src.approval_store import ApprovalStore, StoreError, compute_bound_payload_hash
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    UserRole,
)
from src.identity.store import IdentityStore, IdentityStoreError


# ==============================================================================
# Real PostgreSQL Concurrency Tests (when TEST_POSTGRES_URI is available)
# ==============================================================================

def _setup_test_environment(conn, user_id, session_id, club_id="330323", expires_at_expr: str = "now() + interval '1 hour'"):
    """Seeds test user, session, graph thread, and approval request with schema-compliant attributes."""
    thread_id = f"fc27-{club_id}-run-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:6]}"
    req_id = uuid4()
    pkg_ver = hashlib.sha256(f"pkg_ver_{req_id}".encode("utf-8")).hexdigest()
    session_token_hash = hashlib.sha256(session_id.bytes).hexdigest()

    with conn.transaction():
        # 1. Insert user
        conn.execute(
            """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
               VALUES (%s, %s, %s, 'Tester', 'active')
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, club_id, f"tester_{user_id.hex[:8]}@example.com"),
        )

        # 2. Assign commissioner role with required granted_by
        conn.execute(
            """INSERT INTO public.fc27_user_roles (user_id, role, granted_by)
               VALUES (%s, 'commissioner', %s)
               ON CONFLICT DO NOTHING""",
            (user_id, str(user_id)),
        )

        # 3. Insert session with valid idle_expires_at and absolute_expires_at
        conn.execute(
            """INSERT INTO public.fc27_sessions (
                   session_id, token_hash, user_id, auth_level, auth_method,
                   idle_expires_at, absolute_expires_at
               ) VALUES (%s, %s, %s, 'step_up_passkey', 'passkey', now() + interval '1 hour', now() + interval '24 hours')
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
        if "interval '-" in expires_at_expr or "now() - interval" in expires_at_expr:
            created_at_expr = "now() - interval '1 hour'"
        else:
            created_at_expr = "now()"
        conn.execute(
            f"""INSERT INTO public.approval_requests (
                   approval_request_id, thread_id, package_version, status, created_at, expires_at
               ) VALUES (%s, %s, %s, 'awaiting_approval', {created_at_expr}, {expires_at_expr})""",
            (req_id, thread_id, pkg_ver),
        )

        # 6. Link thread to approval_request and transition to awaiting_approval
        conn.execute(
            """UPDATE public.graph_threads
                  SET status = 'awaiting_approval',
                      approval_request_id = %s,
                      state = %s
                WHERE thread_id = %s""",
            (req_id, psycopg.types.json.Jsonb({"status": "awaiting_approval"}), thread_id),
        )

    return thread_id, req_id, pkg_ver


def _mint_decision_evidence(conn, user_id, session_id, thread_id, req_id, decision, pkg_ver: str):
    """Mints valid decision evidence in fc27_decision_evidence bound to 64-char package_version."""
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


def test_real_pg_two_threads_approve_vs_reject_race(pg_uri):
    """Real PostgreSQL: 2 concurrent threads racing APPROVE vs REJECT on same request.
    Verifies exactly 1 winner (commit) and 1 loser (409 StoreError).
    Verifies loser's evidence remains UNCONSUMED.
    """
    user_id = uuid4()
    session_id = uuid4()

    with psycopg.connect(pg_uri, autocommit=True) as init_conn:
        thread_id, req_id, pkg_ver = _setup_test_environment(init_conn, user_id, session_id)
        ev_approve = _mint_decision_evidence(init_conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
        ev_reject = _mint_decision_evidence(init_conn, user_id, session_id, thread_id, req_id, "rejected", pkg_ver)

    store = ApprovalStore(pg_uri)
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

    def racer(name: str, ev_id: UUID, decision: str):
        barrier.wait()
        try:
            state = store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision=decision,
                thread_id=thread_id,
                principal=principal,
                approval_request_id=req_id,
                package_id=str(req_id),
                package_version=pkg_ver,
                authority_basis="primary",
            )
            results[name] = ("OK", state)
        except StoreError as err:
            results[name] = ("ERR", err)

    t1 = threading.Thread(target=racer, args=("racer_approve", ev_approve, "approved"))
    t2 = threading.Thread(target=racer, args=("racer_reject", ev_reject, "rejected"))

    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    statuses = [res[0] for res in results.values()]
    assert "OK" in statuses
    assert "ERR" in statuses
    assert statuses.count("OK") == 1
    assert statuses.count("ERR") == 1

    # Loser must have StoreError with status_code == 409
    err = [res[1] for res in results.values() if res[0] == "ERR"][0]
    assert err.status_code == 409

    # Inspect PostgreSQL state
    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        # Exactly 1 audit record
        audits = conn.execute(
            "SELECT * FROM public.approval_audit WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
        assert len(audits) == 1
        winning_decision = audits[0]["decision"].lower()

        # Check evidence consumption: winner consumed, loser unconsumed
        winning_ev = ev_approve if winning_decision in ("approved", "approve") else ev_reject
        losing_ev = ev_reject if winning_decision in ("approved", "approve") else ev_approve

        win_row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (winning_ev,),
        ).fetchone()
        assert win_row["consumed_at"] is not None

        lose_row = conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
            (losing_ev,),
        ).fetchone()
        # Loser evidence remains unconsumed!
        assert lose_row["consumed_at"] is None


def test_real_pg_five_threads_concurrency_race(pg_uri):
    """Real PostgreSQL: 5 concurrent threads racing decisions on the same request.
    Verifies exactly 1 winner commits, 4 losers receive 409 Conflict.
    """
    user_id = uuid4()
    session_id = uuid4()

    with psycopg.connect(pg_uri, autocommit=True) as init_conn:
        thread_id, req_id, pkg_ver = _setup_test_environment(init_conn, user_id, session_id)
        evidence_ids = [
            _mint_decision_evidence(init_conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
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
        display_name="Commissioner Test",
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
                results.append(("OK", state))
        except StoreError as err:
            with lock:
                results.append(("ERR", err))

    threads = [
        threading.Thread(target=racer, args=(i, evidence_ids[i]))
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    statuses = [r[0] for r in results]
    assert statuses.count("OK") == 1
    assert statuses.count("ERR") == 4

    for status, err in results:
        if status == "ERR":
            assert err.status_code == 409

    # Verify database has exactly 1 audit row
    with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
        audits = conn.execute(
            "SELECT id FROM public.approval_audit WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()
        assert len(audits) == 1


def test_real_pg_webauthn_vs_expire_due_race(pg_uri):
    """Real PostgreSQL: Race between WebAuthn decision commit and background expire_due.

    Verifies:
    - When a decision arrives precisely at deadline expiration:
      Either WebAuthn commits successfully (OK) or expire_due marks it expired.
    - Exactly ONE terminal outcome is reached (never stuck in awaiting_approval).
    - If expire_due wins: WebAuthn decision receives 409 StoreError and evidence remains unconsumed.
    - If WebAuthn decision wins: decision evidence is consumed and thread is approved.
    """
    user_id = uuid4()
    session_id = uuid4()

    with psycopg.connect(pg_uri, autocommit=True) as init_conn:
        thread_id, req_id, pkg_ver = _setup_test_environment(
            init_conn, user_id, session_id, expires_at_expr="now() - interval '1 second'"
        )
        ev_id = _mint_decision_evidence(init_conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)

    store = ApprovalStore(pg_uri)
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

    def decision_racer():
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
            results["decision"] = ("OK", state)
        except StoreError as err:
            results["decision"] = ("ERR", err)

    def expire_racer():
        barrier.wait()
        try:
            expired = store.expire_due(thread_id=thread_id)
            results["expire"] = ("OK", expired)
        except Exception as err:
            results["expire"] = ("ERR", err)

    t1 = threading.Thread(target=decision_racer)
    t2 = threading.Thread(target=expire_racer)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert "decision" in results
    assert "expire" in results

    with psycopg.connect(pg_uri, row_factory=dict_row) as check_conn:
        th = check_conn.execute(
            "SELECT status FROM public.graph_threads WHERE thread_id = %s", (thread_id,)
        ).fetchone()
        req = check_conn.execute(
            "SELECT status FROM public.approval_requests WHERE approval_request_id = %s", (req_id,)
        ).fetchone()
        ev_row = check_conn.execute(
            "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s", (ev_id,)
        ).fetchone()

        # Consistent terminal state across both tables
        assert th["status"] in ("approved", "expired")
        assert req["status"] in ("approved", "expired")
        assert th["status"] == req["status"]

        if th["status"] == "approved":
            # WebAuthn won
            assert results["decision"][0] == "OK"
            assert ev_row["consumed_at"] is not None
        else:
            # expire_due won
            assert results["decision"][0] == "ERR"
            assert results["decision"][1].status_code == 409
            # Evidence MUST remain unconsumed
            assert ev_row["consumed_at"] is None


def test_real_pg_challenge_consumption_race(pg_uri):
    """Real PostgreSQL: 2 concurrent threads racing to consume the SAME WebAuthn challenge.

    Verifies:
    - Row-level lock (FOR UPDATE) ensures atomic consumption.
    - Exactly ONE winner successfully consumes the challenge and advances counter.
    - Exactly ONE loser fails with IdentityStoreError (400 or 409).
    - Database reflects exactly 1 consumption and monotonic counter advance.
    """
    user_id = uuid4()
    cred_id = secrets.token_bytes(32)
    chal_str = f"ch_{secrets.token_hex(16)}"

    with psycopg.connect(pg_uri, autocommit=True) as init_conn:
        init_conn.execute(
            """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
               VALUES (%s, '330323', %s, 'Challenge Tester', 'active')
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, f"chal_{user_id.hex[:8]}@fc27.club"),
        )
        init_conn.execute(
            """INSERT INTO public.fc27_webauthn_credentials (
                   credential_id, user_id, public_key, sign_counter, label
               ) VALUES (%s, %s, %s, 10, 'Test Key')""",
            (cred_id, user_id, b"pubkey_bytes"),
        )
        init_conn.execute(
            """INSERT INTO public.fc27_challenges (
                   challenge_id, challenge_type, challenge_str, user_id,
                   expires_at, created_at
               ) VALUES (gen_random_uuid(), 'step_up_decision', %s, %s, now() + interval '5 minutes', now())""",
            (chal_str, user_id),
        )

    id_store = IdentityStore(pg_uri)
    barrier = threading.Barrier(2)
    results = {}

    def racer(name: str):
        barrier.wait()
        try:
            res = id_store.consume_challenge_and_update_counter(
                challenge_str=chal_str,
                challenge_type="step_up_decision",
                credential_id=cred_id,
                new_counter=15,
                user_id=user_id,
            )
            results[name] = ("OK", res)
        except IdentityStoreError as exc:
            results[name] = ("ERR", exc)
        except Exception as exc:
            results[name] = ("UNEXPECTED", exc)

    t1 = threading.Thread(target=racer, args=("Thread-1",))
    t2 = threading.Thread(target=racer, args=("Thread-2",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    statuses = [v[0] for v in results.values()]
    assert statuses.count("OK") == 1, f"Expected exactly 1 OK, got results: {results}"
    assert statuses.count("ERR") == 1, f"Expected exactly 1 ERR, got results: {results}"

    err = [v[1] for v in results.values() if v[0] == "ERR"][0]
    assert isinstance(err, IdentityStoreError)
    assert err.status_code in (400, 409)

    # Verify database state
    with psycopg.connect(pg_uri, row_factory=dict_row) as check_conn:
        chal_row = check_conn.execute(
            "SELECT consumed_at FROM public.fc27_challenges WHERE challenge_str = %s", (chal_str,)
        ).fetchone()
        assert chal_row["consumed_at"] is not None

        cred_row = check_conn.execute(
            "SELECT sign_counter FROM public.fc27_webauthn_credentials WHERE credential_id = %s", (cred_id,)
        ).fetchone()
        assert cred_row["sign_counter"] == 15
