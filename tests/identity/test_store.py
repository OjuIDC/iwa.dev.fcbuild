"""Integration and component tests for src/identity/store.py (Phase 2 Milestone 1)."""
from __future__ import annotations

import hashlib
import secrets
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
import psycopg

from src.identity.crypto import IdentityCrypto
from src.identity.store import IdentityStore, IdentityStoreError


@pytest.fixture
def crypto_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def older_crypto_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def test_crypto(crypto_key) -> IdentityCrypto:
    return IdentityCrypto(crypto_key, active_key_version=1)


@pytest.fixture
def store(pg_uri, test_crypto) -> IdentityStore:
    return IdentityStore(pg_uri, crypto=test_crypto)


# =====================================================================
# 1. Users and Roles Lifecycle Tests
# =====================================================================

def test_user_and_role_lifecycle(store):
    club_id = "100"
    login_id = f"user_{secrets.token_hex(4)}@fc27.com"
    display_name = "Alex Hunter"

    # 1. Create User
    user = store.create_user(
        club_id=club_id,
        login_id=login_id,
        display_name=display_name,
        status="invited",
    )
    user_id = user["user_id"]
    assert user["status"] == "invited"
    assert user["login_id"] == login_id.lower()
    assert user["club_id"] == club_id

    # 2. Get User
    fetched = store.get_user(user_id)
    assert fetched is not None
    assert fetched["display_name"] == display_name

    by_login = store.get_user_by_login(login_id.upper())
    assert by_login is not None
    assert by_login["user_id"] == user_id

    # 3. Assign & Query Roles
    store.assign_role(user_id, "player", actor="system")
    store.assign_role(user_id, "staff", actor="system")
    roles = store.get_user_roles(user_id)
    assert roles == {"player", "staff"}

    # 4. Status Transition: invited -> active
    activated = store.update_user_status(user_id, "active", actor="admin")
    assert activated["status"] == "active"
    assert activated["activated_at"] is not None

    # 5. Status Transition: active -> suspended -> active
    suspended = store.update_user_status(user_id, "suspended", actor="admin")
    assert suspended["status"] == "suspended"
    assert suspended["suspended_at"] is not None

    resumed = store.update_user_status(user_id, "active", actor="admin")
    assert resumed["status"] == "active"
    assert resumed["suspended_at"] is None

    # 6. Revoke Role
    store.revoke_role(user_id, "staff", actor="admin")
    assert store.get_user_roles(user_id) == {"player"}


def test_duplicate_login_id_conflict(store):
    login = f"dup_{secrets.token_hex(4)}@fc27.com"
    store.create_user(club_id="101", login_id=login, display_name="User 1")
    with pytest.raises(IdentityStoreError) as exc_info:
        store.create_user(club_id="101", login_id=login, display_name="User 2")
    assert exc_info.value.status_code == 409


def test_disabled_user_cannot_be_reactivated(store):
    login = f"disabled_{secrets.token_hex(4)}@fc27.com"
    user = store.create_user(club_id="102", login_id=login, display_name="Disabled User", status="active")
    store.update_user_status(user["user_id"], "disabled", actor="admin")

    with pytest.raises(Exception):
        store.update_user_status(user["user_id"], "active", actor="admin")


# =====================================================================
# 2. Invitations Lifecycle Tests
# =====================================================================

def test_invitation_lifecycle(store):
    club_id = "200"
    intended_role = "coach"
    email = f"coach_{secrets.token_hex(4)}@fc27.com"
    creator_id = "commissioner_1"

    # Create
    raw_token, inv = store.create_invitation(
        club_id=club_id,
        intended_role=intended_role,
        invited_email=email,
        creator_id=creator_id,
        actor="commissioner_1",
    )
    inv_id = inv["invitation_id"]
    assert len(raw_token) >= 32

    # Fetch by token
    fetched = store.get_invitation_by_token(raw_token)
    assert fetched is not None
    assert fetched["invitation_id"] == inv_id

    # Consume
    user_id = uuid4()
    consumed = store.consume_invitation(raw_token, user_id=user_id, actor="coach_invitee")
    assert consumed["consumed_at"] is not None

    # Second consumption fails
    with pytest.raises(IdentityStoreError) as exc_info:
        store.consume_invitation(raw_token, user_id=user_id, actor="coach_invitee")
    assert exc_info.value.status_code == 409


def test_invitation_revocation(store):
    raw_token, inv = store.create_invitation(
        club_id="201",
        intended_role="staff",
        invited_email="staff@fc27.com",
        creator_id="admin",
        actor="admin",
    )
    revoked = store.revoke_invitation(inv["invitation_id"], actor="admin")
    assert revoked["revoked_at"] is not None

    with pytest.raises(IdentityStoreError) as exc_info:
        store.consume_invitation(raw_token, user_id=uuid4(), actor="invitee")
    assert exc_info.value.status_code == 410


# =====================================================================
# 3. Sessions Lifecycle & Rotation Tests
# =====================================================================

def test_session_dual_expiry_and_rotation(store):
    user = store.create_user(
        club_id="300",
        login_id=f"sess_{secrets.token_hex(4)}@fc27.com",
        display_name="Session User",
        status="active",
    )
    user_id = user["user_id"]

    # 1. Create Session
    raw_token, sess = store.create_session(
        user_id=user_id,
        auth_level="authenticated",
        auth_method="passkey",
        ip_address="127.0.0.1",
        user_agent="Mozilla/5.0 Test",
        idle_ttl_minutes=30,
        absolute_ttl_hours=24,
        actor="user",
    )
    assert sess["user_id"] == user_id

    # 2. Get and touch session
    active_sess = store.get_session_by_token(raw_token, touch=True)
    assert active_sess is not None
    assert active_sess["login_id"] == user["login_id"]

    # 3. Rotate Session (e.g. privilege step-up)
    new_raw_token, new_sess = store.rotate_session(
        raw_token,
        new_auth_level="step_up_passkey",
        actor="user",
    )
    assert new_raw_token != raw_token
    assert new_sess["auth_level"] == "step_up_passkey"

    # Old token is now invalid / revoked
    assert store.get_session_by_token(raw_token) is None

    # New token is active
    assert store.get_session_by_token(new_raw_token) is not None

    # 4. Revoke all user sessions
    count = store.revoke_all_user_sessions(user_id, actor="admin")
    assert count >= 1
    assert store.get_session_by_token(new_raw_token) is None


# =====================================================================
# 4. TOTP Credential Storage & Rotation Tests
# =====================================================================

def test_totp_credential_storage_and_rotation(store, crypto_key, older_crypto_key):
    user = store.create_user(
        club_id="400",
        login_id=f"totp_{secrets.token_hex(4)}@fc27.com",
        display_name="TOTP User",
        status="active",
    )
    user_id = user["user_id"]
    seed = "JBSWY3DPEHPK3PXP"

    # 1. Save TOTP Credential with older key
    old_crypto = IdentityCrypto(older_crypto_key, active_key_version=1)
    store_old = IdentityStore(store.conninfo, crypto=old_crypto)
    totp_row = store_old.save_totp_credential(user_id=user_id, plaintext_seed=seed, actor="user")
    assert totp_row["status"] == "enrolled"

    # 2. Switch store to new key (version 2) with older key as fallback
    new_crypto = IdentityCrypto(crypto_key, [older_crypto_key], active_key_version=2)
    store_new = IdentityStore(store.conninfo, crypto=new_crypto)

    # Decrypt across fallback key succeeds
    assert store_new.get_totp_seed(user_id) == seed

    # 3. Rotate key at rest
    rotated = store_new.verify_and_rotate_totp_key(user_id)
    assert rotated is True

    # After rotation, decrypt still succeeds
    assert store_new.get_totp_seed(user_id) == seed

    # 4. Update status and revoke
    store_new.update_totp_status(user_id, "active", actor="user")
    store_new.update_totp_status(user_id, "revoked", actor="admin")
    assert store_new.get_totp_seed(user_id) is None


# =====================================================================
# 5. Recovery Codes Tests
# =====================================================================

def test_recovery_codes_batch_and_consumption(store):
    user = store.create_user(
        club_id="500",
        login_id=f"rec_{secrets.token_hex(4)}@fc27.com",
        display_name="Recovery User",
        status="active",
    )
    user_id = user["user_id"]
    codes = [f"REC-{i:04d}-ABCD" for i in range(5)]

    # Save codes
    code_ids = store.save_recovery_codes(user_id=user_id, plaintext_codes=codes, actor="commissioner")
    assert len(code_ids) == 5
    assert store.get_unconsumed_recovery_code_count(user_id) == 5

    # Consume code 0
    ok = store.verify_and_consume_recovery_code(user_id, codes[0], actor="commissioner")
    assert ok is True
    assert store.get_unconsumed_recovery_code_count(user_id) == 4

    # Reuse code 0 fails
    ok_retry = store.verify_and_consume_recovery_code(user_id, codes[0], actor="commissioner")
    assert ok_retry is False

    # Invalid code fails
    assert store.verify_and_consume_recovery_code(user_id, "INVALID-CODE", actor="commissioner") is False

    # Deleting recovery code is rejected by trigger
    with pytest.raises(psycopg.errors.CheckViolation, match="is append-only"):
        with store.connect() as conn, conn.transaction():
            conn.execute("DELETE FROM public.fc27_recovery_codes WHERE code_id = %s", (code_ids[0],))


# =====================================================================
# 6. WebAuthn Credentials Tests
# =====================================================================

def test_webauthn_credential_lifecycle(store):
    user = store.create_user(
        club_id="600",
        login_id=f"webauthn_{secrets.token_hex(4)}@fc27.com",
        display_name="Passkey User",
        status="active",
    )
    user_id = user["user_id"]
    cred_id = secrets.token_bytes(32)
    pub_key = secrets.token_bytes(65)

    # 1. Save passkey
    saved = store.save_webauthn_credential(
        credential_id=cred_id,
        user_id=user_id,
        public_key=pub_key,
        sign_counter=0,
        label="MacBook Touch ID",
        actor="user",
    )
    assert bytes(saved["credential_id"]) == cred_id

    # 2. Get passkey
    fetched = store.get_webauthn_credential(cred_id)
    assert fetched is not None
    assert fetched["label"] == "MacBook Touch ID"

    # 3. Monotonic counter increment
    store.update_webauthn_counter(cred_id, 1)
    store.update_webauthn_counter(cred_id, 2)
    updated = store.get_webauthn_credential(cred_id)
    assert updated["sign_counter"] == 2

    # 4. Clone/replay detection: counter decrease fails
    with pytest.raises(IdentityStoreError) as exc_info:
        store.update_webauthn_counter(cred_id, 1)
    assert exc_info.value.status_code == 401

    # 5. Revocation
    store.revoke_webauthn_credential(cred_id, actor="user")
    assert store.get_webauthn_credential(cred_id) is None


# =====================================================================
# 7. Action Tokens Tests
# =====================================================================

def test_action_token_lifecycle(store):
    user = store.create_user(
        club_id="700",
        login_id=f"act_{secrets.token_hex(4)}@fc27.com",
        display_name="Action Token User",
        status="active",
    )
    user_id = user["user_id"]

    raw_token, tok = store.create_action_token(
        user_id=user_id,
        scope="player_decision",
        ttl_seconds=3600,
        actor="system",
    )
    assert tok["scope"] == "player_decision"

    # Fetch
    fetched = store.get_action_token_by_token(raw_token)
    assert fetched is not None

    # Scope mismatch fails
    with pytest.raises(IdentityStoreError) as exc_info:
        store.consume_action_token(raw_token, expected_scope="step_up", actor="system")
    assert exc_info.value.status_code == 403

    # Matching scope consumption succeeds
    consumed = store.consume_action_token(raw_token, expected_scope="player_decision", actor="player")
    assert consumed["consumed_at"] is not None

    # Second consumption fails
    with pytest.raises(IdentityStoreError) as exc_info:
        store.consume_action_token(raw_token, actor="player")
    assert exc_info.value.status_code == 409


# =====================================================================
# 8. Challenges Tests
# =====================================================================

def test_challenge_single_use_and_binding(store):
    challenge_str = secrets.token_urlsafe(32)
    bound_hash = hashlib.sha256(b"package_v1_payload").hexdigest()

    ch = store.create_challenge(
        challenge_type="step_up_decision",
        challenge_str=challenge_str,
        bound_payload_hash=bound_hash,
        ttl_seconds=300,
    )
    assert ch["challenge_type"] == "step_up_decision"

    # Mismatched bound hash fails consumption
    wrong_hash = hashlib.sha256(b"other").hexdigest()
    assert store.consume_challenge(
        challenge_str=challenge_str,
        challenge_type="step_up_decision",
        expected_payload_hash=wrong_hash,
    ) is None

    # Matching hash succeeds
    consumed = store.consume_challenge(
        challenge_str=challenge_str,
        challenge_type="step_up_decision",
        expected_payload_hash=bound_hash,
    )
    assert consumed is not None

    # Single-use: already consumed returns None
    assert store.consume_challenge(
        challenge_str=challenge_str,
        challenge_type="step_up_decision",
        expected_payload_hash=bound_hash,
    ) is None
