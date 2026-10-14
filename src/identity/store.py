"""Phase 2 Identity Database Persistence Layer.

Authoritative Implementation for Phase 2 Milestone 1.

Provides IdentityStore for managing:
- fc27_users & fc27_user_roles (Users model, account status transitions, role grants)
- fc27_invitations (Invitation issuance, retrieval, consumption, revocation)
- fc27_sessions (Session creation, dual-expiry, rotation, revocation)
- fc27_webauthn_credentials (Passkey public keys, sign counters, labels)
- fc27_totp_credentials (MultiFernet encrypted seeds, key version, status)
- fc27_recovery_codes (Argon2id recovery code hashes, consumption)
- fc27_action_tokens (Hashed action tokens, single-use, scope)
- fc27_challenges (WebAuthn & step-up challenge persistence)

Invariants:
- PostgreSQL is authoritative for all identity state.
- No secrets or credentials are ever stored in plaintext.
- No credentials or sensitive values emitted in domain_events audit log.
- All mutating operations execute in atomic transactions.
- Zero player dispatch occurs (DISPATCH_ENABLED=false at all times).
"""
from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence, cast
from uuid import UUID, uuid4

import pyotp

import psycopg
from psycopg import sql
from psycopg.rows import dict_row, tuple_row

from src.events import record_event
from src.identity.crypto import (
    IdentityCrypto,
    get_identity_crypto,
    hash_recovery_code,
    hash_token,
    verify_recovery_code,
)

__all__ = [
    "IdentityStoreError",
    "IdentityStore",
]


@dataclass
class IdentityStoreError(Exception):
    """Raised on business logic / validation failures within the identity store."""
    status_code: int
    detail: str

    def __str__(self) -> str:
        return f"{self.status_code}: {self.detail}"


def _safe_record_event(conn: psycopg.Connection[Any], **kwargs: Any) -> int:
    """Safely executes record_event with tuple_row factory to avoid dict indexing errors."""
    old_factory = conn.row_factory
    conn.row_factory = tuple_row
    try:
        return record_event(conn, **kwargs)
    finally:
        conn.row_factory = old_factory


BOOTSTRAP_ADVISORY_LOCK_KEY = "fc27:commissioner_bootstrap"


def _acquire_bootstrap_advisory_lock(conn: psycopg.Connection[Any]) -> None:
    """Blocks until this transaction holds the commissioner-bootstrap advisory lock.

    Released automatically at COMMIT or ROLLBACK, so a failed ceremony never
    leaves the lock held.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (BOOTSTRAP_ADVISORY_LOCK_KEY,))


class IdentityStore:
    """Authoritative PostgreSQL persistence layer for Phase 2 identity tables."""

    VALID_ACCOUNT_STATUSES = {"invited", "active", "suspended", "recovery_required", "disabled"}
    VALID_ROLES = {
        "commissioner",
        "commissioner_delegate",
        "coach",
        "analyst",
        "staff",
        "player",
        "bff_service",
        "graph_service",
    }
    PROHIBITED_INVITATION_ROLES = frozenset({
        "bff_service",
        "graph_service",
        "worker",
        "worker_service",
        "service",
    })
    VALID_INVITATION_ROLES = frozenset({
        "commissioner",
        "commissioner_delegate",
        "coach",
        "analyst",
        "staff",
        "player",
    })
    VALID_AUTH_LEVELS = {"authenticated", "mfa_totp", "step_up_passkey", "bootstrap_enrollment"}
    VALID_AUTH_METHODS = {"passkey", "totp", "service_token", "recovery_code", "bootstrap"}
    VALID_ACTION_SCOPES = {"player_decision", "step_up", "delegation", "invitation", "recovery"}
    VALID_CHALLENGE_TYPES = {
        "webauthn_registration",
        "webauthn_login",
        "step_up_decision",
        "player_decision",
    }

    def __init__(self, conninfo: str, *, crypto: IdentityCrypto | None = None) -> None:
        self.conninfo = conninfo
        self._crypto = crypto

    @property
    def crypto(self) -> IdentityCrypto:
        if self._crypto is None:
            self._crypto = get_identity_crypto()
        return self._crypto

    def connect(self) -> psycopg.Connection[dict[str, Any]]:
        """Returns a new psycopg connection with dict_row factory."""
        return cast(psycopg.Connection[dict[str, Any]], psycopg.connect(self.conninfo, row_factory=dict_row))

    # =================================================================
    # 1. Users and Roles
    # =================================================================

    def create_user(
        self,
        *,
        club_id: str,
        login_id: str,
        display_name: str,
        user_id: UUID | None = None,
        status: str = "invited",
        player_id: str | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Creates an authoritative user account record in fc27_users."""
        normalized_login = login_id.strip().lower()
        if not normalized_login:
            raise IdentityStoreError(422, "login_id must not be empty")
        if status not in self.VALID_ACCOUNT_STATUSES:
            raise IdentityStoreError(422, f"invalid user status '{status}'")

        uid = user_id or uuid4()
        cid = correlation_id or uuid4()

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_users (
                    user_id, club_id, login_id, display_name, status, player_id,
                    created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (login_id) DO NOTHING
                RETURNING *
                """,
                (uid, club_id, normalized_login, display_name.strip(), status, player_id),
            ).fetchone()

            if row is None:
                raise IdentityStoreError(409, f"user with login '{normalized_login}' already exists")

            _safe_record_event(
                conn,
                event_type="identity.user_created",
                entity_type="user",
                entity_id=str(uid),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                to_status=status,
                source_table="fc27_users",
                source_id=str(uid),
            )
            return dict(row)

    def get_user(self, user_id: UUID) -> dict[str, Any] | None:
        """Retrieves user by UUID PK."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_users WHERE user_id = %s", (user_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            if "login_id" in d and "login_identifier" not in d:
                d["login_identifier"] = d["login_id"]
            return d

    def get_user_by_login(self, login_id: str) -> dict[str, Any] | None:
        """Retrieves user by normalized login identifier."""
        normalized_login = login_id.strip().lower()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_users WHERE login_id = %s", (normalized_login,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            if "login_id" in d and "login_identifier" not in d:
                d["login_identifier"] = d["login_id"]
            return d

    def get_user_by_login_id(self, login_id: str) -> dict[str, Any] | None:
        """Alias for get_user_by_login."""
        return self.get_user_by_login(login_id)

    def update_user_status(
        self,
        user_id: UUID,
        new_status: str,
        *,
        expected_from_status: str | None = None,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Transitions user status adhering to the lifecycle state machine."""
        if new_status not in self.VALID_ACCOUNT_STATUSES:
            raise IdentityStoreError(422, f"invalid target status '{new_status}'")

        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            user = conn.execute(
                "SELECT * FROM public.fc27_users WHERE user_id = %s FOR UPDATE", (user_id,)
            ).fetchone()
            if not user:
                raise IdentityStoreError(404, f"user {user_id} not found")

            curr_status = user["status"]
            if expected_from_status and curr_status != expected_from_status:
                raise IdentityStoreError(
                    409, f"user status mismatch: expected {expected_from_status}, found {curr_status}"
                )

            if curr_status == new_status:
                return dict(user)

            activated_clause = (
                sql.SQL(", activated_at = now()")
                if new_status == "active" and not user["activated_at"]
                else sql.SQL("")
            )
            suspended_clause = (
                sql.SQL(", suspended_at = now()")
                if new_status == "suspended"
                else sql.SQL("")
            )

            query = sql.SQL(
                """
                UPDATE public.fc27_users
                SET status = %s, updated_at = now() {activated_clause} {suspended_clause}
                WHERE user_id = %s
                RETURNING *
                """
            ).format(
                activated_clause=activated_clause,
                suspended_clause=suspended_clause,
            )

            row = conn.execute(
                query,
                (new_status, user_id),
            ).fetchone()

            _safe_record_event(
                conn,
                event_type="identity.status_changed",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=user["club_id"],
                from_status=curr_status,
                to_status=new_status,
                source_table="fc27_users",
                source_id=str(user_id),
            )
            return dict(row)

    def assign_role(
        self,
        user_id: UUID,
        role: str,
        *,
        granted_by: str = "system",
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> None:
        """Assigns an application role to a user."""
        if role not in self.VALID_ROLES:
            raise IdentityStoreError(422, f"unknown role '{role}'")

        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO public.fc27_user_roles (user_id, role, granted_at, granted_by)
                VALUES (%s, %s, now(), %s)
                ON CONFLICT (user_id, role) DO NOTHING
                """,
                (user_id, role, granted_by),
            )
            _safe_record_event(
                conn,
                event_type="identity.role_granted",
                entity_type="user_role",
                entity_id=f"{user_id}:{role}",
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=role,
                source_table="fc27_user_roles",
                source_id=str(user_id),
            )

    def assign_user_role(
        self,
        user_id: UUID,
        role: str,
        *,
        granted_by: str = "system",
        actor: str = "system",
        correlation_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Alias forwarding to assign_role with default actor='system'."""
        self.assign_role(
            user_id=user_id,
            role=role,
            granted_by=granted_by,
            actor=actor,
            correlation_id=correlation_id,
        )

    def revoke_role(
        self,
        user_id: UUID,
        role: str,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> None:
        """Revokes an application role from a user."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM public.fc27_user_roles WHERE user_id = %s AND role = %s",
                (user_id, role),
            )
            _safe_record_event(
                conn,
                event_type="identity.role_revoked",
                entity_type="user_role",
                entity_id=f"{user_id}:{role}",
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=role,
                source_table="fc27_user_roles",
                source_id=str(user_id),
            )

    def get_user_roles(self, user_id: UUID) -> set[str]:
        """Returns the set of application roles for a user."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role FROM public.fc27_user_roles WHERE user_id = %s", (user_id,)
            ).fetchall()
            return {r["role"] for r in rows}

    def get_user_by_id(self, user_id: UUID) -> dict[str, Any] | None:
        """Retrieves user by UUID PK (alias for get_user)."""
        return self.get_user(user_id)

    def suspend_user(
        self,
        user_id: UUID,
        *,
        actor: str = "commissioner",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Suspends user account and revokes all active sessions."""
        user = self.get_user(user_id)
        if not user:
            raise IdentityStoreError(404, "user not found")
        updated = self.update_user_status(user_id, "suspended", actor=actor, correlation_id=correlation_id)
        revoked_count = self.revoke_all_user_sessions(user_id, actor=actor, correlation_id=correlation_id)
        updated["revoked_sessions"] = revoked_count
        return updated

    def list_users(self, club_id: str) -> list[dict[str, Any]]:
        """Lists all users belonging to a club."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, club_id, login_id, display_name, status, player_id,
                       created_at, activated_at, suspended_at, updated_at
                FROM public.fc27_users
                WHERE club_id = %s
                ORDER BY created_at ASC
                """,
                (club_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # =================================================================
    # 2. Invitations
    # =================================================================

    def create_invitation(
        self,
        *,
        club_id: str,
        intended_role: str,
        invited_email: str | None = None,
        login_identifier: str | None = None,
        creator_id: str,
        display_name: str | None = None,
        ttl_hours: int = 72,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Creates an invitation with a 64-char hex token, storing only its SHA-256 hash.
        
        Also provisions the user record in fc27_users with status='invited' if not exists.
        """
        if intended_role in self.PROHIBITED_INVITATION_ROLES:
            raise IdentityStoreError(422, f"machine identity role '{intended_role}' cannot be provisioned via invitation")
        if intended_role not in self.VALID_INVITATION_ROLES:
            raise IdentityStoreError(422, f"invalid role '{intended_role}'")
        if ttl_hours <= 0 or ttl_hours > 720:
            raise IdentityStoreError(422, "ttl_hours must be positive and <= 720")

        email_to_use = (invited_email or login_identifier or "").strip().lower()
        if not email_to_use:
            raise IdentityStoreError(422, "invited_email/login_identifier must not be empty")

        raw_token = secrets.token_hex(32)  # 32 bytes = 64 hex characters
        tok_hash = hash_token(raw_token)
        invitation_id = uuid4()
        cid = correlation_id or uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

        with self.connect() as conn, conn.transaction():
            # Check or provision user in 'invited' status
            existing_user = conn.execute(
                "SELECT * FROM public.fc27_users WHERE login_id = %s FOR UPDATE",
                (email_to_use,),
            ).fetchone()

            if existing_user:
                if existing_user["status"] == "disabled":
                    raise IdentityStoreError(403, "cannot invite disabled user account")
                if existing_user["status"] == "active":
                    raise IdentityStoreError(409, "user account is already active")
                if existing_user["club_id"] != club_id:
                    raise IdentityStoreError(409, "user account belongs to another club")
                user_id = existing_user["user_id"]
            else:
                user_id = uuid4()
                disp_name = (display_name or email_to_use.split("@")[0]).strip()
                conn.execute(
                    """
                    INSERT INTO public.fc27_users (
                        user_id, club_id, login_id, display_name, status, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, 'invited', now(), now())
                    """,
                    (user_id, club_id, email_to_use, disp_name),
                )
                _safe_record_event(
                    conn,
                    event_type="identity.user_created",
                    entity_type="user",
                    entity_id=str(user_id),
                    actor=actor,
                    correlation_id=cid,
                    visibility="audit_only",
                    scope_club_id=club_id,
                    to_status="invited",
                    source_table="fc27_users",
                    source_id=str(user_id),
                )

            row = conn.execute(
                """
                INSERT INTO public.fc27_invitations (
                    invitation_id, token_hash, club_id, intended_role,
                    invited_email, creator_id, issued_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
                RETURNING *
                """,
                (invitation_id, tok_hash, club_id, intended_role, email_to_use, creator_id, expires_at),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to create invitation")

            _safe_record_event(
                conn,
                event_type="identity.invitation_issued",
                entity_type="invitation",
                entity_id=str(invitation_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                source_table="fc27_invitations",
                source_id=str(invitation_id),
            )
            res_dict = dict(row)
            res_dict["user_id"] = user_id
            return raw_token, res_dict

    def get_invitation_by_token(self, token: str) -> dict[str, Any] | None:
        """Retrieves invitation record by raw token."""
        tok_hash = hash_token(token)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE token_hash = %s", (tok_hash,)
            ).fetchone()
            return dict(row) if row else None

    def validate_invitation(self, token: str) -> dict[str, Any]:
        """Publicly inspects whether an invitation is valid, unconsumed, and unexpired."""
        tok_hash = hash_token(token)
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE token_hash = %s",
                (tok_hash,),
            ).fetchone()
            if not row:
                raise IdentityStoreError(404, "invitation not found")
            if row["revoked_at"] is not None:
                raise IdentityStoreError(410, "invitation was revoked")
            if row["consumed_at"] is not None:
                raise IdentityStoreError(409, "invitation already consumed")
            if row["expires_at"] <= now:
                raise IdentityStoreError(410, "invitation has expired")
            return {
                "valid": True,
                "invitation_id": row["invitation_id"],
                "club_id": row["club_id"],
                "intended_role": row["intended_role"],
                "role": row["intended_role"],
                "invited_email": row["invited_email"],
                "expires_at": row["expires_at"],
            }

    def consume_invitation(
        self,
        token: str,
        user_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Consumes an invitation, validating single-use, expiry, and revocation."""
        tok_hash = hash_token(token)
        cid = correlation_id or uuid4()
        now = datetime.now(timezone.utc)

        with self.connect() as conn, conn.transaction():
            inv = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE token_hash = %s FOR UPDATE", (tok_hash,)
            ).fetchone()
            if not inv:
                raise IdentityStoreError(404, "invitation not found")
            if inv["revoked_at"] is not None:
                raise IdentityStoreError(410, "invitation was revoked")
            if inv["consumed_at"] is not None:
                raise IdentityStoreError(409, "invitation already consumed")
            if inv["expires_at"] <= now:
                raise IdentityStoreError(410, "invitation has expired")

            row = conn.execute(
                "UPDATE public.fc27_invitations SET consumed_at = now() WHERE invitation_id = %s RETURNING *",
                (inv["invitation_id"],),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to consume invitation")

            _safe_record_event(
                conn,
                event_type="identity.invitation_consumed",
                entity_type="invitation",
                entity_id=str(inv["invitation_id"]),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=inv["club_id"],
                reason=f"accepted_by_user:{user_id}",
                source_table="fc27_invitations",
                source_id=str(inv["invitation_id"]),
            )
            return dict(row)

    def consume_invitation_and_activate_user(
        self,
        token: str,
        *,
        display_name: str | None = None,
        player_id: str | None = None,
        actor: str = "user",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Atomically locks invitation, validates single-use/expiry, activates user, and grants role."""
        tok_hash = hash_token(token)
        cid = correlation_id or uuid4()
        now = datetime.now(timezone.utc)

        with self.connect() as conn, conn.transaction():
            inv = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE token_hash = %s FOR UPDATE",
                (tok_hash,),
            ).fetchone()

            if not inv:
                raise IdentityStoreError(404, "invitation not found")
            if inv["revoked_at"] is not None:
                raise IdentityStoreError(410, "invitation was revoked")
            if inv["consumed_at"] is not None:
                raise IdentityStoreError(409, "invitation already consumed")
            if inv["expires_at"] <= now:
                raise IdentityStoreError(410, "invitation has expired")

            norm_email = inv["invited_email"]
            user = conn.execute(
                "SELECT * FROM public.fc27_users WHERE login_id = %s FOR UPDATE",
                (norm_email,),
            ).fetchone()

            resolved_display_name = (display_name or (user["display_name"] if user else norm_email.split("@")[0])).strip()

            if user is None:
                user_id = uuid4()
                conn.execute(
                    """
                    INSERT INTO public.fc27_users (
                        user_id, club_id, login_id, display_name, status, player_id, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, 'invited', %s, now(), now())
                    """,
                    (user_id, inv["club_id"], norm_email, resolved_display_name, player_id),
                )
                user_row = conn.execute(
                    """
                    UPDATE public.fc27_users
                    SET status = 'active', updated_at = now()
                    WHERE user_id = %s
                    RETURNING *
                    """,
                    (user_id,),
                ).fetchone()
            else:
                user_id = user["user_id"]
                if user["status"] == "disabled":
                    raise IdentityStoreError(403, "cannot activate disabled user account")
                if user["status"] == "active":
                    raise IdentityStoreError(409, "user account already active")
                if user["status"] != "invited":
                    raise IdentityStoreError(409, f"account in status '{user['status']}' cannot be activated from invitation")

                user_row = conn.execute(
                    """
                    UPDATE public.fc27_users
                    SET status = 'active', display_name = %s, player_id = COALESCE(%s, player_id), updated_at = now()
                    WHERE user_id = %s
                    RETURNING *
                    """,
                    (resolved_display_name, player_id, user_id),
                ).fetchone()

            if not user_row:
                raise IdentityStoreError(500, "failed to activate user account")

            role = inv["intended_role"]
            if role in self.PROHIBITED_INVITATION_ROLES:
                raise IdentityStoreError(422, f"machine identity role '{role}' cannot be provisioned via invitation")
            conn.execute(
                """
                INSERT INTO public.fc27_user_roles (user_id, role, granted_at, granted_by)
                VALUES (%s, %s, now(), %s)
                ON CONFLICT (user_id, role) DO NOTHING
                """,
                (user_id, role, inv["creator_id"]),
            )

            conn.execute(
                "UPDATE public.fc27_invitations SET consumed_at = now() WHERE invitation_id = %s",
                (inv["invitation_id"],),
            )

            _safe_record_event(
                conn,
                event_type="identity.invitation_consumed",
                entity_type="invitation",
                entity_id=str(inv["invitation_id"]),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=inv["club_id"],
                reason=f"user_activated:{user_id}",
                source_table="fc27_invitations",
                source_id=str(inv["invitation_id"]),
            )
            _safe_record_event(
                conn,
                event_type="identity.status_changed",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=inv["club_id"],
                from_status="invited",
                to_status="active",
                source_table="fc27_users",
                source_id=str(user_id),
            )
            _safe_record_event(
                conn,
                event_type="identity.role_granted",
                entity_type="user_role",
                entity_id=f"{user_id}:{role}",
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=inv["club_id"],
                reason=f"invitation:{inv['invitation_id']}",
                source_table="fc27_user_roles",
                source_id=str(user_id),
            )

            return {
                "user_id": user_row["user_id"],
                "club_id": user_row["club_id"],
                "login_id": user_row["login_id"],
                "display_name": user_row["display_name"],
                "status": user_row["status"],
                "role": role,
                "player_id": user_row["player_id"],
                "activated_at": user_row["activated_at"] or now,
            }

    def revoke_invitation(
        self,
        invitation_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Revokes an outstanding invitation. Raises 409 if already consumed or revoked."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            inv = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE invitation_id = %s FOR UPDATE", (invitation_id,)
            ).fetchone()
            if not inv:
                raise IdentityStoreError(404, "invitation not found")
            if inv["consumed_at"] is not None:
                raise IdentityStoreError(409, "cannot revoke already consumed invitation")
            if inv["revoked_at"] is not None:
                raise IdentityStoreError(409, "invitation is already revoked")

            row = conn.execute(
                "UPDATE public.fc27_invitations SET revoked_at = now() WHERE invitation_id = %s RETURNING *",
                (invitation_id,),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to revoke invitation")

            _safe_record_event(
                conn,
                event_type="identity.invitation_revoked",
                entity_type="invitation",
                entity_id=str(invitation_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=inv["club_id"],
                source_table="fc27_invitations",
                source_id=str(invitation_id),
            )
            return dict(row)

    # =================================================================
    # 3. Sessions (Dual-Expiry & Rotation)
    # =================================================================

    def create_session(
        self,
        *,
        user_id: UUID,
        auth_level: str,
        auth_method: str,
        club_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        idle_ttl_minutes: int = 30,
        absolute_ttl_hours: int = 24,
        actor: str = "system",
        correlation_id: UUID | None = None,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Creates a server-side session with dual idle and absolute timeouts."""
        if auth_level not in self.VALID_AUTH_LEVELS:
            raise IdentityStoreError(422, f"invalid auth_level '{auth_level}'")
        if auth_method not in self.VALID_AUTH_METHODS:
            raise IdentityStoreError(422, f"invalid auth_method '{auth_method}'")

        raw_token = secrets.token_hex(32)
        tok_hash = hash_token(raw_token)
        session_id = uuid4()
        cid = correlation_id or uuid4()
        now = datetime.now(timezone.utc)
        idle_exp = now + timedelta(minutes=idle_ttl_minutes)
        abs_exp = now + timedelta(hours=absolute_ttl_hours)

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_sessions (
                    session_id, token_hash, user_id, auth_level, auth_method,
                    ip_address, user_agent, created_at, last_used_at,
                    idle_expires_at, absolute_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s)
                RETURNING *
                """,
                (session_id, tok_hash, user_id, auth_level, auth_method, ip_address, user_agent, idle_exp, abs_exp),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to create session")

            _safe_record_event(
                conn,
                event_type="identity.session_created",
                entity_type="session",
                entity_id=str(session_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                source_table="fc27_sessions",
                source_id=str(session_id),
            )
            res_dict = dict(row)
            if club_id is not None and "club_id" not in res_dict:
                res_dict["club_id"] = club_id
            return raw_token, res_dict

    def get_session_by_token(
        self,
        token: str,
        *,
        touch: bool = True,
        idle_ttl_minutes: int = 30,
    ) -> dict[str, Any] | None:
        """Retrieves and touches session by raw token. Fails closed on expiry or revocation."""
        tok_hash = hash_token(token)
        now = datetime.now(timezone.utc)

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                SELECT s.*, u.club_id, u.status AS user_status, u.login_id, u.display_name, u.player_id
                FROM public.fc27_sessions s
                JOIN public.fc27_users u ON s.user_id = u.user_id
                WHERE s.token_hash = %s
                FOR UPDATE OF s
                """,
                (tok_hash,),
            ).fetchone()

            if not row:
                return None
            if row["user_status"] != "active" and row["auth_level"] != "bootstrap_enrollment":
                if row["user_status"] in ("suspended", "disabled"):
                    raise IdentityStoreError(401, f"account {row['user_status']}")
                return None
            if row["revoked_at"] is not None:
                raise IdentityStoreError(401, "session revoked")
            if now > row["absolute_expires_at"] or now > row["idle_expires_at"]:
                raise IdentityStoreError(401, "session expired")

            if touch:
                new_idle = min(now + timedelta(minutes=idle_ttl_minutes), row["absolute_expires_at"])
                conn.execute(
                    "UPDATE public.fc27_sessions SET last_used_at = now(), idle_expires_at = %s WHERE session_id = %s",
                    (new_idle, row["session_id"]),
                )
                row["last_used_at"] = now
                row["idle_expires_at"] = new_idle

            return dict(row)

    def rotate_session(
        self,
        old_token: str,
        *,
        new_auth_level: str | None = None,
        new_auth_method: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Atomically revokes the old session and creates a new rotated session."""
        old_sess = self.get_session_by_token(old_token, touch=False)
        if not old_sess:
            raise IdentityStoreError(401, "invalid or expired session for rotation")

        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            # Revoke old session with concurrency locking
            res = conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s AND revoked_at IS NULL",
                (old_sess["session_id"],),
            )
            if res.rowcount == 0:
                raise IdentityStoreError(409, "session already rotated or revoked")

            # Create new session
            raw_new_token = secrets.token_hex(32)
            tok_hash = hash_token(raw_new_token)
            new_session_id = uuid4()
            auth_level = new_auth_level or old_sess["auth_level"]
            auth_method = new_auth_method or old_sess["auth_method"]
            now = datetime.now(timezone.utc)
            idle_exp = now + timedelta(minutes=30)
            abs_exp = now + timedelta(hours=24)

            new_row = conn.execute(
                """
                INSERT INTO public.fc27_sessions (
                    session_id, token_hash, user_id, auth_level, auth_method,
                    ip_address, user_agent, created_at, last_used_at,
                    idle_expires_at, absolute_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s)
                RETURNING *
                """,
                (
                    new_session_id,
                    tok_hash,
                    old_sess["user_id"],
                    auth_level,
                    auth_method,
                    ip_address or old_sess["ip_address"],
                    user_agent or old_sess["user_agent"],
                    idle_exp,
                    abs_exp,
                ),
            ).fetchone()
            if not new_row:
                raise IdentityStoreError(500, "failed to rotate session")

            _safe_record_event(
                conn,
                event_type="identity.session_rotated",
                entity_type="session",
                entity_id=str(new_session_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=old_sess["club_id"],
                reason=f"rotated_from:{old_sess['session_id']}",
                source_table="fc27_sessions",
                source_id=str(new_session_id),
            )
            ret = dict(new_row)
            if "club_id" not in ret and old_sess.get("club_id"):
                ret["club_id"] = old_sess["club_id"]
            return raw_new_token, ret

    def revoke_session(
        self,
        session_id: UUID,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> None:
        """Revokes an individual session."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s AND revoked_at IS NULL",
                (session_id,),
            )
            _safe_record_event(
                conn,
                event_type="identity.session_revoked",
                entity_type="session",
                entity_id=str(session_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_sessions",
                source_id=str(session_id),
            )

    def revoke_all_user_sessions(
        self,
        user_id: UUID,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> int:
        """Revokes all active sessions for a user."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            res = conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
            count = res.rowcount
            _safe_record_event(
                conn,
                event_type="identity.all_sessions_revoked",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=f"revoked_count:{count}",
                source_table="fc27_sessions",
                source_id=str(user_id),
            )
            return count

    def get_session_by_id(self, session_id: UUID) -> dict[str, Any] | None:
        """Retrieves session record by session_id with user and club data."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, u.club_id, u.status AS user_status, u.login_id, u.display_name, u.player_id
                FROM public.fc27_sessions s
                JOIN public.fc27_users u ON s.user_id = u.user_id
                WHERE s.session_id = %s
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d.pop("token_hash", None)
            return d

    def list_user_sessions(
        self,
        user_id: UUID,
        *,
        current_session_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Returns all active, non-expired, non-revoked sessions for a user."""
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, u.club_id, u.status AS user_status, u.login_id, u.display_name, u.player_id
                FROM public.fc27_sessions s
                JOIN public.fc27_users u ON s.user_id = u.user_id
                WHERE s.user_id = %s
                  AND s.revoked_at IS NULL
                  AND s.idle_expires_at > %s
                  AND s.absolute_expires_at > %s
                  AND u.status = 'active'
                ORDER BY s.last_used_at DESC
                """,
                (user_id, now, now),
            ).fetchall()

            result = []
            for r in rows:
                d = dict(r)
                d["is_current"] = (current_session_id is not None and d["session_id"] == current_session_id)
                d.pop("token_hash", None)
                result.append(d)
            return result

    def touch_session_by_token(
        self,
        token: str,
        *,
        idle_ttl_minutes: int = 30,
    ) -> dict[str, Any]:
        """Explicitly touches session, extending idle_expires_at up to absolute_expires_at."""
        session = self.get_session_by_token(token, touch=True, idle_ttl_minutes=idle_ttl_minutes)
        if not session:
            raise IdentityStoreError(401, "invalid, expired, or revoked session")
        session.pop("token_hash", None)
        return session

    def revoke_session_by_token(
        self,
        token: str,
        *,
        actor: str = "user",
        correlation_id: UUID | None = None,
    ) -> UUID | None:
        """Revokes session identified by raw token. Returns session_id if revoked, else None."""
        sess = self.get_session_by_token(token, touch=False)
        if not sess:
            return None
        session_id = sess["session_id"]
        self.revoke_session(session_id, actor=actor, correlation_id=correlation_id)
        return session_id

    # =================================================================
    # 4. TOTP Credentials (MultiFernet Encryption)
    # =================================================================

    def save_totp_credential(
        self,
        *,
        user_id: UUID,
        plaintext_seed: str,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Encrypts TOTP seed at rest using MultiFernet active key."""
        encrypted_seed = self.crypto.encrypt_secret(plaintext_seed)
        key_ver = self.crypto.active_key_version
        cid = correlation_id or uuid4()

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_totp_credentials (
                    user_id, encrypted_seed, key_version, status, enrolled_at
                ) VALUES (%s, %s, %s, 'enrolled', now())
                ON CONFLICT (user_id) DO UPDATE SET
                    encrypted_seed = EXCLUDED.encrypted_seed,
                    key_version = EXCLUDED.key_version,
                    status = 'enrolled',
                    enrolled_at = now(),
                    last_verified_at = NULL,
                    last_consumed_step = NULL,
                    revoked_at = NULL
                RETURNING user_id, key_version, status, enrolled_at
                """,
                (user_id, encrypted_seed, key_ver),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to save totp credential")

            _safe_record_event(
                conn,
                event_type="identity.totp_enrolled",
                entity_type="totp_credential",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_totp_credentials",
                source_id=str(user_id),
            )
            return dict(row)

    def get_totp_seed(self, user_id: UUID) -> str | None:
        """Retrieves and decrypts the TOTP seed for a user."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT encrypted_seed, status FROM public.fc27_totp_credentials WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            if not row or row["status"] == "revoked":
                return None
            return self.crypto.decrypt_secret(row["encrypted_seed"])

    def verify_and_rotate_totp_key(self, user_id: UUID) -> bool:
        """Rotates stored TOTP seed encryption to the active key if needed."""
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT encrypted_seed, key_version FROM public.fc27_totp_credentials WHERE user_id = %s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if not row:
                return False

            new_ciphertext, rotated = self.crypto.rotate_secret(row["encrypted_seed"])
            if rotated:
                conn.execute(
                    """
                    UPDATE public.fc27_totp_credentials
                    SET encrypted_seed = %s, key_version = %s
                    WHERE user_id = %s
                    """,
                    (new_ciphertext, self.crypto.active_key_version, user_id),
                )
                return True
            return False

    def update_totp_status(
        self,
        user_id: UUID,
        status: str,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> None:
        """Updates TOTP enrollment status ('enrolled', 'active', 'revoked')."""
        if status not in {"enrolled", "active", "revoked"}:
            raise IdentityStoreError(422, f"invalid TOTP status '{status}'")

        cid = correlation_id or uuid4()
        revoked_clause = (
            sql.SQL(", revoked_at = now()")
            if status == "revoked"
            else sql.SQL("")
        )
        verified_clause = (
            sql.SQL(", last_verified_at = now()")
            if status == "active"
            else sql.SQL("")
        )

        query = sql.SQL(
            "UPDATE public.fc27_totp_credentials SET status = %s{revoked_clause}{verified_clause} WHERE user_id = %s"
        ).format(
            revoked_clause=revoked_clause,
            verified_clause=verified_clause,
        )

        with self.connect() as conn, conn.transaction():
            conn.execute(
                query,
                (status, user_id),
            )
            _safe_record_event(
                conn,
                event_type=f"identity.totp_{status}",
                entity_type="totp_credential",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_totp_credentials",
                source_id=str(user_id),
            )

    def get_totp_credential(self, user_id: UUID) -> dict[str, Any] | None:
        """Retrieves TOTP credential record metadata for a user."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, encrypted_seed, key_version, status,
                       enrolled_at, last_verified_at, last_consumed_step, revoked_at
                FROM public.fc27_totp_credentials
                WHERE user_id = %s
                """,
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def record_totp_verification(
        self,
        user_id: UUID,
        *,
        verified_at: datetime | None = None,
        consumed_step: int | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> None:
        """Atomically updates last_verified_at and last_consumed_step to prevent code replay."""
        v_time = verified_at or datetime.now(timezone.utc)
        step = consumed_step if consumed_step is not None else int(v_time.timestamp()) // 30
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                UPDATE public.fc27_totp_credentials
                SET last_verified_at = %s,
                    last_consumed_step = COALESCE(%s, last_consumed_step),
                    status = 'active'
                WHERE user_id = %s
                """,
                (v_time, step, user_id),
            )
            _safe_record_event(
                conn,
                event_type="identity.totp_verified",
                entity_type="totp_credential",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_totp_credentials",
                source_id=str(user_id),
            )

    def verify_and_consume_totp(
        self,
        user_id: UUID,
        code: str,
        *,
        allowed_statuses: Sequence[str] = ("enrolled", "active"),
        current_time: datetime | None = None,
        valid_window: int = 1,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Atomically verifies and consumes a TOTP code under an exclusive row lock.

        Guarantees:
        - Holds explicit row lock (FOR UPDATE) on fc27_totp_credentials in an atomic transaction.
        - Matches candidate code against candidate steps (drift tolerance: current step ± valid_window).
        - Rejects any candidate step where candidate_step <= last_consumed_step (monotonicity).
        - Persists updated last_consumed_step, last_verified_at, and status = 'active'.
        - Emits identity.totp_verified domain event.
        - Commits or rolls back atomically.
        """
        cid = correlation_id or uuid4()
        now = current_time or datetime.now(timezone.utc)
        current_epoch = int(now.timestamp())
        totp_interval = 30
        totp_digits = 6
        current_step = current_epoch // totp_interval

        if not isinstance(code, str) or len(code.strip()) != totp_digits or not code.strip().isdigit():
            raise IdentityStoreError(400, "malformed_code")

        clean_code = code.strip()

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                SELECT user_id, encrypted_seed, key_version, status,
                       last_consumed_step, last_verified_at, enrolled_at
                FROM public.fc27_totp_credentials
                WHERE user_id = %s
                FOR UPDATE
                """,
                (user_id,),
            ).fetchone()

            if not row:
                raise IdentityStoreError(400, "no active TOTP enrollment found")

            if row["status"] == "revoked":
                raise IdentityStoreError(400, "TOTP credential has been revoked")

            if allowed_statuses and row["status"] not in allowed_statuses:
                raise IdentityStoreError(400, f"TOTP credential status '{row['status']}' not in allowed statuses")

            seed = self.crypto.decrypt_secret(row["encrypted_seed"])
            if not seed:
                raise IdentityStoreError(500, "unable to retrieve TOTP secret")

            totp = pyotp.TOTP(seed, digits=totp_digits, interval=totp_interval)

            matched_step: int | None = None
            # Evaluate current step (offset 0) first, then backward (-1) and forward (+1)
            candidate_offsets = [0]
            for w in range(1, valid_window + 1):
                candidate_offsets.extend([-w, w])

            for offset in candidate_offsets:
                cand_step = current_step + offset
                expected_code = totp.at(cand_step * totp_interval)
                if hmac.compare_digest(clean_code, expected_code):
                    matched_step = cand_step
                    break

            if matched_step is None:
                raise IdentityStoreError(400, "invalid_or_expired_code")

            last_step = row["last_consumed_step"]
            # Fallback to last_verified_at timestamp for legacy rows
            if last_step is None and row["last_verified_at"] is not None:
                last_step = int(row["last_verified_at"].timestamp()) // totp_interval

            if last_step is not None and matched_step <= last_step:
                raise IdentityStoreError(409, "replay_detected: TOTP code already used in this or earlier window")

            matched_dt = datetime.fromtimestamp(matched_step * totp_interval, tz=timezone.utc)

            conn.execute(
                """
                UPDATE public.fc27_totp_credentials
                SET last_consumed_step = %s,
                    last_verified_at = %s,
                    status = 'active'
                WHERE user_id = %s
                """,
                (matched_step, matched_dt, user_id),
            )

            _safe_record_event(
                conn,
                event_type="identity.totp_verified",
                entity_type="totp_credential",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_totp_credentials",
                source_id=str(user_id),
            )

            return {
                "verified": True,
                "user_id": user_id,
                "consumed_step": matched_step,
                "verified_at": matched_dt,
                "status": "active",
            }

    # -----------------------------------------------------------------
    # Compatibility Aliases for Secret Encryption Layer
    # -----------------------------------------------------------------

    def save_totp_secret(
        self,
        *,
        user_id: UUID,
        plaintext_seed: str,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Alias for save_totp_credential."""
        return self.save_totp_credential(
            user_id=user_id,
            plaintext_seed=plaintext_seed,
            actor=actor,
            correlation_id=correlation_id,
        )

    def get_totp_secret(self, user_id: UUID) -> str | None:
        """Alias for get_totp_seed."""
        return self.get_totp_seed(user_id)

    def rotate_totp_secret(self, user_id: UUID) -> bool:
        """Alias for verify_and_rotate_totp_key."""
        return self.verify_and_rotate_totp_key(user_id)

    def deactivate_totp_secret(
        self,
        user_id: UUID,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> None:
        """Alias for revoking TOTP credential."""
        self.update_totp_status(
            user_id=user_id,
            status="revoked",
            actor=actor,
            correlation_id=correlation_id,
        )

    # =================================================================
    # 5. Recovery Codes (Argon2id Hashing)
    # =================================================================

    def save_recovery_codes(
        self,
        *,
        user_id: UUID,
        plaintext_codes: Sequence[str],
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> list[UUID]:
        """Saves a batch of single-use recovery codes as Argon2id hashes."""
        if not plaintext_codes:
            raise IdentityStoreError(422, "plaintext_codes must not be empty")

        cid = correlation_id or uuid4()
        code_ids: list[UUID] = []

        with self.connect() as conn, conn.transaction():
            # Revoke prior unconsumed recovery codes for this user before inserting new batch
            conn.execute(
                """
                UPDATE public.fc27_recovery_codes
                SET consumed_at = now()
                WHERE user_id = %s AND consumed_at IS NULL
                """,
                (user_id,),
            )
            for code in plaintext_codes:
                cid_code = uuid4()
                hashed = hash_recovery_code(code)
                conn.execute(
                    """
                    INSERT INTO public.fc27_recovery_codes (code_id, user_id, code_hash, created_at)
                    VALUES (%s, %s, %s, now())
                    """,
                    (cid_code, user_id, hashed),
                )
                code_ids.append(cid_code)

            _safe_record_event(
                conn,
                event_type="identity.recovery_codes_generated",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=f"count:{len(code_ids)}",
                source_table="fc27_recovery_codes",
                source_id=str(user_id),
            )
            return code_ids

    def verify_and_consume_recovery_code(
        self,
        user_id: UUID,
        plaintext_code: str,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> bool:
        """Verifies candidate recovery code against unconsumed hashes and marks consumed."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            rows = conn.execute(
                """
                SELECT code_id, code_hash
                FROM public.fc27_recovery_codes
                WHERE user_id = %s AND consumed_at IS NULL
                FOR UPDATE
                """,
                (user_id,),
            ).fetchall()

            for r in rows:
                if verify_recovery_code(plaintext_code, r["code_hash"]):
                    conn.execute(
                        "UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s AND consumed_at IS NULL",
                        (r["code_id"],),
                    )
                    _safe_record_event(
                        conn,
                        event_type="identity.recovery_code_consumed",
                        entity_type="recovery_code",
                        entity_id=str(r["code_id"]),
                        actor=actor,
                        correlation_id=cid,
                        visibility="audit_only",
                        scope_club_id=None,
                        source_table="fc27_recovery_codes",
                        source_id=str(r["code_id"]),
                    )
                    return True

            _safe_record_event(
                conn,
                event_type="identity.recovery_code_failed",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_recovery_codes",
                source_id=str(user_id),
            )
            return False

    def get_unconsumed_recovery_code_count(self, user_id: UUID) -> int:
        """Returns count of active unconsumed recovery codes for a user."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS cnt FROM public.fc27_recovery_codes WHERE user_id = %s AND consumed_at IS NULL",
                (user_id,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    # =================================================================
    # 5B. Commissioner Bootstrap & Recovery Engine
    # =================================================================

    def is_bootstrapped(self, club_id: str | None = None) -> bool:
        """Checks whether the commissioner bootstrap ceremony has been permanently locked."""
        with self.connect() as conn:
            try:
                row = conn.execute(
                    "SELECT 1 FROM public.fc27_bootstrap_locks WHERE lock_id = 'commissioner_bootstrap'"
                ).fetchone()
                if row:
                    return True
            except psycopg.errors.UndefinedTable:
                pass

            # Defense-in-depth: check if active commissioner role already exists in database
            query = """
                SELECT 1 FROM public.fc27_user_roles r
                JOIN public.fc27_users u ON u.user_id = r.user_id
                WHERE r.role = 'commissioner' AND u.status != 'disabled'
            """
            params: tuple[Any, ...] = ()
            if club_id:
                query += " AND u.club_id = %s"
                params = (club_id,)
            role_row = conn.execute(query, params).fetchone()
            return role_row is not None

    def record_bootstrap_lock(
        self,
        commissioner_user_id: UUID,
        club_id: str,
        actor: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Records the permanent commissioner bootstrap lock."""
        with self.connect() as conn, conn.transaction():
            import psycopg.types.json
            conn.execute(
                """
                INSERT INTO public.fc27_bootstrap_locks (
                    lock_id, commissioner_user_id, club_id, bootstrapped_at, actor, metadata
                ) VALUES ('commissioner_bootstrap', %s, %s, now(), %s, %s)
                """,
                (commissioner_user_id, club_id, actor, psycopg.types.json.Jsonb(metadata or {})),
            )

    def execute_commissioner_bootstrap(
        self,
        *,
        login_id: str,
        display_name: str,
        club_id: str,
        plaintext_recovery_codes: Sequence[str],
        actor: str = "commissioner_bootstrap",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Atomically executes Commissioner bootstrap: user, role, recovery codes, lock, session."""
        cid = correlation_id or uuid4()
        user_id = uuid4()
        norm_login = login_id.strip().lower()

        with self.connect() as conn, conn.transaction():
            # 0. Serialize concurrent ceremonies. FOR UPDATE on a lock row that does
            #    not exist yet locks nothing (and fc27_app deliberately holds only
            #    SELECT/INSERT on the immutable lock table), so a transaction-scoped
            #    advisory lock makes the existence checks below race-free.
            _acquire_bootstrap_advisory_lock(conn)

            # 1. Acquire and verify bootstrap lock
            existing_lock = conn.execute(
                "SELECT 1 FROM public.fc27_bootstrap_locks WHERE lock_id = 'commissioner_bootstrap'"
            ).fetchone()
            if existing_lock:
                raise IdentityStoreError(409, "commissioner bootstrap ceremony is permanently locked")

            # 2. Check if commissioner role or user already exists
            existing_comm = conn.execute(
                """
                SELECT 1 FROM public.fc27_user_roles r
                JOIN public.fc27_users u ON u.user_id = r.user_id
                WHERE r.role = 'commissioner' AND u.status != 'disabled'
                """
            ).fetchone()
            if existing_comm:
                raise IdentityStoreError(409, "commissioner identity already exists in database")

            existing_user = conn.execute(
                "SELECT 1 FROM public.fc27_users WHERE login_id = %s",
                (norm_login,),
            ).fetchone()
            if existing_user:
                raise IdentityStoreError(409, f"user with login identifier '{norm_login}' already exists")

            # 3. Create active Commissioner user
            user_row = conn.execute(
                """
                INSERT INTO public.fc27_users (
                    user_id, club_id, login_id, display_name, status, created_at, activated_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'active', now(), now(), now())
                RETURNING *
                """,
                (user_id, club_id, norm_login, display_name.strip()),
            ).fetchone()
            if not user_row:
                raise IdentityStoreError(500, "failed to create commissioner user")

            # 4. Assign commissioner role
            conn.execute(
                """
                INSERT INTO public.fc27_user_roles (user_id, role, granted_by, granted_at)
                VALUES (%s, 'commissioner', %s, now())
                """,
                (user_id, actor),
            )

            # 5. Save recovery code Argon2id hashes
            for code in plaintext_recovery_codes:
                cid_code = uuid4()
                hashed = hash_recovery_code(code)
                conn.execute(
                    """
                    INSERT INTO public.fc27_recovery_codes (code_id, user_id, code_hash, created_at)
                    VALUES (%s, %s, %s, now())
                    """,
                    (cid_code, user_id, hashed),
                )

            # 6. Create restricted bootstrap enrollment session
            raw_token = secrets.token_hex(32)
            tok_hash = hash_token(raw_token)
            session_id = uuid4()
            now_dt = datetime.now(timezone.utc)
            idle_exp = now_dt + timedelta(minutes=30)
            abs_exp = now_dt + timedelta(hours=24)

            conn.execute(
                """
                INSERT INTO public.fc27_sessions (
                    session_id, token_hash, user_id, auth_level, auth_method,
                    created_at, last_used_at, idle_expires_at, absolute_expires_at
                ) VALUES (%s, %s, %s, 'bootstrap_enrollment', 'bootstrap', now(), now(), %s, %s)
                """,
                (session_id, tok_hash, user_id, idle_exp, abs_exp),
            )

            # 7. Record authoritative audit event
            _safe_record_event(
                conn,
                event_type="identity.bootstrap_initiated",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                reason=f"recovery_codes_count:{len(plaintext_recovery_codes)}",
                source_table="fc27_users",
                source_id=str(user_id),
            )

            user_dict = dict(user_row)
            user_dict.setdefault("login_identifier", norm_login)

            return {
                "user": user_dict,
                "session_token": raw_token,
                "session_id": session_id,
            }

    def complete_commissioner_bootstrap(
        self,
        *,
        user_id: UUID,
        club_id: str,
        enrollment_token: str,
        actor: str = "commissioner_bootstrap",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Completes commissioner bootstrap after passkey registration.

        Guarantees:
        - Verifies at least one unrevoked WebAuthn credential exists for user.
        - Inserts permanent lock into fc27_bootstrap_locks.
        - Ensures commissioner role is granted.
        - Revokes enrollment session.
        - Mints active commissioner session with auth_level='step_up_passkey', auth_method='passkey'.
        - Emits identity.bootstrap_completed event.
        """
        cid = correlation_id or uuid4()
        now_dt = datetime.now(timezone.utc)
        enrollment_tok_hash = hash_token(enrollment_token)

        with self.connect() as conn, conn.transaction():
            _acquire_bootstrap_advisory_lock(conn)

            # 1. Acquire and verify bootstrap lock does not exist
            existing_lock = conn.execute(
                "SELECT 1 FROM public.fc27_bootstrap_locks WHERE lock_id = 'commissioner_bootstrap'"
            ).fetchone()
            if existing_lock:
                raise IdentityStoreError(409, "commissioner bootstrap ceremony is permanently locked")

            # 2. Verify registered passkey exists
            cred_row = conn.execute(
                "SELECT 1 FROM public.fc27_webauthn_credentials WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            ).fetchone()
            if not cred_row:
                raise IdentityStoreError(400, "passkey registration required before completing bootstrap")

            # 3. Insert permanent bootstrap lock
            conn.execute(
                """
                INSERT INTO public.fc27_bootstrap_locks (
                    lock_id, commissioner_user_id, club_id, bootstrapped_at, actor
                ) VALUES ('commissioner_bootstrap', %s, %s, now(), %s)
                """,
                (user_id, club_id, actor),
            )

            # 4. Ensure commissioner role is assigned
            conn.execute(
                """
                INSERT INTO public.fc27_user_roles (user_id, role, granted_by, granted_at)
                VALUES (%s, 'commissioner', %s, now())
                ON CONFLICT (user_id, role) DO NOTHING
                """,
                (user_id, actor),
            )

            # 5. Revoke enrollment session
            conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE token_hash = %s",
                (enrollment_tok_hash,),
            )

            # 6. Mint active step_up_passkey session
            raw_token = secrets.token_hex(32)
            tok_hash = hash_token(raw_token)
            session_id = uuid4()
            idle_exp = now_dt + timedelta(minutes=30)
            abs_exp = now_dt + timedelta(hours=24)

            conn.execute(
                """
                INSERT INTO public.fc27_sessions (
                    session_id, token_hash, user_id, auth_level, auth_method,
                    created_at, last_used_at, idle_expires_at, absolute_expires_at
                ) VALUES (%s, %s, %s, 'step_up_passkey', 'passkey', now(), now(), %s, %s)
                """,
                (session_id, tok_hash, user_id, idle_exp, abs_exp),
            )

            # 7. Record authoritative audit event
            _safe_record_event(
                conn,
                event_type="identity.bootstrap_completed",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                source_table="fc27_users",
                source_id=str(user_id),
            )

            return {
                "session_token": raw_token,
                "session_id": session_id,
                "auth_level": "step_up_passkey",
                "auth_method": "passkey",
            }

    # =================================================================
    # 6. WebAuthn Credentials
    # =================================================================

    @staticmethod
    def _normalize_credential_id(credential_id: bytes | str) -> bytes:
        """Normalizes credential_id input from bytes, hex string, or base64url string."""
        if isinstance(credential_id, bytes):
            return credential_id
        if isinstance(credential_id, str):
            # Try hex decoding first
            try:
                if len(credential_id) % 2 == 0:
                    return bytes.fromhex(credential_id)
            except ValueError:
                pass
            # Try base64url decoding
            try:
                import base64
                padded = credential_id + "=" * ((4 - len(credential_id) % 4) % 4)
                return base64.urlsafe_b64decode(padded)
            except Exception:
                pass
            return credential_id.encode("utf-8")
        raise IdentityStoreError(422, "invalid credential_id format")

    def save_webauthn_credential(
        self,
        *,
        credential_id: bytes | str,
        user_id: UUID,
        public_key: bytes,
        sign_counter: int = 0,
        label: str | None = None,
        aaguid: UUID | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Saves registered WebAuthn credential public key."""
        cred_bytes = self._normalize_credential_id(credential_id)
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_webauthn_credentials (
                    credential_id, user_id, public_key, sign_counter,
                    label, aaguid, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (cred_bytes, user_id, public_key, sign_counter, label or "Passkey", aaguid),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to save webauthn credential")

            _safe_record_event(
                conn,
                event_type="identity.passkey_registered",
                entity_type="webauthn_credential",
                entity_id=cred_bytes.hex(),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_webauthn_credentials",
                source_id=cred_bytes.hex(),
            )
            res = dict(row)
            res.setdefault("sign_count", res.get("sign_counter", 0))
            return res

    def save_credential(
        self,
        *,
        credential_id: bytes | str,
        user_id: UUID,
        public_key: bytes,
        sign_counter: int | None = None,
        sign_count: int | None = None,
        label: str | None = None,
        aaguid: UUID | None = None,
        transports: list[str] | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Saves WebAuthn credential, accepting sign_counter or sign_count alias and hex/bytes ID."""
        counter = sign_counter if sign_counter is not None else (sign_count if sign_count is not None else 0)
        return self.save_webauthn_credential(
            credential_id=credential_id,
            user_id=user_id,
            public_key=public_key,
            sign_counter=counter,
            label=label,
            aaguid=aaguid,
            actor=actor,
            correlation_id=correlation_id,
        )

    def get_webauthn_credential(self, credential_id: bytes | str) -> dict[str, Any] | None:
        """Retrieves active WebAuthn credential."""
        cred_bytes = self._normalize_credential_id(credential_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_webauthn_credentials WHERE credential_id = %s AND revoked_at IS NULL",
                (cred_bytes,),
            ).fetchone()
            if not row:
                return None
            res = dict(row)
            if "sign_count" not in res:
                res["sign_count"] = res.get("sign_counter", 0)
            return res

    def get_credential(self, credential_id: bytes | str) -> dict[str, Any] | None:
        """Retrieves active credential by bytes, hex, or base64url string ID."""
        return self.get_webauthn_credential(credential_id)

    def list_user_webauthn_credentials(self, user_id: UUID) -> list[dict[str, Any]]:
        """Lists active WebAuthn credentials for a user."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM public.fc27_webauthn_credentials WHERE user_id = %s AND revoked_at IS NULL ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                if "sign_count" not in d:
                    d["sign_count"] = d.get("sign_counter", 0)
                results.append(d)
            return results

    def list_credentials_for_user(self, user_id: UUID) -> list[dict[str, Any]]:
        """Lists active credentials for a user with sign_count alias."""
        return self.list_user_webauthn_credentials(user_id)

    def update_webauthn_counter(self, credential_id: bytes | str, new_counter: int) -> None:
        """Updates WebAuthn sign counter with clone detection check."""
        cred_bytes = self._normalize_credential_id(credential_id)
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT sign_counter FROM public.fc27_webauthn_credentials WHERE credential_id = %s FOR UPDATE",
                (cred_bytes,),
            ).fetchone()
            if not row:
                raise IdentityStoreError(404, "credential not found")
            if row["sign_counter"] > 0 and new_counter <= row["sign_counter"]:
                raise IdentityStoreError(401, "authenticator sign counter replay or clone detected")

            conn.execute(
                "UPDATE public.fc27_webauthn_credentials SET sign_counter = %s, last_used_at = now() WHERE credential_id = %s",
                (new_counter, cred_bytes),
            )

    def update_credential_counter(
        self,
        credential_id: bytes | str,
        new_counter: int,
    ) -> None:
        """Updates sign counter and last_used_at with clone detection."""
        self.update_webauthn_counter(credential_id, new_counter)

    def consume_challenge_and_update_counter(
        self,
        *,
        challenge_str: str,
        challenge_type: str,
        credential_id: bytes | str,
        new_counter: int,
        expected_payload_hash: str | None = None,
        user_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Atomically locks and consumes challenge and updates authenticator counter in ONE transaction."""
        cred_bytes = self._normalize_credential_id(credential_id)
        now = datetime.now(timezone.utc)
        with self.connect() as conn, conn.transaction():
            # 1. Lock challenge row FOR UPDATE
            chal_row = conn.execute(
                """
                SELECT * FROM public.fc27_challenges
                WHERE challenge_str = %s AND challenge_type = %s AND consumed_at IS NULL
                FOR UPDATE
                """,
                (challenge_str, challenge_type),
            ).fetchone()
            if not chal_row:
                raise IdentityStoreError(400, "challenge expired, invalid, or already consumed")
            if chal_row["expires_at"] <= now:
                raise IdentityStoreError(400, "challenge has expired")
            if expected_payload_hash and chal_row.get("bound_payload_hash") != expected_payload_hash:
                raise IdentityStoreError(400, "bound payload hash mismatch")
            if user_id and chal_row.get("user_id") and str(chal_row["user_id"]) != str(user_id):
                raise IdentityStoreError(403, "challenge not issued to this user")

            # 2. Lock credential row FOR UPDATE
            cred_row = conn.execute(
                "SELECT sign_counter, user_id FROM public.fc27_webauthn_credentials WHERE credential_id = %s AND revoked_at IS NULL FOR UPDATE",
                (cred_bytes,),
            ).fetchone()
            if not cred_row:
                raise IdentityStoreError(404, "credential not found or revoked")
            if user_id and str(cred_row["user_id"]) != str(user_id):
                raise IdentityStoreError(403, "credential does not belong to specified user")

            current_counter = int(cred_row["sign_counter"] or 0)

            # 3. Clone detection / monotonicity check
            # Zero-counter authenticators (0 -> 0) are permitted.
            # Initial counter setting (0 -> N) is permitted.
            # Replay / clone rollback (current_counter > 0 and new_counter <= current_counter) fails closed.
            if current_counter > 0 and new_counter <= current_counter:
                raise IdentityStoreError(401, "authenticator sign counter replay or clone detected: counter did not increase")

            # 4. Atomically consume challenge
            conn.execute(
                "UPDATE public.fc27_challenges SET consumed_at = now() WHERE challenge_id = %s",
                (chal_row["challenge_id"],),
            )

            # 5. Atomically update sign counter
            conn.execute(
                "UPDATE public.fc27_webauthn_credentials SET sign_counter = %s, last_used_at = now() WHERE credential_id = %s",
                (new_counter, cred_bytes),
            )

            return dict(chal_row)

    def revoke_webauthn_credential(
        self,
        credential_id: bytes | str,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> None:
        """Revokes a registered passkey."""
        cred_bytes = self._normalize_credential_id(credential_id)
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                "UPDATE public.fc27_webauthn_credentials SET revoked_at = now() WHERE credential_id = %s",
                (cred_bytes,),
            )
            _safe_record_event(
                conn,
                event_type="identity.passkey_revoked",
                entity_type="webauthn_credential",
                entity_id=cred_bytes.hex(),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_webauthn_credentials",
                source_id=cred_bytes.hex(),
            )

    # =================================================================
    # 7. Action Tokens
    # =================================================================

    def create_action_token(
        self,
        *,
        user_id: UUID,
        scope: str,
        assignment_id: UUID | None = None,
        ttl_seconds: int = 3600,
        actor: str = "system",
        correlation_id: UUID | None = None,
        raw_token: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Creates a single-use action token, storing only its SHA-256 hash."""
        if scope not in self.VALID_ACTION_SCOPES:
            raise IdentityStoreError(422, f"invalid action token scope '{scope}'")

        token_to_use = raw_token or f"sat_{secrets.token_urlsafe(32)}"
        tok_hash = hash_token(token_to_use)
        token_id = uuid4()
        cid = correlation_id or uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_action_tokens (
                    token_id, token_hash, user_id, assignment_id, scope,
                    expires_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (token_id, tok_hash, user_id, assignment_id, scope, expires_at),
            ).fetchone()

            _safe_record_event(
                conn,
                event_type="identity.action_token_issued",
                entity_type="action_token",
                entity_id=str(token_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=scope,
                source_table="fc27_action_tokens",
                source_id=str(token_id),
            )
            return token_to_use, dict(row)

    def get_action_token_by_token(self, token: str) -> dict[str, Any] | None:
        """Retrieves action token record by raw token."""
        tok_hash = hash_token(token)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_action_tokens WHERE token_hash = %s", (tok_hash,)
            ).fetchone()
            return dict(row) if row else None

    def get_action_token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        """Retrieves action token record by SHA-256 token hash."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_action_tokens WHERE token_hash = %s", (token_hash,)
            ).fetchone()
            return dict(row) if row else None

    def consume_action_token(
        self,
        token: str,
        *,
        expected_scope: str | None = None,
        expected_user_id: UUID | str | None = None,
        expected_assignment_id: UUID | str | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Consumes an action token, verifying single-use, expiry, revocation, scope, user, and assignment."""
        tok_hash = hash_token(token)
        cid = correlation_id or uuid4()
        now = datetime.now(timezone.utc)

        with self.connect() as conn, conn.transaction():
            tok = conn.execute(
                "SELECT * FROM public.fc27_action_tokens WHERE token_hash = %s FOR UPDATE", (tok_hash,)
            ).fetchone()
            if not tok:
                raise IdentityStoreError(404, "action token not found")
            if tok["revoked_at"] is not None:
                raise IdentityStoreError(410, "action token was revoked")
            if tok["consumed_at"] is not None:
                raise IdentityStoreError(409, "action token already consumed")
            if tok["expires_at"] <= now:
                raise IdentityStoreError(410, "action token has expired")
            if expected_scope and tok["scope"] != expected_scope:
                raise IdentityStoreError(403, f"token scope mismatch: expected {expected_scope}, found {tok['scope']}")
            if expected_user_id and str(tok["user_id"]) != str(expected_user_id):
                raise IdentityStoreError(403, "token user mismatch")
            if expected_assignment_id and str(tok.get("assignment_id") or "") != str(expected_assignment_id):
                raise IdentityStoreError(403, "token assignment mismatch")

            row = conn.execute(
                """
                UPDATE public.fc27_action_tokens
                SET consumed_at = now()
                WHERE token_id = %s AND consumed_at IS NULL AND revoked_at IS NULL
                RETURNING *
                """,
                (tok["token_id"],),
            ).fetchone()
            if not row:
                raise IdentityStoreError(409, "action token is already consumed or revoked")

            _safe_record_event(
                conn,
                event_type="identity.action_token_consumed",
                entity_type="action_token",
                entity_id=str(tok["token_id"]),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=tok["scope"],
                source_table="fc27_action_tokens",
                source_id=str(tok["token_id"]),
            )
            return dict(row)

    def revoke_action_token(
        self,
        token_id: UUID,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Revokes an outstanding action token."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            tok = conn.execute(
                "SELECT * FROM public.fc27_action_tokens WHERE token_id = %s FOR UPDATE", (token_id,)
            ).fetchone()
            if not tok:
                raise IdentityStoreError(404, "action token not found")
            if tok["consumed_at"] is not None:
                raise IdentityStoreError(409, "cannot revoke already consumed action token")
            if tok["revoked_at"] is not None:
                return dict(tok)

            row = conn.execute(
                "UPDATE public.fc27_action_tokens SET revoked_at = now() WHERE token_id = %s RETURNING *",
                (token_id,),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to revoke action token")

            _safe_record_event(
                conn,
                event_type="identity.action_token_revoked",
                entity_type="action_token",
                entity_id=str(token_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=tok["scope"],
                source_table="fc27_action_tokens",
                source_id=str(token_id),
            )
            return dict(row)

    # =================================================================
    # 8. Challenges (WebAuthn / Step-Up)
    # =================================================================

    def create_challenge(
        self,
        *,
        challenge_type: str,
        challenge_str: str,
        user_id: UUID | None = None,
        bound_payload_hash: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Creates a time-bounded, single-use authentication or step-up challenge."""
        if challenge_type not in self.VALID_CHALLENGE_TYPES:
            raise IdentityStoreError(422, f"invalid challenge_type '{challenge_type}'")

        cid = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_challenges (
                    challenge_id, challenge_type, challenge_str, user_id,
                    bound_payload_hash, expires_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (cid, challenge_type, challenge_str, user_id, bound_payload_hash, expires_at),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to create challenge")
            return dict(row)

    def consume_challenge(
        self,
        *,
        challenge_str: str,
        challenge_type: str,
        expected_payload_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """Consumes a challenge verifying single-use, expiry, and payload binding."""
        now = datetime.now(timezone.utc)
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                SELECT * FROM public.fc27_challenges
                WHERE challenge_str = %s AND challenge_type = %s AND consumed_at IS NULL
                FOR UPDATE
                """,
                (challenge_str, challenge_type),
            ).fetchone()

            if not row:
                return None
            if row["expires_at"] <= now:
                return None
            if expected_payload_hash and row["bound_payload_hash"] != expected_payload_hash:
                return None

            conn.execute(
                "UPDATE public.fc27_challenges SET consumed_at = now() WHERE challenge_id = %s",
                (row["challenge_id"],),
            )
            return dict(row)

    def get_challenge(self, challenge_str: str) -> dict[str, Any] | None:
        """Retrieves challenge by challenge_str."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_challenges WHERE challenge_str = %s",
                (challenge_str,),
            ).fetchone()
            return dict(row) if row else None

    # =================================================================
    # 9. Human Delegations
    # =================================================================

    PROHIBITED_DELEGATION_ACTIONS = frozenset({
        "club_strategy", "team_tactics", "formations", "programme_release",
        "club_dispatch_activation", "policy_exception", "roster",
        "transfer", "recruitment", "discipline", "fair_play",
        "identity_change", "access_control", "delegation_policy"
    })

    def create_delegation(
        self,
        *,
        delegator_id: str,
        delegate_user_id: UUID,
        club_id: str,
        action_type: str | list[str],
        scope: str = "club",
        expires_in_hours: int = 24,
        starts_at: datetime | None = None,
        target_player_id: str | None = None,
        target_package_type: str | None = None,
        actor: str = "commissioner",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Creates an explicit, time-bounded, action-scoped delegation."""
        actions = [action_type] if isinstance(action_type, str) else list(action_type)
        if not actions:
            raise IdentityStoreError(422, "at least one action_type must be specified")

        # Prohibit delegating club governance actions
        for act in actions:
            if act in self.PROHIBITED_DELEGATION_ACTIONS:
                raise IdentityStoreError(403, f"action '{act}' cannot be delegated")

        if expires_in_hours <= 0 or expires_in_hours > 168:
            raise IdentityStoreError(422, "expires_in_hours must be between 1 and 168")

        now = datetime.now(timezone.utc)
        start_time = starts_at or now
        expires_at = start_time + timedelta(hours=expires_in_hours)
        delegation_id = uuid4()
        cid = correlation_id or uuid4()

        with self.connect() as conn, conn.transaction():
            # Validate delegate user exists, is in same club, and is active
            delegate_user = conn.execute(
                "SELECT club_id, status FROM public.fc27_users WHERE user_id = %s",
                (delegate_user_id,),
            ).fetchone()
            if not delegate_user:
                raise IdentityStoreError(404, "delegate user not found")
            if delegate_user["club_id"] != club_id:
                raise IdentityStoreError(403, "cannot delegate across clubs")
            if delegate_user["status"] != "active":
                raise IdentityStoreError(403, f"delegate user is {delegate_user['status']}, must be active")

            row = conn.execute(
                """
                INSERT INTO public.authority_delegations (
                    delegation_id, delegator_id, delegate_id, club_id, scope,
                    allowed_action_types, starts_at, expires_at,
                    target_player_id, target_package_type, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    delegation_id,
                    delegator_id,
                    str(delegate_user_id),
                    club_id,
                    scope,
                    actions,
                    start_time,
                    expires_at,
                    target_player_id,
                    target_package_type,
                ),
            ).fetchone()

            _safe_record_event(
                conn,
                event_type="identity.delegation_granted",
                entity_type="delegation",
                entity_id=str(delegation_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                reason=f"actions:{','.join(actions)}",
                source_table="authority_delegations",
                source_id=str(delegation_id),
            )
            res = dict(row)
            res["action_type"] = actions[0] if len(actions) == 1 else actions
            return res

    def get_delegation(self, delegation_id: UUID) -> dict[str, Any] | None:
        """Retrieves delegation by UUID."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.authority_delegations WHERE delegation_id = %s",
                (delegation_id,),
            ).fetchone()
            if not row:
                return None
            res = dict(row)
            actions = res.get("allowed_action_types") or []
            res["action_type"] = actions[0] if len(actions) == 1 else actions
            return res

    def list_delegations(
        self,
        club_id: str,
        delegate_user_id: UUID | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Lists delegations for a club with optional active filtering."""
        with self.connect() as conn:
            clauses = [sql.SQL("club_id = %s")]
            params: list[Any] = [club_id]
            if delegate_user_id:
                clauses.append(sql.SQL("delegate_id = %s"))
                params.append(str(delegate_user_id))
            if active_only:
                clauses.append(sql.SQL("revoked_at IS NULL AND expires_at > now()"))

            where_clause = sql.SQL(" AND ").join(clauses)
            query = sql.SQL(
                "SELECT * FROM public.authority_delegations WHERE {} ORDER BY created_at DESC"
            ).format(where_clause)
            rows = conn.execute(query, params).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                actions = d.get("allowed_action_types") or []
                d["action_type"] = actions[0] if len(actions) == 1 else actions
                result.append(d)
            return result

    def revoke_delegation(
        self,
        delegation_id: UUID,
        *,
        revoked_by: str,
        reason: str = "commissioner_revoked",
        actor: str = "commissioner",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Explicitly revokes an active delegation immediately."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM public.authority_delegations WHERE delegation_id = %s FOR UPDATE",
                (delegation_id,),
            ).fetchone()
            if not row:
                raise IdentityStoreError(404, "delegation not found")
            if row["revoked_at"] is not None:
                return dict(row)

            updated = conn.execute(
                """
                UPDATE public.authority_delegations
                SET revoked_at = now(), revoked_by = %s, revocation_reason = %s
                WHERE delegation_id = %s
                RETURNING *
                """,
                (revoked_by, reason, delegation_id),
            ).fetchone()
            if not updated:
                raise IdentityStoreError(500, "failed to revoke delegation")

            _safe_record_event(
                conn,
                event_type="identity.delegation_revoked",
                entity_type="delegation",
                entity_id=str(delegation_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=row["club_id"],
                reason=reason,
                source_table="authority_delegations",
                source_id=str(delegation_id),
            )
            return dict(updated)

    def evaluate_delegation(
        self,
        delegation_id: UUID,
        *,
        principal_user_id: UUID,
        club_id: str,
        action: str,
        resource: dict[str, Any],
    ) -> tuple[bool, str]:
        """Pure evaluation function verifying active unexpired unrevoked scope-matching delegation."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.authority_delegations WHERE delegation_id = %s",
                (delegation_id,),
            ).fetchone()
            if not row:
                return False, "delegation_not_found"

            if row["delegate_id"] != str(principal_user_id):
                return False, "delegate_user_mismatch"

            if row["revoked_at"] is not None:
                return False, "delegation_revoked"

            now = datetime.now(timezone.utc)
            if row["starts_at"] and now < row["starts_at"]:
                return False, "delegation_not_yet_active"

            if now >= row["expires_at"]:
                return False, "delegation_expired"

            if row["club_id"] != club_id:
                return False, "club_mismatch"

            allowed = set(row.get("allowed_action_types") or [])
            if action not in allowed and "*" not in allowed:
                return False, "action_type_not_delegated"

            if row.get("target_player_id"):
                res_player = resource.get("subject_player_id") or resource.get("player_id")
                if str(row["target_player_id"]) != str(res_player):
                    return False, "target_player_mismatch"

            if row.get("scope") == "player_specific" and resource.get("scope") == "club":
                return False, "scope_mismatch"

            return True, "authorized"

    # =================================================================
    # 10. Decision Evidence (WebAuthn Step-Up)
    # =================================================================

    def create_decision_evidence(
        self,
        *,
        user_id: UUID,
        package_id: str,
        package_version: str | int,
        package_hash: str,
        decision: str | None = None,
        normalized_decision: str | None = None,
        canonical_bound_payload_hash: str | None = None,
        bound_payload_hash: str | None = None,
        session_id: UUID | str | None = None,
        thread_id: str | None = None,
        approval_request_id: UUID | str | None = None,
        challenge_id: UUID | str | None = None,
        credential_id: bytes | None = None,
        webauthn_credential_id: str | None = None,
        ttl_seconds: int = 300,
        actor: str = "system",
        correlation_id: UUID | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Creates a single-use decision evidence record bound to exact package parameters."""
        evidence_id = uuid4()
        cid = correlation_id or uuid4()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        norm_decision = (normalized_decision or decision or "approved").lower().strip()
        if norm_decision in ("approve", "approved"):
            norm_decision = "approved"
        elif norm_decision in ("reject", "rejected"):
            norm_decision = "rejected"
        elif norm_decision in ("request_revision", "revision_requested"):
            norm_decision = "revision_requested"
        bound_hash = canonical_bound_payload_hash or bound_payload_hash or ""
        cred_str = webauthn_credential_id or (credential_id.hex() if credential_id else None)
        sess_id = str(session_id) if session_id else None
        th_id = thread_id if thread_id else None
        appr_id = str(approval_request_id) if approval_request_id else None
        chal_id = str(challenge_id) if challenge_id else None

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_decision_evidence (
                    evidence_id, user_id, session_id, thread_id, approval_request_id,
                    package_id, package_version, package_hash, normalized_decision,
                    canonical_bound_payload_hash, challenge_id, webauthn_credential_id,
                    issued_at, expires_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    evidence_id,
                    str(user_id),
                    sess_id,
                    th_id,
                    appr_id,
                    package_id,
                    str(package_version),
                    package_hash,
                    norm_decision,
                    bound_hash,
                    chal_id,
                    cred_str,
                    now,
                    expires_at,
                ),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to record decision evidence")

            _safe_record_event(
                conn,
                event_type="identity.step_up_verified",
                entity_type="decision_evidence",
                entity_id=str(evidence_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=norm_decision,
                source_table="fc27_decision_evidence",
                source_id=str(evidence_id),
            )
            return dict(row)

    def get_decision_evidence(self, evidence_id: UUID | str) -> dict[str, Any] | None:
        """Retrieves evidence record by UUID."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_decision_evidence WHERE evidence_id = %s",
                (str(evidence_id),),
            ).fetchone()
            return dict(row) if row else None

    def consume_decision_evidence(
        self,
        evidence_id: UUID | str,
        *,
        expected_user_id: UUID | str | None = None,
        expected_session_id: UUID | str | None = None,
        expected_approval_request_id: UUID | str | None = None,
        expected_thread_id: str | None = None,
        expected_package_id: str | None = None,
        expected_package_hash: str | None = None,
        expected_package_version: str | None = None,
        expected_decision: str | None = None,
        expected_normalized_decision: str | None = None,
        expected_canonical_bound_payload_hash: str | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
        conn: psycopg.Connection[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically validates scope and consumes decision evidence (Feature 20 & Finding R2).

        Raises IdentityStoreError:
        - 404: Evidence not found
        - 400/403: Scope/binding mismatch
        - 410/400: Evidence expired
        - 409: Evidence already consumed (replay attack)
        """
        now = datetime.now(timezone.utc)
        cid = correlation_id or uuid4()

        def _do_consume(tx_conn: psycopg.Connection[dict[str, Any]]) -> dict[str, Any]:
            row = tx_conn.execute(
                "SELECT * FROM public.fc27_decision_evidence WHERE evidence_id = %s FOR UPDATE",
                (str(evidence_id),),
            ).fetchone()
            if not row:
                raise IdentityStoreError(404, "evidence not found")

            # Check if already consumed (Replay)
            if row["consumed_at"] is not None:
                raise IdentityStoreError(409, "evidence already consumed")

            # Check expiry
            if row["expires_at"] <= now:
                raise IdentityStoreError(400, "evidence expired")

            # Binding validations
            if expected_user_id and str(row["user_id"]) != str(expected_user_id):
                raise IdentityStoreError(403, "evidence bound to different user ID")
            if expected_session_id and str(row["session_id"]) != str(expected_session_id):
                raise IdentityStoreError(403, "evidence bound to different session ID")
            if expected_approval_request_id and str(row["approval_request_id"]) != str(expected_approval_request_id):
                raise IdentityStoreError(400, "evidence bound to different approval request ID")
            if expected_thread_id and str(row["thread_id"]) != expected_thread_id:
                raise IdentityStoreError(400, "evidence bound to different thread ID")
            if expected_package_id and row["package_id"] != expected_package_id:
                raise IdentityStoreError(400, "evidence bound to different package ID")
            if expected_package_hash and str(row["package_hash"]).lower() != expected_package_hash.lower():
                raise IdentityStoreError(400, "evidence bound to different package hash")
            if expected_package_version and str(row["package_version"]) != expected_package_version:
                raise IdentityStoreError(400, "evidence bound to different package version")

            exp_decision = expected_normalized_decision or expected_decision
            if exp_decision:
                norm_exp = exp_decision.lower().strip()
                if norm_exp in ("approve", "approved"):
                    norm_exp = "approved"
                elif norm_exp in ("reject", "rejected"):
                    norm_exp = "rejected"
                elif norm_exp in ("request_revision", "revision_requested"):
                    norm_exp = "revision_requested"
                if row["normalized_decision"].lower() != norm_exp:
                    raise IdentityStoreError(403, "evidence bound to different decision")

            if expected_canonical_bound_payload_hash and row["canonical_bound_payload_hash"].lower() != expected_canonical_bound_payload_hash.lower():
                raise IdentityStoreError(400, "canonical bound payload hash mismatch")

            # Mark consumed
            updated = tx_conn.execute(
                "UPDATE public.fc27_decision_evidence SET consumed_at = now() WHERE evidence_id = %s AND consumed_at IS NULL RETURNING *",
                (str(evidence_id),),
            ).fetchone()
            if not updated:
                raise IdentityStoreError(409, "evidence already consumed")

            _safe_record_event(
                tx_conn,
                event_type="identity.decision_evidence_consumed",
                entity_type="decision_evidence",
                entity_id=str(evidence_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=row["normalized_decision"],
                source_table="fc27_decision_evidence",
                source_id=str(evidence_id),
            )
            return dict(updated)

        if conn is not None:
            return _do_consume(conn)
        else:
            with self.connect() as new_conn, new_conn.transaction():
                return _do_consume(new_conn)

    # =================================================================
    # 11. Player Assignments and Decisions
    # =================================================================

    def get_player_assignment(self, assignment_id: UUID | str) -> dict[str, Any] | None:
        """Retrieves player_assignment record by UUID PK."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.player_assignments WHERE assignment_id = %s",
                (str(assignment_id),),
            ).fetchone()
            return dict(row) if row else None

    def list_player_assignments(
        self,
        club_id: str,
        player_id: str,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Lists player_assignments scoped to exact club_id and subject_player_id."""
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM public.player_assignments
                    WHERE club_id = %s AND subject_player_id = %s AND status = %s
                    ORDER BY created_at DESC
                    """,
                    (club_id, player_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM public.player_assignments
                    WHERE club_id = %s AND subject_player_id = %s
                    ORDER BY created_at DESC
                    """,
                    (club_id, player_id),
                ).fetchall()
            return [dict(r) for r in rows]

    def create_player_assignment(
        self,
        *,
        package_id: str,
        package_version: str,
        package_hash: str,
        subject_player_id: str,
        club_id: str,
        assignment_version: int = 1,
        assignment_hash: str,
        action_type: str = "personal_training",
        status: str = "active",
        requires_player_consent: bool = True,
        fallback_eligible: bool = False,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Creates an authoritative player assignment in public.player_assignments."""
        assignment_id = uuid4()
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.player_assignments (
                    assignment_id, package_id, package_version, package_hash,
                    subject_player_id, club_id, assignment_version, assignment_hash,
                    assignment_scope, action_type, status, requires_player_consent,
                    fallback_eligible, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 'player_specific', %s, %s, %s, %s, now(), now()
                )
                RETURNING *
                """,
                (
                    assignment_id, package_id, package_version, package_hash,
                    subject_player_id, club_id, assignment_version, assignment_hash,
                    action_type, status, requires_player_consent, fallback_eligible
                ),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to create player assignment")

            _safe_record_event(
                conn,
                event_type="identity.player_assignment_created",
                entity_type="player_assignment",
                entity_id=str(assignment_id),
                actor=actor,
                correlation_id=cid,
                visibility="player_owner",
                scope_club_id=club_id,
                source_table="player_assignments",
                source_id=str(assignment_id),
            )
            return dict(row)

    def create_player_decision_request(
        self,
        *,
        assignment_id: UUID | str,
        subject_player_id: str,
        challenge_nonce: str | None = None,
        action_token_reference: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Creates an authoritative player_decision_requests challenge record."""
        request_id = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.player_decision_requests (
                    request_id, assignment_id, subject_player_id, status,
                    challenge_nonce, action_token_reference, created_at, expires_at
                ) VALUES (%s, %s, %s, 'awaiting_player', %s, %s, now(), %s)
                RETURNING *
                """,
                (request_id, str(assignment_id), subject_player_id, challenge_nonce, action_token_reference, expires_at),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to create player decision request")
            return dict(row)

    def get_player_decision_request(self, request_id: UUID | str) -> dict[str, Any] | None:
        """Retrieves player decision request by UUID."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.player_decision_requests WHERE request_id = %s",
                (str(request_id),),
            ).fetchone()
            return dict(row) if row else None

    def get_active_decision_request(self, assignment_id: UUID | str) -> dict[str, Any] | None:
        """Retrieves active awaiting_player decision request for assignment."""
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM public.player_decision_requests
                WHERE assignment_id = %s AND status = 'awaiting_player' AND expires_at > %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(assignment_id), now),
            ).fetchone()
            return dict(row) if row else None

    def record_player_decision(
        self,
        *,
        request_id: UUID | str,
        assignment_id: UUID | str,
        subject_player_id: str,
        actor_id: str,
        decision: str,
        authority_basis: str,
        assignment_version: int,
        assignment_hash: str,
        package_id: str,
        package_version: str,
        package_hash: str,
        notes: str = "",
        authentication_evidence_id: UUID | str | None = None,
        action_token_reference: str | None = None,
        idempotency_key: str | None = None,
        actor: str = "player",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Atomically commits player decision, updates request & assignment status, and records audit event."""
        if not authentication_evidence_id and not action_token_reference:
            raise IdentityStoreError(422, "player decision requires authentication evidence or action token reference")

        cid = correlation_id or uuid4()
        decision_id = uuid4()
        now = datetime.now(timezone.utc)

        with self.connect() as conn, conn.transaction():
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM public.player_decisions WHERE idempotency_key = %s",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return dict(existing)

            req = conn.execute(
                "SELECT * FROM public.player_decision_requests WHERE request_id = %s FOR UPDATE",
                (str(request_id),),
            ).fetchone()
            if not req:
                raise IdentityStoreError(404, "decision request not found")
            if req["status"] != "awaiting_player":
                raise IdentityStoreError(409, f"decision request is already in status '{req['status']}'")
            if req["expires_at"] <= now:
                raise IdentityStoreError(410, "decision request has expired")

            assign = conn.execute(
                "SELECT * FROM public.player_assignments WHERE assignment_id = %s FOR UPDATE",
                (str(assignment_id),),
            ).fetchone()
            if not assign:
                raise IdentityStoreError(404, "assignment not found")

            if assign["assignment_version"] != assignment_version or assign["assignment_hash"] != assignment_hash:
                raise IdentityStoreError(409, "assignment version or hash mismatch")
            if assign["package_id"] != package_id or assign["package_hash"] != package_hash:
                raise IdentityStoreError(409, "package binding mismatch")
            if str(assign["subject_player_id"]) != subject_player_id or subject_player_id != actor_id:
                raise IdentityStoreError(403, "player actor does not match assignment subject")

            req_update = conn.execute(
                """
                UPDATE public.player_decision_requests
                SET status = 'decided', decided_at = now()
                WHERE request_id = %s AND status = 'awaiting_player'
                RETURNING *
                """,
                (str(request_id),),
            ).fetchone()
            if not req_update:
                raise IdentityStoreError(409, "player decision request is not awaiting player or already decided")

            row = conn.execute(
                """
                INSERT INTO public.player_decisions (
                    decision_id, request_id, assignment_id, subject_player_id,
                    actor_type, actor_id, decision, authority_basis,
                    assignment_version, assignment_hash, package_id,
                    package_version, package_hash, notes,
                    authentication_evidence_id, action_token_reference,
                    idempotency_key, decided_at, created_at
                ) VALUES (
                    %s, %s, %s, %s, 'player', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()
                )
                RETURNING *
                """,
                (
                    decision_id, str(request_id), str(assignment_id), subject_player_id,
                    actor_id, decision, authority_basis,
                    assignment_version, assignment_hash, package_id,
                    package_version, package_hash, notes,
                    str(authentication_evidence_id) if authentication_evidence_id else None,
                    action_token_reference, idempotency_key
                ),
            ).fetchone()
            if not row:
                raise IdentityStoreError(500, "failed to record player decision")

            if decision == "pause":
                conn.execute(
                    """
                    UPDATE public.player_assignments
                    SET status = 'paused', updated_at = now()
                    WHERE assignment_id = %s AND status = 'active'
                    RETURNING *
                    """,
                    (str(assignment_id),),
                )

            _safe_record_event(
                conn,
                event_type="identity.player_decision_committed",
                entity_type="player_decision",
                entity_id=str(decision_id),
                actor=actor,
                correlation_id=cid,
                visibility="player_owner",
                scope_club_id=assign["club_id"],
                reason=decision,
                to_status=decision,
                source_table="player_decisions",
                source_id=str(decision_id),
            )
            return dict(row)

    commit_player_decision = record_player_decision

