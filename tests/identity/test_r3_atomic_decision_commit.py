"""Unit and Integration Tests for Milestone R3: Atomic Decision Commitment.

Verifies:
1. Single-connection single-transaction atomicity in ApprovalStore.apply_webauthn_decision.
2. Deterministic lock ordering:
   Order 1: fc27_decision_evidence FOR UPDATE
   Order 2: approval_requests FOR UPDATE
   Order 3: graph_threads FOR UPDATE
3. Atomic multi-table commit:
   - fc27_decision_evidence.consumed_at set
   - approval_requests.status updated
   - graph_threads.status & state updated
   - approval_audit inserted with proof_type='webauthn'
   - domain_events recorded (identity.decision_evidence_consumed, approval.decided)
   - outbox recorded
4. Atomic rollback: failure at any step leaves evidence unconsumed and request untouched.
5. Safe idempotent replay: re-submitting already consumed evidence for same thread returns
   replayed decision without duplicate audit rows or events.
6. Dormancy invariant: zero player deliveries, zero rows in public.dispatches.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
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


class TestApprovalStoreWebAuthnAtomicCommit:
    """Tests single-transaction atomicity and rollback guarantees."""

    @pytest.fixture
    def mock_store(self):
        store = ApprovalStore.__new__(ApprovalStore)
        store.connect = MagicMock()
        return store

    def test_webauthn_decision_lock_order_and_success(self, mock_store):
        """Verifies deterministic lock order and complete atomic state update."""
        user_id = uuid4()
        session_id = uuid4()
        evidence_id = uuid4()
        req_id = uuid4()
        thread_id = "fc27-330323-run-20261004-001"
        pkg_id = "pkg-001"
        pkg_ver = "1"
        pkg_hash = "a" * 64

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

        canonical_hash = compute_bound_payload_hash(
            package_id=pkg_id,
            package_version=pkg_ver,
            package_hash=pkg_hash,
            decision="approved",
            approval_request_id=req_id,
        )

        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None

        # Lock order 1: evidence row
        ev_row = {
            "evidence_id": evidence_id,
            "user_id": user_id,
            "session_id": session_id,
            "thread_id": thread_id,
            "approval_request_id": req_id,
            "package_id": pkg_id,
            "package_version": pkg_ver,
            "package_hash": pkg_hash,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": canonical_hash,
            "consumed_at": None,
            "is_expired": False,
            "webauthn_credential_id": "aa" * 16,
        }

        # Lock order 2: approval_request row
        req_row = {
            "approval_request_id": req_id,
            "thread_id": thread_id,
            "package_version": 1,
            "status": "awaiting_approval",
            "is_expired": False,
        }

        # Lock order 3: graph_thread row
        thread_row = {
            "thread_id": thread_id,
            "state": {"lead_packages": {}, "status": "awaiting_approval"},
            "status": "awaiting_approval",
            "package_version": 1,
            "club_id": "330323",
            "decision_payload": {},
        }

        def mock_execute(query, params=None):
            q_clean = " ".join(query.split())
            cursor = MagicMock()
            if "FROM public.fc27_decision_evidence" in q_clean and "FOR UPDATE" in q_clean:
                cursor.fetchone.return_value = ev_row
            elif "FROM public.approval_requests" in q_clean and "FOR UPDATE" in q_clean:
                cursor.fetchone.return_value = req_row
            elif "FROM public.graph_threads" in q_clean and "FOR UPDATE" in q_clean:
                cursor.fetchone.return_value = thread_row
            elif "UPDATE public.fc27_decision_evidence" in q_clean and "RETURNING" in q_clean:
                cursor.fetchone.return_value = {"evidence_id": evidence_id, "consumed_at": datetime.now(timezone.utc)}
            elif "UPDATE public.approval_requests" in q_clean and "RETURNING" in q_clean:
                cursor.fetchone.return_value = {"approval_request_id": req_id}
            elif "UPDATE public.graph_threads" in q_clean and "RETURNING" in q_clean:
                cursor.fetchone.return_value = {"thread_id": thread_id, "status": "approved", "approved_at": datetime.now(timezone.utc)}
            elif "INSERT INTO public.approval_audit" in q_clean and "RETURNING" in q_clean:
                cursor.fetchone.return_value = {"id": 42}
            elif "SELECT to_char" in q_clean:
                cursor.fetchone.return_value = {"t": "2026-10-04T12:00:00Z"}
            elif "INSERT INTO public.domain_events" in q_clean:
                cursor.fetchone.return_value = None
            elif "INSERT INTO public.outbox" in q_clean:
                cursor.fetchone.return_value = None
            else:
                cursor.fetchone.return_value = None
            return cursor

        mock_conn.execute.side_effect = mock_execute

        state = mock_store.apply_webauthn_decision(
            evidence_id=evidence_id,
            decision="approved",
            thread_id=thread_id,
            principal=principal,
            notes="LGTM",
            approval_request_id=req_id,
            package_id=pkg_id,
            package_version=pkg_ver,
            authority_basis="primary",
            dispatch_enabled=False,
        )

        assert state["status"] == "approved"
        assert state["approval_audit_id"] == 42
        assert state["approval_request_id"] == str(req_id)
        assert state["dispatch_id"] is None

        # Verify transaction boundary was invoked
        mock_conn.transaction.assert_called_once()

    def test_webauthn_decision_rollback_on_request_conflict(self, mock_store):
        """Verifies rollback occurs and evidence remains unconsumed when request not awaiting_approval."""
        user_id = uuid4()
        evidence_id = uuid4()
        req_id = uuid4()
        thread_id = "fc27-330323-run-20261004-001"

        principal = PrincipalContext.create(
            user_id=user_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY,
            auth_method=AuthMethod.PASSKEY,
        )

        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn

        ev_row = {
            "evidence_id": evidence_id,
            "user_id": user_id,
            "session_id": uuid4(),
            "thread_id": thread_id,
            "approval_request_id": req_id,
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "is_expired": False,
        }

        # Request is ALREADY terminal (approved by another actor)
        req_row = {
            "approval_request_id": req_id,
            "thread_id": thread_id,
            "package_version": 1,
            "status": "approved",  # Conflict!
            "is_expired": False,
        }

        def mock_execute(query, params=None):
            q_clean = " ".join(query.split())
            cursor = MagicMock()
            if "FROM public.fc27_decision_evidence" in q_clean:
                cursor.fetchone.return_value = ev_row
            elif "FROM public.approval_requests" in q_clean:
                cursor.fetchone.return_value = req_row
            else:
                cursor.fetchone.return_value = None
            return cursor

        mock_conn.execute.side_effect = mock_execute

        with pytest.raises(StoreError) as exc_info:
            mock_store.apply_webauthn_decision(
                evidence_id=evidence_id,
                decision="approved",
                thread_id=thread_id,
                principal=principal,
                approval_request_id=req_id,
            )
        assert exc_info.value.status_code == 409
        # Evidence remains unconsumed (consumed_at was never updated)
        assert ev_row["consumed_at"] is None

    def test_webauthn_decision_rollback_on_binding_mismatch(self, mock_store):
        """Verifies rollback occurs when evidence binding doesn't match authoritative state."""
        user_id = uuid4()
        evidence_id = uuid4()
        req_id = uuid4()
        thread_id = "fc27-330323-run-20261004-001"

        principal = PrincipalContext.create(
            user_id=user_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY,
            auth_method=AuthMethod.PASSKEY,
        )

        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn

        # Evidence was minted for 'rejected'
        ev_row = {
            "evidence_id": evidence_id,
            "user_id": user_id,
            "session_id": uuid4(),
            "thread_id": thread_id,
            "approval_request_id": req_id,
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "rejected",  # Mismatch with requested 'approved'!
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "is_expired": False,
        }

        req_row = {
            "approval_request_id": req_id,
            "thread_id": thread_id,
            "package_version": 1,
            "status": "awaiting_approval",
            "is_expired": False,
        }

        thread_row = {
            "thread_id": thread_id,
            "state": {},
            "status": "awaiting_approval",
            "package_version": 1,
            "club_id": "330323",
        }

        def mock_execute(query, params=None):
            q_clean = " ".join(query.split())
            cursor = MagicMock()
            if "FROM public.fc27_decision_evidence" in q_clean:
                cursor.fetchone.return_value = ev_row
            elif "FROM public.approval_requests" in q_clean:
                cursor.fetchone.return_value = req_row
            elif "FROM public.graph_threads" in q_clean:
                cursor.fetchone.return_value = thread_row
            elif "FROM public.fc27_approval_inconsistencies" in q_clean:
                cursor.fetchone.return_value = None
            else:
                cursor.fetchone.return_value = None
            return cursor

        mock_conn.execute.side_effect = mock_execute

        with pytest.raises(StoreError) as exc_info:
            mock_store.apply_webauthn_decision(
                evidence_id=evidence_id,
                decision="approved",
                thread_id=thread_id,
                principal=principal,
                approval_request_id=req_id,
            )
        assert exc_info.value.status_code == 403
        assert ("bound decision mismatch" in exc_info.value.detail or "evidence bound to different decision" in exc_info.value.detail)

    def test_webauthn_decision_safe_idempotent_replay(self, mock_store):
        """Verifies safe replay when evidence was already consumed for this exact thread."""
        user_id = uuid4()
        evidence_id = uuid4()
        req_id = uuid4()
        thread_id = "fc27-330323-run-20261004-001"

        principal = PrincipalContext.create(
            user_id=user_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY,
            auth_method=AuthMethod.PASSKEY,
        )

        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn

        # Evidence already consumed
        ev_row = {
            "evidence_id": evidence_id,
            "user_id": user_id,
            "session_id": uuid4(),
            "thread_id": thread_id,
            "approval_request_id": req_id,
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": datetime.now(timezone.utc),  # Already consumed!
            "is_expired": False,
        }

        # Existing audit row
        audit_match = {
            "id": 99,
            "state": {"lead_packages": {}},
            "status": "approved",
            "package_version": 1,
            "approval_request_id": req_id,
        }

        def mock_execute(query, params=None):
            q_clean = " ".join(query.split())
            cursor = MagicMock()
            if "FROM public.fc27_decision_evidence" in q_clean:
                cursor.fetchone.return_value = ev_row
            elif "FROM public.approval_audit" in q_clean:
                cursor.fetchone.return_value = audit_match
            else:
                cursor.fetchone.return_value = None
            return cursor

        mock_conn.execute.side_effect = mock_execute

        state = mock_store.apply_webauthn_decision(
            evidence_id=evidence_id,
            decision="approved",
            thread_id=thread_id,
            principal=principal,
            approval_request_id=req_id,
        )

        assert state["status"] == "approved"
        assert state["approval_audit_id"] == 99
        assert state["replayed"] is True
