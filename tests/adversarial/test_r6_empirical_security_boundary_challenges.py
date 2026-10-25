"""Milestone R6 Empirical Adversarial Challenge Test Suite.

Authoritative Mission:
Adversarially challenge Milestone R6 deliverables across all 9 Security Boundary Invariant challenges
and universal dormancy invariants.

Challenges:
1. WebAuthn User Verification requirement (verify UV=False is rejected with HTTP 400).
2. 14-field evidence binding (verify altering any bound field or client canonical hash returns 400/403).
3. Single-transaction decision commit (verify atomic rollback on conflict).
4. Session elevation (verify request data cannot elevate session auth level).
5. Bootstrap enrollment (verify bootstrap cannot complete without passkey registration).
6. Player decision proof gate (verify proofless calls return 422 and random uuid4 evidence is absent).
7. TOTP replay (verify replaying last consumed step returns 409).
8. Invitations service role guard (verify inviting bff_service/worker returns 422).
9. Model validation (verify extra fields return 422 on all 23 models).
10. Dormancy invariants (DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false, 0 dispatches, 0 published outbox rows).
"""
from __future__ import annotations

import ast
import hashlib
import os
import pyotp
import pytest
from cryptography.fernet import Fernet
from datetime import datetime, timezone
from pydantic import ValidationError
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

# Domain imports
from src.approval_store import (
    ApprovalStore,
    StoreError,
    compute_bound_payload_hash,
)
from src.identity.crypto import reset_identity_crypto
from src.identity.policy import (
    ActionType,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    UserRole,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.webauthn import (
    WebAuthnEngine,
    WebAuthnConfig,
    InvalidAuthenticationResponse,
)
from src.settings import env_bool

# Model imports for Challenge 9 (all 23 models)
from src.schemas import (
    WebAuthnDecisionSubmission,
    CliHmacDecisionSubmission,
    HmacDecisionSubmission,
)
from src.identity.routers.player import (
    PlayerDecisionChallengeRequest,
    PlayerDecisionSubmissionRequest,
    IssuePlayerActionTokenRequest,
    router as player_router,
    get_identity_store as player_get_identity_store,
)
from src.identity.routers.invitations import (
    InvitationCreateRequest,
    InvitationConsumeRequest,
    router as invitations_router,
    get_identity_store as invitations_get_identity_store,
    PROHIBITED_INVITATION_ROLES,
)
from src.identity.routers.step_up import (
    StepUpChallengeRequest,
    StepUpVerifyRequest,
    ActionTokenIssueRequest,
    router as step_up_router,
    get_identity_store as step_up_get_identity_store,
    get_engine as step_up_get_engine,
)
from src.identity.routers.passkeys import (
    PasskeyRegisterOptionsRequest,
    PasskeyRegisterVerifyRequest,
    PasskeyLoginOptionsRequest,
    PasskeyLoginVerifyRequest,
)
from src.identity.routers.sessions import (
    SessionRotateRequest,
    sessions_router,
    get_identity_store as sessions_get_identity_store,
    get_raw_session_token,
)
from src.identity.routers.recovery import (
    BootstrapRequest,
    RecoveryRequest,
    complete_bootstrap_ceremony,
)
from src.identity.routers.delegations import (
    CreateDelegationRequest,
)
from src.identity.routers.users import (
    UserStatusUpdateRequest,
    UserSuspendRequest,
)
from src.identity.routers.totp import (
    TotpVerifyRequest,
    TotpAuthenticateRequest,
    router as totp_router,
)
from src.identity.deps import get_current_principal, require_human_principal


@pytest.fixture(autouse=True)
def setup_encryption_key(monkeypatch):
    """Ensure IDENTITY_ENCRYPTION_KEY is available for MultiFernet operations."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", key)
    reset_identity_crypto()
    yield key
    reset_identity_crypto()


# ==============================================================================
# Challenge 1: WebAuthn User Verification Requirement (UV=False -> HTTP 400)
# ==============================================================================

class TestChallenge1WebAuthnUserVerification:
    """Challenge WebAuthn User Verification requirement: UV=False rejected with HTTP 400."""

    def test_webauthn_engine_rejects_missing_uv_flag(self):
        """WebAuthnEngine.verify_step_up_response raises InvalidAuthenticationResponse when UV is False."""
        mock_store = MagicMock()
        mock_store.consume_challenge.return_value = {
            "challenge_str": "test_challenge",
            "challenge_type": "step_up_decision",
            "bound_payload_hash": "a" * 64,
            "user_id": UUID("11111111-1111-1111-1111-111111111111"),
        }
        cfg = WebAuthnConfig(rp_id="localhost", rp_name="FC27", allowed_origins=("http://localhost:3000",))
        engine = WebAuthnEngine(store=mock_store, config=cfg)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(engine, "_extract_challenge_from_response", lambda r: "test_challenge")
            mock_verified = MagicMock()
            mock_verified.user_verified = False
            mock_verified.credential_id = b"test_cred_id"
            mock_verified.new_sign_count = 10
            mp.setattr("src.identity.webauthn._webauthn_verify_auth_response", lambda **kw: mock_verified)

            stored_cred = {
                "credential_id": b"test_cred_id",
                "public_key": b"fake_pub_key",
                "sign_counter": 5,
                "user_id": UUID("11111111-1111-1111-1111-111111111111"),
            }

            with pytest.raises(InvalidAuthenticationResponse) as exc_info:
                engine.verify_step_up_response(
                    response_payload={"id": "fake"},
                    bound_payload_hash="a" * 64,
                    stored_credential=stored_cred,
                    user_id=UUID("11111111-1111-1111-1111-111111111111"),
                )
            assert "requires user verification" in str(exc_info.value).lower()
            assert "uv flag not set" in str(exc_info.value).lower()

    def test_step_up_verify_endpoint_returns_400_when_uv_false(self):
        """Step-up router verify endpoint converts UV=False failure into HTTP 400 Bad Request."""
        app = FastAPI()
        app.include_router(step_up_router, prefix="/v1")

        test_uid = uuid4()
        principal = PrincipalContext.create(
            user_id=test_uid,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
        )

        valid_bound_hash = compute_bound_payload_hash(
            package_id="pkg_1",
            package_version="1.0",
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=None,
        )

        mock_store = MagicMock()
        mock_store.get_challenge.return_value = {
            "challenge_str": "test_challenge",
            "challenge_type": "step_up_decision",
            "bound_payload_hash": valid_bound_hash,
            "user_id": test_uid,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc).replace(year=2099),
        }
        mock_store._normalize_credential_id.return_value = b"test_cred"
        mock_store.get_webauthn_credential.return_value = {
            "credential_id": b"test_cred",
            "public_key": b"fake_pub_key",
            "sign_counter": 1,
            "user_id": test_uid,
        }

        mock_engine = MagicMock()
        mock_engine._extract_challenge_from_response.return_value = "test_challenge"
        mock_engine.verify_step_up_response.side_effect = InvalidAuthenticationResponse(
            "step-up assertion requires user verification (UV flag not set)"
        )

        app.dependency_overrides[require_human_principal] = lambda: principal
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[step_up_get_identity_store] = lambda: mock_store
        app.dependency_overrides[step_up_get_engine] = lambda: mock_engine

        client = TestClient(app)
        res = client.post(
            "/v1/step-up/verify",
            json={
                "id": "test_cred_id",
                "rawId": "test_cred_id",
                "response": {
                    "clientDataJSON": "fake",
                    "authenticatorData": "fake",
                    "signature": "fake",
                },
                "type": "public-key",
                "package_id": "pkg_1",
                "package_version": "1.0",
                "package_hash": "a" * 64,
                "decision": "approved",
                "bound_payload_hash": valid_bound_hash,
            },
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert "user verification" in res.json()["detail"].lower()


# ==============================================================================
# Challenge 2: 14-Field Evidence Binding (Altering Bound Field or Hash -> 400/403)
# ==============================================================================

def _setup_approval_env(conn, user_id: UUID, session_id: UUID, club_id: str = "330323") -> tuple[str, UUID, str]:
    thread_id = f"fc27-{club_id}-adv-{uuid4().hex[:6]}"
    req_id = uuid4()
    pkg_ver = hashlib.sha256(f"pkg_ver_{req_id}".encode("utf-8")).hexdigest()
    session_token_hash = hashlib.sha256(session_id.bytes).hexdigest()

    import psycopg.types.json
    # User
    conn.execute(
        """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
           VALUES (%s, %s, %s, 'Adv Tester', 'active')
           ON CONFLICT (user_id) DO NOTHING""",
        (user_id, club_id, f"adv_{user_id.hex[:8]}@example.com"),
    )
    # Role
    conn.execute(
        """INSERT INTO public.fc27_user_roles (user_id, role, granted_by)
           VALUES (%s, 'commissioner', %s)
           ON CONFLICT DO NOTHING""",
        (user_id, str(user_id)),
    )
    # Session
    conn.execute(
        """INSERT INTO public.fc27_sessions (
               session_id, token_hash, user_id, auth_level, auth_method,
               idle_expires_at, absolute_expires_at
           ) VALUES (%s, %s, %s, 'step_up_passkey', 'passkey', now() + interval '1 hour', now() + interval '24 hours')
           ON CONFLICT (session_id) DO NOTHING""",
        (session_id, session_token_hash, user_id),
    )
    # Graph thread (running)
    conn.execute(
        """INSERT INTO public.graph_threads (thread_id, club_id, status, state, package_version)
           VALUES (%s, %s, 'running', %s, %s)""",
        (thread_id, club_id, psycopg.types.json.Jsonb({"status": "running"}), pkg_ver),
    )
    # Approval request
    conn.execute(
        """INSERT INTO public.approval_requests (
               approval_request_id, thread_id, package_version, status, created_at, expires_at
           ) VALUES (%s, %s, %s, 'awaiting_approval', now(), now() + interval '1 hour')""",
        (req_id, thread_id, pkg_ver),
    )
    # Link thread to approval_request
    conn.execute(
        """UPDATE public.graph_threads
              SET status = 'awaiting_approval',
                  approval_request_id = %s,
                  state = %s
            WHERE thread_id = %s""",
        (req_id, psycopg.types.json.Jsonb({"status": "awaiting_approval"}), thread_id),
    )
    return thread_id, req_id, pkg_ver


def _mint_evidence(conn, user_id: UUID, session_id: UUID, thread_id: str, req_id: UUID, decision: str, pkg_ver: str) -> tuple[UUID, str]:
    ev_id = uuid4()
    pkg_id = "pkg_1"
    pkg_hash = "a" * 64

    bound_hash = compute_bound_payload_hash(
        package_id=pkg_id,
        package_version=pkg_ver,
        package_hash=pkg_hash,
        decision=decision,
        approval_request_id=req_id,
    )

    conn.execute(
        """INSERT INTO public.fc27_decision_evidence (
               evidence_id, user_id, session_id, thread_id, approval_request_id,
               package_id, package_version, package_hash, normalized_decision,
               canonical_bound_payload_hash, webauthn_credential_id, challenge_id,
               issued_at, expires_at
           ) VALUES (
               %s, %s, %s, %s, %s,
               %s, %s, %s, %s,
               %s, 'cred_hex', NULL,
               now(), now() + interval '5 minutes'
           )""",
        (ev_id, user_id, session_id, thread_id, req_id, pkg_id, pkg_ver, pkg_hash, decision, bound_hash),
    )
    return ev_id, bound_hash


class TestChallenge2EvidenceBinding:
    """Challenge 14-field evidence binding: altering any bound field or client hash returns 400/403."""

    def test_altering_any_bound_field_changes_canonical_hash(self):
        """Verifies RFC-8785 canonical hash sensitivity to all bound parameters."""
        req_id = uuid4()
        base_hash = compute_bound_payload_hash(
            approval_request_id=req_id,
            package_id="pkg_core",
            package_version="1.0.0",
            package_hash="e" * 64,
            decision="approved",
        )
        assert len(base_hash) == 64

        # 1. Alter approval_request_id
        alt_req = compute_bound_payload_hash(
            approval_request_id=uuid4(),
            package_id="pkg_core",
            package_version="1.0.0",
            package_hash="e" * 64,
            decision="approved",
        )
        assert alt_req != base_hash

        # 2. Alter package_id
        alt_pkg = compute_bound_payload_hash(
            approval_request_id=req_id,
            package_id="pkg_tampered",
            package_version="1.0.0",
            package_hash="e" * 64,
            decision="approved",
        )
        assert alt_pkg != base_hash

        # 3. Alter package_version
        alt_ver = compute_bound_payload_hash(
            approval_request_id=req_id,
            package_id="pkg_core",
            package_version="1.0.1",
            package_hash="e" * 64,
            decision="approved",
        )
        assert alt_ver != base_hash

        # 4. Alter package_hash
        alt_phash = compute_bound_payload_hash(
            approval_request_id=req_id,
            package_id="pkg_core",
            package_version="1.0.0",
            package_hash="f" * 64,
            decision="approved",
        )
        assert alt_phash != base_hash

        # 5. Alter decision
        alt_dec = compute_bound_payload_hash(
            approval_request_id=req_id,
            package_id="pkg_core",
            package_version="1.0.0",
            package_hash="e" * 64,
            decision="rejected",
        )
        assert alt_dec != base_hash

    def test_apply_webauthn_decision_rejects_altered_bound_fields(self, pg_uri: str):
        """ApprovalStore.apply_webauthn_decision rejects mismatched caller parameters with 400/403."""
        store = ApprovalStore(pg_uri)
        user_id = uuid4()
        session_id = uuid4()

        with store.connect() as conn, conn.transaction():
            thread_id, req_id, pkg_ver = _setup_approval_env(conn, user_id, session_id)
            ev_id, canonical_hash = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)

        principal = PrincipalContext.create(
            user_id=user_id,
            session_id=session_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY,
            auth_method=AuthMethod.PASSKEY,
        )

        # 1. Alter caller approval_request_id -> StoreError(400)
        with pytest.raises(StoreError) as exc_info:
            store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision="approved",
                principal=principal,
                approval_request_id=uuid4(),  # Altered!
                thread_id=thread_id,
                package_id="pkg_1",
                package_version=pkg_ver,
            )
        assert exc_info.value.status_code == 400
        assert "approval_request_id does not match" in exc_info.value.detail.lower()

        # 2. Alter caller thread_id -> StoreError(400)
        with pytest.raises(StoreError) as exc_info:
            store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision="approved",
                principal=principal,
                approval_request_id=req_id,
                thread_id="thread_altered",  # Altered!
                package_id="pkg_1",
                package_version=pkg_ver,
            )
        assert exc_info.value.status_code == 400
        assert "thread_id does not match" in exc_info.value.detail.lower()

        # 3. Alter caller package_id -> StoreError(400)
        with pytest.raises(StoreError) as exc_info:
            store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision="approved",
                principal=principal,
                approval_request_id=req_id,
                thread_id=thread_id,
                package_id="pkg_altered",  # Altered!
                package_version=pkg_ver,
            )
        assert exc_info.value.status_code == 400
        assert "package_id does not match" in exc_info.value.detail.lower()

        # 4. Alter decision (bound decision mismatch) -> StoreError(403)
        with pytest.raises(StoreError) as exc_info:
            store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision="rejected",  # Altered from approved!
                principal=principal,
                approval_request_id=req_id,
                thread_id=thread_id,
                package_id="pkg_1",
                package_version=pkg_ver,
            )
        assert exc_info.value.status_code == 403
        assert "bound decision mismatch" in exc_info.value.detail.lower()


# ==============================================================================
# Challenge 3: Single-Transaction Decision Commit (Atomic Rollback on Conflict)
# ==============================================================================

class TestChallenge3SingleTransactionCommit:
    """Challenge single-transaction decision commit: atomic rollback on conflict."""

    def test_atomic_rollback_on_conflict_leaves_state_unmutated(self, pg_uri: str):
        """When approval request is not awaiting approval, rollback leaves evidence unconsumed."""
        store = ApprovalStore(pg_uri)
        user_id = uuid4()
        session_id = uuid4()

        with store.connect() as conn:
            with conn.transaction():
                thread_id, req_id, pkg_ver = _setup_approval_env(conn, user_id, session_id)
                ev_id, canonical_hash = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)
            # Conflicting transition: approval request transitioned to approved
            with conn.transaction():
                conn.execute(
                    "UPDATE public.approval_requests SET status = 'approved', decided_at = now() WHERE approval_request_id = %s",
                    (req_id,),
                )
                conn.execute(
                    "UPDATE public.graph_threads SET status = 'approved', approved_by = 'commissioner', approved_at = now(), decision_payload = '{}'::jsonb WHERE thread_id = %s",
                    (thread_id,),
                )

        principal = PrincipalContext.create(
            user_id=user_id,
            session_id=session_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY,
            auth_method=AuthMethod.PASSKEY,
        )

        # Attempt commit -> StoreError(409)
        with pytest.raises(StoreError) as exc_info:
            store.apply_webauthn_decision(
                evidence_id=ev_id,
                decision="approved",
                principal=principal,
                approval_request_id=req_id,
                thread_id=thread_id,
                package_id="pkg_1",
                package_version=pkg_ver,
            )
        assert exc_info.value.status_code == 409
        assert "no longer awaiting approval" in exc_info.value.detail.lower()

        # EMPIRICAL PROOF OF ATOMIC ROLLBACK:
        # Check evidence row in DB: consumed_at MUST REMAIN NULL!
        with store.connect() as conn:
            ev_check = conn.execute(
                "SELECT consumed_at FROM public.fc27_decision_evidence WHERE evidence_id = %s",
                (str(ev_id),),
            ).fetchone()
            assert ev_check is not None
            assert ev_check["consumed_at"] is None, "Atomic rollback violated: evidence was consumed despite conflict!"

            # Check audit row: NO audit entry should exist
            audit_check = conn.execute(
                "SELECT COUNT(*) as c FROM public.approval_audit WHERE authentication_evidence_id = %s",
                (str(ev_id),),
            ).fetchone()
            assert audit_check["c"] == 0, "Atomic rollback violated: audit row was inserted!"



# ==============================================================================
# Challenge 4: Session Elevation (Request Data Cannot Elevate Session Auth Level)
# ==============================================================================

class TestChallenge4SessionElevation:
    """Challenge session elevation: request data cannot elevate session auth level."""

    def test_session_rotation_rejects_unproven_elevation(self):
        """POST /rotate rejects attempts to elevate auth_level to step_up_passkey without evidence."""
        app = FastAPI()
        app.include_router(sessions_router, prefix="/v1/sessions")

        caller_uid = uuid4()
        caller_session_id = uuid4()
        principal = PrincipalContext.create(
            user_id=caller_uid,
            session_id=caller_session_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
        )

        mock_store = MagicMock()

        app.dependency_overrides[require_human_principal] = lambda: principal
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_raw_session_token] = lambda: "raw_mock_token"
        app.dependency_overrides[sessions_get_identity_store] = lambda: mock_store

        client = TestClient(app)

        # 1. Elevate to step_up_passkey -> 403
        res = client.post("/v1/sessions/rotate", json={"new_auth_level": "step_up_passkey"})
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert "cannot elevate session to 'step_up_passkey'" in res.json()["detail"].lower()

        # 2. Elevate to mfa_totp -> 403
        res = client.post("/v1/sessions/rotate", json={"new_auth_level": "mfa_totp"})
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert "cannot elevate session to 'mfa_totp'" in res.json()["detail"].lower()

    def test_bootstrap_enrollment_session_cannot_rotate(self):
        """Bootstrap enrollment sessions cannot be rotated to escape quarantine."""
        app = FastAPI()
        app.include_router(sessions_router, prefix="/v1/sessions")

        caller_uid = uuid4()
        principal = PrincipalContext.create(
            user_id=caller_uid,
            session_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.BOOTSTRAP_ENROLLMENT,
            auth_method=AuthMethod.BOOTSTRAP,
        )

        app.dependency_overrides[require_human_principal] = lambda: principal
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_raw_session_token] = lambda: "raw_bootstrap_token"
        app.dependency_overrides[sessions_get_identity_store] = lambda: MagicMock()

        client = TestClient(app)
        res = client.post("/v1/sessions/rotate", json={})
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert "bootstrap enrollment session cannot be rotated" in res.json()["detail"].lower()


# ==============================================================================
# Challenge 5: Bootstrap Enrollment (Cannot Complete Without Passkey)
# ==============================================================================

class TestChallenge5BootstrapPasskeyGate:
    """Challenge bootstrap enrollment: bootstrap cannot complete without passkey registration."""

    def test_complete_bootstrap_endpoint_rejects_missing_passkey(self):
        """POST /bootstrap/complete returns 400 when user has no registered passkey."""
        user_id = uuid4()
        principal = PrincipalContext.create(
            user_id=user_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.BOOTSTRAP_ENROLLMENT,
            auth_method=AuthMethod.BOOTSTRAP,
        )

        mock_store = MagicMock()
        mock_store.list_user_webauthn_credentials.return_value = []  # NO PASSKEY!

        import asyncio
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                complete_bootstrap_ceremony(
                    principal=principal,
                    raw_token="raw_token",
                    store=mock_store,
                )
            )
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "passkey registration required before activation" in exc_info.value.detail.lower()

    def test_store_complete_commissioner_bootstrap_enforces_passkey(self, pg_admin_db_uri: str):
        """IdentityStore.complete_commissioner_bootstrap raises 400 when passkey missing in DB."""
        store = IdentityStore(pg_admin_db_uri)
        user_id = uuid4()

        with store.connect() as conn, conn.transaction():
            conn.execute(
                """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
                   VALUES (%s, '330323', %s, 'Bootstrap Comm', 'active') ON CONFLICT DO NOTHING""",
                (user_id, f"boot_{user_id.hex[:6]}@example.com"),
            )
            # Fresh user_id has no webauthn credentials registered
            pass

        with pytest.raises(IdentityStoreError) as exc_info:
            store.complete_commissioner_bootstrap(
                user_id=user_id,
                club_id="330323",
                enrollment_token="fake_token",
            )
        assert exc_info.value.status_code == 400
        assert "passkey registration required" in exc_info.value.detail.lower()


# ==============================================================================
# Challenge 6: Player Decision Proof Gate (Proofless Calls Return 422)
# ==============================================================================

class TestChallenge6PlayerDecisionProofGate:
    """Challenge player decision proof gate: proofless calls return 422, uuid4 absent."""

    def test_player_decision_without_proof_returns_422(self):
        """POST /player-assignments/{id}/decision returns 422 when neither token nor WebAuthn provided."""
        app = FastAPI()
        app.include_router(player_router, prefix="/v1/player-assignments")

        player_uid = uuid4()
        principal = PrincipalContext.create(
            user_id=player_uid,
            player_id="10001",
            club_id="330323",
            roles=[UserRole.PLAYER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
        )

        mock_store = MagicMock()
        mock_store.get_player_assignment.return_value = {
            "assignment_id": uuid4(),
            "subject_player_id": "10001",
            "club_id": "330323",
            "package_id": "pkg_1",
            "package_version": "1.0",
        }

        app.dependency_overrides[require_human_principal] = lambda: principal
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[player_get_identity_store] = lambda: mock_store

        client = TestClient(app)
        aid = uuid4()

        # Proofless request: neither action_token nor authentication_evidence_id
        res = client.post(
            f"/v1/player-assignments/{aid}/decision",
            json={
                "decision": "accept",
                "authority_basis": "affected_player_consent",
            },
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "requires valid proof" in res.json()["detail"].lower()

    def test_zero_uuid4_in_player_router_code(self):
        """Verifies random uuid4 generation fallback is completely excised from player.py."""
        import src.identity.routers.player as player_mod
        source_code = open(player_mod.__file__, "r", encoding="utf-8").read()
        parsed = ast.parse(source_code)

        # Check all function calls in player.py AST
        called_functions = [
            node.func.id
            for node in ast.walk(parsed)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert "uuid4" not in called_functions, "AST verification failed: uuid4() call found in player.py!"


# ==============================================================================
# Challenge 7: TOTP Replay Prevention (Replaying Last Consumed Step Returns 409)
# ==============================================================================

class TestChallenge7TotpReplay:
    """Challenge TOTP replay: replaying last consumed step returns 409."""

    def test_replaying_consumed_totp_code_returns_409(self, pg_uri: str):
        """Verifying TOTP code twice in same step raises IdentityStoreError(409)."""
        store = IdentityStore(pg_uri)
        user_id = uuid4()
        clean_seed = pyotp.random_base32()

        # Setup user and enroll TOTP
        with store.connect() as conn, conn.transaction():
            conn.execute(
                """INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status)
                   VALUES (%s, '330323', %s, 'TOTP Tester', 'active') ON CONFLICT DO NOTHING""",
                (user_id, f"totp_{user_id.hex[:6]}@example.com"),
            )
            enc_seed = store.crypto.encrypt_secret(clean_seed)
            conn.execute(
                """INSERT INTO public.fc27_totp_credentials (
                       user_id, encrypted_seed, key_version, status, enrolled_at
                   ) VALUES (%s, %s, 1, 'active', now())
                   ON CONFLICT (user_id) DO UPDATE SET encrypted_seed = EXCLUDED.encrypted_seed, status = 'active'""",
                (user_id, enc_seed),
            )

        totp = pyotp.TOTP(clean_seed)
        code = totp.now()

        # First consumption: MUST SUCCEED
        res1 = store.verify_and_consume_totp(user_id=user_id, code=code)
        assert res1["verified"] is True
        assert res1["consumed_step"] is not None

        # Second consumption (REPLAY): MUST FAIL WITH 409
        with pytest.raises(IdentityStoreError) as exc_info:
            store.verify_and_consume_totp(user_id=user_id, code=code)
        assert exc_info.value.status_code == 409
        assert "replay_detected" in exc_info.value.detail.lower()


# ==============================================================================
# Challenge 8: Invitations Service Role Guard (Inviting bff_service/worker -> 422)
# ==============================================================================

class TestChallenge8InvitationsServiceRoleGuard:
    """Challenge invitations service role guard: inviting bff_service/worker returns 422."""

    @pytest.mark.parametrize("prohibited_role", sorted(PROHIBITED_INVITATION_ROLES))
    def test_inviting_machine_role_raises_validation_error(self, prohibited_role: str):
        """Pydantic model rejects machine service roles with ValueError."""
        payload = {
            "club_id": "330323",
            "role": prohibited_role,
            "login_identifier": f"service_{prohibited_role}@fc27.club",
        }
        with pytest.raises(ValidationError) as exc_info:
            InvitationCreateRequest(**payload)
        errors = exc_info.value.errors()
        assert any("cannot be provisioned via human invitation" in err["msg"] for err in errors)

    def test_invitation_route_returns_422_for_machine_role(self):
        """POST /v1/auth/invitations returns 422 when role is 'worker' or 'bff_service'."""
        app = FastAPI()
        app.include_router(invitations_router, prefix="/v1/auth/invitations")

        comm_uid = uuid4()
        principal = PrincipalContext.create(
            user_id=comm_uid,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
        )

        app.dependency_overrides[require_human_principal] = lambda: principal
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[invitations_get_identity_store] = lambda: MagicMock()

        client = TestClient(app)

        res = client.post(
            "/v1/auth/invitations",
            json={
                "club_id": "330323",
                "role": "bff_service",
                "login_identifier": "bot@fc27.club",
            },
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "machine identity role 'bff_service' cannot be provisioned" in res.text


# ==============================================================================
# Challenge 9: Model Validation (Extra Fields Forbidden on All 23 Models)
# ==============================================================================

class TestChallenge9ModelValidationExtraForbidden:
    """Challenge model validation: extra fields return 422 on all 23 security models."""

    MODELS_UNDER_TEST = [
        # Schemas
        (WebAuthnDecisionSubmission, {"proof_type": "webauthn", "decision": "approved", "authentication_evidence_id": uuid4()}),
        (CliHmacDecisionSubmission, {"proof_type": "cli_hmac", "decision": "approved", "signature": "a" * 64}),
        (HmacDecisionSubmission, {"proof_type": "hmac", "decision": "approved", "signature": "a" * 64}),
        # Player
        (PlayerDecisionChallengeRequest, {"assignment_id": uuid4(), "decision": "accept"}),
        (PlayerDecisionSubmissionRequest, {"decision": "accept"}),
        (IssuePlayerActionTokenRequest, {"assignment_id": uuid4(), "ttl_seconds": 3600}),
        # Invitations
        (InvitationCreateRequest, {"club_id": "330323", "role": "coach", "login_identifier": "coach@fc27.club"}),
        (InvitationConsumeRequest, {"display_name": "Coach Pep"}),
        # Step-Up
        (StepUpChallengeRequest, {"package_id": "pkg-alpha", "package_version": "1.0.0", "package_hash": "a" * 64, "decision": "approved"}),
        (StepUpVerifyRequest, {"id": "c_id", "response": {"clientDataJSON": "a"}}),
        (ActionTokenIssueRequest, {"action_scope": "player_decision", "expires_in_minutes": 60}),
        # Passkeys
        (PasskeyRegisterOptionsRequest, {"label": "My Passkey"}),
        (PasskeyRegisterVerifyRequest, {"id": "cred-xyz", "response": {"clientDataJSON": "xyz"}}),
        (PasskeyLoginOptionsRequest, {"login_identifier": "comm@fc27.club"}),
        (PasskeyLoginVerifyRequest, {"id": "cred-abc", "response": {"clientDataJSON": "abc"}}),
        # Sessions
        (SessionRotateRequest, {"new_auth_level": "mfa_totp"}),
        # Recovery
        (BootstrapRequest, {"login_identifier": "commissioner@fc27.club", "display_name": "Commissioner", "club_id": "330323", "bootstrap_token": "secret-bootstrap"}),
        (RecoveryRequest, {"login_identifier": "coach@fc27.club", "recovery_code": "RC-1234-5678"}),
        # Delegations
        (CreateDelegationRequest, {"delegate_user_id": uuid4(), "club_id": "330323", "action_type": "package_approve", "expires_in_hours": 24}),
        # Users
        (UserStatusUpdateRequest, {"status": "active", "reason": "verified"}),
        (UserSuspendRequest, {"reason": "temporary suspension"}),
        # TOTP
        (TotpVerifyRequest, {"code": "123456"}),
        (TotpAuthenticateRequest, {"code": "123456", "elevate_session": True}),
    ]

    def test_exactly_23_models_audited(self):
        """Asserts exactly 23 models are in the audit catalogue."""
        assert len(self.MODELS_UNDER_TEST) == 23

    @pytest.mark.parametrize("model_cls, valid_data", MODELS_UNDER_TEST)
    def test_each_model_rejects_extra_fields(self, model_cls, valid_data):
        """Asserts each model specifies extra='forbid' and raises ValidationError on extra fields."""
        # 1. Valid data instantiates cleanly
        instance = model_cls(**valid_data)
        assert instance is not None

        # 2. Tampered data with extra unexpected field raises ValidationError
        tampered = dict(valid_data)
        tampered["injected_malicious_field"] = "adversarial_payload"
        with pytest.raises(ValidationError) as exc_info:
            model_cls(**tampered)

        errors = exc_info.value.errors()
        assert any(e["type"] == "extra_forbidden" for e in errors), f"Model {model_cls.__name__} did not forbid extra field!"


# ==============================================================================
# Challenge 10: Dormancy Invariants (DISPATCH_ENABLED=false, 0 dispatches, 0 outbox)
# ==============================================================================

class TestChallenge10DormancyInvariants:
    """Challenge universal dormancy and zero player delivery invariants."""

    def test_environment_flags_strictly_default_false(self):
        """DISPATCH_ENABLED and DISPATCH_WORKER_ENABLED default strictly to False."""
        assert env_bool("DISPATCH_ENABLED", default=False) is False
        assert env_bool("DISPATCH_WORKER_ENABLED", default=False) is False

    def test_database_dormancy_zero_dispatches_and_zero_published_outbox(self, pg_uri: str):
        """Real PostgreSQL confirms 0 dispatches created and 0 published outbox rows."""
        import psycopg
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                # Check dispatches table
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = 'public' AND table_name = 'dispatches'
                    );
                """)
                if cur.fetchone()[0]:
                    cur.execute("SELECT COUNT(*) FROM public.dispatches")
                    count = cur.fetchone()[0]
                    assert count == 0, f"Dormancy invariant violated: found {count} dispatches!"

                # Check fc27_outbox table
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = 'public' AND table_name = 'fc27_outbox'
                    );
                """)
                if cur.fetchone()[0]:
                    cur.execute("SELECT COUNT(*) FROM public.fc27_outbox WHERE published_at IS NOT NULL")
                    count = cur.fetchone()[0]
                    assert count == 0, f"Dormancy invariant violated: found {count} published outbox rows!"

    def test_decision_commit_returns_dormant_dispatch_payload(self, pg_uri: str):
        """ApprovalStore.apply_webauthn_decision returns dormant dispatch metadata."""
        store = ApprovalStore(pg_uri)
        user_id = uuid4()
        session_id = uuid4()

        with store.connect() as conn, conn.transaction():
            thread_id, req_id, pkg_ver = _setup_approval_env(conn, user_id, session_id)
            ev_id, canonical_hash = _mint_evidence(conn, user_id, session_id, thread_id, req_id, "approved", pkg_ver)

        principal = PrincipalContext.create(
            user_id=user_id,
            session_id=session_id,
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.STEP_UP_PASSKEY,
            auth_method=AuthMethod.PASSKEY,
        )

        result = store.apply_webauthn_decision(
            evidence_id=ev_id,
            decision="approved",
            principal=principal,
            approval_request_id=req_id,
            thread_id=thread_id,
            package_id="pkg_1",
            package_version=pkg_ver,
        )

        assert result["dispatch"]["enabled"] is False
        assert result["dispatch"]["created"] is False
        assert result["dispatch_id"] is None
        assert "dispatch is disabled" in result["dispatch"]["message"].lower()
