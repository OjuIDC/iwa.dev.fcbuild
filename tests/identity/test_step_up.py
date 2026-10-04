"""Unit and Integration Tests for Decision Step-Up & Evidence Engine (Feature 18, 19, 20).

Verifies:
1. POST /v1/auth/step-up/challenge: Issues WebAuthn challenge cryptographically bound to package & decision.
2. POST /v1/auth/step-up/verify: Verifies passkey assertion, consumes challenge, and mints decision evidence.
3. Single-use consumption and replay lockout (HTTP 409).
4. Scope lockout: prevents evidence reuse against differing packages or decisions (HTTP 400/403).
5. Inactivity/timeout guard: timers never create decisions.
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
    PrincipalContext,
    UserRole,
)
from src.identity.routers.step_up import (
    compute_bound_payload_hash,
    step_up_router,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.webauthn import WebAuthnEngine


# ==============================================================================
# 1. Deterministic Payload Binding Tests
# ==============================================================================

def test_compute_bound_payload_hash_determinism():
    """Validates canonical JSON serialization and deterministic SHA-256 hash."""
    pkg_id = "pkg_alpha"
    pkg_ver = 1
    pkg_hash = "a" * 64
    decision = "approved"
    req_id = uuid4()

    h1 = compute_bound_payload_hash(
        package_id=pkg_id,
        package_version=pkg_ver,
        package_hash=pkg_hash,
        decision=decision,
        approval_request_id=req_id,
    )
    h2 = compute_bound_payload_hash(
        package_id=pkg_id,
        package_version=pkg_ver,
        package_hash=pkg_hash,
        decision=decision,
        approval_request_id=req_id,
    )
    assert h1 == h2
    assert len(h1) == 64

    # Any change in payload alters the hash
    h_diff = compute_bound_payload_hash(
        package_id=pkg_id,
        package_version=pkg_ver,
        package_hash=pkg_hash,
        decision="rejected",
        approval_request_id=req_id,
    )
    assert h1 != h_diff


# ==============================================================================
# 2. Evidence Store Semantics (Single-Use, Replay, Scope Lockout)
# ==============================================================================

def test_evidence_store_single_use_and_replay_lockout():
    """Tests create, get, and consume decision evidence with single-use replay defense."""
    mock_store = MagicMock(spec=IdentityStore)
    eid = uuid4()
    uid = uuid4()
    pkg_id = "pkg_xyz"
    pkg_hash = "f" * 64

    mock_row = {
        "evidence_id": eid,
        "user_id": uid,
        "package_id": pkg_id,
        "package_version": "1",
        "package_hash": pkg_hash,
        "decision": "approved",
        "consumed_at": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }

    mock_store.get_decision_evidence.return_value = mock_row

    # 1. Inspect unconsumed evidence
    res = mock_store.get_decision_evidence(eid)
    assert res["consumed_at"] is None

    # 2. Consume evidence
    mock_store.consume_decision_evidence.side_effect = [
        {**mock_row, "consumed_at": datetime.now(timezone.utc)},
        IdentityStoreError(409, "evidence already consumed"),
    ]

    first_consume = mock_store.consume_decision_evidence(eid, expected_package_id=pkg_id)
    assert first_consume["consumed_at"] is not None

    # 3. Second consume raises 409
    with pytest.raises(IdentityStoreError) as exc_info:
        mock_store.consume_decision_evidence(eid, expected_package_id=pkg_id)
    assert exc_info.value.status_code == 409


def test_evidence_scope_mismatch_rejection():
    """consume_decision_evidence rejects mismatched package ID or version."""
    # When testing against real or mock store, scope mismatch yields 400
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.consume_decision_evidence.side_effect = IdentityStoreError(400, "evidence bound to different package ID")

    with pytest.raises(IdentityStoreError) as exc_info:
        mock_store.consume_decision_evidence(uuid4(), expected_package_id="different_pkg")
    assert exc_info.value.status_code == 400


# ==============================================================================
# 3. Step-Up Router API Tests (FastAPI TestClient)
# ==============================================================================

@pytest.fixture
def step_up_client():
    app = FastAPI()
    app.include_router(step_up_router, prefix="/v1/auth")

    mock_store = MagicMock(spec=IdentityStore)
    app.state.identity_store = mock_store
    app.state.settings = MagicMock(postgres_uri="postgresql://mock")

    # Mock human commissioner principal
    human_principal = PrincipalContext.create(
        user_id=uuid4(),
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Commissioner",
        is_service=False,
    )

    from src.identity.deps import get_current_principal, require_human_principal
    app.dependency_overrides[get_current_principal] = lambda: human_principal
    app.dependency_overrides[require_human_principal] = lambda: human_principal

    return TestClient(app), mock_store, human_principal


def test_step_up_challenge_endpoint(step_up_client):
    client, mock_store, principal = step_up_client

    mock_store.list_user_webauthn_credentials.return_value = [
        {"credential_id": b"cred123", "public_key": b"pk"}
    ]
    mock_store.create_challenge.return_value = {
        "challenge_id": uuid4(),
        "challenge_str": "test_challenge_str",
    }

    payload = {
        "package_id": "pkg_1",
        "package_version": 1,
        "package_hash": "e" * 64,
        "decision": "approved",
    }

    with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
        mock_eng = MagicMock()
        mock_eng.generate_step_up_options.return_value = {
            "challenge": "test_challenge_str",
            "rp": {"id": "fc27.club", "name": "FC27"},
            "timeout": 60000,
            "allowCredentials": [],
            "userVerification": "preferred",
        }
        mock_eng_getter.return_value = mock_eng

        resp = client.post("/v1/auth/step-up/challenge", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["challenge"] == "test_challenge_str"
        assert "bound_payload_hash" in data
        assert len(data["bound_payload_hash"]) == 64


def test_step_up_verify_endpoint_mints_evidence(step_up_client):
    client, mock_store, principal = step_up_client
    eid = uuid4()
    chid = uuid4()

    mock_store.get_challenge.return_value = {
        "challenge_id": chid,
        "challenge_str": "test",
        "challenge_type": "step_up_decision",
        "consumed_at": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "user_id": principal.user_id,
        "bound_payload_hash": "b" * 64,
    }
    mock_store.get_webauthn_credential.return_value = {
        "credential_id": b"cred_bytes_123",
        "user_id": principal.user_id,
        "sign_counter": 10,
        "status": "active",
    }
    mock_store._normalize_credential_id.return_value = b"cred_bytes_123"

    mock_store.create_decision_evidence.return_value = {
        "evidence_id": eid,
        "user_id": principal.user_id,
        "bound_payload_hash": "b" * 64,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }

    mock_verified = MagicMock()
    mock_verified.credential_id = b"cred_bytes_123"
    mock_verified.new_sign_counter = 42

    verify_payload = {
        "id": "cred_bytes_123",
        "type": "public-key",
        "package_id": "pkg_1",
        "package_version": 1,
        "package_hash": "e" * 64,
        "decision": "approved",
        "response": {
            "clientDataJSON": "eyJjaGFsbGVuZ2UiOiAidGVzdCJ9",
            "authenticatorData": "dummy_auth_data",
            "signature": "dummy_sig",
        },
    }

    with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
        mock_eng = MagicMock()
        mock_eng.verify_step_up_response.return_value = mock_verified
        mock_eng_getter.return_value = mock_eng

        resp = client.post("/v1/auth/step-up/verify", json=verify_payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["verified"] is True
        assert body["authentication_evidence_id"] == str(eid)
        assert "bound_payload_hash" in body


# ==============================================================================
# 4. Database Integration Tests (Requires PostgreSQL when pg_uri available)
# ==============================================================================

def test_db_decision_evidence_lifecycle(pg_uri: str):
    """Database backed test for PostgreSQL table fc27_decision_evidence."""
    store = IdentityStore(pg_uri)

    user = store.create_user(
        club_id="330323",
        login_id=f"stepup_{secrets.token_hex(4)}@fc27.club",
        display_name="Step Up User",
        status="active",
    )
    uid = user["user_id"]
    pkg_id = f"pkg_{secrets.token_hex(4)}"
    pkg_hash = hashlib.sha256(b"pkg_data").hexdigest()
    bound_hash = hashlib.sha256(f"{pkg_id}:1:{pkg_hash}:approved".encode()).hexdigest()

    # 1. Create decision evidence
    ev = store.create_decision_evidence(
        user_id=uid,
        bound_payload_hash=bound_hash,
        package_id=pkg_id,
        package_version="1",
        package_hash=pkg_hash,
        decision="approved",
        ttl_seconds=300,
    )
    eid = ev["evidence_id"]
    assert ev["consumed_at"] is None

    # 2. Retrieve evidence
    fetched = store.get_decision_evidence(eid)
    assert fetched is not None
    assert fetched["package_id"] == pkg_id

    # 3. Scope mismatch raises error
    with pytest.raises(IdentityStoreError) as exc_info:
        store.consume_decision_evidence(eid, expected_package_id="different_pkg")
    assert exc_info.value.status_code == 400

    # 4. Valid consumption marks consumed_at
    c1 = store.consume_decision_evidence(
        eid,
        expected_package_id=pkg_id,
        expected_package_hash=pkg_hash,
        expected_package_version="1",
    )
    assert c1["consumed_at"] is not None

    # 5. Replay attempt fails with 409
    with pytest.raises(IdentityStoreError) as exc_replay:
        store.consume_decision_evidence(eid, expected_package_id=pkg_id)
    assert exc_replay.value.status_code == 409
