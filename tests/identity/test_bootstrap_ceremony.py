"""End-to-End and Integration Tests for Commissioner Bootstrap Ceremony Lifecycle (tests/identity/test_bootstrap_ceremony.py).

Verifies Finding R7 (Bootstrap Enrollment Session Safety):
1. POST /v1/auth/bootstrap issues a restricted session with auth_level="bootstrap_enrollment".
2. The bootstrap_enrollment session is strictly quarantined:
   - Permitted actions: passkey_register, passkey_view.
   - Forbidden actions: package_approve, user_role_assign, package_view, etc. (raises RoleNotPermittedError / 403).
   - Session rotation is forbidden (HTTP 403).
3. POST /v1/auth/bootstrap/complete fails (HTTP 400) if no passkey has been registered for the user.
4. After passkey registration, POST /v1/auth/bootstrap/complete succeeds (HTTP 201):
   - Inserts permanent lock in fc27_bootstrap_locks.
   - Revokes bootstrap_enrollment session (old token returns HTTP 401).
   - Mints active commissioner session with auth_level="step_up_passkey".
5. Subsequent bootstrap attempts fail closed with HTTP 409 (permanent lock).
"""
from __future__ import annotations

import secrets
from uuid import UUID, uuid4

import psycopg
import pytest

from src.identity.policy import (
    ActionType,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
    authorize,
)
from src.identity.recovery import DEFAULT_BOOTSTRAP_TOKEN
from src.identity.store import IdentityStore
from tests.e2e.phase2_helpers import session_header


@pytest.fixture
def store(pg_uri: str) -> IdentityStore:
    return IdentityStore(pg_uri)


def test_bootstrap_enrollment_session_quarantine_policy():
    """Verify policy quarantines bootstrap_enrollment to passkey operations only."""
    uid = uuid4()
    principal = PrincipalContext.create(
        user_id=uid,
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.BOOTSTRAP_ENROLLMENT,
        auth_method=AuthMethod.BOOTSTRAP,
        is_service=False,
    )
    resource = Resource(resource_type="passkey", club_id="330323", owner_user_id=uid)

    # Allowed actions
    res_reg = authorize(principal, ActionType.PASSKEY_REGISTER, resource)
    assert res_reg.allowed is True
    res_view = authorize(principal, ActionType.PASSKEY_VIEW, resource)
    assert res_view.allowed is True

    # Denied actions
    for forbidden_action in [
        ActionType.PACKAGE_APPROVE,
        ActionType.PACKAGE_REJECT,
        ActionType.PACKAGE_VIEW,
        ActionType.USER_ROLE_ASSIGN,
        ActionType.DELEGATION_CREATE,
    ]:
        with pytest.raises(RoleNotPermittedError) as exc:
            authorize(principal, forbidden_action, resource)
        assert "restricted to passkey registration" in str(exc.value)


def test_bootstrap_ceremony_full_lifecycle(client, store: IdentityStore, pg_uri: str):
    """Test full multi-step ceremony from bootstrap -> quarantine -> completion -> permanent lock."""
    # Ensure database is clean of prior bootstrap locks
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("DELETE FROM public.fc27_bootstrap_locks WHERE lock_id = 'commissioner_bootstrap'")
        conn.execute("DELETE FROM public.fc27_users WHERE login_id = 'commissioner.r7@fc27.club'")

    # 1. Initiate bootstrap
    bootstrap_payload = {
        "login_identifier": "commissioner.r7@fc27.club",
        "display_name": "Commissioner R7",
        "club_id": "330323",
        "bootstrap_token": DEFAULT_BOOTSTRAP_TOKEN,
    }
    res_boot = client.post("/v1/auth/bootstrap", json=bootstrap_payload)
    assert res_boot.status_code in (200, 201), res_boot.text
    boot_data = res_boot.json()
    enrollment_token = boot_data["session_token"]
    user_id = UUID(boot_data["user_id"])

    # 2. Inspect session: auth_level must be bootstrap_enrollment
    sess_check = client.get("/v1/auth/session", headers=session_header(enrollment_token))
    assert sess_check.status_code == 200
    sess_info = sess_check.json()
    assert sess_info["auth_level"] == "bootstrap_enrollment"

    # 3. Attempting to rotate bootstrap_enrollment session must return 403
    rot_attempt = client.post("/v1/auth/sessions/rotate", headers=session_header(enrollment_token), json={})
    assert rot_attempt.status_code == 403
    assert "bootstrap enrollment session cannot be rotated" in rot_attempt.json().get("detail", "")

    # 4. Attempting to complete bootstrap BEFORE passkey registration must return 400
    comp_fail = client.post("/v1/auth/bootstrap/complete", headers=session_header(enrollment_token))
    assert comp_fail.status_code == 400
    assert "passkey registration required" in comp_fail.json().get("detail", "")

    # 5. Register a passkey for the user
    cred_id = secrets.token_bytes(32)
    pub_key = secrets.token_bytes(65)
    store.save_webauthn_credential(
        credential_id=cred_id,
        user_id=user_id,
        public_key=pub_key,
        sign_counter=0,
        label="Commissioner Hardware Key",
    )

    # 6. Complete bootstrap ceremony
    comp_success = client.post("/v1/auth/bootstrap/complete", headers=session_header(enrollment_token))
    assert comp_success.status_code == 201, comp_success.text
    comp_data = comp_success.json()
    active_token = comp_data["session_token"]
    assert active_token != enrollment_token

    # 7. Old enrollment token must now be revoked (HTTP 401)
    old_check = client.get("/v1/auth/session", headers=session_header(enrollment_token))
    assert old_check.status_code == 401

    # 8. New active token must have step_up_passkey auth_level
    new_check = client.get("/v1/auth/session", headers=session_header(active_token))
    assert new_check.status_code == 200
    assert new_check.json()["auth_level"] == "step_up_passkey"

    # 9. Verify permanent lock recorded in database
    with psycopg.connect(pg_uri) as conn:
        lock_row = conn.execute(
            "SELECT lock_id, commissioner_user_id FROM public.fc27_bootstrap_locks WHERE lock_id = 'commissioner_bootstrap'"
        ).fetchone()
        assert lock_row is not None
        assert str(lock_row[1]) == str(user_id)

    # 10. Subsequent bootstrap attempts must be rejected with HTTP 409
    res_boot_replay = client.post("/v1/auth/bootstrap", json=bootstrap_payload)
    assert res_boot_replay.status_code == 409
    assert "locked" in res_boot_replay.json().get("detail", "")
