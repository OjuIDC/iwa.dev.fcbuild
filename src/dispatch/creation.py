"""Atomic dispatch creation upon Commissioner approval."""
from __future__ import annotations

import re
from typing import Any, Mapping
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from ..events import record_event
from ..schemas import DecisionIntent, Objective
from .transition import transition


class NotDispatchable(Exception):
    """Raised when an approved recommendation cannot be dispatched."""


_PLAYER_ID_RE = re.compile(r"^[0-9]{1,20}$")


def _safe_record_event(conn: psycopg.Connection, **kwargs: Any) -> int:
    old_factory = conn.row_factory
    conn.row_factory = tuple_row
    try:
        return record_event(conn, **kwargs)
    finally:
        conn.row_factory = old_factory


def create_dispatch_for_approval(
    conn: psycopg.Connection,
    *,
    thread: Mapping[str, Any],
    candidate: Mapping[str, Any],
    intent: DecisionIntent,
    approval_audit_id: int,
    approval_request_id: str | UUID,
    payload_sha256: str,
    principal_name: str,
    correlation_id: UUID,
) -> UUID:
    """Atomically create a pending dispatch and supersede active dispatches.

    Must be called within an open PostgreSQL transaction (inside apply_decision).
    """
    # -------------------------------------------------------------------------
    # 1. Dispatchability checks
    # -------------------------------------------------------------------------
    raw_obj = candidate.get("objective")
    if not raw_obj:
        raise NotDispatchable("candidate has no objective")

    try:
        if isinstance(raw_obj, Objective):
            objective = raw_obj
        else:
            objective = Objective.model_validate(raw_obj)
    except Exception as exc:
        raise NotDispatchable(f"invalid objective: {exc}") from exc

    cand_player_id = candidate.get("player_id")
    if not isinstance(cand_player_id, str) or not _PLAYER_ID_RE.match(cand_player_id):
        raise NotDispatchable(f"player_id must match ^[0-9]{{1,20}}$, got {cand_player_id!r}")

    thread_club_id = str(thread["club_id"])

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT player_id, club_id, active, status, identity_verified_at
              FROM public.players
             WHERE player_id = %s
            """,
            (cand_player_id,),
        )
        p_row = cur.fetchone()

        if p_row is None:
            raise NotDispatchable(f"player {cand_player_id} not found in players")

        # Exact string equality check (e.g. '7' vs '007')
        if p_row["player_id"] != cand_player_id:
            raise NotDispatchable(f"exact player_id mismatch: {p_row['player_id']} != {cand_player_id}")

        if not p_row["active"] or p_row["status"] != "active":
            raise NotDispatchable(f"player {cand_player_id} is not active")

        if p_row["identity_verified_at"] is None:
            raise NotDispatchable(f"player {cand_player_id} identity is unverified")

        if str(p_row["club_id"]) != thread_club_id:
            raise NotDispatchable(f"player club {p_row['club_id']} does not match thread club {thread_club_id}")

        # ---------------------------------------------------------------------
        # 2. Advisory xact lock per partition
        # ---------------------------------------------------------------------
        lock_key = f"{cand_player_id}|{intent.owner.value}|{objective.metric}"
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))

        # ---------------------------------------------------------------------
        # 3. Supersede active dispatches for same (player_id, owner, metric)
        # ---------------------------------------------------------------------
        new_dispatch_id = uuid4()

        cur.execute(
            """
            SELECT dispatch_id, dispatch_status, version
              FROM public.dispatches
             WHERE player_id = %s
               AND owner = %s
               AND (approved_instruction->'objective'->>'metric') = %s
               AND dispatch_status NOT IN ('completed', 'expired', 'cancelled', 'superseded')
            """,
            (cand_player_id, intent.owner.value, objective.metric),
        )
        active_dispatches = cur.fetchall()

        for d_row in active_dispatches:
            transition(
                conn,
                dispatch_id=d_row["dispatch_id"],
                expected_status=d_row["dispatch_status"],
                expected_version=d_row["version"],
                to_status="superseded",
                patch={"superseded_by": str(new_dispatch_id)},
                actor=f"commissioner:{principal_name}",
                correlation_id=correlation_id,
            )

        # ---------------------------------------------------------------------
        # 4. Insert into dispatches (status: pending)
        # ---------------------------------------------------------------------
        approved_instruction = {
            "candidate": candidate,
            "objective": objective.model_dump(mode="json"),
            "owner": intent.owner.value,
            "selection": intent.selection.value,
            "package_version": thread["package_version"],
            "fc_version": "27",
            "platform": "common-gen5",
        }

        cur.execute(
            """
            INSERT INTO public.dispatches (
                dispatch_id,
                source_thread_id,
                approval_id,
                approval_request_id,
                approval_payload_hash,
                package_version,
                player_id,
                candidate_id,
                owner,
                selection,
                club_id,
                approved_instruction,
                dispatch_status,
                dispatch_effective,
                version,
                plan_version,
                created_by_principal,
                created_correlation_id,
                fc_version,
                platform
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'pending', true, 1, 0, %s, %s, '27', 'common-gen5'
            )
            """,
            (
                new_dispatch_id,
                thread["thread_id"],
                approval_audit_id,
                UUID(str(approval_request_id)),
                payload_sha256,
                thread["package_version"],
                cand_player_id,
                candidate["candidate_id"],
                intent.owner.value,
                intent.selection.value,
                thread_club_id,
                Jsonb(approved_instruction),
                principal_name,
                correlation_id,
            ),
        )

    # -------------------------------------------------------------------------
    # 5. Record domain event & outbox row
    # -------------------------------------------------------------------------
    _safe_record_event(
        conn,
        event_type="dispatch.created",
        entity_type="dispatch",
        entity_id=str(new_dispatch_id),
        entity_version=1,
        actor=f"commissioner:{principal_name}",
        to_status="pending",
        correlation_id=correlation_id,
        visibility="player_owner",
        source_table="dispatches",
        source_id=str(new_dispatch_id),
        scope_club_id=thread_club_id,
        scope_player_id=cand_player_id,
    )

    return new_dispatch_id
