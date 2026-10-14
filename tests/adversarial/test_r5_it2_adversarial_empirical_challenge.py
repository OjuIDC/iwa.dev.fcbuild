"""Empirical Adversarial Challenge Test Suite for Milestone R5 Iteration 2.

Authoritative Mission:
Adversarially challenge Milestone R5 Iteration 2 concurrency races and trigger compliance
on branch fix/phase2-identity-victory-remediation on live PostgreSQL.

Verification Dimensions:
1. Concurrency Race Defense on Real PostgreSQL:
   - 2-thread and 5-thread and 10-thread decision races on approval_requests:
     Verify exactly 1 winner commits, N-1 losers get HTTP 409 Conflict with unconsumed evidence.
   - Player decision races: 2-thread and 5-thread races:
     Verify exactly 1 winner commits, losers get 409 Conflict.
   - WebAuthn vs expire_due race:
     Verify exactly 1 winner commits and trigger fc27_guard_approval_request_transition() is not violated.
   - WebAuthn challenge consumption race: 2-thread and 5-thread races:
     Verify exactly 1 winner advances sign counter and losers get 400/409.
2. Trigger Compliance & Active Trigger Proof:
   - Verify fc27_check_approval_pair() is genuinely satisfied in step-up lifecycle setup.
   - Adversarially verify fc27_check_approval_pair() and fc27_guard_approval_request_transition()
     are active and strictly enforce constraints (raising CheckViolation on violations).
3. Dormancy and Zero Delivery Invariants:
   - DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false, zero player delivery.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import psycopg.errors
import pytest
from psycopg.rows import dict_row

from src.approval_store import ApprovalStore, StoreError, compute_bound_payload_hash
from src.identity.crypto import hash_token
from src.identity.policy import (
    ActionType,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    UserRole,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import ScopedActionTokenEngine


# ==============================================================================
# Helper Functions for DB Seeding
# ==============================================================================

def _setup_approval_env(conn, user_id: UUID, session_id: UUID, club_id: str = "330323", expires_at_expr: str = "now() + interval '1 hour'"):
    """Seeds test user, session, graph thread, and approval request with schema-compliant attributes."""
    thread_id = f"fc27-{club_id}-adv-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:6]}"
    req_id = uuid4()
    pkg_ver = hashlib.sha256(f"pkg_ver_{req_id}".encode("utf-8")).hexdigest()
    session_token_hash = hashlib.sha256(session_id.bytes).hexdigest()

    with conn.transaction():
        # User
        conn.execute(
            """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
               VALUES (%s, %s, %s, 'Adv Tester', 'active')
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, club_id, f"adv_{user_id.hex[:8]}@example.com"),
        )
        # Role
        conn.execute(
            """INSERT INTO public.fc27_user_roles (user_id, role, granted_by)
               VALUES (%s, 'commissioner', %s)
               ON CONFLICT DO NOTHING""",
            (user_id, str(user_id)),
        )
        # Session
        conn.execute(
            """INSERT INTO public.fc27_sessions (
                   session_id, token_hash, user_id, auth_level, auth_method,
                   idle_expires_at, absolute_expires_at
               ) VALUES (%s, %s, %s, 'step_up_passkey', 'passkey', now() + interval '1 hour', now() + interval '24 hours')
               ON CONFLICT (session_id) DO NOTHING""",
            (session_id, session_token_hash, user_id),
        )
        # Graph thread (running)
        conn.execute(
            """INSERT INTO public.graph_threads (thread_id, club_id, status, state, package_version)
               VALUES (%s, %s, 'running', %s, %s)""",
            (thread_id, club_id, psycopg.types.json.Jsonb({"status": "running"}), pkg_ver),
        )
        # Approval request
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
        # Link thread to approval_request
        conn.execute(
            """UPDATE public.graph_threads
                  SET status = 'awaiting_approval',
                      approval_request_id = %s,
                      state = %s
                WHERE thread_id = %s""",
            (req_id, psycopg.types.json.Jsonb({"status": "awaiting_approval"}), thread_id),
        )

    return thread_id, req_id, pkg_ver


def _mint_evidence(conn, user_id: UUID, session_id: UUID, thread_id: str, req_id: UUID, decision: str, pkg_ver: str) -> UUID:
    """Mints valid decision evidence in public.fc27_decision_evidence."""
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


def _seed_player_env(conn, club_id: str = "330323", player_id: str = "99001"):
    """Seeds test user, assignment, and player decision request."""
    user_id = uuid4()
    aid = uuid4()
    req_id = uuid4()
    pkg_ver = hashlib.sha256(f"pkg_ver_{aid}".encode()).hexdigest()
    pkg_hash = hashlib.sha256(b"pkg_hash_adv").hexdigest()
    assignment_hash = hashlib.sha256(b"assign_hash_adv").hexdigest()

    with conn.transaction():
        conn.execute(
            """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
               VALUES (%s, %s, %s, 'Adv Player', 'active')
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, club_id, f"adv_player_{user_id.hex[:8]}@example.com"),
        )
        conn.execute(
            """INSERT INTO public.fc27_user_roles (user_id, role, granted_by)
               VALUES (%s, 'player', %s)
               ON CONFLICT DO NOTHING""",
            (user_id, str(user_id)),
        )
        conn.execute(
            """INSERT INTO public.player_assignments (
                   assignment_id, club_id, subject_player_id, action_type,
                   package_id, package_version, package_hash,
                   assignment_version, assignment_hash, status, created_at, updated_at
               ) VALUES (
                   %s, %s, %s, 'personal_training',
                   'pkg_adv', %s, %s,
                   1, %s, 'active', now(), now()
               )""",
            (aid, club_id, player_id, pkg_ver, pkg_hash, assignment_hash),
        )
        conn.execute(
            """INSERT INTO public.player_decision_requests (
                   request_id, assignment_id, subject_player_id,
                   status, expires_at, created_at
               ) VALUES (
                   %s, %s, %s, 'awaiting_player', now() + interval '10 minutes', now()
               )""",
            (req_id, aid, player_id),
        )

    return user_id, aid, req_id, pkg_ver, pkg_hash, assignment_hash


def _mint_sat_token(conn, user_id: UUID, assignment_id: UUID, scope: str = "player_decision") -> tuple[str, UUID]:
    """Mints an action token into public.fc27_action_tokens."""
    token_id = uuid4()
    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    tok_hash = hash_token(raw_token)
    with conn.transaction():
        conn.execute(
            """INSERT INTO public.fc27_action_tokens (
                   token_id, token_hash, user_id, assignment_id, scope,
                   expires_at, created_at
               ) VALUES (%s, %s, %s, %s, %s, now() + interval '1 hour', now())""",
            (token_id, tok_hash, user_id, assignment_id, scope),
        )
    return raw_token, token_id


# ==============================================================================
# Challenge 1: Approval Request Concurrency Races (2, 5, 10 Threads)
# ==============================================================================

class TestEmpiricalApprovalRequestConcurrency:
    """Adversarial stress testing of ApprovalStore.apply_webauthn_decision concurrency."""

    def test_challenge_2_thread_approve_vs_reject_race(self, pg_uri):
        """Two concurrent threads racing APPROVE vs REJECT on identical approval request."""
        user_id = uuid4()
        session_id = uuid4()

        with psycopg.connect(pg_uri, autocommit=True) as init_conn:
            thread_id, req_id, pkg_ver = _setup_approval_env(init_conn, user_id, session_id)
            ev_app = _mint_evidence(init_conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
            ev_rej = _mint_evidence(init_conn, user_id, session_id, thread_id, req_id, "rejected", pkg_ver)

        store = ApprovalStore(pg_uri)
        principal = PrincipalContext.create(
            user_id=user_id, club_id="330323", roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY, auth_method=AuthMethod.PASSKEY,
            session_id=session_id, display_name="Adv Commissioner", is_service=False,
        )

        barrier = threading.Barrier(2)
        results = {}

        def racer(name: str, ev_id: UUID, dec: str):
            barrier.wait()
            try:
                res = store.apply_webauthn_decision(
                    evidence_id=ev_id, decision=dec, thread_id=thread_id,
                    principal=principal, approval_request_id=req_id,
                    package_id=str(req_id), package_version=pkg_ver, authority_basis="primary",
                )
                results[name] = ("WIN", res)
            except StoreError as exc:
                results[name] = ("LOSS", exc)

        t1 = threading.Thread(target=racer, args=("T_APPROVE", ev_app, "approved"))
        t2 = threading.Thread(target=racer, args=("T_REJECT", ev_rej, "rejected"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        statuses = [v[0] for v in results.values()]
        assert statuses.count("WIN") == 1, f"Expected 1 winner, got {results}"
        assert statuses.count("LOSS") == 1, f"Expected 1 loser, got {results}"

        loser_err = [v[1] for v in results.values() if v[0] == "LOSS"][0]
        assert loser_err.status_code == 409

        # Database verification: loser evidence unconsumed, winner consumed
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            audits = conn.execute("SELECT * FROM public.approval_audit WHERE thread_id = %s", (thread_id,)).fetchall()
            assert len(audits) == 1
            winning_dec = audits[0]["decision"].lower()

            win_ev = ev_app if winning_dec in ("approved", "approve") else ev_rej
            lose_ev = ev_rej if winning_dec in ("approved", "approve") else ev_app

            assert conn.execute("SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s", (win_ev,)).fetchone()["consumed_at"] is not None
            assert conn.execute("SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s", (lose_ev,)).fetchone()["consumed_at"] is None

    def test_challenge_5_thread_mixed_decision_race(self, pg_uri):
        """Five concurrent threads with mixed decisions (3 approve, 2 reject) racing simultaneously."""
        user_id = uuid4()
        session_id = uuid4()

        with psycopg.connect(pg_uri, autocommit=True) as init_conn:
            thread_id, req_id, pkg_ver = _setup_approval_env(init_conn, user_id, session_id)
            evs = [
                (
                    _mint_evidence(init_conn, user_id, session_id, thread_id, req_id, "approved" if i % 2 == 0 else "rejected", pkg_ver),
                    "approved" if i % 2 == 0 else "rejected",
                )
                for i in range(5)
            ]

        store = ApprovalStore(pg_uri)
        principal = PrincipalContext.create(
            user_id=user_id, club_id="330323", roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY, auth_method=AuthMethod.PASSKEY,
            session_id=session_id, display_name="Adv Commissioner", is_service=False,
        )

        barrier = threading.Barrier(5)
        results = []
        lock = threading.Lock()

        def racer(idx: int, ev_id: UUID, dec: str):
            barrier.wait()
            try:
                res = store.apply_webauthn_decision(
                    evidence_id=ev_id, decision=dec, thread_id=thread_id,
                    principal=principal, approval_request_id=req_id,
                    package_id=str(req_id), package_version=pkg_ver, authority_basis="primary",
                )
                with lock:
                    results.append(("WIN", ev_id, res))
            except StoreError as exc:
                with lock:
                    results.append(("LOSS", ev_id, exc))

        threads = [
            threading.Thread(target=racer, args=(i, evs[i][0], evs[i][1]))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        statuses = [r[0] for r in results]
        assert statuses.count("WIN") == 1, f"Expected 1 winner, got {results}"
        assert statuses.count("LOSS") == 4, f"Expected 4 losers, got {results}"

        for status, _, err in results:
            if status == "LOSS":
                assert err.status_code == 409

        # Verify winner consumed, 4 losers unconsumed
        winning_ev_id = [r[1] for r in results if r[0] == "WIN"][0]
        losing_ev_ids = [r[1] for r in results if r[0] == "LOSS"]

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            win_row = conn.execute("SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s", (winning_ev_id,)).fetchone()
            assert win_row["consumed_at"] is not None

            for lose_id in losing_ev_ids:
                lose_row = conn.execute("SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s", (lose_id,)).fetchone()
                assert lose_row["consumed_at"] is None, f"Loser evidence {lose_id} was improperly consumed!"

            audits = conn.execute("SELECT id FROM public.approval_audit WHERE thread_id = %s", (thread_id,)).fetchall()
            assert len(audits) == 1

    def test_challenge_10_thread_extreme_contention_race(self, pg_uri):
        """Ten concurrent threads racing decisions on the same approval request."""
        user_id = uuid4()
        session_id = uuid4()

        with psycopg.connect(pg_uri, autocommit=True) as init_conn:
            thread_id, req_id, pkg_ver = _setup_approval_env(init_conn, user_id, session_id)
            evs = [
                _mint_evidence(init_conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
                for _ in range(10)
            ]

        store = ApprovalStore(pg_uri)
        principal = PrincipalContext.create(
            user_id=user_id, club_id="330323", roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY, auth_method=AuthMethod.PASSKEY,
            session_id=session_id, display_name="Adv Commissioner", is_service=False,
        )

        barrier = threading.Barrier(10)
        results = []
        lock = threading.Lock()

        def racer(idx: int, ev_id: UUID):
            barrier.wait()
            try:
                res = store.apply_webauthn_decision(
                    evidence_id=ev_id, decision="approved", thread_id=thread_id,
                    principal=principal, approval_request_id=req_id,
                    package_id=str(req_id), package_version=pkg_ver, authority_basis="primary",
                )
                with lock:
                    results.append(("WIN", ev_id, res))
            except StoreError as exc:
                with lock:
                    results.append(("LOSS", ev_id, exc))

        threads = [threading.Thread(target=racer, args=(i, evs[i])) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        statuses = [r[0] for r in results]
        assert statuses.count("WIN") == 1
        assert statuses.count("LOSS") == 9

        for status, _, err in results:
            if status == "LOSS":
                assert err.status_code == 409

        # Verify DB integrity: exactly 1 audit row, exactly 1 consumed evidence, 9 unconsumed
        winning_ev_id = [r[1] for r in results if r[0] == "WIN"][0]
        losing_ev_ids = [r[1] for r in results if r[0] == "LOSS"]

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            win_row = conn.execute("SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s", (winning_ev_id,)).fetchone()
            assert win_row["consumed_at"] is not None

            for lose_id in losing_ev_ids:
                lose_row = conn.execute("SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s", (lose_id,)).fetchone()
                assert lose_row["consumed_at"] is None

            audits = conn.execute("SELECT id FROM public.approval_audit WHERE thread_id = %s", (thread_id,)).fetchall()
            assert len(audits) == 1


# ==============================================================================
# Challenge 2: Player Decision & Action Token Concurrency Races
# ==============================================================================

class TestEmpiricalPlayerDecisionConcurrency:
    """Adversarial stress testing of player decisions and single-use action tokens."""

    def test_challenge_2_thread_player_decision_race(self, pg_uri):
        """Two threads racing ACCEPT vs REJECT on same player decision request."""
        with psycopg.connect(pg_uri, autocommit=True) as init_conn:
            user_id, aid, req_id, pkg_ver, pkg_hash, assignment_hash = _seed_player_env(init_conn)
            raw_tok_1, tok_id_1 = _mint_sat_token(init_conn, user_id, aid)
            raw_tok_2, tok_id_2 = _mint_sat_token(init_conn, user_id, aid)

        store = IdentityStore(pg_uri)
        barrier = threading.Barrier(2)
        results = {}

        def racer(name: str, dec: str, tok_id: UUID):
            barrier.wait()
            try:
                res = store.record_player_decision(
                    request_id=req_id, assignment_id=aid, subject_player_id="99001",
                    actor_id="99001", decision=dec, authority_basis="affected_player_consent",
                    assignment_version=1, assignment_hash=assignment_hash,
                    package_id="pkg_adv", package_version=pkg_ver, package_hash=pkg_hash,
                    notes=f"Race {name}", action_token_reference=str(tok_id), actor="99001",
                )
                results[name] = ("WIN", res)
            except IdentityStoreError as exc:
                results[name] = ("LOSS", exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(racer, "T1", "accept", tok_id_1)
            f2 = ex.submit(racer, "T2", "reject", tok_id_2)
            f1.result()
            f2.result()

        statuses = [v[0] for v in results.values()]
        assert statuses.count("WIN") == 1
        assert statuses.count("LOSS") == 1

        loser_err = [v[1] for v in results.values() if v[0] == "LOSS"][0]
        assert loser_err.status_code == 409
        assert "already in status 'decided'" in loser_err.detail or "already decided" in loser_err.detail

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            decs = conn.execute("SELECT * FROM public.player_decisions WHERE request_id = %s", (str(req_id),)).fetchall()
            assert len(decs) == 1
            req_row = conn.execute("SELECT status, decided_at FROM public.player_decision_requests WHERE request_id = %s", (str(req_id),)).fetchone()
            assert req_row["status"] == "decided"
            assert req_row["decided_at"] is not None

    def test_challenge_5_thread_player_decision_race(self, pg_uri):
        """Five threads racing on same player decision request."""
        with psycopg.connect(pg_uri, autocommit=True) as init_conn:
            user_id, aid, req_id, pkg_ver, pkg_hash, assignment_hash = _seed_player_env(init_conn)
            tokens = [_mint_sat_token(init_conn, user_id, aid) for _ in range(5)]

        store = IdentityStore(pg_uri)
        barrier = threading.Barrier(5)
        results = []
        lock = threading.Lock()

        def racer(idx: int, tok_id: UUID):
            barrier.wait()
            try:
                res = store.record_player_decision(
                    request_id=req_id, assignment_id=aid, subject_player_id="99001",
                    actor_id="99001", decision="accept" if idx % 2 == 0 else "reject",
                    authority_basis="affected_player_consent", assignment_version=1,
                    assignment_hash=assignment_hash, package_id="pkg_adv",
                    package_version=pkg_ver, package_hash=pkg_hash,
                    notes=f"Race {idx}", action_token_reference=str(tok_id), actor="99001",
                )
                with lock:
                    results.append(("WIN", res))
            except IdentityStoreError as exc:
                with lock:
                    results.append(("LOSS", exc))

        threads = [threading.Thread(target=racer, args=(i, tokens[i][1])) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        statuses = [r[0] for r in results]
        assert statuses.count("WIN") == 1
        assert statuses.count("LOSS") == 4

        for status, err in results:
            if status == "LOSS":
                assert err.status_code == 409

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            decs = conn.execute("SELECT * FROM public.player_decisions WHERE request_id = %s", (str(req_id),)).fetchall()
            assert len(decs) == 1

    def test_challenge_5_thread_action_token_single_use_race(self, pg_uri):
        """Five threads racing to consume the SAME action token concurrently."""
        with psycopg.connect(pg_uri, autocommit=True) as init_conn:
            user_id, aid, *_ = _seed_player_env(init_conn)
            raw_tok, tok_id = _mint_sat_token(init_conn, user_id, aid)

        store = IdentityStore(pg_uri)
        barrier = threading.Barrier(5)
        results = []
        lock = threading.Lock()

        def consumer(idx: int):
            barrier.wait()
            try:
                res = store.consume_action_token(
                    raw_tok, expected_scope="player_decision",
                    expected_user_id=user_id, expected_assignment_id=aid, actor="99001",
                )
                with lock:
                    results.append(("WIN", res))
            except IdentityStoreError as exc:
                with lock:
                    results.append(("LOSS", exc))

        threads = [threading.Thread(target=consumer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        statuses = [r[0] for r in results]
        assert statuses.count("WIN") == 1, f"Expected 1 winner, got {results}"
        assert statuses.count("LOSS") == 4, f"Expected 4 losers, got {results}"

        for status, err in results:
            if status == "LOSS":
                assert err.status_code == 409
                assert "already consumed" in err.detail.lower()

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            tok_row = conn.execute("SELECT consumed_at FROM public.fc27_action_tokens WHERE token_id = %s", (tok_id,)).fetchone()
            assert tok_row["consumed_at"] is not None


# ==============================================================================
# Challenge 3: WebAuthn Decision vs expire_due Race & Trigger Non-Violation
# ==============================================================================

class TestEmpiricalWebAuthnVsExpireDueRace:
    """Adversarial stress testing of race between WebAuthn decision commit and background expire_due."""

    def test_challenge_webauthn_vs_expire_due_subsecond_race(self, pg_uri):
        """Race at subsecond expiration boundary: verify fc27_guard_approval_request_transition is not violated."""
        user_id = uuid4()
        session_id = uuid4()

        with psycopg.connect(pg_uri, autocommit=True) as init_conn:
            # Seed request with expires_at in the past without executing illegal UPDATE
            thread_id, req_id, pkg_ver = _setup_approval_env(
                init_conn, user_id, session_id, expires_at_expr="now() - interval '1 second'"
            )
            ev_id = _mint_evidence(init_conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)

        store = ApprovalStore(pg_uri)
        principal = PrincipalContext.create(
            user_id=user_id, club_id="330323", roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY, auth_method=AuthMethod.PASSKEY,
            session_id=session_id, display_name="Adv Commissioner", is_service=False,
        )

        barrier = threading.Barrier(2)
        results = {}

        def decision_racer():
            barrier.wait()
            try:
                state = store.apply_webauthn_decision(
                    evidence_id=ev_id, decision="approved", thread_id=thread_id,
                    principal=principal, approval_request_id=req_id,
                    package_id=str(req_id), package_version=pkg_ver, authority_basis="primary",
                )
                results["decision"] = ("OK", state)
            except StoreError as err:
                results["decision"] = ("ERR", err)
            except psycopg.errors.CheckViolation as err:
                results["decision"] = ("TRIGGER_VIOLATION", err)

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

        assert results["decision"][0] != "TRIGGER_VIOLATION", f"fc27_guard_approval_request_transition violated: {results['decision'][1]}"

        # Exactly 1 consistent terminal outcome
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            th = conn.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (thread_id,)).fetchone()
            req = conn.execute("SELECT status FROM public.approval_requests WHERE approval_request_id = %s", (req_id,)).fetchone()
            ev_row = conn.execute("SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s", (ev_id,)).fetchone()

            assert th["status"] in ("approved", "expired")
            assert req["status"] in ("approved", "expired")
            assert th["status"] == req["status"], f"Status mismatch: thread={th['status']}, req={req['status']}"

            if th["status"] == "approved":
                assert results["decision"][0] == "OK"
                assert ev_row["consumed_at"] is not None
            else:
                assert results["decision"][0] == "ERR"
                assert results["decision"][1].status_code == 409
                assert ev_row["consumed_at"] is None

    def test_challenge_webauthn_vs_expire_due_multiple_trials(self, pg_uri):
        """Execute 3 consecutive trials to stress test race branches and assert zero trigger violations."""
        for trial in range(3):
            user_id = uuid4()
            session_id = uuid4()

            with psycopg.connect(pg_uri, autocommit=True) as init_conn:
                thread_id, req_id, pkg_ver = _setup_approval_env(
                    init_conn, user_id, session_id, expires_at_expr="now() - interval '500 milliseconds'"
                )
                ev_id = _mint_evidence(init_conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)

            store = ApprovalStore(pg_uri)
            principal = PrincipalContext.create(
                user_id=user_id, club_id="330323", roles=[UserRole.COMMISSIONER],
                auth_level=AuthLevel.STEP_UP_PASSKEY, auth_method=AuthMethod.PASSKEY,
                session_id=session_id, display_name=f"Trial {trial}", is_service=False,
            )

            barrier = threading.Barrier(2)
            results = {}

            def decision_racer():
                barrier.wait()
                try:
                    res = store.apply_webauthn_decision(
                        evidence_id=ev_id, decision="approved", thread_id=thread_id,
                        principal=principal, approval_request_id=req_id,
                        package_id=str(req_id), package_version=pkg_ver, authority_basis="primary",
                    )
                    results["decision"] = ("OK", res)
                except Exception as err:
                    results["decision"] = ("ERR", err)

            def expire_racer():
                barrier.wait()
                try:
                    res = store.expire_due(thread_id=thread_id)
                    results["expire"] = ("OK", res)
                except Exception as err:
                    results["expire"] = ("ERR", err)

            t1 = threading.Thread(target=decision_racer)
            t2 = threading.Thread(target=expire_racer)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            # Assert no trigger violation
            assert not isinstance(results.get("decision", [None, None])[1], psycopg.errors.CheckViolation)

            with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
                th = conn.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (thread_id,)).fetchone()
                req = conn.execute("SELECT status FROM public.approval_requests WHERE approval_request_id = %s", (req_id,)).fetchone()
                assert th["status"] in ("approved", "expired")
                assert th["status"] == req["status"]


# ==============================================================================
# Challenge 4: WebAuthn Challenge Consumption & Counter Monotonicity Race
# ==============================================================================

class TestEmpiricalWebAuthnChallengeRace:
    """Adversarial stress testing of atomic WebAuthn challenge consumption and counter advance."""

    def test_challenge_5_thread_webauthn_challenge_race(self, pg_uri):
        """Five threads racing to consume the SAME WebAuthn challenge. Exactly 1 wins, 4 get 400."""
        user_id = uuid4()
        cred_id = secrets.token_bytes(32)
        chal_str = f"ch_race_{secrets.token_hex(16)}"

        with psycopg.connect(pg_uri, autocommit=True) as init_conn:
            init_conn.execute(
                """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
                   VALUES (%s, '330323', %s, 'Chal Tester', 'active')
                   ON CONFLICT (user_id) DO NOTHING""",
                (user_id, f"chal_{user_id.hex[:8]}@fc27.club"),
            )
            init_conn.execute(
                """INSERT INTO public.fc27_webauthn_credentials (
                       credential_id, user_id, public_key, sign_counter, label
                   ) VALUES (%s, %s, %s, 100, 'Test Key')""",
                (cred_id, user_id, b"pubkey_bytes"),
            )
            init_conn.execute(
                """INSERT INTO public.fc27_challenges (
                       challenge_id, challenge_type, challenge_str, user_id,
                       expires_at, created_at
                   ) VALUES (gen_random_uuid(), 'step_up_decision', %s, %s, now() + interval '5 minutes', now())""",
                (chal_str, user_id),
            )

        store = IdentityStore(pg_uri)
        barrier = threading.Barrier(5)
        results = []
        lock = threading.Lock()

        def racer(idx: int):
            barrier.wait()
            try:
                res = store.consume_challenge_and_update_counter(
                    challenge_str=chal_str, challenge_type="step_up_decision",
                    credential_id=cred_id, new_counter=101, user_id=user_id,
                )
                with lock:
                    results.append(("WIN", res))
            except IdentityStoreError as exc:
                with lock:
                    results.append(("LOSS", exc))

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        statuses = [r[0] for r in results]
        assert statuses.count("WIN") == 1, f"Expected 1 winner, got {results}"
        assert statuses.count("LOSS") == 4, f"Expected 4 losers, got {results}"

        for status, err in results:
            if status == "LOSS":
                assert err.status_code in (400, 409)

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            chal_row = conn.execute("SELECT consumed_at FROM public.fc27_challenges WHERE challenge_str = %s", (chal_str,)).fetchone()
            assert chal_row["consumed_at"] is not None

            cred_row = conn.execute("SELECT sign_counter FROM public.fc27_webauthn_credentials WHERE credential_id = %s", (cred_id,)).fetchone()
            assert cred_row["sign_counter"] == 101, f"Expected sign_counter=101, got {cred_row['sign_counter']}"


# ==============================================================================
# Challenge 5: Trigger Compliance in Step-Up & Active Constraint Verification
# ==============================================================================

class TestTriggerComplianceAndActiveConstraints:
    """Verifies that fc27_check_approval_pair() is genuinely active and enforced."""

    def test_step_up_lifecycle_satisfies_paired_trigger(self, pg_uri):
        """Verifies atomic transaction setup in test_step_up.py cleanly satisfies deferred trigger."""
        uid = uuid4()
        session_id = uuid4()
        thread_id = f"fc27-330323-run-stepup-{uuid4().hex[:6]}"
        req_id = uuid4()
        pkg_ver = hashlib.sha256(f"ver_{req_id}".encode()).hexdigest()

        # Execute setup wrapped inside transaction
        with psycopg.connect(pg_uri) as conn:
            with conn.transaction():
                conn.execute(
                    """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
                       VALUES (%s, '330323', %s, 'StepUp User', 'active') ON CONFLICT DO NOTHING""",
                    (uid, f"stepup_{uid.hex[:6]}@fc27.club"),
                )
                conn.execute(
                    """INSERT INTO public.fc27_sessions (
                           session_id, token_hash, user_id, auth_level, auth_method,
                           idle_expires_at, absolute_expires_at
                       ) VALUES (%s, %s, %s, 'step_up_passkey', 'passkey', now() + interval '1 hour', now() + interval '8 hours')""",
                    (session_id, secrets.token_hex(32), uid),
                )
                conn.execute(
                    """INSERT INTO public.graph_threads (thread_id, club_id, state, status, package_version)
                       VALUES (%s, '330323', '{}'::jsonb, 'running', %s)""",
                    (thread_id, pkg_ver),
                )
                conn.execute(
                    """INSERT INTO public.approval_requests (
                           approval_request_id, thread_id, package_version, status, created_at, expires_at
                       ) VALUES (%s, %s, %s, 'awaiting_approval', now(), now() + interval '1 hour')""",
                    (req_id, thread_id, pkg_ver),
                )
                conn.execute(
                    """UPDATE public.graph_threads
                          SET status = 'awaiting_approval',
                              approval_request_id = %s
                        WHERE thread_id = %s""",
                    (req_id, thread_id),
                )

        # Confirm rows committed cleanly
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            gt = conn.execute("SELECT status, approval_request_id FROM public.graph_threads WHERE thread_id = %s", (thread_id,)).fetchone()
            ar = conn.execute("SELECT status FROM public.approval_requests WHERE approval_request_id = %s", (req_id,)).fetchone()
            assert gt["status"] == "awaiting_approval"
            assert ar["status"] == "awaiting_approval"
            assert gt["approval_request_id"] == req_id

    def test_adversarial_mutation_proves_trigger_active(self, pg_uri):
        """Proves fc27_check_approval_pair() is genuinely active and raises CheckViolation on violations."""
        thread_id = f"fc27-330323-adv-trigger-{uuid4().hex[:6]}"
        pkg_ver = hashlib.sha256(b"trigger_test").hexdigest()

        # Violation Case 1: An awaiting_approval thread with package_version but NO approval_request_id
        with pytest.raises(psycopg.errors.CheckViolation) as exc_v1:
            with psycopg.connect(pg_uri) as conn:
                with conn.transaction():
                    conn.execute(
                        """INSERT INTO public.graph_threads (thread_id, club_id, state, status, package_version)
                           VALUES (%s, '330323', '{}'::jsonb, 'awaiting_approval', %s)""",
                        (thread_id, pkg_ver),
                    )
        assert "approval pair: awaiting thread requires link" in str(exc_v1.value)

        # Violation Case 2: Attempting to update expires_at raises CheckViolation from fc27_guard_approval_request_transition()
        user_id = uuid4()
        session_id = uuid4()
        with psycopg.connect(pg_uri, autocommit=True) as conn:
            tid, rid, pver = _setup_approval_env(conn, user_id, session_id)

        with pytest.raises(psycopg.errors.CheckViolation) as exc_v2:
            with psycopg.connect(pg_uri) as conn:
                conn.execute(
                    "UPDATE public.approval_requests SET expires_at = now() - interval '10 seconds' WHERE approval_request_id = %s",
                    (rid,),
                )
        assert "expires_at is immutable" in str(exc_v2.value)


# ==============================================================================
# Challenge 6: Non-Negotiable Invariants: Dormancy & Zero Delivery
# ==============================================================================

class TestDormancyInvariantsEmpirical:
    """Verifies non-negotiable dormancy invariants on live PostgreSQL database."""

    def test_dormancy_and_zero_delivery_invariants(self, pg_uri, pg_dispatch_uri):
        """Verifies DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false, zero dispatches."""
        assert os.getenv("DISPATCH_ENABLED", "false").lower() in ("false", "0", "")
        assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() in ("false", "0", "")

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            dispatches = conn.execute("SELECT count(*) AS cnt FROM public.dispatches").fetchone()["cnt"]
            assert dispatches == 0, f"Dormancy invariant violated: {dispatches} dispatches exist"

        # outbox and domain_events SELECT requires fc27_dispatch role (fc27_app only has INSERT on outbox)
        with psycopg.connect(pg_dispatch_uri, row_factory=dict_row) as conn:
            published = conn.execute("SELECT count(*) AS cnt FROM public.outbox WHERE published_at IS NOT NULL").fetchone()["cnt"]
            assert published == 0, f"Dormancy invariant violated: {published} published outbox records exist"

            events = conn.execute("SELECT count(*) AS cnt FROM public.domain_events WHERE event_type LIKE 'dispatch.%'").fetchone()["cnt"]
            assert events == 0, f"Dormancy invariant violated: {events} dispatch domain events exist"
