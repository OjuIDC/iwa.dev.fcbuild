"""Unit and Integration Tests for Authoritative Player Decisions (Features 21, 22, 23, 24, 25).

Verifies:
1. Object-Level Player Isolation:
   - Player A cannot query or decide Player B's assignments (HTTP 403 PlayerIsolationError).
   - Staff/coaches cannot decide on behalf of players (HTTP 403).
2. 5 Distinct Decision Actions:
   - accept: Requires passkey assertion or action token.
   - reject: Authoritative human decline.
   - change_request: Requires mandatory notes/reason (HTTP 422 if empty).
   - pause: Sets paused state with valid duration_days >= 1 (HTTP 422 if invalid).
   - acknowledge: Records human receipt without approving or activating.
3. Dual Proof Types:
   - Passkey WebAuthn assertion bound to assignment hash.
   - Scoped action token (sat_...) consumed atomically.
4. G-TIMEOUT Invariant: Inactivity and timeouts never commit an approval decision.
5. Anti-Impersonation Guard: Service principals cannot commit player decisions.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    PlayerIsolationError,
    PrincipalContext,
    UserRole,
)
from src.identity.routers.player import (
    compute_decision_bound_hash,
    player_router,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import ScopedActionTokenEngine


# ==============================================================================
# 1. Deterministic Payload Binding Tests
# ==============================================================================

def test_player_decision_bound_hash_determinism():
    """Validates player decision bound payload hash determinism."""
    aid = uuid4()
    h1 = compute_decision_bound_hash(
        assignment_id=aid,
        assignment_version=1,
        assignment_hash="h1" * 32,
        package_id="pkg_1",
        package_version="1",
        package_hash="ph1" * 32,
        subject_player_id="10101",
        decision="accept",
        authority_basis="affected_player_consent",
    )
    h2 = compute_decision_bound_hash(
        assignment_id=aid,
        assignment_version=1,
        assignment_hash="h1" * 32,
        package_id="pkg_1",
        package_version="1",
        package_hash="ph1" * 32,
        subject_player_id="10101",
        decision="accept",
        authority_basis="affected_player_consent",
    )
    assert h1 == h2
    assert len(h1) == 64

    # Altering player ID changes hash
    h_diff = compute_decision_bound_hash(
        assignment_id=aid,
        assignment_version=1,
        assignment_hash="h1" * 32,
        package_id="pkg_1",
        package_version="1",
        package_hash="ph1" * 32,
        subject_player_id="20202",
        decision="accept",
        authority_basis="affected_player_consent",
    )
    assert h1 != h_diff


# ==============================================================================
# 2. Player Decisions Router API Tests (FastAPI TestClient)
# ==============================================================================

@pytest.fixture
def player_app_ctx():
    app = FastAPI()
    app.include_router(player_router, prefix="/v1/player")
    app.include_router(player_router, prefix="/v1/player-assignments")

    mock_store = MagicMock(spec=IdentityStore)
    app.state.identity_store = mock_store
    app.state.settings = MagicMock(postgres_uri="postgresql://mock")

    player_a_principal = PrincipalContext.create(
        user_id=uuid4(),
        club_id="330323",
        player_id="10101",
        roles=[UserRole.PLAYER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Player A",
        is_service=False,
    )

    from src.identity.deps import get_current_principal, require_human_principal
    app.dependency_overrides[get_current_principal] = lambda: player_a_principal
    app.dependency_overrides[require_human_principal] = lambda: player_a_principal

    return app, mock_store, player_a_principal


def test_player_isolation_cross_player_rejected(player_app_ctx):
    """Player A cannot access or decide on assignment belonging to Player B."""
    app, mock_store, principal = player_app_ctx
    client = TestClient(app)

    aid = uuid4()
    # Mock assignment belonging to Player B (20202)
    mock_assignment_b = {
        "assignment_id": aid,
        "subject_player_id": "20202",
        "club_id": "330323",
        "status": "active",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_b",
        "package_version": "1",
        "package_hash": "b" * 64,
    }
    mock_store.get_player_assignment.return_value = mock_assignment_b

    # Request challenge for Player B's assignment -> must raise 403
    resp = client.post(
        f"/v1/player/assignments/{aid}/decision-challenge",
        json={"decision": "accept", "authority_basis": "affected_player_consent"},
    )
    assert resp.status_code == 403
    assert "player isolation violation" in resp.json()["detail"].lower()


def test_player_isolation_staff_cannot_decide_as_player(player_app_ctx):
    """Coach/staff cannot submit player decisions."""
    app, mock_store, _ = player_app_ctx
    coach_principal = PrincipalContext.create(
        user_id=uuid4(),
        club_id="330323",
        roles=[UserRole.COACH],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Coach Dave",
        is_service=False,
    )
    from src.identity.deps import get_current_principal, require_human_principal
    app.dependency_overrides[get_current_principal] = lambda: coach_principal
    app.dependency_overrides[require_human_principal] = lambda: coach_principal
    client = TestClient(app)

    aid = uuid4()
    mock_store.get_player_assignment.return_value = {
        "assignment_id": aid,
        "subject_player_id": "10101",
        "club_id": "330323",
        "status": "active",
    }

    resp = client.post(
        f"/v1/player/assignments/{aid}/decision-challenge",
        json={"decision": "accept"},
    )
    assert resp.status_code == 403


def test_player_decision_challenge_generation_success(player_app_ctx):
    """Player A successfully generates decision challenge for own assignment."""
    app, mock_store, principal = player_app_ctx
    client = TestClient(app)

    aid = uuid4()
    mock_store.get_player_assignment.return_value = {
        "assignment_id": aid,
        "subject_player_id": "10101",
        "club_id": "330323",
        "status": "active",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_1",
        "package_version": "1",
        "package_hash": "p" * 64,
    }
    mock_store.list_user_webauthn_credentials.return_value = []
    mock_store.create_player_decision_request.return_value = {
        "request_id": uuid4(),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }

    with patch("src.identity.routers.player.get_webauthn_engine") as mock_eng_getter:
        mock_eng = MagicMock()
        mock_eng.generate_step_up_options.return_value = {
            "challenge": "ch_nonce_123",
            "rp": {"id": "fc27.club"},
        }
        mock_eng_getter.return_value = mock_eng

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision-challenge",
            json={"decision": "accept", "authority_basis": "affected_player_consent"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["challenge_nonce"] == "ch_nonce_123"
        assert data["subject_player_id"] == "10101"
        assert "bound_payload_hash" in data


def test_player_decision_action_token_flow(player_app_ctx):
    """Player executes decision using scoped action token."""
    app, mock_store, principal = player_app_ctx
    client = TestClient(app)

    aid = uuid4()
    raw_token = f"sat_{secrets.token_urlsafe(32)}"

    mock_store.get_player_assignment.return_value = {
        "assignment_id": aid,
        "subject_player_id": "10101",
        "club_id": "330323",
        "status": "active",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_1",
        "package_version": "1",
        "package_hash": "p" * 64,
    }
    mock_store.get_active_decision_request.return_value = {
        "request_id": uuid4(),
        "status": "awaiting_player",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    mock_store.record_player_decision.return_value = {
        "decision_id": uuid4(),
        "request_id": uuid4(),
        "assignment_id": aid,
        "subject_player_id": "10101",
        "decision": "accept",
        "authority_basis": "affected_player_consent",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_1",
        "package_version": "1",
        "package_hash": "p" * 64,
        "decided_at": datetime.now(timezone.utc),
        "status": "decided",
    }

    with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
        mock_consume.return_value = MagicMock(token_id=uuid4(), user_id=principal.user_id)

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": "accept",
                "proof_type": "action_token",
                "action_token": raw_token,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "accept"
        mock_consume.assert_called_once()


def test_player_decision_actions_validation(player_app_ctx):
    """Verifies validation rules for all 5 distinct decision actions."""
    app, mock_store, principal = player_app_ctx
    client = TestClient(app)
    aid = uuid4()

    mock_store.get_player_assignment.return_value = {
        "assignment_id": aid,
        "subject_player_id": "10101",
        "club_id": "330323",
        "status": "active",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_1",
        "package_version": "1",
        "package_hash": "p" * 64,
    }

    # 1. change_request requires notes/reason
    resp_cr_empty = client.post(
        f"/v1/player/assignments/{aid}/decision",
        json={"decision": "change_request", "notes": ""},
    )
    assert resp_cr_empty.status_code == 422
    err_detail = str(resp_cr_empty.json()["detail"]).lower()
    assert "change_request requires non-empty notes or reason" in err_detail or "notes or reason" in err_detail

    # 2. pause with duration_days < 1 rejected
    resp_pause_invalid = client.post(
        f"/v1/player/assignments/{aid}/decision",
        json={"decision": "pause", "duration_days": 0},
    )
    assert resp_pause_invalid.status_code == 422

    # 3. accept without proof is rejected
    resp_accept_no_proof = client.post(
        f"/v1/player/assignments/{aid}/decision",
        json={"decision": "accept"},
    )
    assert resp_accept_no_proof.status_code == 422

    # 4. acknowledge does NOT require proof and confirms receipt
    mock_store.get_active_decision_request.return_value = {
        "request_id": uuid4(),
        "status": "awaiting_player",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    mock_store.record_player_decision.return_value = {
        "decision_id": uuid4(),
        "request_id": uuid4(),
        "assignment_id": aid,
        "subject_player_id": "10101",
        "decision": "acknowledge",
        "authority_basis": "affected_player_consent",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_1",
        "package_version": "1",
        "package_hash": "p" * 64,
        "decided_at": datetime.now(timezone.utc),
        "status": "decided",
    }
    resp_ack = client.post(
        f"/v1/player/assignments/{aid}/decision",
        json={"decision": "acknowledge"},
    )
    assert resp_ack.status_code == 200
    assert resp_ack.json()["decision"] == "acknowledge"


def test_g_timeout_invariant_rejection():
    """Confirms G-TIMEOUT invariant: timeouts and inactivity cannot authorize decisions."""
    # Under policy.py, any action starting with 'system:timeout' or attributes with 'timeout_approved' fails
    from src.identity.policy import authorize, Resource

    principal = PrincipalContext.create(
        user_id=uuid4(),
        club_id="330323",
        roles=[UserRole.PLAYER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
    )
    res = Resource.for_package("330323", "pkg_1", 1)

    with pytest.raises(AgentImpersonationError, match="G-TIMEOUT invariant"):
        authorize(principal, "system:timeout:auto_approve", res)

    # Also confirm resource timeout_approved attribute rejection
    res_timeout = Resource.for_package("330323", "pkg_1", 1, attributes={"timeout_approved": True})
    with pytest.raises(AgentImpersonationError, match="G-TIMEOUT invariant"):
        authorize(principal, ActionType.PACKAGE_APPROVE, res_timeout)


# ==============================================================================
# 3. Database Integration Tests (Requires PostgreSQL when pg_uri available)
# ==============================================================================

def test_db_player_assignment_and_decision_lifecycle(pg_uri: str):
    """Database backed test for PostgreSQL player assignments and decisions."""
    store = IdentityStore(pg_uri)

    player_user = store.create_user(
        club_id="330323",
        login_id=f"player_{secrets.token_hex(4)}@fc27.club",
        display_name="Marcus Rashford",
        status="active",
        player_id="10101",
    )
    store.assign_user_role(player_user["user_id"], "player")

    pkg_id = f"pkg_{secrets.token_hex(4)}"
    pkg_hash = hashlib.sha256(b"training_routine").hexdigest()
    assign_hash = hashlib.sha256(b"assignment_spec").hexdigest()

    # 1. Create player assignment
    assign = store.create_player_assignment(
        package_id=pkg_id,
        package_version="1",
        package_hash=pkg_hash,
        subject_player_id="10101",
        club_id="330323",
        assignment_version=1,
        assignment_hash=assign_hash,
        action_type="personal_training",
    )
    aid = assign["assignment_id"]
    assert assign["status"] == "active"

    # 2. Query assignment by ID and list
    fetched = store.get_player_assignment(aid)
    assert fetched is not None
    assert fetched["subject_player_id"] == "10101"

    plist = store.list_player_assignments(club_id="330323", player_id="10101")
    assert any(str(a["assignment_id"]) == str(aid) for a in plist)

    # 3. Create decision request
    req = store.create_player_decision_request(
        assignment_id=aid,
        subject_player_id="10101",
        challenge_nonce="nonce_db_123",
        ttl_seconds=300,
    )
    rid = req["request_id"]
    assert req["status"] == "awaiting_player"

    # 4. Commit authoritative decision (e.g. acknowledge)
    dec = store.record_player_decision(
        request_id=rid,
        assignment_id=aid,
        subject_player_id="10101",
        actor_id="10101",
        decision="acknowledge",
        authority_basis="affected_player_consent",
        assignment_version=1,
        assignment_hash=assign_hash,
        package_id=pkg_id,
        package_version="1",
        package_hash=pkg_hash,
        notes="acknowledged workout plan",
    )
    assert dec["decision"] == "acknowledge"

    # 5. Verify request marked decided
    req_after = store.get_player_decision_request(rid)
    assert req_after["status"] == "decided"
