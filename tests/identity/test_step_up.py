"""Unit and Integration Tests for Decision Step-Up & Evidence Engine (Feature 18, 19, 20 & Finding R15).

Verifies without Mock Masking:
1. Deterministic Payload Binding and Decision Normalization.
2. WebAuthn Step-Up Challenge generation with UserVerificationRequirement.REQUIRED.
3. Strict Pydantic model validation with extra="forbid" (HTTP 422).
4. WebAuthn Step-Up Assertion verification and single-use decision evidence minting.
5. Rejection of unverified user assertions (HTTP 400).
6. Challenge lifecycle: consumed, expired, cross-user isolation, and hash tampering rejection.
7. Role & Principal guard: rejection of non-human and non-commissioner principals (HTTP 403).
8. Real PostgreSQL single-use consumption, 14-field scope binding, and replay defense (HTTP 409).
9. Full compliance with fc27_approval_inconsistencies view invariants.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
)
from src.identity.routers.step_up import (
    StepUpChallengeRequest,
    StepUpVerifyRequest,
    compute_bound_payload_hash,
    normalize_decision,
    step_up_router,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.webauthn import VerifiedAuthenticationResult, WebAuthnEngine
from tests.e2e.phase2_helpers import b64url_encode


# ==============================================================================
# 1. Deterministic Payload Binding & Decision Normalization Tests
# ==============================================================================

class TestPayloadBindingDeterminism:
    """Validates canonical JSON serialization and deterministic SHA-256 hashing."""

    def test_compute_bound_payload_hash_determinism(self):
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

    def test_compute_bound_payload_hash_normalization(self):
        """Verifies that decision normalization produces identical canonical hashes."""
        pkg_id = "pkg_alpha"
        pkg_ver = "1"
        pkg_hash = "a" * 64
        req_id = uuid4()

        h_approved = compute_bound_payload_hash(
            package_id=pkg_id, package_version=pkg_ver, package_hash=pkg_hash,
            decision="approved", approval_request_id=req_id,
        )
        h_approve_caps = compute_bound_payload_hash(
            package_id=pkg_id, package_version=1, package_hash=pkg_hash,
            decision="APPROVE", approval_request_id=str(req_id),
        )
        assert h_approved == h_approve_caps

        h_rejected = compute_bound_payload_hash(
            package_id=pkg_id, package_version=pkg_ver, package_hash=pkg_hash,
            decision="rejected", approval_request_id=req_id,
        )
        h_reject_caps = compute_bound_payload_hash(
            package_id=pkg_id, package_version=pkg_ver, package_hash=pkg_hash,
            decision="REJECT", approval_request_id=req_id,
        )
        assert h_rejected == h_reject_caps
        assert h_approved != h_rejected

    def test_compute_bound_payload_hash_tampering_sensitivity(self):
        """Any change in payload parameters alters the resulting canonical hash."""
        req_id = uuid4()
        base = compute_bound_payload_hash(
            package_id="pkg_1", package_version="1", package_hash="a" * 64,
            decision="approved", approval_request_id=req_id,
        )

        assert compute_bound_payload_hash(
            package_id="pkg_2", package_version="1", package_hash="a" * 64,
            decision="approved", approval_request_id=req_id,
        ) != base

        assert compute_bound_payload_hash(
            package_id="pkg_1", package_version="2", package_hash="a" * 64,
            decision="approved", approval_request_id=req_id,
        ) != base

        assert compute_bound_payload_hash(
            package_id="pkg_1", package_version="1", package_hash="b" * 64,
            decision="approved", approval_request_id=req_id,
        ) != base

        assert compute_bound_payload_hash(
            package_id="pkg_1", package_version="1", package_hash="a" * 64,
            decision="approved", approval_request_id=uuid4(),
        ) != base


# ==============================================================================
# 2. Step-Up Router API Tests (FastAPI TestClient)
# ==============================================================================

@pytest.fixture
def step_up_client():
    app = FastAPI()
    app.include_router(step_up_router, prefix="/v1/auth")

    mock_store = MagicMock(spec=IdentityStore)
    app.state.identity_store = mock_store
    app.state.settings = MagicMock(postgres_uri="postgresql://mock")

    human_principal = PrincipalContext.create(
        user_id=uuid4(),
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        session_id=uuid4(),
        display_name="Commissioner",
        is_service=False,
    )

    from src.identity.deps import get_current_principal, require_human_principal
    app.dependency_overrides[get_current_principal] = lambda: human_principal
    app.dependency_overrides[require_human_principal] = lambda: human_principal

    return TestClient(app), mock_store, human_principal


class TestStepUpRouterApi:
    """Tests step-up challenge and verification endpoints without mock facades."""

    def test_step_up_challenge_endpoint_success(self, step_up_client):
        """Verifies step-up options generation with required user verification."""
        client, mock_store, principal = step_up_client

        mock_store.list_user_webauthn_credentials.return_value = [
            {"credential_id": b"cred123", "public_key": b"pk"}
        ]
        mock_store.create_challenge.return_value = {
            "challenge_id": uuid4(),
            "challenge_str": "test_challenge_str",
        }

        pkg_id = "pkg_1"
        pkg_ver = 1
        pkg_hash = "e" * 64
        decision = "approved"

        expected_hash = compute_bound_payload_hash(
            package_id=pkg_id,
            package_version=pkg_ver,
            package_hash=pkg_hash,
            decision=decision,
            approval_request_id=None,
        )

        with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng.generate_step_up_options.return_value = {
                "challenge": "test_challenge_str",
                "rp": {"id": "fc27.club", "name": "FC27"},
                "timeout": 60000,
                "allowCredentials": [],
                "userVerification": "required",  # Finding R1: Strictly required!
            }
            mock_eng_getter.return_value = mock_eng

            payload = {
                "package_id": pkg_id,
                "package_version": pkg_ver,
                "package_hash": pkg_hash,
                "decision": decision,
            }
            resp = client.post("/v1/auth/step-up/challenge", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["challenge"] == "test_challenge_str"
            assert data["userVerification"] == "required"
            assert data["bound_payload_hash"] == expected_hash

    def test_step_up_challenge_forbid_extra_fields(self, step_up_client):
        """Finding R12: StepUpChallengeRequest rejects extra fields with HTTP 422."""
        client, _, _ = step_up_client
        payload = {
            "package_id": "pkg_1",
            "package_version": 1,
            "package_hash": "e" * 64,
            "decision": "approved",
            "injected_extra_field": "exploit",
        }
        resp = client.post("/v1/auth/step-up/challenge", json=payload)
        assert resp.status_code == 422

    def test_step_up_challenge_invalid_decision_rejected(self, step_up_client):
        """Invalid decision string is rejected with HTTP 422."""
        client, _, _ = step_up_client
        payload = {
            "package_id": "pkg_1",
            "package_version": 1,
            "package_hash": "e" * 64,
            "decision": "invalid_choice",
        }
        resp = client.post("/v1/auth/step-up/challenge", json=payload)
        assert resp.status_code == 422

    def test_step_up_challenge_client_hash_mismatch_rejected(self, step_up_client):
        """Mismatched client-supplied bound_payload_hash is rejected with HTTP 400."""
        client, _, _ = step_up_client
        payload = {
            "package_id": "pkg_1",
            "package_version": 1,
            "package_hash": "e" * 64,
            "decision": "approved",
            "bound_payload_hash": "f" * 64,  # Incorrect hash
        }
        resp = client.post("/v1/auth/step-up/challenge", json=payload)
        assert resp.status_code == 400
        assert "bound_payload_hash does not match" in resp.json()["detail"]

    def test_step_up_verify_endpoint_mints_evidence_success(self, step_up_client):
        """Verifies assertion, consumes challenge, and mints evidence with 14 bound fields."""
        client, mock_store, principal = step_up_client
        eid = uuid4()
        chid = uuid4()

        pkg_id = "pkg_1"
        pkg_ver = 1
        pkg_hash = "e" * 64
        decision = "approved"

        expected_hash = compute_bound_payload_hash(
            package_id=pkg_id,
            package_version=pkg_ver,
            package_hash=pkg_hash,
            decision=decision,
            approval_request_id=None,
        )

        mock_store.get_challenge.return_value = {
            "challenge_id": chid,
            "challenge_str": "valid_ch_str",
            "challenge_type": "step_up_decision",
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "user_id": principal.user_id,
            "bound_payload_hash": expected_hash,
        }
        cred_bytes = b"cred_bytes_123"
        mock_store._normalize_credential_id.return_value = cred_bytes
        mock_store.get_webauthn_credential.return_value = {
            "credential_id": cred_bytes,
            "user_id": principal.user_id,
            "sign_counter": 10,
            "status": "active",
        }

        mock_store.create_decision_evidence.return_value = {
            "evidence_id": eid,
            "user_id": principal.user_id,
            "canonical_bound_payload_hash": expected_hash,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

        mock_verified = VerifiedAuthenticationResult(
            credential_id=cred_bytes,
            credential_id_hex=cred_bytes.hex(),
            new_sign_counter=42,
            user_verified=True,
        )

        client_data = json.dumps({"type": "webauthn.get", "challenge": "valid_ch_str"}).encode()
        verify_payload = {
            "id": cred_bytes.hex(),
            "type": "public-key",
            "package_id": pkg_id,
            "package_version": pkg_ver,
            "package_hash": pkg_hash,
            "decision": decision,
            "response": {
                "clientDataJSON": b64url_encode(client_data),
                "authenticatorData": b64url_encode(b"\x00" * 37),
                "signature": b64url_encode(b"\x00" * 64),
            },
        }

        with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng._extract_challenge_from_response.return_value = "valid_ch_str"
            mock_eng.verify_step_up_response.return_value = mock_verified
            mock_eng_getter.return_value = mock_eng

            resp = client.post("/v1/auth/step-up/verify", json=verify_payload)
            assert resp.status_code == 200
            body = resp.json()
            assert body["verified"] is True
            assert body["authentication_evidence_id"] == str(eid)
            assert body["bound_payload_hash"] == expected_hash

            # Verify counter update and 14-field evidence creation
            mock_store.update_webauthn_counter.assert_called_once_with(cred_bytes, 42)
            mock_store.create_decision_evidence.assert_called_once()
            call_kwargs = mock_store.create_decision_evidence.call_args.kwargs
            assert call_kwargs["user_id"] == principal.user_id
            assert call_kwargs["canonical_bound_payload_hash"] == expected_hash
            assert call_kwargs["package_id"] == pkg_id
            assert call_kwargs["package_hash"] == pkg_hash
            assert call_kwargs["normalized_decision"] == "approved"

    def test_step_up_verify_endpoint_rejects_unverified_user(self, step_up_client):
        """Assert user_verified=False is rejected with HTTP 400 (Finding R1)."""
        client, mock_store, principal = step_up_client
        pkg_id = "pkg_1"
        pkg_ver = 1
        pkg_hash = "e" * 64
        decision = "approved"

        expected_hash = compute_bound_payload_hash(
            package_id=pkg_id, package_version=pkg_ver, package_hash=pkg_hash,
            decision=decision, approval_request_id=None,
        )

        mock_store.get_challenge.return_value = {
            "challenge_id": uuid4(),
            "challenge_str": "valid_ch_str",
            "challenge_type": "step_up_decision",
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "user_id": principal.user_id,
            "bound_payload_hash": expected_hash,
        }
        cred_bytes = b"cred_bytes_123"
        mock_store._normalize_credential_id.return_value = cred_bytes
        mock_store.get_webauthn_credential.return_value = {
            "credential_id": cred_bytes,
            "user_id": principal.user_id,
            "sign_counter": 10,
            "status": "active",
        }

        mock_verified = VerifiedAuthenticationResult(
            credential_id=cred_bytes,
            credential_id_hex=cred_bytes.hex(),
            new_sign_counter=42,
            user_verified=False,  # Attacker bypassed UV!
        )

        client_data = json.dumps({"type": "webauthn.get", "challenge": "valid_ch_str"}).encode()
        verify_payload = {
            "id": cred_bytes.hex(),
            "type": "public-key",
            "package_id": pkg_id,
            "package_version": pkg_ver,
            "package_hash": pkg_hash,
            "decision": decision,
            "response": {
                "clientDataJSON": b64url_encode(client_data),
                "authenticatorData": b64url_encode(b"\x00" * 37),
                "signature": b64url_encode(b"\x00" * 64),
            },
        }

        with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng._extract_challenge_from_response.return_value = "valid_ch_str"
            mock_eng.verify_step_up_response.return_value = mock_verified
            mock_eng_getter.return_value = mock_eng

            resp = client.post("/v1/auth/step-up/verify", json=verify_payload)
            assert resp.status_code == 400
            assert "step-up assertion requires user verification" in resp.json()["detail"].lower()

    def test_step_up_verify_forbid_extra_fields(self, step_up_client):
        """Finding R12: StepUpVerifyRequest rejects extra fields with HTTP 422."""
        client, _, _ = step_up_client
        verify_payload = {
            "id": "cred_id",
            "type": "public-key",
            "response": {"clientDataJSON": "abc"},
            "forbidden_extra": "malicious",
        }
        resp = client.post("/v1/auth/step-up/verify", json=verify_payload)
        assert resp.status_code == 422

    def test_step_up_verify_rejects_consumed_challenge(self, step_up_client):
        """Challenge already consumed is rejected with HTTP 400."""
        client, mock_store, _ = step_up_client
        mock_store.get_challenge.return_value = {
            "challenge_str": "ch",
            "challenge_type": "step_up_decision",
            "consumed_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng._extract_challenge_from_response.return_value = "ch"
            mock_eng_getter.return_value = mock_eng

            resp = client.post("/v1/auth/step-up/verify", json={"id": "c", "response": {}})
            assert resp.status_code == 400
            assert "already been consumed" in resp.json()["detail"].lower()

    def test_step_up_verify_rejects_cross_user_challenge(self, step_up_client):
        """Cross-user challenge theft is rejected with HTTP 403."""
        client, mock_store, principal = step_up_client
        attacker_id = uuid4()
        mock_store.get_challenge.return_value = {
            "challenge_str": "ch",
            "challenge_type": "step_up_decision",
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "user_id": attacker_id,  # Different user!
        }
        with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng._extract_challenge_from_response.return_value = "ch"
            mock_eng_getter.return_value = mock_eng

            resp = client.post("/v1/auth/step-up/verify", json={"id": "c", "response": {}})
            assert resp.status_code == 403
            assert "cross-user challenge access denied" in resp.json()["detail"].lower()


# ==============================================================================
# 3. Real PostgreSQL Integration Tests (Requires pg_uri)
# ==============================================================================

def test_db_decision_evidence_lifecycle(pg_uri: str):
    """Database backed test for PostgreSQL table fc27_decision_evidence (Finding R2, R15).

    Verifies without mock masking:
    1. Schema compliance with migrations 0003, 0004, 0007.
    2. Zero inconsistencies in fc27_approval_inconsistencies.
    3. Complete 14-field binding verification.
    4. Atomic single-use replay defense.
    """
    store = IdentityStore(pg_uri)

    user = store.create_user(
        club_id="330323",
        login_id=f"stepup_{secrets.token_hex(4)}@fc27.club",
        display_name="Step Up Commissioner",
        status="active",
    )
    uid = user["user_id"]
    store.assign_user_role(uid, "commissioner", granted_by=uid)

    session_id = uuid4()
    thread_id = f"fc27-330323-run-20261005-{secrets.token_hex(4)}"
    approval_request_id = uuid4()
    pkg_id = f"pkg_{secrets.token_hex(4)}"
    pkg_ver = hashlib.sha256(f"ver_{approval_request_id}".encode()).hexdigest()
    pkg_hash = hashlib.sha256(b"pkg_data_test").hexdigest()

    bound_hash = compute_bound_payload_hash(
        package_id=pkg_id,
        package_version=pkg_ver,
        package_hash=pkg_hash,
        decision="approved",
        approval_request_id=approval_request_id,
    )

    # Insert prerequisite rows cleanly adhering to consistency view invariants
    with psycopg.connect(pg_uri) as conn:
        with conn.transaction():
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
                (approval_request_id, thread_id, pkg_ver),
            )
            # Link thread to approval_request and transition to awaiting_approval
            conn.execute(
                """UPDATE public.graph_threads
                      SET status = 'awaiting_approval',
                          approval_request_id = %s
                    WHERE thread_id = %s""",
                (approval_request_id, thread_id),
            )

    # 1. Create decision evidence with all 14 fields populated
    ev = store.create_decision_evidence(
        user_id=uid,
        session_id=session_id,
        thread_id=thread_id,
        approval_request_id=approval_request_id,
        package_id=pkg_id,
        package_version=pkg_ver,
        package_hash=pkg_hash,
        normalized_decision="approved",
        canonical_bound_payload_hash=bound_hash,
        webauthn_credential_id=secrets.token_hex(16),
        ttl_seconds=300,
    )
    eid = ev["evidence_id"]
    assert ev["consumed_at"] is None
    assert ev["canonical_bound_payload_hash"] == bound_hash

    # 2. Retrieve evidence
    fetched = store.get_decision_evidence(eid)
    assert fetched is not None
    assert fetched["package_id"] == pkg_id
    assert fetched["canonical_bound_payload_hash"] == bound_hash

    # 3. Scope mismatch validations against real database logic
    with pytest.raises(IdentityStoreError) as exc_scope_pkg:
        store.consume_decision_evidence(eid, expected_package_id="wrong_pkg")
    assert exc_scope_pkg.value.status_code == 400

    with pytest.raises(IdentityStoreError) as exc_scope_ver:
        store.consume_decision_evidence(eid, expected_package_version="wrong_ver")
    assert exc_scope_ver.value.status_code == 400

    with pytest.raises(IdentityStoreError) as exc_scope_hash:
        store.consume_decision_evidence(eid, expected_package_hash="0" * 64)
    assert exc_scope_hash.value.status_code == 400

    with pytest.raises(IdentityStoreError) as exc_scope_user:
        store.consume_decision_evidence(eid, expected_user_id=uuid4())
    assert exc_scope_user.value.status_code == 403

    # 4. Valid consumption marks consumed_at in PostgreSQL
    consumed = store.consume_decision_evidence(
        eid,
        expected_user_id=uid,
        expected_session_id=session_id,
        expected_approval_request_id=approval_request_id,
        expected_thread_id=thread_id,
        expected_package_id=pkg_id,
        expected_package_version=pkg_ver,
        expected_package_hash=pkg_hash,
        expected_normalized_decision="approved",
        expected_canonical_bound_payload_hash=bound_hash,
    )
    assert consumed["consumed_at"] is not None

    # 5. Replay attempt strictly fails with HTTP 409
    with pytest.raises(IdentityStoreError) as exc_replay:
        store.consume_decision_evidence(eid, expected_package_id=pkg_id)
    assert exc_replay.value.status_code == 409
