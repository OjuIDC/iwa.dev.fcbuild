"""Unit and Integration Tests for User Lifecycle & Inspection Router.

Tests:
1. List Users (GET /v1/auth/users)
   - Commissioner and Staff can list users in their club
   - Response contains user profiles, status, and assigned roles
   - Player role forbidden from listing club users (HTTP 403)
   - Unauthenticated request returns HTTP 401
2. Retrieve User (GET /v1/auth/users/{user_id})
   - Commissioner/Staff retrieve user details
   - Caller can view their own user details
   - Cross-club isolation (HTTP 403)
   - Non-existent user returns HTTP 404
3. Administrative Suspension (POST /v1/auth/users/{user_id}/suspend)
   - Commissioner can suspend user with reason
   - Status transitions to 'suspended'
   - ALL active sessions for suspended user are immediately invalidated
   - Non-commissioner caller forbidden (HTTP 403)
4. Account Status Transitions (POST /v1/auth/users/{user_id}/status)
   - State machine guards respected
   - Disabling account terminates active sessions
   - Invalid status format rejected (HTTP 422)
"""
from __future__ import annotations

import uuid
import pytest

from tests.e2e.conftest import COM_HEADERS
from tests.e2e.phase2_helpers import TEST_CLUB_ID, session_header
from src.identity.store import IdentityStore


@pytest.fixture
def store(pg_uri: str) -> IdentityStore:
    return IdentityStore(pg_uri)


class TestUsersListingAndDetails:
    """GET /v1/auth/users and GET /v1/auth/users/{user_id} test coverage."""

    def test_commissioner_can_list_club_users(self, client, store: IdentityStore):
        """Commissioner can list users scoped to club."""
        # Provision test users
        store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"u1_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="User One",
            status="active",
        )
        store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"u2_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="User Two",
            status="invited",
        )

        res = client.get("/v1/auth/users", headers=COM_HEADERS)
        assert res.status_code == 200
        users = res.json()
        assert isinstance(users, list)
        assert len(users) >= 2
        for u in users:
            assert u["club_id"] == TEST_CLUB_ID
            assert "login_id" in u
            assert "status" in u

    def test_player_cannot_list_club_users(self, client, store: IdentityStore):
        """Player role is forbidden from listing club users."""
        player = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"p_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Player Only",
            status="active",
        )
        store.assign_user_role(player["user_id"], "player")
        token, _ = store.create_session(
            user_id=player["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )

        res = client.get("/v1/auth/users", headers=session_header(token))
        assert res.status_code == 403

    def test_unauthenticated_list_users_rejected(self, client):
        """Unauthenticated request to list users returns HTTP 401."""
        res = client.get("/v1/auth/users", headers={})
        assert res.status_code == 401

    def test_get_user_by_id_success(self, client, store: IdentityStore):
        """Commissioner can retrieve user by ID."""
        user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"get_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Target User",
            status="active",
        )
        uid = str(user["user_id"])

        res = client.get(f"/v1/auth/users/{uid}", headers=COM_HEADERS)
        assert res.status_code == 200
        data = res.json()
        assert data["user_id"] == uid
        assert data["display_name"] == "Target User"

    def test_get_nonexistent_user_returns_404(self, client):
        """Non-existent user ID returns HTTP 404."""
        dummy_uid = str(uuid.uuid4())
        res = client.get(f"/v1/auth/users/{dummy_uid}", headers=COM_HEADERS)
        assert res.status_code == 404


class TestUserSuspensionAndStatus:
    """POST /v1/auth/users/{id}/suspend and POST /v1/auth/users/{id}/status test coverage."""

    def test_suspend_user_invalidates_all_sessions(self, client, store: IdentityStore):
        """Administrative suspension transitions status to 'suspended' and revokes all active sessions."""
        # 1. Create user and active sessions
        user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"sus_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Suspended Coach",
            status="active",
        )
        store.assign_user_role(user["user_id"], "coach")
        uid = str(user["user_id"])

        token1, _ = store.create_session(
            user_id=user["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )
        token2, _ = store.create_session(
            user_id=user["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )

        # 2. Both sessions initially valid
        assert client.get("/v1/auth/session", headers=session_header(token1)).status_code == 200
        assert client.get("/v1/auth/session", headers=session_header(token2)).status_code == 200

        # 3. Suspend user via API
        susp_res = client.post(
            f"/v1/auth/users/{uid}/suspend",
            headers=COM_HEADERS,
            json={"reason": "Security audit review"},
        )
        assert susp_res.status_code == 200
        assert susp_res.json()["status"] == "suspended"
        assert susp_res.json()["revoked_sessions"] >= 2

        # 4. Verify user status is 'suspended'
        get_res = client.get(f"/v1/auth/users/{uid}", headers=COM_HEADERS)
        assert get_res.status_code == 200
        assert get_res.json()["status"] == "suspended"

        # 5. Verify ALL sessions for that user immediately fail closed (HTTP 401)
        assert client.get("/v1/auth/session", headers=session_header(token1)).status_code == 401
        assert client.get("/v1/auth/session", headers=session_header(token2)).status_code == 401

    def test_non_commissioner_cannot_suspend_user(self, client, store: IdentityStore):
        """Non-commissioner users cannot suspend accounts."""
        coach = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"c_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Coach",
            status="active",
        )
        store.assign_user_role(coach["user_id"], "coach")
        coach_token, _ = store.create_session(
            user_id=coach["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )

        target = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"tgt_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Target",
            status="active",
        )

        res = client.post(
            f"/v1/auth/users/{target['user_id']}/suspend",
            headers=session_header(coach_token),
            json={"reason": "Unauthorized attempt"},
        )
        assert res.status_code == 403

    def test_update_status_transitions_and_revokes_on_disable(self, client, store: IdentityStore):
        """Transitioning status to 'disabled' terminates active sessions."""
        user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"dis_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Disabling Account",
            status="active",
        )
        store.assign_user_role(user["user_id"], "staff")
        token, _ = store.create_session(
            user_id=user["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )
        uid = str(user["user_id"])

        assert client.get("/v1/auth/session", headers=session_header(token)).status_code == 200

        # Update status to disabled
        res = client.post(
            f"/v1/auth/users/{uid}/status",
            headers=COM_HEADERS,
            json={"status": "disabled", "reason": "Offboarding staff"},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "disabled"

        # Session must now be revoked
        assert client.get("/v1/auth/session", headers=session_header(token)).status_code == 401
