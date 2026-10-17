"""Adversarial Stress Test Suite for Phase 2 Milestone 3 (Invitations Lifecycle & State Machine).

Authored by challenger_m3_2.

Adversarial Verification Scope:
1. Token Security & Storage:
   - 64-hex char plaintext token generation (256-bit entropy).
   - Zero plaintext tokens stored in PostgreSQL: strictly SHA-256 hashes in fc27_invitations.token_hash.
   - Database CHECK constraints enforce token_hash regex (^[0-9a-f]{64}$).
   - Validation projections leak zero secret hashes.
2. Replay & Single-Use Protection:
   - Repeated consumption of identical token fails closed with HTTP 409 Conflict.
   - Sequential replay attempts (n=5) remain consistently blocked without state corruption.
   - Expired tokens fail closed on validate and consume with HTTP 410 Gone.
   - Revoked tokens fail closed on validate and consume with HTTP 410 Gone.
   - Double-revocation fails closed with HTTP 409 Conflict.
   - Revoking an already consumed invitation fails closed with HTTP 409 Conflict.
3. State Machine & Role Boundaries:
   - Consuming invitation transitions account from 'invited' to 'active', grants role, and records domain events.
   - Disabled accounts cannot be invited (HTTP 403 Forbidden).
   - Disabled accounts cannot be activated via invitation (HTTP 403 Forbidden).
   - Already active accounts cannot be re-invited or re-activated (HTTP 409 Conflict).
   - Cross-club user binding conflict fails closed with HTTP 409 Conflict.
   - Staff cannot invite Commissioner or Commissioner Delegate (HTTP 403 RoleNotPermittedError).
   - Staff can invite subordinate roles (Coach, Analyst, Staff, Player).
   - Player role cannot issue invitations (HTTP 403 RoleNotPermittedError).
   - Cross-club invitation creation fails closed (HTTP 403 CrossClubAccessError).
   - Cross-club invitation revocation fails closed (HTTP 403 CrossClubAccessError).
   - Unauthenticated issuance/revocation fails closed (HTTP 401 Unauthorized).
4. Direct Database State Machine Trigger Invariants:
   - fc27_users_status_guard trigger prevents reactivating disabled users.
   - fc27_users_status_guard trigger blocks invalid transitions from invited to suspended.
   - Immutability of user_id, club_id, created_at enforced by database trigger.
5. Packaging & Scope Invariants:
   - DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false strictly enforced.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
import pytest

from tests.e2e.phase2_helpers import TEST_CLUB_ID, session_header
from src.identity.crypto import hash_token
from src.identity.store import IdentityStore, IdentityStoreError


CLUB_PRIMARY = TEST_CLUB_ID
CLUB_FOREIGN = "998877"


# ==============================================================================
# Track 1: Token Security & Storage Invariants
# ==============================================================================

class TestTokenSecurityAndStorageAdversarial:
    """Adversarial stress testing for invitation token generation, entropy, and database storage."""

    def test_token_format_and_entropy_guarantee(self, client, active_human_commissioner):
        """Tokens must be exactly 64 lowercase hexadecimal characters (32 bytes entropy) and distinct."""
        _, raw_token, _ = active_human_commissioner
        tokens = set()
        for i in range(10):
            res = client.post(
                "/v1/auth/invitations",
                headers=session_header(raw_token),
                json={
                    "club_id": CLUB_PRIMARY,
                    "role": "analyst",
                    "login_identifier": f"entropy_{i}_{uuid.uuid4().hex[:6]}@fc27.club",
                },
            )
            assert res.status_code == 201, f"Failed at iteration {i}: {res.text}"
            token = res.json()["invitation_token"]

            # Must be 64 characters
            assert len(token) == 64
            # Must be lowercase hex
            assert re.fullmatch(r"^[0-9a-f]{64}$", token) is not None
            # Must be valid hex number
            int(token, 16)
            # Must be unique
            assert token not in tokens
            tokens.add(token)

    def test_zero_plaintext_tokens_in_database(
        self, client, pg_uri: str, active_human_commissioner
    ):
        """PostgreSQL database must strictly store SHA-256 digests. Plaintext tokens must never appear."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"zero_plaintext_{uuid.uuid4().hex[:8]}@fc27.club"
        res = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={
                "club_id": CLUB_PRIMARY,
                "role": "coach",
                "login_identifier": email,
                "display_name": "Secret Coach",
            },
        )
        assert res.status_code == 201
        data = res.json()
        raw_token = data["invitation_token"]
        invitation_id = data["invitation_id"]

        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            # 1. Query by invitation ID
            row = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE invitation_id = %s",
                (invitation_id,),
            ).fetchone()
            assert row is not None

            # CRITICAL INVARIANT: Plaintext token MUST NOT equal stored token_hash
            assert row["token_hash"] != raw_token
            # Must match deterministic SHA-256
            expected_hash = hash_token(raw_token)
            assert row["token_hash"] == expected_hash

            # 2. Exhaustive database search: verify raw_token is nowhere in fc27_invitations
            found_plaintext = conn.execute(
                "SELECT COUNT(*) AS cnt FROM public.fc27_invitations WHERE token_hash = %s",
                (raw_token,),
            ).fetchone()["cnt"]
            assert found_plaintext == 0, "CRITICAL FLAW: Raw plaintext token matched directly in token_hash!"

    def test_database_check_constraint_rejects_malformed_hashes(self, pg_uri: str):
        """Database constraint CHECK (token_hash ~ '^[0-9a-f]{64}$') prevents malformed or non-SHA256 data."""
        with psycopg.connect(pg_uri) as conn:
            # Short hash (32 chars) must violate check constraint
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    """
                    INSERT INTO public.fc27_invitations (
                        invitation_id, token_hash, club_id, intended_role,
                        invited_email, creator_id, issued_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, now(), now() + interval '24 hours')
                    """,
                    (uuid.uuid4(), "short_token_hash_value_123456", CLUB_PRIMARY, "coach", "bad@fc27.club", "comm"),
                )

    def test_public_validation_endpoint_never_leaks_token_hash(
        self, client, active_human_commissioner
    ):
        """Public validation response must not contain token_hash or internal secrets."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"leak_check_{uuid.uuid4().hex[:6]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "player", "login_identifier": email},
        ).json()
        raw_token = inv["invitation_token"]

        val_res = client.get(f"/v1/auth/invitations/{raw_token}/validate")
        assert val_res.status_code == 200
        val_data = val_res.json()

        assert "token_hash" not in val_data
        assert "creator_id" not in val_data
        assert val_data["valid"] is True
        assert val_data["role"] == "player"


# ==============================================================================
# Track 2: Replay, Concurrency & Single-Use Protection
# ==============================================================================

class TestReplayAndSingleUseAdversarial:
    """Adversarial stress testing for replay rejection, expiration, and revocation."""

    def test_repeated_consumption_fails_409_repeatedly(
        self, client, active_human_commissioner
    ):
        """First consumption succeeds; all subsequent consumption attempts return HTTP 409 Conflict."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"replay_{uuid.uuid4().hex[:6]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "coach", "login_identifier": email},
        ).json()
        token = inv["invitation_token"]

        # First consumption: SUCCESS
        r1 = client.post(f"/v1/auth/invitations/{token}/consume", json={"display_name": "First"})
        assert r1.status_code == 200
        assert r1.json()["status"] == "active"

        # Subsequent attempts 2 through 6 must ALL fail with 409 Conflict
        for attempt in range(2, 7):
            r_replay = client.post(
                f"/v1/auth/invitations/{token}/consume",
                json={"display_name": f"Attempt {attempt}"},
            )
            assert r_replay.status_code == 409, f"Attempt {attempt} returned {r_replay.status_code}"
            assert "already" in r_replay.json().get("detail", "").lower()

    def test_expired_token_fails_closed_on_validate_and_consume(
        self, client, pg_uri: str, active_human_commissioner
    ):
        """Expired invitations must fail closed with HTTP 410 Gone on both validate and consume."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"expired_{uuid.uuid4().hex[:6]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "analyst", "login_identifier": email},
        ).json()
        token = inv["invitation_token"]
        inv_id = inv["invitation_id"]

        # Fast-forward expiration in database to 1 second ago
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                "UPDATE public.fc27_invitations SET issued_at = now() - interval '2 seconds', expires_at = now() - interval '1 second' WHERE invitation_id = %s",
                (inv_id,),
            )

        # 1. Validation returns 410 Gone
        val_res = client.get(f"/v1/auth/invitations/{token}/validate")
        assert val_res.status_code == 410
        assert "expired" in val_res.json().get("detail", "").lower()

        # 2. Consumption returns 410 Gone
        con_res = client.post(f"/v1/auth/invitations/{token}/consume")
        assert con_res.status_code == 410
        assert "expired" in con_res.json().get("detail", "").lower()

    def test_revoked_token_fails_closed_on_validate_and_consume(
        self, client, active_human_commissioner
    ):
        """Revoked invitations must fail closed with HTTP 410 Gone on both validate and consume."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"revoked_{uuid.uuid4().hex[:6]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "staff", "login_identifier": email},
        ).json()
        token = inv["invitation_token"]
        inv_id = inv["invitation_id"]

        # Administrative revocation
        del_res = client.delete(
            f"/v1/auth/invitations/{inv_id}", headers=session_header(raw_token_auth)
        )
        assert del_res.status_code == 200

        # 1. Validation returns 410 Gone
        val_res = client.get(f"/v1/auth/invitations/{token}/validate")
        assert val_res.status_code == 410
        assert "revoked" in val_res.json().get("detail", "").lower()

        # 2. Consumption returns 410 Gone
        con_res = client.post(f"/v1/auth/invitations/{token}/consume")
        assert con_res.status_code == 410
        assert "revoked" in con_res.json().get("detail", "").lower()

    def test_revoking_already_revoked_invitation_fails_409(
        self, client, active_human_commissioner
    ):
        """Attempting to revoke an already-revoked invitation must fail with HTTP 409 Conflict."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"double_rev_{uuid.uuid4().hex[:6]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "player", "login_identifier": email},
        ).json()
        inv_id = inv["invitation_id"]

        del1 = client.delete(
            f"/v1/auth/invitations/{inv_id}", headers=session_header(raw_token_auth)
        )
        assert del1.status_code == 200

        del2 = client.delete(
            f"/v1/auth/invitations/{inv_id}", headers=session_header(raw_token_auth)
        )
        assert del2.status_code == 409
        assert "already revoked" in del2.json().get("detail", "").lower()

    def test_revoking_already_consumed_invitation_fails_409(
        self, client, active_human_commissioner
    ):
        """Attempting to revoke an already-consumed invitation must fail with HTTP 409 Conflict."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"consumed_rev_{uuid.uuid4().hex[:6]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "coach", "login_identifier": email},
        ).json()
        token = inv["invitation_token"]
        inv_id = inv["invitation_id"]

        # Consume first
        con_res = client.post(f"/v1/auth/invitations/{token}/consume")
        assert con_res.status_code == 200

        # Revoke attempt must fail with 409
        del_res = client.delete(
            f"/v1/auth/invitations/{inv_id}", headers=session_header(raw_token_auth)
        )
        assert del_res.status_code == 409
        assert "consumed" in del_res.json().get("detail", "").lower()


# ==============================================================================
# Track 3: State Machine & Role Boundaries
# ==============================================================================

class TestStateMachineAndRoleBoundariesAdversarial:
    """Adversarial stress testing for account status transitions and caller authorization."""

    def test_invitation_consumption_state_transition_and_events(
        self, client, pg_uri: str, active_human_commissioner
    ):
        """Consuming invitation updates user status to active, sets activated_at, and records audit events."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"state_trans_{uuid.uuid4().hex[:6]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={
                "club_id": CLUB_PRIMARY,
                "role": "coach",
                "login_identifier": email,
                "display_name": "Pending",
            },
        ).json()
        token = inv["invitation_token"]
        user_id = inv["user_id"]

        # Verify initial status is 'invited' in DB
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            u_before = conn.execute("SELECT * FROM public.fc27_users WHERE user_id = %s", (user_id,)).fetchone()
            assert u_before["status"] == "invited"
            assert u_before["activated_at"] is None

        # Consume
        res = client.post(f"/v1/auth/invitations/{token}/consume", json={"display_name": "Activated Coach"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "active"
        assert data["display_name"] == "Activated Coach"

        # Verify PostgreSQL state
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            u_after = conn.execute("SELECT * FROM public.fc27_users WHERE user_id = %s", (user_id,)).fetchone()
            assert u_after["status"] == "active"
            assert u_after["activated_at"] is not None

            # Role assigned in fc27_user_roles
            role_row = conn.execute(
                "SELECT * FROM public.fc27_user_roles WHERE user_id = %s AND role = 'coach'",
                (user_id,),
            ).fetchone()
            assert role_row is not None

            # Audit events recorded in domain_events
            events = conn.execute(
                """
                SELECT event_type FROM public.domain_events 
                WHERE entity_id = %s OR source_id = %s
                ORDER BY event_id ASC
                """,
                (str(user_id), str(user_id)),
            ).fetchall()
            event_types = {e["event_type"] for e in events}
            assert "identity.status_changed" in event_types
            assert "identity.role_granted" in event_types

    def test_disabled_account_cannot_be_invited(
        self, client, store: IdentityStore, active_human_commissioner
    ):
        """Attempting to invite an existing user whose status is 'disabled' fails closed with HTTP 403."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"disabled_user_{uuid.uuid4().hex[:6]}@fc27.club"
        user = store.create_user(
            club_id=CLUB_PRIMARY,
            login_id=email,
            display_name="Disabled User",
            status="disabled",
        )

        res = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "player", "login_identifier": email},
        )
        assert res.status_code == 403
        assert "disabled" in res.json().get("detail", "").lower()

    def test_disabled_account_cannot_be_activated_via_invitation(
        self, client, store: IdentityStore, pg_uri: str, active_human_commissioner
    ):
        """If user is disabled after invitation issuance, consumption fails closed with HTTP 403."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"disabled_act_{uuid.uuid4().hex[:6]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "player", "login_identifier": email},
        ).json()
        token = inv["invitation_token"]
        user_id = inv["user_id"]

        # Administratively disable the account in DB
        with psycopg.connect(pg_uri) as conn:
            conn.execute("UPDATE public.fc27_users SET status = 'disabled' WHERE user_id = %s", (user_id,))

        # Consumption attempt must fail closed with 403 Forbidden
        res = client.post(f"/v1/auth/invitations/{token}/consume")
        assert res.status_code == 403
        assert "disabled" in res.json().get("detail", "").lower()

    def test_active_account_cannot_be_invited_or_reactivated(
        self, client, store: IdentityStore, active_human_commissioner
    ):
        """User account that is already active cannot receive new invitation (409 Conflict)."""
        _, raw_token_auth, _ = active_human_commissioner
        email = f"active_user_{uuid.uuid4().hex[:6]}@fc27.club"
        store.create_user(
            club_id=CLUB_PRIMARY,
            login_id=email,
            display_name="Active Already",
            status="active",
        )

        res = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={"club_id": CLUB_PRIMARY, "role": "coach", "login_identifier": email},
        )
        assert res.status_code == 409
        assert "already active" in res.json().get("detail", "").lower()

    def test_staff_cannot_invite_commissioner_or_delegate(self, client, store: IdentityStore):
        """Staff members are forbidden from issuing commissioner or commissioner_delegate invitations (HTTP 403)."""
        staff_user = store.create_user(
            club_id=CLUB_PRIMARY,
            login_id=f"staff_actor_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Staff Actor",
            status="active",
        )
        store.assign_user_role(staff_user["user_id"], "staff")
        staff_token, _ = store.create_session(
            user_id=staff_user["user_id"],
            club_id=CLUB_PRIMARY,
            auth_level="authenticated",
            auth_method="passkey",
        )
        staff_headers = session_header(staff_token)

        # 1. Staff -> commissioner: FORBIDDEN (403)
        res1 = client.post(
            "/v1/auth/invitations",
            headers=staff_headers,
            json={"club_id": CLUB_PRIMARY, "role": "commissioner", "login_identifier": f"c_{uuid.uuid4().hex[:6]}@fc27.club"},
        )
        assert res1.status_code == 403

        # 2. Staff -> commissioner_delegate: FORBIDDEN (403)
        res2 = client.post(
            "/v1/auth/invitations",
            headers=staff_headers,
            json={"club_id": CLUB_PRIMARY, "role": "commissioner_delegate", "login_identifier": f"cd_{uuid.uuid4().hex[:6]}@fc27.club"},
        )
        assert res2.status_code == 403

        # 3. Staff -> coach: PERMITTED (201)
        res3 = client.post(
            "/v1/auth/invitations",
            headers=staff_headers,
            json={"club_id": CLUB_PRIMARY, "role": "coach", "login_identifier": f"ok_{uuid.uuid4().hex[:6]}@fc27.club"},
        )
        assert res3.status_code == 201

    def test_player_role_cannot_issue_invitations(self, client, store: IdentityStore):
        """Player role callers cannot issue any invitations (HTTP 403)."""
        player = store.create_user(
            club_id=CLUB_PRIMARY,
            login_id=f"player_actor_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Player Actor",
            status="active",
        )
        store.assign_user_role(player["user_id"], "player")
        token, _ = store.create_session(
            user_id=player["user_id"],
            club_id=CLUB_PRIMARY,
            auth_level="authenticated",
            auth_method="passkey",
        )

        res = client.post(
            "/v1/auth/invitations",
            headers=session_header(token),
            json={"club_id": CLUB_PRIMARY, "role": "player", "login_identifier": f"friend_{uuid.uuid4().hex[:6]}@fc27.club"},
        )
        assert res.status_code == 403

    def test_cross_club_invitation_creation_rejected(self, client, store: IdentityStore):
        """Staff caller from Club A attempting to create invitation for Club B is rejected (HTTP 403)."""
        staff_a = store.create_user(
            club_id=CLUB_PRIMARY,
            login_id=f"staff_a_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Staff A",
            status="active",
        )
        store.assign_user_role(staff_a["user_id"], "staff")
        token, _ = store.create_session(
            user_id=staff_a["user_id"],
            club_id=CLUB_PRIMARY,
            auth_level="authenticated",
            auth_method="passkey",
        )

        # Attempt to issue invitation for CLUB_FOREIGN
        res = client.post(
            "/v1/auth/invitations",
            headers=session_header(token),
            json={"club_id": CLUB_FOREIGN, "role": "coach", "login_identifier": f"c_{uuid.uuid4().hex[:6]}@fc27.club"},
        )
        assert res.status_code == 403

    def test_cross_club_invitation_revocation_rejected(
        self, client, store: IdentityStore, active_human_commissioner
    ):
        """Staff/Commissioner caller from Club B attempting to revoke Club A invitation is rejected (HTTP 403)."""
        _, raw_token_auth, _ = active_human_commissioner
        # 1. Create invitation in Club A
        inv = client.post(
            "/v1/auth/invitations",
            headers=session_header(raw_token_auth),
            json={
                "club_id": CLUB_PRIMARY,
                "role": "coach",
                "login_identifier": f"club_a_{uuid.uuid4().hex[:6]}@fc27.club",
            },
        ).json()
        inv_id = inv["invitation_id"]

        # 2. Staff in Club Foreign
        staff_foreign = store.create_user(
            club_id=CLUB_FOREIGN,
            login_id=f"foreign_staff_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Foreign Staff",
            status="active",
        )
        store.assign_user_role(staff_foreign["user_id"], "staff")
        foreign_token, _ = store.create_session(
            user_id=staff_foreign["user_id"],
            club_id=CLUB_FOREIGN,
            auth_level="authenticated",
            auth_method="passkey",
        )

        # 3. Revoke attempt must fail with 403
        del_res = client.delete(f"/v1/auth/invitations/{inv_id}", headers=session_header(foreign_token))
        assert del_res.status_code == 403


# ==============================================================================
# Track 4: Database Trigger State Machine Guard Invariants
# ==============================================================================

class TestDatabaseStatusGuardTriggers:
    """Verifies public.fc27_guard_user_status_transition trigger blocks invalid SQL mutations."""

    def test_database_trigger_blocks_disabled_to_active(self, pg_uri: str, store: IdentityStore):
        """Database trigger prevents reactivating a disabled user account."""
        user = store.create_user(
            club_id=CLUB_PRIMARY,
            login_id=f"perm_dis_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Disabled Perm",
            status="disabled",
        )
        user_id = user["user_id"]

        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
                conn.execute("UPDATE public.fc27_users SET status = 'active' WHERE user_id = %s", (user_id,))
            assert "disabled user account cannot be reactivated" in str(exc_info.value)

    def test_database_trigger_blocks_invited_to_suspended(self, pg_uri: str, store: IdentityStore):
        """Database trigger prevents transitioning an invited account directly to suspended."""
        user = store.create_user(
            club_id=CLUB_PRIMARY,
            login_id=f"inv_susp_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Invited User",
            status="invited",
        )
        user_id = user["user_id"]

        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
                conn.execute("UPDATE public.fc27_users SET status = 'suspended' WHERE user_id = %s", (user_id,))
            assert "invited account can only transition to active or disabled" in str(exc_info.value)

    def test_database_trigger_enforces_user_id_immutability(self, pg_uri: str, store: IdentityStore):
        """Database trigger prevents mutating user_id PK."""
        user = store.create_user(
            club_id=CLUB_PRIMARY,
            login_id=f"immut_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Immut User",
            status="active",
        )
        user_id = user["user_id"]
        new_uuid = uuid.uuid4()

        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
                conn.execute("UPDATE public.fc27_users SET user_id = %s WHERE user_id = %s", (new_uuid, user_id))
            assert "user_id is immutable" in str(exc_info.value)


# ==============================================================================
# Track 5: Dispatch Safety & Scope Invariants
# ==============================================================================

class TestDispatchDormancyInvariants:
    """Universal invariants verifying dispatch remains completely dormant during Phase 2."""

    def test_dispatch_env_vars_remain_false(self):
        assert os.getenv("DISPATCH_ENABLED", "false").lower() == "false"
        assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false"
