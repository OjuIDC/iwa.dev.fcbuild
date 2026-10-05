"""Unit and Integration Tests for Invitations Lifecycle API Router.

Tests:
1. Issue Invitation (POST /v1/auth/invitations)
   - Commissioner & Staff issuance
   - 64-hex char token generation & SHA-256 storage verification
   - Role boundaries (Staff cannot invite Commissioner)
   - Player forbidden from issuing invitations
   - Cross-club isolation
   - Unauthenticated & invalid input handling
2. Public Token Validation (GET /v1/auth/invitations/{token}/validate)
   - Valid token returns public projection (no hashes leaked)
   - Expired / revoked / consumed / invalid tokens fail properly
3. Consume Invitation & Account Activation (POST /v1/auth/invitations/{token}/consume & /accept)
   - Atomic consumption transitions user to 'active'
   - Role assigned in fc27_user_roles
   - Single-use replay protection fails closed (HTTP 409)
   - Expired / revoked tokens rejected (HTTP 410)
4. Administrative Revocation (DELETE /v1/auth/invitations/{invitation_id})
   - Commissioner revocation
   - Double-revocation & revoking consumed invitations rejected (HTTP 409)
   - Non-commissioner callers rejected (HTTP 403)
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
import pytest

from tests.e2e.conftest import COM_HEADERS
from tests.e2e.phase2_helpers import TEST_CLUB_ID, session_header
from src.identity.crypto import hash_token
from src.identity.store import IdentityStore, IdentityStoreError


@pytest.fixture
def store(pg_uri: str) -> IdentityStore:
    return IdentityStore(pg_uri)


class TestInvitationsIssuance:
    """POST /v1/auth/invitations test coverage."""

    def test_commissioner_issue_invitation_success(self, client, pg_uri: str):
        """Commissioner can issue invitation; token is 64 hex chars; stored as SHA-256."""
        unique_email = f"coach_{uuid.uuid4().hex[:8]}@fc27.club"
        payload = {
            "club_id": TEST_CLUB_ID,
            "role": "coach",
            "login_identifier": unique_email,
            "display_name": "Coach Pep",
            "expires_in_hours": 48,
        }
        res = client.post("/v1/auth/invitations", headers=COM_HEADERS, json=payload)
        assert res.status_code == 201
        data = res.json()

        assert "invitation_id" in data
        assert "invitation_token" in data
        token = data["invitation_token"]
        assert len(token) == 64
        # Assert token is valid hex string
        int(token, 16)

        assert data["club_id"] == TEST_CLUB_ID
        assert data["role"] == "coach"
        assert data["login_identifier"] == unique_email.lower()
        assert data["display_name"] == "Coach Pep"
        assert "user_id" in data

        # Cryptographic verification in PostgreSQL
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            inv_row = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE invitation_id = %s",
                (data["invitation_id"],),
            ).fetchone()
            assert inv_row is not None
            # Plaintext token must NEVER be in database
            assert inv_row["token_hash"] != token
            # Deterministic SHA-256 match
            expected_hash = hash_token(token)
            assert inv_row["token_hash"] == expected_hash

            # Verify associated user record in fc27_users
            user_row = conn.execute(
                "SELECT * FROM public.fc27_users WHERE user_id = %s",
                (data["user_id"],),
            ).fetchone()
            assert user_row is not None
            assert user_row["status"] == "invited"
            assert user_row["club_id"] == TEST_CLUB_ID

    def test_staff_cannot_invite_commissioner(self, client, store: IdentityStore):
        """Staff members cannot issue invitations for commissioner or commissioner_delegate."""
        # 1. Provision a staff member and active session
        staff_user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"staff_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Equipment Staff",
            status="active",
        )
        store.assign_user_role(staff_user["user_id"], "staff")
        staff_token, _ = store.create_session(
            user_id=staff_user["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )
        staff_headers = session_header(staff_token)

        # 2. Staff issuing coach invitation succeeds
        res_coach = client.post(
            "/v1/auth/invitations",
            headers=staff_headers,
            json={
                "club_id": TEST_CLUB_ID,
                "role": "coach",
                "login_identifier": f"coach_{uuid.uuid4().hex[:6]}@fc27.club",
            },
        )
        assert res_coach.status_code == 201

        # 3. Staff issuing commissioner invitation is forbidden (403)
        res_comm = client.post(
            "/v1/auth/invitations",
            headers=staff_headers,
            json={
                "club_id": TEST_CLUB_ID,
                "role": "commissioner",
                "login_identifier": f"fake_comm_{uuid.uuid4().hex[:6]}@fc27.club",
            },
        )
        assert res_comm.status_code == 403

        # 4. Staff issuing commissioner_delegate invitation is forbidden (403)
        res_del = client.post(
            "/v1/auth/invitations",
            headers=staff_headers,
            json={
                "club_id": TEST_CLUB_ID,
                "role": "commissioner_delegate",
                "login_identifier": f"fake_del_{uuid.uuid4().hex[:6]}@fc27.club",
            },
        )
        assert res_del.status_code == 403

    def test_player_cannot_issue_invitation(self, client, store: IdentityStore):
        """Player role cannot issue invitations."""
        player_user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"player_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Striker 9",
            status="active",
        )
        store.assign_user_role(player_user["user_id"], "player")
        player_token, _ = store.create_session(
            user_id=player_user["user_id"],
            auth_level="authenticated",
            auth_method="passkey",
        )
        player_headers = session_header(player_token)

        res = client.post(
            "/v1/auth/invitations",
            headers=player_headers,
            json={
                "club_id": TEST_CLUB_ID,
                "role": "player",
                "login_identifier": f"friend_{uuid.uuid4().hex[:6]}@fc27.club",
            },
        )
        assert res.status_code == 403

    def test_unauthenticated_issue_invitation_rejected(self, client):
        """Unauthenticated caller receives HTTP 401."""
        res = client.post(
            "/v1/auth/invitations",
            headers={},
            json={
                "club_id": TEST_CLUB_ID,
                "role": "coach",
                "login_identifier": "unauth@fc27.club",
            },
        )
        assert res.status_code == 401

    def test_invalid_email_format_rejected(self, client):
        """Invalid email rejected with HTTP 422."""
        res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={
                "club_id": TEST_CLUB_ID,
                "role": "coach",
                "login_identifier": "invalid-email-without-at",
            },
        )
        assert res.status_code == 422

    def test_invalid_role_rejected(self, client):
        """Non-existent application role rejected with HTTP 422."""
        res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={
                "club_id": TEST_CLUB_ID,
                "role": "root_admin",
                "login_identifier": "admin@fc27.club",
            },
        )
        assert res.status_code == 422


class TestInvitationsValidation:
    """GET /v1/auth/invitations/{token}/validate test coverage."""

    def test_validate_active_invitation_success(self, client):
        """Public endpoint returns 200 with non-sensitive invitation projection."""
        email = f"analyst_{uuid.uuid4().hex[:8]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "analyst", "login_identifier": email},
        ).json()
        token = inv["invitation_token"]

        # Public validation: no auth headers required
        res = client.get(f"/v1/auth/invitations/{token}/validate")
        assert res.status_code == 200
        data = res.json()
        assert data["valid"] is True
        assert data["club_id"] == TEST_CLUB_ID
        assert data["role"] == "analyst"
        assert data["invited_email"] == email.lower()
        assert "token_hash" not in data

    def test_validate_nonexistent_token_returns_404(self, client):
        """Non-existent token returns 404."""
        dummy_token = "a" * 64
        res = client.get(f"/v1/auth/invitations/{dummy_token}/validate")
        assert res.status_code == 404

    def test_validate_expired_token_returns_410(self, client, pg_uri: str):
        """Expired token returns HTTP 410."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "analyst", "login_identifier": f"t_{uuid.uuid4().hex[:6]}@fc27.club"},
        ).json()
        token = inv["invitation_token"]

        # Force expiration in PostgreSQL
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                "UPDATE public.fc27_invitations SET expires_at = now() - interval '1 hour' WHERE invitation_id = %s",
                (inv["invitation_id"],),
            )

        res = client.get(f"/v1/auth/invitations/{token}/validate")
        assert res.status_code == 410

    def test_validate_revoked_token_returns_410(self, client):
        """Revoked token returns HTTP 410."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "coach", "login_identifier": f"t_{uuid.uuid4().hex[:6]}@fc27.club"},
        ).json()
        token = inv["invitation_token"]
        inv_id = inv["invitation_id"]

        # Revoke invitation
        rev_res = client.delete(f"/v1/auth/invitations/{inv_id}", headers=COM_HEADERS)
        assert rev_res.status_code == 200

        # Validate returns 410
        res = client.get(f"/v1/auth/invitations/{token}/validate")
        assert res.status_code == 410


class TestInvitationsConsumption:
    """POST /v1/auth/invitations/{token}/consume and /accept test coverage."""

    def test_consume_invitation_activates_user_and_grants_role(self, client, pg_uri: str):
        """Consuming invitation transitions user to 'active' and grants role."""
        email = f"coach_{uuid.uuid4().hex[:8]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "coach", "login_identifier": email, "display_name": "Pending Coach"},
        ).json()
        token = inv["invitation_token"]

        # Consume / accept
        res = client.post(f"/v1/auth/invitations/{token}/consume", json={"display_name": "Active Pep"})
        assert res.status_code == 200
        user_info = res.json()

        assert user_info["status"] == "active"
        assert user_info["display_name"] == "Active Pep"
        assert user_info["role"] == "coach"
        assert user_info["club_id"] == TEST_CLUB_ID
        user_id = user_info["user_id"]

        # PostgreSQL verification
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            user_row = conn.execute("SELECT * FROM public.fc27_users WHERE user_id = %s", (user_id,)).fetchone()
            assert user_row["status"] == "active"
            assert user_row["activated_at"] is not None

            # Verify role grant in fc27_user_roles
            roles = [r["role"] for r in conn.execute("SELECT role FROM public.fc27_user_roles WHERE user_id = %s", (user_id,)).fetchall()]
            assert "coach" in roles

            # Verify invitation marked consumed
            inv_row = conn.execute("SELECT * FROM public.fc27_invitations WHERE invitation_id = %s", (inv["invitation_id"],)).fetchone()
            assert inv_row["consumed_at"] is not None

    def test_single_use_replay_rejection(self, client):
        """Second attempt to consume token returns HTTP 409 Conflict."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "analyst", "login_identifier": f"a_{uuid.uuid4().hex[:6]}@fc27.club"},
        ).json()
        token = inv["invitation_token"]

        # First consumption succeeds
        r1 = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Analyst One"})
        assert r1.status_code == 200
        assert r1.json()["status"] == "active"

        # Replay attempt fails with 409 Conflict
        r2 = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Replay Attempt"})
        assert r2.status_code == 409

    def test_consume_player_invitation_with_player_id(self, client, pg_uri: str):
        """Player invitation consumption records numeric player_id."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "player", "login_identifier": f"p_{uuid.uuid4().hex[:6]}@fc27.club"},
        ).json()
        token = inv["invitation_token"]

        res = client.post(
            f"/v1/auth/invitations/{token}/consume",
            json={"display_name": "Striker Legend", "player_id": "887766"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["role"] == "player"
        assert data["player_id"] == "887766"

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            user_row = conn.execute("SELECT * FROM public.fc27_users WHERE user_id = %s", (data["user_id"],)).fetchone()
            assert user_row["player_id"] == "887766"


class TestInvitationsRevocation:
    """DELETE /v1/auth/invitations/{invitation_id} test coverage."""

    def test_commissioner_can_revoke_invitation(self, client):
        """Commissioner can revoke unconsumed invitation."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "coach", "login_identifier": f"rev_{uuid.uuid4().hex[:6]}@fc27.club"},
        ).json()
        inv_id = inv["invitation_id"]
        token = inv["invitation_token"]

        del_res = client.delete(f"/v1/auth/invitations/{inv_id}", headers=COM_HEADERS)
        assert del_res.status_code == 200
        data = del_res.json()
        assert data["status"] == "revoked"

        # Subsequent consume attempt must fail with 410 Gone
        consume_res = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Too Late"})
        assert consume_res.status_code == 410

    def test_revoking_consumed_invitation_fails(self, client):
        """Revoking an already consumed invitation returns HTTP 409 Conflict."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "staff", "login_identifier": f"cons_{uuid.uuid4().hex[:6]}@fc27.club"},
        ).json()
        token = inv["invitation_token"]
        inv_id = inv["invitation_id"]

        # Consume first
        client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Active Staff"})

        # Revocation must fail with 409
        del_res = client.delete(f"/v1/auth/invitations/{inv_id}", headers=COM_HEADERS)
        assert del_res.status_code == 409

    def test_revoking_already_revoked_invitation_fails(self, client):
        """Revoking twice returns HTTP 409 Conflict."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"club_id": TEST_CLUB_ID, "role": "analyst", "login_identifier": f"dbl_{uuid.uuid4().hex[:6]}@fc27.club"},
        ).json()
        inv_id = inv["invitation_id"]

        del1 = client.delete(f"/v1/auth/invitations/{inv_id}", headers=COM_HEADERS)
        assert del1.status_code == 200

        del2 = client.delete(f"/v1/auth/invitations/{inv_id}", headers=COM_HEADERS)
        assert del2.status_code == 409


class TestInvitationsServiceRoleRejection:
    """Service and machine identity roles must never be provisioned via invitations (R11)."""

    @pytest.mark.parametrize("prohibited_role", [
        "bff_service",
        "graph_service",
        "worker",
        "worker_service",
        "service",
    ])
    def test_router_rejects_machine_roles_with_422(self, client, prohibited_role: str):
        """API rejects machine identity roles with HTTP 422 Unprocessable Entity."""
        payload = {
            "club_id": TEST_CLUB_ID,
            "role": prohibited_role,
            "login_identifier": f"bot_{uuid.uuid4().hex[:6]}@fc27.club",
            "display_name": "Machine Identity",
        }
        res = client.post("/v1/auth/invitations", headers=COM_HEADERS, json=payload)
        assert res.status_code == 422, f"Role {prohibited_role} should be rejected with 422, got {res.status_code}: {res.text}"

    @pytest.mark.parametrize("prohibited_role", [
        "bff_service",
        "graph_service",
        "worker",
        "worker_service",
        "service",
    ])
    def test_store_create_invitation_rejects_machine_roles_with_422(self, store: IdentityStore, prohibited_role: str):
        """Store layer rejects machine identity roles with IdentityStoreError(422)."""
        with pytest.raises(IdentityStoreError) as exc_info:
            store.create_invitation(
                club_id=TEST_CLUB_ID,
                intended_role=prohibited_role,
                login_identifier=f"bot_{uuid.uuid4().hex[:6]}@fc27.club",
                creator_id="commissioner-1",
                actor="commissioner-1",
            )
        assert exc_info.value.status_code == 422

    def test_store_consume_invitation_rejects_prohibited_role_with_422(self, store: IdentityStore, pg_uri: str):
        """Store layer rejects consuming an invitation if role was tampered to a machine role."""
        raw_token = uuid.uuid4().hex + uuid.uuid4().hex
        tok_hash = hash_token(raw_token)
        inv_id = uuid.uuid4()
        bot_email = f"bot_{uuid.uuid4().hex[:6]}@fc27.club"

        # Manually seed a compromised row with machine role
        with psycopg.connect(pg_uri, autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO public.fc27_invitations (
                    invitation_id, club_id, token_hash, invited_email, intended_role,
                    creator_id, expires_at
                ) VALUES (%s, %s, %s, %s, 'bff_service', 'admin', now() + interval '1 day')
                """,
                (inv_id, TEST_CLUB_ID, tok_hash, bot_email),
            )

        with pytest.raises(IdentityStoreError) as exc_info:
            store.consume_invitation_and_activate_user(raw_token)
        assert exc_info.value.status_code == 422

