"""Python wrapper service for public.fc27_dispatch_transition."""
from __future__ import annotations

import json
import uuid
from typing import Any, Mapping
import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb


class DispatchTransitionError(Exception):
    """Base exception for all dispatch transition failures."""

    def __init__(self, message: str, code: str | None = None, dispatch_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.dispatch_id = dispatch_id


class TransitionError(DispatchTransitionError):
    """Typed transition error with code and message."""

    def __init__(self, code_or_msg: str, message: str | None = None, *, code: str | None = None, dispatch_id: str | None = None):
        if message is not None:
            c = code_or_msg
            m = message
        else:
            c = code or "UNKNOWN"
            m = code_or_msg
        super().__init__(m, code=c, dispatch_id=dispatch_id)


class DispatchNotFoundError(TransitionError):
    """Raised when dispatch record does not exist (FC001 / P0002)."""


class TransitionConflictError(TransitionError):
    """Raised on optimistic concurrency version/status mismatch (FC001)."""


class VersionConflictError(TransitionConflictError):
    """Alias for version conflict."""


class InvalidTransitionError(TransitionError):
    """Raised when transition edge is disallowed by state machine (FC002)."""


class IllegalTransitionError(InvalidTransitionError):
    """Alias for illegal transition."""


class Phase1DeliveryBlockedError(InvalidTransitionError):
    """Raised when delivery is attempted while DISPATCH_ENABLED=false (Phase 1 invariant)."""


class InvalidPatchError(TransitionError):
    """Raised when patch keys or types are invalid (FC003)."""


class InvalidActorError(TransitionError):
    """Raised when caller DB role or actor is invalid (FC004)."""


class UnauthorizedRoleError(InvalidActorError):
    """Raised when session DB role is not authorized (FC002/FC004)."""


class UnauthorizedActorError(InvalidActorError):
    """Raised when actor_kind is not permitted (FC004)."""


class NullCorrelationIdError(TransitionError):
    """Raised when correlation_id is null (FC005)."""


class PreconditionFailedError(TransitionError):
    """Raised when prerequisite row is missing (FC006)."""


class ReasonInvalidError(TransitionError):
    """Raised when reason is missing, required, or too long (FC007)."""


class MissingReasonError(ReasonInvalidError):
    """Alias for missing required reason."""


def transition(
    conn: psycopg.Connection,
    *,
    dispatch_id: uuid.UUID | str,
    expected_status: str | None = None,
    expected_version: int | None = None,
    to_status: str,
    reason: str | None = None,
    correlation_id: uuid.UUID | str | None = None,
    patch: Mapping[str, Any] | None = None,
    actor: str | None = None,
) -> int:
    """Execute authorized dispatch state transition via public.fc27_dispatch_transition.

    Must be executed within an open database transaction block.

    Args:
        conn: Open psycopg database connection.
        dispatch_id: Target dispatch UUID.
        expected_status: Expected current status for optimistic concurrency check.
        expected_version: Expected current version for optimistic concurrency check.
        to_status: Target status string.
        reason: Explanatory reason text (mandatory for certain transitions).
        correlation_id: Distributed tracing correlation UUID.
        patch: Optional dictionary of edge-specific fields.
        actor: Optional actor string to set in transaction-local config.

    Returns:
        Integer representing the new version of the dispatch.
    """
    if conn.info.transaction_status == TransactionStatus.IDLE:
        raise RuntimeError("transition must be called inside an open transaction block")

    disp_id_str = str(dispatch_id)
    corr_id_str = str(correlation_id) if correlation_id is not None else None
    patch_payload = Jsonb(dict(patch)) if patch is not None else Jsonb({})

    try:
        with conn.cursor() as cur:
            if actor is not None:
                cur.execute("SELECT set_config('fc27.actor', %s, true)", (actor,))

            cur.execute(
                """
                SELECT public.fc27_dispatch_transition(
                    %s::uuid,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s::uuid,
                    %s
                )
                """,
                (
                    disp_id_str,
                    expected_status,
                    expected_version,
                    to_status,
                    reason,
                    corr_id_str,
                    patch_payload,
                ),
            )
            row = cur.fetchone()
            new_version = row["fc27_dispatch_transition"] if isinstance(row, Mapping) else row[0]
            return int(new_version)

    except psycopg.Error as exc:
        pgcode = getattr(exc, "pgcode", "") or ""
        diag = getattr(exc, "diag", None)
        msg = diag.message_primary if diag and diag.message_primary else str(exc)
        lower_msg = msg.lower()

        if "phase 1 invariant violation" in lower_msg or "delivered is disabled" in lower_msg:
            raise Phase1DeliveryBlockedError(msg, code="FC002", dispatch_id=disp_id_str) from exc

        if pgcode == "FC001" or "version conflict" in lower_msg or "status conflict" in lower_msg:
            if "not found" in lower_msg:
                raise DispatchNotFoundError(msg, code="FC001", dispatch_id=disp_id_str) from exc
            raise TransitionConflictError(msg, code="FC001", dispatch_id=disp_id_str) from exc

        if pgcode == "FC002" or "illegal transition" in lower_msg:
            if "not permitted for role" in lower_msg or "not authorized" in lower_msg:
                raise UnauthorizedRoleError(msg, code="FC002", dispatch_id=disp_id_str) from exc
            raise InvalidTransitionError(msg, code="FC002", dispatch_id=disp_id_str) from exc

        if pgcode == "FC003" or "patch" in lower_msg:
            raise InvalidPatchError(msg, code="FC003", dispatch_id=disp_id_str) from exc

        if pgcode == "FC004" or "actor" in lower_msg:
            if "actor_kind" in lower_msg:
                raise UnauthorizedActorError(msg, code="FC004", dispatch_id=disp_id_str) from exc
            raise InvalidActorError(msg, code="FC004", dispatch_id=disp_id_str) from exc

        if pgcode == "FC005" or "null correlation id" in lower_msg:
            raise NullCorrelationIdError(msg, code="FC005", dispatch_id=disp_id_str) from exc

        if pgcode == "FC006" or "missing" in lower_msg:
            raise PreconditionFailedError(msg, code="FC006", dispatch_id=disp_id_str) from exc

        if pgcode == "FC007" or "reason" in lower_msg:
            if "required" in lower_msg:
                raise MissingReasonError(msg, code="FC007", dispatch_id=disp_id_str) from exc
            raise ReasonInvalidError(msg, code="FC007", dispatch_id=disp_id_str) from exc

        raise TransitionError(pgcode, msg, code=pgcode, dispatch_id=disp_id_str) from exc


transition_dispatch = transition
