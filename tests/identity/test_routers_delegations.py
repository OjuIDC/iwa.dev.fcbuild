"""Unit and Integration Tests for Delegations Lifecycle Router (tests/identity/test_routers_delegations.py).

Verifies:
1. Step-Up Passkey Requirement (Finding R1):
   - Commissioner without step-up (auth_level="authenticated") is rejected with HTTP 403.
   - Commissioner with step-up (auth_level="step_up_passkey") successfully creates delegation (HTTP 201).
   - Legacy commissioner token with step-up level successfully creates delegation (HTTP 201).
2. Role and Tenancy Boundaries:
   - Non-commissioner roles (staff, coach, analyst, player) cannot create delegations (HTTP 403).
   - Bootstrap enrollment session (auth_level="bootstrap_enrollment") is quarantined (HTTP 403).
   - Service principals (BFF, Graph, Worker) cannot create delegations (HTTP 403).
   - Cross-club delegation attempts are rejected (HTTP 403).
3. Schema and Governance Hardening:
   - Prohibited governance actions (transfer, roster, club_strategy, policy_exception) fail with HTTP 422.
   - Extra fields in payload fail with HTTP 422 (extra="forbid").
4. Delegations Lifecycle & Revocation:
   - Full CRUD lifecycle: create (201) -> list (200) -> get (200) -> revoke (200) -> get confirms revoked.
   - Nonexistent delegation retrieval and revocation return HTTP 404.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet

from src.identity.crypto import reset_identity_crypto
from src.identity.store import IdentityStore
from tests.e2e.conftest import COM_HEADERS
from tests.e2e.phase2_helpers import (
    BFF_HEADERS,
    GRAPH_HEADERS,
    TEST_CLUB_ID,
    WORKER_HEADERS,
    session_header,
)


@pytest.fixture(autouse=True)
def setup_encryption_key(monkeypatch):
    """Ensure IDENTITY_ENCRYPTION_KEY is available."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", key)
    reset_identity_crypto()
    yield key
    reset_identity_crypto()


@pytest.fixture
def store(pg_uri: str) -> IdentityStore:
    return IdentityStore(pg_uri)


def _make_principal_session(
    store: IdentityStore,
    role: str = "commissioner",
    auth_level: str = "step_up_passkey",
    club_id: str = TEST_CLUB_ID,
) -> tuple[dict, str]:
    """Helper to provision a user with a specific role and session auth level."""
    uid = uuid4()
    user = store.create_user(
        club_id=club_id,
        login_id=f"{role}_{uid.hex[:8]}@fc27.club",
        display_name=f"Test {role.title()}",
        status="active",
    )
    store.assign_user_role(user["user_id"], role)
    token, _ = store.create_session(
        user_id=user["user_id"],
        auth_level=auth_level,
        auth_method="passkey",
    )
    return user, token


class TestDelegationsStepUpAuthorization:
    """Step-Up Passkey authorization requirement tests."""

    def test_commissioner_without_step_up_rejected_403(self, client, store: IdentityStore):
        """Commissioner with auth_level='authenticated' must be rejected with HTTP 403."""
        _, token = _make_principal_session(store, role="commissioner", auth_level="authenticated")
        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 24,
        }

        res = client.post("/v1/auth/delegations", headers=session_header(token), json=payload)
        assert res.status_code == 403
        detail = res.json().get("detail", "")
        assert "requires step-up passkey verification" in detail or "step_up_passkey" in detail

    def test_commissioner_with_step_up_succeeds_201(self, client, store: IdentityStore):
        """Commissioner with auth_level='step_up_passkey' successfully creates delegation."""
        _, token = _make_principal_session(store, role="commissioner", auth_level="step_up_passkey")
        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 24,
        }

        res = client.post("/v1/auth/delegations", headers=session_header(token), json=payload)
        assert res.status_code == 201
        data = res.json()
        assert "delegation_id" in data
        assert data["delegate_user_id"] == str(delegate_id)
        assert data["club_id"] == TEST_CLUB_ID
        assert data["is_active"] is True

    def test_legacy_commissioner_token_rejected_403(self, client):
        """Legacy COM_HEADERS carries CLI_HMAC / is_service=True and is rejected with HTTP 403 (R5)."""
        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 24,
        }

        res = client.post("/v1/auth/delegations", headers=COM_HEADERS, json=payload)
        assert res.status_code == 403


class TestDelegationsRoleAndIsolationBoundaries:
    """Role permissions, tenancy isolation, and quarantine checks."""

    @pytest.mark.parametrize("role", ["staff", "coach", "analyst", "player"])
    def test_non_commissioner_roles_rejected_403(self, client, store: IdentityStore, role: str):
        """Non-commissioner roles cannot create delegations even with step_up_passkey."""
        _, token = _make_principal_session(store, role=role, auth_level="step_up_passkey")
        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 24,
        }

        res = client.post("/v1/auth/delegations", headers=session_header(token), json=payload)
        assert res.status_code == 403

    def test_bootstrap_enrollment_session_quarantined_403(self, client, store: IdentityStore):
        """Session with auth_level='bootstrap_enrollment' is quarantined from creating delegations."""
        uid = uuid4()
        user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"comm_boot_{uid.hex[:8]}@fc27.club",
            display_name="Bootstrap Commissioner",
            status="active",
        )
        store.assign_user_role(user["user_id"], "commissioner")
        token, _ = store.create_session(
            user_id=user["user_id"],
            auth_level="bootstrap_enrollment",
            auth_method="bootstrap",
        )

        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 24,
        }

        res = client.post("/v1/auth/delegations", headers=session_header(token), json=payload)
        assert res.status_code == 403
        detail = res.json().get("detail", "")
        assert "restricted to passkey registration" in detail or "bootstrap" in detail

    @pytest.mark.parametrize("svc_headers", [BFF_HEADERS, GRAPH_HEADERS, WORKER_HEADERS])
    def test_service_principal_cannot_create_delegation_403(self, client, svc_headers: dict[str, str]):
        """Service accounts cannot perform human decision actions like creating delegations."""
        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 24,
        }

        res = client.post("/v1/auth/delegations", headers=svc_headers, json=payload)
        assert res.status_code == 403

    def test_cross_club_delegation_denied_403(self, client, store: IdentityStore):
        """Commissioner cannot create delegation for a club different from their own."""
        _, token = _make_principal_session(store, role="commissioner", auth_level="step_up_passkey", club_id=TEST_CLUB_ID)
        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": "999999",
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 24,
        }

        res = client.post("/v1/auth/delegations", headers=session_header(token), json=payload)
        assert res.status_code == 403
        detail = res.json().get("detail", "")
        assert "another club" in detail or "cross-club" in detail


class TestDelegationsSchemaAndValidation:
    """Pydantic schema validation and governance checks."""

    @pytest.mark.parametrize("prohibited_act", ["transfer", "roster", "club_strategy", "policy_exception"])
    def test_prohibited_governance_actions_fail_422(self, client, prohibited_act: str):
        """Prohibited governance actions cannot be delegated and fail schema validation (HTTP 422)."""
        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": prohibited_act,
            "scope": "club",
            "expires_in_hours": 24,
        }

        res = client.post("/v1/auth/delegations", headers=COM_HEADERS, json=payload)
        assert res.status_code == 422

    def test_extra_fields_forbidden_422(self, client):
        """Extra or unknown fields in request payload trigger validation failure (HTTP 422)."""
        delegate_id = uuid4()
        payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 24,
            "malicious_extra_field": "injected",
        }

        res = client.post("/v1/auth/delegations", headers=COM_HEADERS, json=payload)
        assert res.status_code == 422


class TestDelegationsLifecycleAndRevocation:
    """Full HTTP CRUD lifecycle and revocation tests."""

    def test_delegation_crud_and_revocation_lifecycle(self, client, store: IdentityStore):
        """Verify create -> list -> get -> revoke -> get lifecycle."""
        _, token = _make_principal_session(store, role="commissioner", auth_level="step_up_passkey")
        headers = session_header(token)
        delegate_id = uuid4()

        # 1. Create delegation
        create_payload = {
            "delegate_user_id": str(delegate_id),
            "club_id": TEST_CLUB_ID,
            "action_type": "package:approve",
            "scope": "club",
            "expires_in_hours": 48,
        }
        res_create = client.post("/v1/auth/delegations", headers=headers, json=create_payload)
        assert res_create.status_code == 201
        created = res_create.json()
        del_id = created["delegation_id"]
        assert created["is_active"] is True
        assert created["revoked_at"] is None

        # 2. List delegations
        res_list = client.get("/v1/auth/delegations", headers=headers)
        assert res_list.status_code == 200
        items = res_list.json()
        assert isinstance(items, list)
        matching = [d for d in items if d["delegation_id"] == del_id]
        assert len(matching) == 1

        # 3. Retrieve delegation by ID
        res_get = client.get(f"/v1/auth/delegations/{del_id}", headers=headers)
        assert res_get.status_code == 200
        del_details = res_get.json()
        assert del_details["delegation_id"] == del_id
        assert del_details["revoked_at"] is None

        # 4. Revoke delegation
        res_rev = client.delete(f"/v1/auth/delegations/{del_id}?reason=test_revocation", headers=headers)
        assert res_rev.status_code == 200
        rev_data = res_rev.json()
        assert rev_data["is_active"] is False
        assert rev_data["revoked_at"] is not None

        # 5. Confirm revoked status persists
        res_get_rev = client.get(f"/v1/auth/delegations/{del_id}", headers=headers)
        assert res_get_rev.status_code == 200
        final_details = res_get_rev.json()
        assert final_details["is_active"] is False
        assert final_details["revoked_at"] is not None

    def test_nonexistent_delegation_returns_404(self, client, store: IdentityStore):
        """Requests for nonexistent delegation UUIDs return HTTP 404."""
        _, token = _make_principal_session(store, role="commissioner", auth_level="step_up_passkey")
        headers = session_header(token)
        fake_id = uuid4()

        res_get = client.get(f"/v1/auth/delegations/{fake_id}", headers=headers)
        assert res_get.status_code == 404

        res_del = client.delete(f"/v1/auth/delegations/{fake_id}", headers=headers)
        assert res_del.status_code == 404
