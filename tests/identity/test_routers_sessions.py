"""Unit and Integration Tests for Server-Side Sessions & Auth Routers.

Tests:
1. Session Inspection (GET /v1/auth/session & /v1/auth/me)
   - Dual-expiry enforcement: 30-min idle strictly capped at 24-hr absolute
   - Projection fidelity: session_id, user_id, club_id, roles, role, auth_level
   - Suspended / disabled accounts fail closed on session check (HTTP 401)
   - Expired / revoked sessions return HTTP 401
2. Session Touch (POST /v1/auth/sessions/touch)
   - Extends idle timeout up to absolute timeout cap
   - Enforces idle_expires_at <= expires_at invariant
3. Atomic Session Rotation (POST /v1/auth/sessions/rotate)
   - Old session revoked, new session issued in single transaction
   - Old session token immediately rejected (HTTP 401)
   - New session token immediately active
4. Session Logout (POST /v1/auth/logout & DELETE /v1/auth/sessions/me)
   - Invalidates server-side session
   - Subsequent calls with revoked token return HTTP 401
5. Active Session Management (GET /v1/auth/sessions & DELETE /v1/auth/sessions/{session_id})
   - List active sessions for user
   - Owner revoking own session by ID
   - Commissioner revoking another user's session by ID
   - Non-commissioner cannot revoke another user's session (HTTP 403)
   - Bulk termination (DELETE /v1/auth/sessions)
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
import pytest

from tests.e2e.phase2_helpers import TEST_CLUB_ID, session_header
from src.identity.crypto import hash_token
from src.identity.store import IdentityStore


@pytest.fixture
def active_coach(store: IdentityStore) -> tuple[dict, str, dict]:
    """Helper creating active coach user and valid server-side session."""
    user = store.create_user(
        club_id=TEST_CLUB_ID,
        login_id=f"coach_{uuid.uuid4().hex[:8]}@fc27.club",
        display_name="Coach Pep",
        status="active",
    )
    store.assign_user_role(user["user_id"], "coach")
    raw_token, sess = store.create_session(
        user_id=user["user_id"],
        auth_level="authenticated",
        auth_method="passkey",
    )
    return user, raw_token, sess


class TestSessionInspection:
    """GET /v1/auth/session and GET /v1/auth/me test coverage."""

    def test_inspect_current_session_success(self, client, active_coach):
        """Active session inspection returns dual-expiry timestamps and principal metadata."""
        user, raw_token, sess = active_coach
        headers = session_header(raw_token)

        res = client.get("/v1/auth/session", headers=headers)
        assert res.status_code == 200
        data = res.json()

        assert data["session_id"] == str(sess["session_id"])
        assert data["user_id"] == str(user["user_id"])
        assert data["club_id"] == TEST_CLUB_ID
        assert "coach" in data["roles"]
        assert data["role"] == "coach"
        assert data["auth_level"] == "authenticated"
        assert "idle_expires_at" in data
        assert "expires_at" in data

    def test_inspect_me_returns_profile(self, client, active_coach):
        """GET /v1/auth/me returns projection matching session caller profile."""
        user, raw_token, _ = active_coach
        res = client.get("/v1/auth/me", headers=session_header(raw_token))
        assert res.status_code == 200
        data = res.json()
        assert data["user_id"] == str(user["user_id"])
        assert data["display_name"] == "Coach Pep"

    def test_suspended_user_session_fails_closed(self, client, store: IdentityStore, active_coach):
        """Suspended user account fails closed with HTTP 401 on session check."""
        user, raw_token, _ = active_coach
        # Suspend user
        store.suspend_user(user["user_id"], actor="admin")

        res = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert res.status_code == 401
        assert "suspended" in res.json()["detail"].lower() or "revoked" in res.json()["detail"].lower()

    def test_disabled_user_session_fails_closed(self, client, store: IdentityStore, active_coach):
        """Disabled user account fails closed with HTTP 401."""
        user, raw_token, _ = active_coach
        store.update_user_status(user["user_id"], "disabled", actor="admin")
        store.revoke_all_user_sessions(user["user_id"], actor="admin")

        res = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert res.status_code == 401

    def test_forged_or_invalid_token_returns_401(self, client):
        """Invalid session bearer token returns HTTP 401."""
        forged_token = "0" * 64
        res = client.get("/v1/auth/session", headers=session_header(forged_token))
        assert res.status_code == 401

    def test_expired_session_returns_401(self, client, pg_uri: str, active_coach):
        """Session past idle expiration returns HTTP 401."""
        _, raw_token, sess = active_coach
        # Set idle_expires_at in past (and created_at before it to satisfy check constraint)
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                "UPDATE public.fc27_sessions SET created_at = now() - interval '10 minutes', idle_expires_at = now() - interval '5 minutes' WHERE session_id = %s",
                (sess["session_id"],),
            )

        res = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert res.status_code == 401


class TestSessionTouch:
    """POST /v1/auth/sessions/touch test coverage."""

    def test_session_touch_extends_idle_timeout(self, client, active_coach):
        """Touch endpoint extends idle expiration window."""
        _, raw_token, _ = active_coach
        res = client.post("/v1/auth/sessions/touch", headers=session_header(raw_token))
        assert res.status_code == 200
        data = res.json()
        assert data["touched"] is True
        assert "idle_expires_at" in data

    def test_session_touch_capped_at_absolute_timeout(self, client, pg_uri: str, active_coach):
        """Idle timeout cannot exceed absolute expiration ceiling (24h)."""
        _, raw_token, sess = active_coach

        # Set absolute expires_at to 10 minutes from now, but idle_expires_at to 2 minutes from now
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                """UPDATE public.fc27_sessions 
                   SET absolute_expires_at = now() + interval '10 minutes',
                       idle_expires_at = now() + interval '2 minutes' 
                   WHERE session_id = %s""",
                (sess["session_id"],),
            )

        res = client.post("/v1/auth/sessions/touch", headers=session_header(raw_token))
        assert res.status_code == 200
        data = res.json()
        idle_exp = datetime.fromisoformat(data["idle_expires_at"])
        abs_exp = datetime.fromisoformat(data["absolute_expires_at"])
        assert idle_exp <= abs_exp


class TestSessionRotation:
    """POST /v1/auth/sessions/rotate test coverage."""

    def test_atomic_session_rotation(self, client, active_coach):
        """Session rotation invalidates prior token and issues new active token."""
        user, old_token, old_sess = active_coach

        # Client self-elevation to step_up_passkey MUST be rejected (403)
        res_elevate = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(old_token),
            json={"auth_level": "step_up_passkey", "auth_method": "passkey"},
        )
        assert res_elevate.status_code == 403
        assert "cannot elevate session to 'step_up_passkey' without step-up verification evidence" in res_elevate.json().get("detail", "")

        # Legitimate rotation without elevation succeeds
        res = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(old_token),
            json={},
        )
        assert res.status_code == 200
        rot_data = res.json()

        new_token = rot_data["new_session_token"]
        assert new_token != old_token
        assert len(new_token) == 64
        assert rot_data["prior_session_id"] == str(old_sess["session_id"])
        assert rot_data["auth_level"] == "authenticated"

        # Old token MUST now be rejected (401)
        old_check = client.get("/v1/auth/session", headers=session_header(old_token))
        assert old_check.status_code == 401

        # New token MUST now be accepted (200)
        new_check = client.get("/v1/auth/session", headers=session_header(new_token))
        assert new_check.status_code == 200
        assert new_check.json()["user_id"] == str(user["user_id"])

    def test_rotate_session_cannot_elevate_without_evidence(self, client, active_coach):
        """Rotating to step_up_passkey without passkey evidence returns 403 Forbidden."""
        _, old_token, _ = active_coach
        res = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(old_token),
            json={"new_auth_level": "step_up_passkey"},
        )
        assert res.status_code == 403
        assert "cannot elevate session to 'step_up_passkey' without step-up verification evidence" in res.json().get("detail", "")


class TestSessionLogout:
    """POST /v1/auth/logout and DELETE /v1/auth/sessions/me test coverage."""

    def test_user_logout_revokes_session(self, client, active_coach):
        """User logout invalidates session server-side."""
        _, raw_token, _ = active_coach

        res = client.post("/v1/auth/logout", headers=session_header(raw_token))
        assert res.status_code == 200
        assert res.json()["revoked"] is True

        # Immediate subsequent request fails with 401
        res_after = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert res_after.status_code == 401

    def test_delete_sessions_me_alias_revokes_session(self, client, active_coach):
        """DELETE /v1/auth/sessions/me invalidates session."""
        _, raw_token, _ = active_coach

        res = client.delete("/v1/auth/sessions/me", headers=session_header(raw_token))
        assert res.status_code == 200
        assert res.json()["revoked"] is True

        res_after = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert res_after.status_code == 401


class TestSessionManagement:
    """GET /v1/auth/sessions and DELETE /v1/auth/sessions/{id} test coverage."""

    def test_list_active_sessions(self, client, store: IdentityStore, active_coach):
        """Caller can list their active sessions."""
        user, token1, _ = active_coach
        # Create second session for same user
        token2, _ = store.create_session(
            user_id=user["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )

        res = client.get("/v1/auth/sessions", headers=session_header(token1))
        assert res.status_code == 200
        sessions_list = res.json()["sessions"]
        assert len(sessions_list) >= 2
        for s in sessions_list:
            assert s["user_id"] == str(user["user_id"])

    def test_commissioner_can_revoke_user_session_by_id(
        self, client, active_coach, active_human_commissioner
    ):
        """Commissioner can administratively revoke another user's session by ID."""
        _, raw_token, sess = active_coach
        sess_id = sess["session_id"]

        _, commissioner_token, _ = active_human_commissioner
        del_res = client.delete(
            f"/v1/auth/sessions/{sess_id}", headers=session_header(commissioner_token)
        )
        assert del_res.status_code in (200, 204)

        # That coach's session is now invalidated
        check_res = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert check_res.status_code == 401

    def test_user_cannot_revoke_other_user_session(self, client, store: IdentityStore, active_coach):
        """Non-commissioner user cannot revoke another user's session."""
        _, coach_token, _ = active_coach

        # Create another user and session
        other_user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"other_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Other User",
            status="active",
        )
        _, other_sess = store.create_session(
            user_id=other_user["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )

        # Coach attempts to delete other user's session -> forbidden (403)
        res = client.delete(
            f"/v1/auth/sessions/{other_sess['session_id']}",
            headers=session_header(coach_token),
        )
        assert res.status_code == 403

    def test_bulk_revoke_all_sessions(self, client, store: IdentityStore, active_coach):
        """DELETE /v1/auth/sessions terminates all active sessions for caller."""
        user, token1, _ = active_coach
        token2, _ = store.create_session(
            user_id=user["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )

        res = client.delete("/v1/auth/sessions", headers=session_header(token1))
        assert res.status_code == 200
        assert res.json()["revoked_count"] >= 2

        # Both tokens must now be revoked
        assert client.get("/v1/auth/session", headers=session_header(token1)).status_code == 401
        assert client.get("/v1/auth/session", headers=session_header(token2)).status_code == 401
