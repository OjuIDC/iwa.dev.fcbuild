"""Phase 2 WebAuthn Passkeys Router (src/identity/routers/passkeys.py).

Endpoints:
- POST   /v1/auth/passkeys/register/options   : Authenticated human requests registration options
- POST   /v1/auth/passkeys/register/verify    : Verifies attestation, stores passkey in DB
- POST   /v1/auth/passkeys/authenticate/options: Requests login assertion options
- POST   /v1/auth/passkeys/authenticate/verify : Verifies login assertion, issues session
- POST   /v1/auth/passkeys/login/options      : (Alias) Requests login assertion options
- POST   /v1/auth/passkeys/login/verify       : (Alias) Verifies login assertion, issues session
- GET    /v1/auth/passkeys                    : Lists user's registered passkeys (metadata only)
- DELETE /v1/auth/passkeys/{credential_id}    : Revokes specific passkey

Security Invariants:
- require_human_principal prevents any agent or service from creating/asserting passkeys.
- Centralized authorization via ActionType.PASSKEY_REGISTER, PASSKEY_VIEW, PASSKEY_REVOKE.
- Single-use challenge validation strictly prevents replay attacks.
- Anti-cloning counter verification enforces monotonic increments.
- Zero player delivery; DISPATCH_ENABLED=false at all times.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.identity.deps import (
    get_current_principal,
    require_human_principal,
    require_permission,
)
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    UserRole,
    authorize,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.webauthn import (
    WebAuthnEngine,
    get_webauthn_engine,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)

log = logging.getLogger("fc27.identity.routers.passkeys")

router = APIRouter()
passkeys_router = router


def get_identity_store(request: Request) -> IdentityStore:
    store: IdentityStore | None = getattr(request.app.state, "identity_store", None)
    if store is not None:
        return store
    settings = getattr(request.app.state, "settings", None)
    uri = getattr(settings, "postgres_uri", None) if settings else None
    if not uri:
        uri = os.environ.get("POSTGRES_URI")
    if not uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="database configuration unavailable",
        )
    store = IdentityStore(uri)
    request.app.state.identity_store = store
    return store


# ==============================================================================
# Pydantic Schemas
# ==============================================================================

class PasskeyRegisterOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default="Passkey", max_length=128)


class PasskeyRegisterVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., description="Credential ID base64url or hex")
    rawId: str | None = None
    type: Literal["public-key"] = "public-key"
    response: dict[str, Any]
    label: str | None = Field(default="Passkey", max_length=128)


class PasskeyRegisterVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    credential_id: str
    verified: bool = True
    label: str
    created_at: datetime


class PasskeyLoginOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_identifier: str | None = None
    user_verification: Literal["preferred", "required", "discouraged"] = "preferred"


class PasskeyLoginVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., description="Credential ID base64url or hex")
    rawId: str | None = None
    type: Literal["public-key"] = "public-key"
    response: dict[str, Any]


class PasskeyLoginVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_token: str
    session_id: UUID
    user_id: UUID
    club_id: str
    roles: list[str]
    role: str
    auth_level: str
    auth_method: str = "passkey"
    verified: bool = True


class PasskeyMetadataItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    credential_id: str
    label: str
    aaguid: str | None = None
    sign_counter: int
    created_at: datetime
    last_used_at: datetime | None = None


class PasskeyRevokeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    credential_id: str
    revoked: bool = True


# ==============================================================================
# Endpoints
# ==============================================================================

@router.post(
    "/passkeys/register/options",
    status_code=status.HTTP_200_OK,
    summary="Generate WebAuthn Registration Options",
)
def passkey_register_options(
    request: Request,
    body: PasskeyRegisterOptionsRequest | None = None,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict[str, Any]:
    """Issues single-use challenge and creation options for registering a new passkey."""
    res = Resource(resource_type="webauthn_credential", club_id=principal.club_id)
    authorize(principal, ActionType.PASSKEY_REGISTER, res)

    user = store.get_user(principal.user_id)
    if not user:
        # Fallback for synthetic/legacy principal
        username = principal.display_name or f"user_{principal.user_id}@fc27.club"
        display_name = principal.display_name or "Commissioner"
    else:
        if user["status"] != "active":
            raise HTTPException(status_code=403, detail=f"account in status '{user['status']}' cannot register passkeys")
        username = user.get("login_id") or user.get("login_identifier", str(principal.user_id))
        display_name = user.get("display_name", username)

    existing_creds = store.list_user_webauthn_credentials(principal.user_id)
    engine = get_webauthn_engine(store)

    options_dict = engine.generate_registration_options(
        user_id=principal.user_id,
        username=username,
        display_name=display_name,
        existing_credentials=existing_creds,
    )
    return options_dict


@router.post(
    "/passkeys/register/verify",
    response_model=PasskeyRegisterVerifyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Verify Registration Response and Store Passkey",
)
def passkey_register_verify(
    request: Request,
    body: PasskeyRegisterVerifyRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PasskeyRegisterVerifyResponse:
    """Verifies authenticator attestation, stores public key, and registers passkey."""
    res = Resource(resource_type="webauthn_credential", club_id=principal.club_id)
    authorize(principal, ActionType.PASSKEY_REGISTER, res)

    engine = get_webauthn_engine(store)
    payload_dict = body.model_dump()

    try:
        verified = engine.verify_registration_response(
            response_payload=payload_dict,
            user_id=principal.user_id,
        )
    except (InvalidRegistrationResponse, InvalidJSONStructure) as exc:
        log.warning("passkey registration verification failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        log.warning("passkey registration error: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid registration response")

    label = body.label or "Passkey"
    try:
        saved = store.save_webauthn_credential(
            credential_id=verified.credential_id,
            user_id=principal.user_id,
            public_key=verified.public_key,
            sign_counter=verified.sign_counter,
            label=label,
            aaguid=verified.aaguid,
            actor=principal.display_name or str(principal.user_id),
        )
    except IdentityStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    return PasskeyRegisterVerifyResponse(
        credential_id=verified.credential_id_hex,
        verified=True,
        label=label,
        created_at=saved["created_at"],
    )


@router.post(
    "/passkeys/authenticate/options",
    status_code=status.HTTP_200_OK,
    summary="Generate WebAuthn Authentication Assertion Options",
)
@router.post(
    "/passkeys/login/options",
    status_code=status.HTTP_200_OK,
    summary="Generate WebAuthn Login Options (Alias)",
)
def passkey_authenticate_options(
    request: Request,
    body: PasskeyLoginOptionsRequest | None = None,
    store: IdentityStore = Depends(get_identity_store),
) -> dict[str, Any]:
    """Issues single-use challenge and request options for passkey sign-in."""
    engine = get_webauthn_engine(store)
    allowed_creds = []
    target_user_id = None

    if body and body.login_identifier:
        user = store.get_user_by_login(body.login_identifier)
        if user:
            target_user_id = user["user_id"]
            allowed_creds = store.list_user_webauthn_credentials(target_user_id)

    uv = body.user_verification if body else "preferred"
    options_dict = engine.generate_authentication_options(
        user_id=target_user_id,
        allowed_credentials=allowed_creds,
        user_verification=uv,
    )
    return options_dict


@router.post(
    "/passkeys/authenticate/verify",
    response_model=PasskeyLoginVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify WebAuthn Assertion and Create Session",
)
@router.post(
    "/passkeys/login/verify",
    response_model=PasskeyLoginVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify WebAuthn Assertion and Create Session (Alias)",
)
def passkey_authenticate_verify(
    request: Request,
    body: PasskeyLoginVerifyRequest,
    store: IdentityStore = Depends(get_identity_store),
) -> PasskeyLoginVerifyResponse:
    """Verifies authenticator assertion, checks anti-cloning counter, and creates session."""
    engine = get_webauthn_engine(store)
    payload_dict = body.model_dump()

    raw_cred_id_str = body.rawId or body.id
    try:
        cred_bytes = store._normalize_credential_id(raw_cred_id_str)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid credential ID format")

    stored_cred = store.get_webauthn_credential(cred_bytes)
    if not stored_cred:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="passkey credential not found or revoked",
        )

    user = store.get_user(stored_cred["user_id"])
    if not user or user["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="associated user account is not active",
        )

    try:
        verified = engine.verify_authentication_response(
            response_payload=payload_dict,
            stored_credential=stored_cred,
        )
    except InvalidAuthenticationResponse as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    except (InvalidJSONStructure, Exception) as exc:
        log.warning("passkey assertion verification failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid assertion structure")

    try:
        store.update_webauthn_counter(cred_bytes, verified.new_sign_counter)
    except IdentityStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    roles = list(store.get_user_roles(user["user_id"]))
    primary_role = roles[0] if roles else "player"

    raw_token, session_row = store.create_session(
        user_id=user["user_id"],
        auth_level="authenticated",
        auth_method="passkey",
        club_id=user["club_id"],
        actor=user.get("display_name") or str(user["user_id"]),
    )

    return PasskeyLoginVerifyResponse(
        session_token=raw_token,
        session_id=session_row["session_id"],
        user_id=user["user_id"],
        club_id=user["club_id"],
        roles=roles,
        role=primary_role,
        auth_level="authenticated",
        auth_method="passkey",
        verified=True,
    )


@router.get(
    "/passkeys",
    response_model=list[PasskeyMetadataItem],
    status_code=status.HTTP_200_OK,
    summary="List Registered Passkeys",
)
def list_passkeys(
    request: Request,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> list[PasskeyMetadataItem]:
    """Returns sanitized metadata for all active passkeys owned by the caller."""
    res = Resource(resource_type="webauthn_credential", club_id=principal.club_id)
    authorize(principal, ActionType.PASSKEY_VIEW, res)

    creds = store.list_user_webauthn_credentials(principal.user_id)
    result = []
    for c in creds:
        result.append(
            PasskeyMetadataItem(
                credential_id=c["credential_id"].hex() if isinstance(c["credential_id"], bytes) else str(c["credential_id"]),
                label=c["label"],
                aaguid=str(c["aaguid"]) if c.get("aaguid") else None,
                sign_counter=c.get("sign_counter", c.get("sign_count", 0)),
                created_at=c["created_at"],
                last_used_at=c.get("last_used_at"),
            )
        )
    return result


@router.delete(
    "/passkeys/{credential_id}",
    response_model=PasskeyRevokeResponse,
    status_code=status.HTTP_200_OK,
    summary="Revoke Passkey",
)
def revoke_passkey(
    request: Request,
    credential_id: str = Path(..., description="Hex or Base64URL credential ID"),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PasskeyRevokeResponse:
    """Revokes a registered passkey, verifying caller ownership."""
    res = Resource(resource_type="webauthn_credential", club_id=principal.club_id)
    authorize(principal, ActionType.PASSKEY_REVOKE, res)

    try:
        cred_bytes = store._normalize_credential_id(credential_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid credential ID format")

    cred = store.get_webauthn_credential(cred_bytes)
    if not cred:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="credential not found")

    # Object-level isolation: Must own the credential or be Commissioner
    if cred["user_id"] != principal.user_id and not principal.is_commissioner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot revoke another user's credential")

    store.revoke_webauthn_credential(
        cred_bytes,
        actor=principal.display_name or str(principal.user_id),
    )

    return PasskeyRevokeResponse(
        credential_id=credential_id,
        revoked=True,
    )
