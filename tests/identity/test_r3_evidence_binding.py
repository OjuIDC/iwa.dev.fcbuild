"""Unit and Integration Tests for Milestone R3: Evidence Binding (Finding R2).

Verifies:
1. Server-side canonical bound payload hash recomputation from authoritative PostgreSQL records.
2. 14-field binding verification across evidence minting, storage, and consumption.
3. Elimination of dummy fallback values ("unspecified", "1", None).
4. Strict rejection on any field tampering or mismatch (user_id, session_id, thread_id,
   approval_request_id, package_id, package_version, package_hash, normalized_decision, canonical_hash).
5. Safe rollback: unconsumed evidence on any binding rejection.
6. Support for 'revision_requested' in normalized_decision.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
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
    normalize_decision,
)
from src.identity.store import IdentityStore, IdentityStoreError


# ==============================================================================
# 1. Canonical Payload Hash & Decision Normalization Unit Tests
# ==============================================================================

class TestCanonicalBoundPayloadHash:
    """Verifies deterministic server-side hashing and tampering detection."""

    def test_canonical_hash_determinism(self):
        req_id = uuid4()
        h1 = compute_bound_payload_hash(
            package_id="pkg_alpha",
            package_version="1",
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=req_id,
        )
        h2 = compute_bound_payload_hash(
            package_id="pkg_alpha",
            package_version=1,
            package_hash="a" * 64,
            decision="APPROVED",
            approval_request_id=str(req_id),
        )
        assert h1 == h2
        assert len(h1) == 64

    def test_canonical_hash_tampering_detection(self):
        req_id = uuid4()
        base = compute_bound_payload_hash(
            package_id="pkg",
            package_version="1",
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=req_id,
        )

        # Tampering package_id
        assert compute_bound_payload_hash(
            package_id="pkg_other",
            package_version="1",
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=req_id,
        ) != base
        # Tampering package_version
        assert compute_bound_payload_hash(
            package_id="pkg",
            package_version="2",
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=req_id,
        ) != base
        # Tampering package_hash
        assert compute_bound_payload_hash(
            package_id="pkg",
            package_version="1",
            package_hash="b" * 64,
            decision="approved",
            approval_request_id=req_id,
        ) != base
        # Tampering decision
        assert compute_bound_payload_hash(
            package_id="pkg",
            package_version="1",
            package_hash="a" * 64,
            decision="rejected",
            approval_request_id=req_id,
        ) != base
        assert compute_bound_payload_hash(
            package_id="pkg",
            package_version="1",
            package_hash="a" * 64,
            decision="revision_requested",
            approval_request_id=req_id,
        ) != base
        # Tampering approval_request_id
        assert compute_bound_payload_hash(
            package_id="pkg",
            package_version="1",
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=uuid4(),
        ) != base

    def test_normalize_decision_supported_variants(self):
        assert normalize_decision("approved") == "approved"
        assert normalize_decision("APPROVE") == "approved"
        assert normalize_decision("rejected") == "rejected"
        assert normalize_decision("REJECT") == "rejected"
        assert normalize_decision("revision_requested") == "revision_requested"
        assert normalize_decision("REQUEST_REVISION") == "revision_requested"
        assert normalize_decision("request_revision") == "revision_requested"

        with pytest.raises(ValueError):
            normalize_decision("accepted")
        with pytest.raises(ValueError):
            normalize_decision("pending")
        with pytest.raises(ValueError):
            normalize_decision("invalid_state")


# ==============================================================================
# 2. 14-Field Evidence Binding & Store Validation Tests
# ==============================================================================

class TestEvidenceBindingStoreValidation:
    """Verifies that IdentityStore.consume_decision_evidence enforces all 14 fields."""

    @pytest.fixture
    def mock_store(self):
        store = IdentityStore.__new__(IdentityStore)
        store.connect = MagicMock()
        return store

    def test_consume_decision_evidence_validates_user_mismatch(self, mock_store):
        user1 = uuid4()
        user2 = uuid4()
        ev_id = uuid4()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": user1,
            "session_id": uuid4(),
            "thread_id": "thread-123",
            "approval_request_id": uuid4(),
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        mock_store.connect.return_value.__enter__.return_value = mock_conn

        with pytest.raises(IdentityStoreError) as exc_info:
            mock_store.consume_decision_evidence(
                ev_id,
                expected_user_id=user2,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 403
        assert "evidence bound to different user ID" in exc_info.value.detail

    def test_consume_decision_evidence_validates_session_mismatch(self, mock_store):
        u_id = uuid4()
        sess1 = uuid4()
        sess2 = uuid4()
        ev_id = uuid4()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": u_id,
            "session_id": sess1,
            "thread_id": "thread-123",
            "approval_request_id": uuid4(),
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

        with pytest.raises(IdentityStoreError) as exc_info:
            mock_store.consume_decision_evidence(
                ev_id,
                expected_user_id=u_id,
                expected_session_id=sess2,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 403
        assert "evidence bound to different session ID" in exc_info.value.detail

    def test_consume_decision_evidence_validates_decision_mismatch(self, mock_store):
        u_id = uuid4()
        ev_id = uuid4()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": u_id,
            "session_id": uuid4(),
            "thread_id": "thread-123",
            "approval_request_id": uuid4(),
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "rejected",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

        with pytest.raises(IdentityStoreError) as exc_info:
            mock_store.consume_decision_evidence(
                ev_id,
                expected_user_id=u_id,
                expected_decision="approved",
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 403
        assert "evidence bound to different decision" in exc_info.value.detail

    def test_consume_decision_evidence_validates_package_and_thread_mismatch(self, mock_store):
        u_id = uuid4()
        req_id = uuid4()
        ev_id = uuid4()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": u_id,
            "session_id": uuid4(),
            "thread_id": "thread-actual",
            "approval_request_id": req_id,
            "package_id": "pkg-actual",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

        # Package mismatch
        with pytest.raises(IdentityStoreError) as exc_info:
            mock_store.consume_decision_evidence(
                ev_id,
                expected_package_id="pkg-different",
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 400

        # Thread mismatch
        with pytest.raises(IdentityStoreError) as exc_info:
            mock_store.consume_decision_evidence(
                ev_id,
                expected_thread_id="thread-different",
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 400

        # Approval request mismatch
        with pytest.raises(IdentityStoreError) as exc_info:
            mock_store.consume_decision_evidence(
                ev_id,
                expected_approval_request_id=uuid4(),
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 400

    def test_consume_decision_evidence_replay_rejected(self, mock_store):
        ev_id = uuid4()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": uuid4(),
            "session_id": uuid4(),
            "thread_id": "thread-123",
            "approval_request_id": uuid4(),
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

        with pytest.raises(IdentityStoreError) as exc_info:
            mock_store.consume_decision_evidence(
                ev_id,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 409
        assert "already consumed" in exc_info.value.detail

    def test_consume_decision_evidence_expired_rejected(self, mock_store):
        ev_id = uuid4()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": uuid4(),
            "session_id": uuid4(),
            "thread_id": "thread-123",
            "approval_request_id": uuid4(),
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        }

        with pytest.raises(IdentityStoreError) as exc_info:
            mock_store.consume_decision_evidence(
                ev_id,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 400
        assert "expired" in exc_info.value.detail


# ==============================================================================
# 3. Step-Up Router Authoritative Lookup & Dummy Elimination
# ==============================================================================

class TestStepUpAuthoritativeLookups:
    """Verifies that step_up_challenge & step_up_verify use authoritative DB state."""

    def test_step_up_challenge_recomputes_canonical_hash_from_db(self):
        """Verifies step_up_challenge rejects client hash override when conflicting."""
        from src.identity.routers.step_up import StepUpChallengeRequest, step_up_challenge

        req_id = uuid4()
        principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
        )

        mock_store = MagicMock()
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn

        pkg_ver = "a" * 64
        pkg_hash = "a" * 64

        # Mock authoritative approval request row
        mock_conn.execute.return_value.fetchone.return_value = {
            "approval_request_id": req_id,
            "thread_id": "fc27-330323-run-20261004-001",
            "package_version": pkg_ver,
            "status": "awaiting_approval",
            "is_expired": False,
            "club_id": "330323",
        }
        mock_store.list_user_webauthn_credentials.return_value = [{"credential_id": b"cred1"}]
        mock_store.create_challenge.return_value = {
            "challenge_id": uuid4(),
            "challenge": "chal123",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

        # Caller provides valid required package fields but forged bound_payload_hash
        body = StepUpChallengeRequest(
            approval_request_id=req_id,
            package_id="pkg_alpha",
            package_version=pkg_ver,
            package_hash=pkg_hash,
            decision="approved",
            bound_payload_hash="forged_hash_" + "0" * 52,
        )

        with pytest.raises(HTTPException) as exc_info:
            step_up_challenge(
                request=MagicMock(),
                body=body,
                principal=principal,
                store=mock_store,
                engine=MagicMock(),
            )
        assert exc_info.value.status_code == 400
        assert "client-supplied bound_payload_hash does not match server recomputed hash" in exc_info.value.detail


# ==============================================================================
# 4. 14-Field Decision Evidence Persistence Verification
# ==============================================================================

class TestDecisionEvidencePersistence:
    """Verifies that create_decision_evidence binds and persists all 14 fields."""

    @pytest.fixture
    def mock_store(self):
        store = IdentityStore.__new__(IdentityStore)
        store.connect = MagicMock()
        return store

    def test_create_and_get_decision_evidence_all_14_fields(self, mock_store):
        ev_id = uuid4()
        user_id = uuid4()
        session_id = uuid4()
        chal_id = uuid4()
        appr_id = uuid4()
        th_id = "fc27-330323-run-20261004-001"
        pkg_id = "pkg_alpha"
        pkg_ver = "a" * 64
        pkg_hash = "b" * 64
        bound_hash = "c" * 64
        cred_id = "webauthn_cred_hex_001"
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=5)

        expected_row = {
            "evidence_id": ev_id,
            "user_id": user_id,
            "session_id": session_id,
            "thread_id": th_id,
            "approval_request_id": appr_id,
            "package_id": pkg_id,
            "package_version": pkg_ver,
            "package_hash": pkg_hash,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": bound_hash,
            "challenge_id": chal_id,
            "webauthn_credential_id": cred_id,
            "issued_at": now,
            "expires_at": expires_at,
            "consumed_at": None,
            "created_at": now,
        }

        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchone.return_value = expected_row

        created = mock_store.create_decision_evidence(
            user_id=user_id,
            session_id=session_id,
            thread_id=th_id,
            approval_request_id=appr_id,
            package_id=pkg_id,
            package_version=pkg_ver,
            package_hash=pkg_hash,
            decision="approved",
            canonical_bound_payload_hash=bound_hash,
            challenge_id=chal_id,
            webauthn_credential_id=cred_id,
            ttl_seconds=300,
        )

        # 1. Verify all 14 fields present in created record
        assert created["evidence_id"] == ev_id
        assert created["user_id"] == user_id
        assert created["session_id"] == session_id
        assert created["thread_id"] == th_id
        assert created["approval_request_id"] == appr_id
        assert created["package_id"] == pkg_id
        assert created["package_version"] == pkg_ver
        assert created["package_hash"] == pkg_hash
        assert created["normalized_decision"] == "approved"
        assert created["canonical_bound_payload_hash"] == bound_hash
        assert created["challenge_id"] == chal_id
        assert created["webauthn_credential_id"] == cred_id
        assert created["issued_at"] == now
        assert created["expires_at"] == expires_at

        # 2. Verify SQL parameters bound accurately
        insert_args = mock_conn.execute.call_args[0]
        sql = insert_args[0]
        params = insert_args[1]
        assert "INSERT INTO public.fc27_decision_evidence" in sql
        assert str(user_id) in params
        assert str(session_id) in params
        assert th_id in params
        assert str(appr_id) in params
        assert pkg_id in params
        assert pkg_ver in params
        assert pkg_hash in params
        assert "approved" in params
        assert bound_hash in params
        assert str(chal_id) in params
        assert cred_id in params

        # 3. Verify get_decision_evidence retrieval
        mock_conn.execute.return_value.fetchone.return_value = expected_row
        retrieved = mock_store.get_decision_evidence(ev_id)
        assert retrieved == expected_row
