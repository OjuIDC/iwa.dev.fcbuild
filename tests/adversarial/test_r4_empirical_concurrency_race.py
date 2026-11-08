"""Empirical Adversarial Concurrency Race Suite for Milestone R4 (Findings R8, R15).

Verifies under REAL PostgreSQL multi-connection concurrency:
1. Concurrency Race Defense on Player Decision Request:
   - Two concurrent threads race (e.g. ACCEPT vs REJECT) on the SAME player decision request.
   - Exactly ONE thread commits (HTTP 200 / store success).
   - Exactly ONE thread receives 409 Conflict.
   - Exactly 1 row in public.player_decisions.
   - The player decision request status is 'decided'.
2. Concurrency Race Defense on Scoped Action Token (sat_):
   - Two concurrent threads race to consume the SAME scoped action token (sat_...).
   - Exactly ONE thread wins (token burned, consumed_at timestamped).
   - Exactly ONE thread receives 409 Conflict.
3. FastAPI HTTP Endpoint Concurrency Race:
   - Two concurrent threads submit decisions to /v1/player/assignments/{aid}/decision.
   - Exactly ONE request returns HTTP 200 OK.
   - Exactly ONE request returns HTTP 409 Conflict.
"""
from __future__ import annotations

import concurrent.futures
import secrets
import threading
from datetime import datetime, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from src.identity.crypto import hash_token
from src.identity.policy import (
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    UserRole,
)
from src.identity.routers.player import player_router
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import ScopedActionTokenEngine


def _seed_test_player_assignment(admin_uri: str, club_id="330323", player_id="10101"):
    """Seeds test user, role, assignment, and decision request in migrated database using admin privileges."""
    user_id = uuid4()
    aid = uuid4()
    req_id = uuid4()

    with psycopg.connect(admin_uri, autocommit=True) as conn:
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
                (aid, club_id, player_id, "1" * 64, "f" * 64, "a" * 64),
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

    return user_id, aid, req_id


def _mint_action_token(admin_uri: str, user_id: UUID, assignment_id: UUID, scope: str = "player_decision") -> tuple[str, UUID]:
    """Mints an action token directly into public.fc27_action_tokens."""
    token_id = uuid4()
    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    tok_hash = hash_token(raw_token)
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.fc27_action_tokens (
                   token_id, token_hash, user_id, assignment_id, scope,
                   expires_at, created_at
               ) VALUES (%s, %s, %s, %s, %s, now() + interval '1 hour', now())""",
            (token_id, tok_hash, user_id, assignment_id, scope),
        )
    return raw_token, token_id


def test_empirical_real_pg_player_decision_store_race(pg_admin_db_uri, pg_uri):
    """Two concurrent threads race ACCEPT vs REJECT on the same player decision request at store layer."""
    user_id, aid, req_id = _seed_test_player_assignment(pg_admin_db_uri)
    raw_tok_1, tok_id_1 = _mint_action_token(pg_admin_db_uri, user_id, aid)
    raw_tok_2, tok_id_2 = _mint_action_token(pg_admin_db_uri, user_id, aid)

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
                assignment_hash="a" * 64,
                package_id="pkg_concurrency",
                package_version="1" * 64,
                package_hash="f" * 64,
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
    assert "already decided" in loser_error.detail or "already in status" in loser_error.detail

    # Verify database state
    with psycopg.connect(pg_admin_db_uri, row_factory=dict_row) as check_conn:
        decisions = check_conn.execute(
            "SELECT * FROM public.player_decisions WHERE request_id = %s", (str(req_id),)
        ).fetchall()
        assert len(decisions) == 1

        req_row = check_conn.execute(
            "SELECT * FROM public.player_decision_requests WHERE request_id = %s", (str(req_id),)
        ).fetchone()
        assert req_row["status"] == "decided"
        assert req_row["decided_at"] is not None


def test_empirical_real_pg_action_token_single_use_race(pg_admin_db_uri, pg_uri):
    """Two concurrent threads attempt to consume the SAME action token."""
    user_id, aid, _ = _seed_test_player_assignment(pg_admin_db_uri)
    raw_tok, tok_id = _mint_action_token(pg_admin_db_uri, user_id, aid)

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
    with psycopg.connect(pg_admin_db_uri, row_factory=dict_row) as check_conn:
        tok_row = check_conn.execute(
            "SELECT * FROM public.fc27_action_tokens WHERE token_id = %s", (tok_id,)
        ).fetchone()
        assert tok_row["consumed_at"] is not None


def test_empirical_real_pg_http_router_concurrency_race(pg_admin_db_uri, pg_uri):
    """FastAPI HTTP endpoint: Two concurrent threads submit decisions on the same assignment via HTTP.

    Verifies that:
    - Exactly 1 thread receives HTTP 200 OK.
    - Exactly 1 thread receives HTTP 409 Conflict.
    - Exactly 1 decision is recorded in PostgreSQL.
    """
    user_id, aid, req_id = _seed_test_player_assignment(pg_admin_db_uri)
    raw_tok_1, tok_id_1 = _mint_action_token(pg_admin_db_uri, user_id, aid)
    raw_tok_2, tok_id_2 = _mint_action_token(pg_admin_db_uri, user_id, aid)

    store = IdentityStore(pg_uri)
    app = FastAPI()
    app.include_router(player_router, prefix="/v1/player")

    app.state.identity_store = store

    @app.exception_handler(IdentityStoreError)
    async def identity_store_error_handler(_request, exc: IdentityStoreError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    principal = PrincipalContext.create(
        user_id=user_id,
        club_id="330323",
        player_id="10101",
        roles=[UserRole.PLAYER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Concurrency Player",
        is_service=False,
    )

    from src.identity.deps import get_current_principal, get_identity_store, require_human_principal
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[require_human_principal] = lambda: principal
    app.dependency_overrides[get_identity_store] = lambda: store

    client = TestClient(app)
    barrier = threading.Barrier(2)
    results = {}

    def racer(name: str, decision: str, tok: str):
        barrier.wait()
        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": decision,
                "action_token": tok,
                "proof_type": "action_token",
            },
        )
        results[name] = resp

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(racer, "Thread-1", "accept", raw_tok_1)
        f2 = executor.submit(racer, "Thread-2", "reject", raw_tok_2)
        f1.result()
        f2.result()

    codes = [r.status_code for r in results.values()]
    assert codes.count(200) == 1, f"Expected exactly one HTTP 200, got: {codes}, details: {[r.text for r in results.values()]}"
    assert codes.count(409) == 1, f"Expected exactly one HTTP 409, got: {codes}, details: {[r.text for r in results.values()]}"

    # Verify database state
    with psycopg.connect(pg_admin_db_uri, row_factory=dict_row) as check_conn:
        decisions = check_conn.execute(
            "SELECT * FROM public.player_decisions WHERE request_id = %s", (str(req_id),)
        ).fetchall()
        assert len(decisions) == 1

        req_row = check_conn.execute(
            "SELECT * FROM public.player_decision_requests WHERE request_id = %s", (str(req_id),)
        ).fetchone()
        assert req_row["status"] == "decided"
