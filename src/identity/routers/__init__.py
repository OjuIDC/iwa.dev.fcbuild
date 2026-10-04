"""Phase 2 Identity Routers Package Init."""
from __future__ import annotations

from src.identity.routers.delegations import router as delegations_router
from src.identity.routers.invitations import router as invitations_router
from src.identity.routers.passkeys import passkeys_router
from src.identity.routers.player import player_router
from src.identity.routers.recovery import recovery_router
from src.identity.routers.sessions import auth_router, sessions_router
from src.identity.routers.step_up import step_up_router
from src.identity.routers.totp import totp_router
from src.identity.routers.users import router as users_router

__all__ = [
    "invitations_router",
    "sessions_router",
    "auth_router",
    "users_router",
    "passkeys_router",
    "totp_router",
    "recovery_router",
    "player_router",
    "step_up_router",
    "delegations_router",
]
