"""Real PostgreSQL Concurrency Race Test Suite for Milestone R4 (Finding R8, R15).

Verifies under real PostgreSQL multi-connection concurrency:
1. Two racing threads submitting player decisions for the SAME decision request:
   - Exactly ONE winner commits (HTTP 200 / store success).
   - Exactly ONE loser fails with 409 Conflict.
   - Exactly 1 row inserted into public.player_decisions for that request_id.
   - The decision request status transitions atomically to 'decided'.
2. Two racing threads attempting to consume the SAME single-use scoped action token (sat_...):
   - Exactly ONE winner burns the token (consumed_at timestamped).
   - Exactly ONE loser fails with 409 Conflict ("action token already consumed or revoked").
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from src.identity.crypto import hash_token
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import ScopedActionTokenEngine


def _seed_test_player_assignment(conn, club_id="330323", player_id="10101"):
    """Seeds test user, assignment, and decision request in public tables."""
    user_id = uuid4()
    aid = uuid4()
    req_id = uuid4()
    pkg_ver = hashlib.sha256(f"pkg_ver_{aid}".encode()).hexdigest()
    pkg_hash = hashlib.sha256(b"pkg_hash_concurrency").hexdigest()
    assignment_hash = hashlib.sha256(b"assign_hash_concurrency").hexdigest()

    with conn.transaction():
        # 1. User
        conn.execute(
            """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
               VALUES (%s, %s, %s, 'Concurrency Player', 'active')
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, club_id, f"player_{user_id.hex[:8]}@example.com"),
        )
        conn.execute(
            """INSERT INTO public.fc27_user_roles (user_id, role, granted_by)
               VALUES (%s, 'player', %s)
               ON CONFLICT DO NOTHING""",
            (user_id, str(user_id)),
        )

        # 2. Assignment
        conn.execute(
            """INSERT INTO public.player_assignments (
                   assignment_id, club_id, subject_player_id, action_type,
                   package_id, package_version, package_hash,
                   assignment_version, assignment_hash, status, created_at, updated_at
               ) VALUES (
                   %s, %s, %s, 'personal_training',
                   'pkg_concurrency', %s, %s,
                   1, %s, 'active', now(), now()
               )""",
            (aid, club_id, player_id, pkg_ver, pkg_hash, assignment_hash),
        )

        # 3. Decision request
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


def _mint_action_token(conn, user_id: UUID, assignment_id: UUID, scope: str = "player_decision") -> tuple[str, UUID]:
    """Mints an action token directly into public.fc27_action_tokens."""
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


def test_real_pg_player_decision_race(pg_uri):
    """Two concurrent threads race ACCEPT vs REJECT on the same player decision request.

    Verifies:
    - Exactly 1 winner successfully commits.
    - Exactly 1 loser receives 409 Conflict.
    - Exactly 1 row in public.player_decisions.
    - Decision request is marked 'decided'.
    """
    with psycopg.connect(pg_uri, autocommit=True) as init_conn:
        user_id, aid, req_id, pkg_ver, pkg_hash, assignment_hash = _seed_test_player_assignment(init_conn)
        raw_tok_1, tok_id_1 = _mint_action_token(init_conn, user_id, aid)
        raw_tok_2, tok_id_2 = _mint_action_token(init_conn, user_id, aid)

    store = IdentityStore(pg_uri)
    barrier = threading.Barrier(2)
    results = {}

    def racer(name: str, decision: str, tok_id: UUID):
        barrier.wait()
        try:
            res = store.record_player_decision(
                request_id=req_id,
                assignment_id=aid,
                subject_player_id="10101",
                actor_id="10101",
                decision=decision,
                authority_basis="affected_player_consent",
                assignment_version=1,
                assignment_hash=assignment_hash,
                package_id="pkg_concurrency",
                package_version=pkg_ver,
                package_hash=pkg_hash,
                notes=f"Race by {name}",
                action_token_reference=str(tok_id),
                actor="10101",
            )
            results[name] = ("WIN", res)
        except IdentityStoreError as exc:
            results[name] = ("LOSS", exc)
        except Exception as exc:
            results[name] = ("ERROR", exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(racer, "Thread-1", "accept", tok_id_1)
        f2 = executor.submit(racer, "Thread-2", "reject", tok_id_2)
        f1.result()
        f2.result()

    statuses = [v[0] for v in results.values()]
    assert statuses.count("WIN") == 1, f"Expected exactly 1 winner, got results: {results}"
    assert statuses.count("LOSS") == 1, f"Expected exactly 1 loser with 409, got results: {results}"

    loser_error = [v[1] for v in results.values() if v[0] == "LOSS"][0]
    assert isinstance(loser_error, IdentityStoreError)
    assert loser_error.status_code == 409
    assert "already in status 'decided'" in loser_error.detail or "already decided" in loser_error.detail

    # Verify database state
    with psycopg.connect(pg_uri, row_factory=dict_row) as check_conn:
        decisions = check_conn.execute(
            "SELECT * FROM public.player_decisions WHERE request_id = %s", (str(req_id),)
        ).fetchall()
        assert len(decisions) == 1

        req_row = check_conn.execute(
            "SELECT * FROM public.player_decision_requests WHERE request_id = %s", (str(req_id),)
        ).fetchone()
        assert req_row["status"] == "decided"
        assert req_row["decided_at"] is not None


def test_real_pg_action_token_single_use_race(pg_uri):
    """Two concurrent threads attempt to consume the SAME action token.

    Verifies:
    - Exactly 1 winner consumes the token.
    - Exactly 1 loser fails with 409 Conflict ("action token already consumed or revoked").
    - The database record has consumed_at populated.
    """
    with psycopg.connect(pg_uri, autocommit=True) as init_conn:
        user_id, aid, *_ = _seed_test_player_assignment(init_conn)
        raw_tok, tok_id = _mint_action_token(init_conn, user_id, aid)

    store = IdentityStore(pg_uri)
    barrier = threading.Barrier(2)
    results = {}

    def consumer(name: str):
        barrier.wait()
        try:
            res = store.consume_action_token(
                raw_tok,
                expected_scope="player_decision",
                expected_user_id=user_id,
                expected_assignment_id=aid,
                actor="10101",
            )
            results[name] = ("WIN", res)
        except IdentityStoreError as exc:
            results[name] = ("LOSS", exc)
        except Exception as exc:
            results[name] = ("ERROR", exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(consumer, "Consumer-1")
        f2 = executor.submit(consumer, "Consumer-2")
        f1.result()
        f2.result()

    statuses = [v[0] for v in results.values()]
    assert statuses.count("WIN") == 1, f"Expected exactly 1 winner, got results: {results}"
    assert statuses.count("LOSS") == 1, f"Expected exactly 1 loser, got results: {results}"

    loser_error = [v[1] for v in results.values() if v[0] == "LOSS"][0]
    assert isinstance(loser_error, IdentityStoreError)
    assert loser_error.status_code == 409
    assert "already consumed" in loser_error.detail.lower()

    # Verify database state
    with psycopg.connect(pg_uri, row_factory=dict_row) as check_conn:
        tok_row = check_conn.execute(
            "SELECT * FROM public.fc27_action_tokens WHERE token_id = %s", (tok_id,)
        ).fetchone()
        assert tok_row["consumed_at"] is not None
