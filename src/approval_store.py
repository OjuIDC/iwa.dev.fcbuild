"""PostgreSQL persistence for runs and Commissioner decisions.

Invariants enforced here (and backed by constraints/triggers in migrations 0002 and 0003):

* A thread id is created once. Existing threads are never overwritten.
* Only ``running`` threads can become ``awaiting_approval``/``failed``.
* An awaiting thread must be paired 1:1 with an ``approval_requests`` row.
* A decision is one transaction: nonce validation + consumption, conditional
  ``awaiting_approval -> approved|rejected|revision_requested`` update on both tables,
  and the audit insert. Any failure rolls back all of them.
* Nonces are issued per thread, bound to the exact decision payload and
  package version, single-use, and short-lived (clamped to request TTL).
* Global lock order is strictly enforced: nonces -> approval_requests -> graph_threads.
* Safe idempotent decision replay returns committed decision without 409 conflict.
* Section 1A human authority model: rejects unsupported actor types and authority bases.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, cast
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from . import approval
from .decision import DecisionConflict, apply_decision_to_state, package_version, resolve_candidate
from .lead_packages import emit_pre_approval_validation_trace, validate_pre_approval_packages
from .dispatch.creation import NotDispatchable
from .dispatch.transition import (
    DispatchNotFoundError,
    TransitionConflictError,
    InvalidTransitionError,
    InvalidPatchError,
    UnauthorizedRoleError,
    Phase1DeliveryBlockedError,
)
from .events import record_event
from .schemas import DecisionIntent, DecisionKind, SignedDecision
from .security import Principal, Role
from .thread_ids import allocate_thread_id, is_valid_club_id, parse_thread_id
from .identity.routers.step_up import compute_bound_payload_hash, normalize_decision

MAX_APPROVAL_TTL_SECONDS = 300


def _safe_record_event(conn: psycopg.Connection[Any], **kwargs) -> int:
    old_factory = conn.row_factory
    conn.row_factory = tuple_row
    try:
        return record_event(conn, **kwargs)
    finally:
        conn.row_factory = old_factory


@dataclass
class StoreError(Exception):
    status_code: int
    detail: str

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.status_code}: {self.detail}"


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ApprovalStore:
    def __init__(self, conninfo: str, *, request_ttl: timedelta = timedelta(hours=72)):
        if request_ttl <= timedelta(0):
            raise ValueError("request_ttl must be positive")
        self.conninfo = conninfo
        self.request_ttl = request_ttl

    def connect(self) -> psycopg.Connection[dict[str, Any]]:
        return cast(psycopg.Connection[dict[str, Any]], psycopg.connect(self.conninfo, connect_timeout=3, row_factory=dict_row))

    # ------------------------------------------------------------------ runs
    def create_run(
        self,
        *,
        club_id: str,
        thread_id: str | None,
        initial_state: Callable[[str], dict[str, Any]],
        req_hash: str,
    ) -> dict[str, Any]:
        if not is_valid_club_id(club_id):
            raise StoreError(422, "club_id must contain ASCII digits only")
        with self.connect() as conn, conn.transaction():
            if thread_id is None:
                thread_id = allocate_thread_id(conn, club_id)
            else:
                parts = parse_thread_id(thread_id)
                if not parts:
                    raise StoreError(422, "thread_id must match fc27-<clubId>-run-<YYYYMMDD>-<seq>")
                if parts["club"] != club_id:
                    raise StoreError(422, "thread_id club does not match club_id")
            state = initial_state(thread_id)
            state["thread_id"] = thread_id
            state["status"] = "running"
            row = conn.execute(
                """INSERT INTO public.graph_threads(thread_id, state, status, club_id, request_hash)
                   VALUES (%s, %s, 'running', %s, %s)
                   ON CONFLICT (thread_id) DO NOTHING
                   RETURNING thread_id""",
                (thread_id, Jsonb(state), club_id, req_hash),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    "SELECT status, request_hash FROM public.graph_threads WHERE thread_id = %s", (thread_id,)
                ).fetchone()
                same = existing is not None and existing["request_hash"] == req_hash
                raise StoreError(
                    409,
                    f"thread {thread_id} already exists (status={existing['status'] if existing else 'unknown'}"
                    f"{', identical request' if same else ''}); reruns must use a new thread_id",
                )
        return state

    def complete_run(
        self,
        thread_id: str,
        state: dict[str, Any],
        approval_request_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        status = state.get("status")
        if status not in ("awaiting_approval", "failed"):
            raise StoreError(500, "pipeline returned an invalid status")

        if status == "awaiting_approval":
            if approval_request_id is None:
                if isinstance(state.get("approval_request"), dict):
                    approval_request_id = state["approval_request"].get("approval_request_id")
                if not approval_request_id:
                    approval_request_id = state.get("approval_request_id")

            if approval_request_id is not None and not isinstance(approval_request_id, UUID):
                try:
                    approval_request_id = UUID(str(approval_request_id))
                except ValueError as exc:
                    raise StoreError(422, f"invalid approval_request_id: {approval_request_id}") from exc

            computed_version = package_version(state.get("lead_packages"))
            target_version = state.get("package_version") if state.get("package_version") is not None else computed_version
            validation = validate_pre_approval_packages(
                state.get("lead_packages"),
                package_version_hash=target_version,
                candidates=state.get("candidates"),
                thread_id=thread_id,
                lineup=state.get("lineup"),
                formation=state.get("formation"),
                assignments=state.get("assignments"),
                roster=state.get("roster"),
                analytics_metadata=state.get("analytics_metadata"),
                recommendations=state.get("recommendations"),
            )
            emit_pre_approval_validation_trace(
                None,
                thread_id=thread_id,
                club_id=state.get("club_id"),
                result=validation,
            )
            state = {**state, "pre_approval_validation": validation.to_safe_summary()}

            if computed_version is None or not validation.valid or validation.review_required or bool(validation.reason_codes):
                status = "failed"
                version = None
                err_items = list(state.get("errors", []))
                if computed_version is None and "no lead packages produced" not in err_items:
                    err_items.append("no lead packages produced")
                for code in validation.reason_codes:
                    msg = f"pre_approval_validation: {code}"
                    if msg not in err_items:
                        err_items.append(msg)
                state = {
                    **state,
                    "status": "failed",
                    "package_version": None,
                    "approval_request": None,
                    "approval_request_id": None,
                    "approval_expires_at": None,
                    "errors": err_items,
                }
            else:
                version = computed_version
                state = {**state, "package_version": version}
        else:
            version = None
            state = {**state, "package_version": None}

        with self.connect() as conn, conn.transaction():
            if status == "failed":
                row = conn.execute(
                    """UPDATE public.graph_threads
                          SET state = %s, status = 'failed', package_version = NULL, updated_at = now()
                        WHERE thread_id = %s AND status = 'running'
                    RETURNING thread_id""",
                    (Jsonb(state), thread_id),
                ).fetchone()
                if row is None:
                    raise StoreError(409, f"thread {thread_id} is no longer running")
                return state

            # status == 'awaiting_approval' with validated version
            if approval_request_id is not None:
                req_row = conn.execute(
                    """INSERT INTO public.approval_requests (
                           approval_request_id, thread_id, package_version, status, created_at, expires_at, version
                       ) VALUES (
                           %s, %s, %s, 'awaiting_approval', now(), now() + %s, 1
                       )
                       RETURNING approval_request_id,
                                 to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
                    (approval_request_id, thread_id, version, self.request_ttl),
                ).fetchone()
            else:
                req_row = conn.execute(
                    """INSERT INTO public.approval_requests (
                           thread_id, package_version, status, created_at, expires_at, version
                       ) VALUES (
                           %s, %s, 'awaiting_approval', now(), now() + %s, 1
                       )
                       RETURNING approval_request_id,
                                 to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
                    (thread_id, version, self.request_ttl),
                ).fetchone()

            if req_row is None:
                raise StoreError(500, "failed to insert approval_request")

            inserted_id: UUID = req_row["approval_request_id"]
            approval_expires_at: str = req_row["approval_expires_at"]

            state = {
                **state,
                "approval_request_id": str(inserted_id),
                "approval_expires_at": approval_expires_at,
            }
            if isinstance(state.get("approval_request"), dict):
                state["approval_request"] = {
                    **state["approval_request"],
                    "approval_request_id": str(inserted_id),
                }

            thread_row = conn.execute(
                """UPDATE public.graph_threads
                      SET state = %s, status = 'awaiting_approval', package_version = %s,
                          approval_request_id = %s, updated_at = now()
                    WHERE thread_id = %s AND status = 'running'
                RETURNING thread_id, club_id""",
                (Jsonb(state), version, inserted_id, thread_id),
            ).fetchone()
            if thread_row is None:
                raise StoreError(409, f"thread {thread_id} is no longer running")

            _safe_record_event(
                conn,
                event_type="approval.created",
                entity_type="approval_request",
                entity_id=str(inserted_id),
                actor="system",
                correlation_id=uuid4(),
                visibility="staff",
                scope_club_id=thread_row["club_id"],
                source_table="approval_requests",
                source_id=str(inserted_id),
                to_status="awaiting_approval",
            )

        return state

    def load(self, thread_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT thread_id, state, status, package_version,
                          approval_request_id, approved_by, approved_at, decision_payload
                     FROM public.graph_threads WHERE thread_id = %s""",
                (thread_id,),
            ).fetchone()

    def terminal_thread_ids(self, days: int = 14) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT thread_id FROM public.graph_threads
                    WHERE status IN ('approved', 'rejected', 'revision_requested')
                      AND approved_at > now() - make_interval(days => %s)
                    ORDER BY approved_at""",
                (days,),
            ).fetchall()
        return [r["thread_id"] for r in rows]

    def pending_approvals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.approval_request_id,
                          r.thread_id,
                          r.package_version,
                          r.status,
                          r.created_at,
                          r.expires_at,
                          g.club_id,
                          g.state,
                          g.request_hash
                     FROM public.approval_requests r
                     JOIN public.graph_threads g USING (thread_id)
                    WHERE r.status = 'awaiting_approval'
                      AND g.status = 'awaiting_approval'
                      AND r.expires_at > now()
                    ORDER BY r.created_at ASC
                    LIMIT %s""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- decisions
    def issue_challenge(
        self, thread_id: str, intent: DecisionIntent, principal: Principal, ttl_seconds: int
    ) -> dict[str, Any]:
        if principal.role is not Role.commissioner:
            raise StoreError(403, "only the commissioner can request a decision challenge")
        ttl = max(10, min(int(ttl_seconds), MAX_APPROVAL_TTL_SECONDS))

        # 1. Proactive expiry evaluation for this thread before challenge lock
        self.expire_due(thread_id=thread_id)

        with self.connect() as conn, conn.transaction():
            # 2. Inconsistency view check
            inconsistency = conn.execute(
                """SELECT 1 FROM public.fc27_approval_inconsistencies
                    WHERE thread_id = %s AND kind <> 'legacy_unlinked'""",
                (thread_id,),
            ).fetchone()
            if inconsistency is not None:
                raise StoreError(409, "inconsistent_state")

            # 3. Lock approval_requests FOR SHARE (§7.6 lock order)
            req = conn.execute(
                """SELECT approval_request_id, status, package_version,
                          ceil(extract(epoch FROM expires_at))::bigint AS req_exp_epoch,
                          now() >= expires_at AS is_expired
                     FROM public.approval_requests
                    WHERE thread_id = %s
                      FOR SHARE""",
                (thread_id,),
            ).fetchone()
            if req is None:
                thread_chk = conn.execute(
                    "SELECT status FROM public.graph_threads WHERE thread_id = %s", (thread_id,)
                ).fetchone()
                if thread_chk is not None and thread_chk["status"] != "awaiting_approval":
                    raise StoreError(409, f"thread is {thread_chk['status']}, not awaiting_approval")
                raise StoreError(404, "approval request not found")
            if req["status"] != "awaiting_approval" or req["is_expired"]:
                raise StoreError(409, "approval_expired")

            # 4. Lock graph_threads FOR SHARE
            thread = conn.execute(
                """SELECT state, status, package_version, club_id
                     FROM public.graph_threads
                    WHERE thread_id = %s
                      FOR SHARE""",
                (thread_id,),
            ).fetchone()
            if thread is None:
                raise StoreError(404, "thread not found")
            if thread["status"] != "awaiting_approval":
                raise StoreError(409, f"thread is {thread['status']}, not awaiting_approval")
            if not thread["package_version"]:
                raise StoreError(409, "thread has no versioned lead package; start a new run")
            thread_state = thread["state"] if isinstance(thread["state"], dict) else {}
            pre_val = thread_state.get("pre_approval_validation")
            if isinstance(pre_val, dict) and (
                pre_val.get("valid") is False
                or pre_val.get("review_required") is True
                or bool(pre_val.get("reason_codes"))
            ):
                raise StoreError(409, "pre_approval_validation_failed")

            try:
                resolve_candidate(thread["state"], intent)
            except DecisionConflict as exc:
                raise StoreError(409, str(exc)) from exc

            now_row = conn.execute("SELECT extract(epoch FROM now())::bigint AS t").fetchone()
            if now_row is None:
                raise StoreError(500, "failed to query database time")
            now_epoch = now_row["t"]
            issued_at = int(now_epoch)
            expires_at = min(issued_at + ttl, int(req["req_exp_epoch"]))
            if expires_at <= issued_at:
                raise StoreError(409, "approval_expired")

            payload = {
                "thread_id": thread_id,
                **intent.model_dump(mode="json"),
                "package_version": thread["package_version"],
                "nonce": secrets.token_urlsafe(32),
                "issued_at": issued_at,
                "expires_at": expires_at,
            }

            conn.execute(
                """INSERT INTO public.approval_nonces (
                       nonce, thread_id, payload_sha256, package_version,
                       issued_at, expires_at, created_by, subject_type
                   ) VALUES (
                       %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, 'thread'
                   )""",
                (
                    payload["nonce"],
                    thread_id,
                    approval.payload_sha256(payload),
                    thread["package_version"],
                    payload["issued_at"],
                    payload["expires_at"],
                    principal.name,
                ),
            )

        return payload

    def apply_decision(
        self,
        thread_id: str,
        signed: SignedDecision,
        principal: Principal,
        signing_secret: str,
        *,
        correlation_id: UUID | None = None,
        dispatch_enabled: bool = False,
        dispatch_creator: Callable[..., UUID] | None = None,
        actor_type: str = "commissioner",
        authority_basis: str = "primary",
        actor: str | None = None,
    ) -> dict[str, Any]:
        # 1. Section 1A Authority Validation for Phase 1 Bootstrap
        if principal.role is not Role.commissioner:
            raise StoreError(403, "only the commissioner can decide")
        if actor and ("agent" in actor or actor.startswith("agent:")):
            raise StoreError(403, "agents cannot be decision actors")
        if actor_type == "player":
            raise StoreError(403, "player decisions are unsupported in Phase 1")
        if authority_basis != "primary":
            raise StoreError(403, f"unsupported authority basis in Phase 1: {authority_basis}")
        if actor_type != "commissioner":
            raise StoreError(403, f"unsupported actor type in Phase 1: {actor_type}")

        if signed.thread_id != thread_id:
            raise StoreError(400, "signed thread_id does not match the URL")

        payload = signed.model_dump(mode="json", exclude={"signature", "proof_type"})
        try:
            approval.verify_signature(payload, signed.signature, signing_secret)
        except approval.ApprovalSignatureError as exc:
            raise StoreError(403, str(exc)) from exc

        digest = approval.payload_sha256(payload)
        sig_hash = hashlib.sha256(signed.signature.encode()).hexdigest()
        intent = DecisionIntent.model_validate({k: payload[k] for k in DecisionIntent.model_fields})
        corr_id = correlation_id or uuid4()

        # Proactive expiry evaluation before locking
        self.expire_due(thread_id=thread_id)

        with self.connect() as conn, conn.transaction():
            # Lock order 1: approval_nonces (§7.6)
            nonce = conn.execute(
                """SELECT nonce, thread_id, payload_sha256, package_version,
                          consumed_at, revoked_at,
                          extract(epoch FROM issued_at)::bigint AS issued_at,
                          extract(epoch FROM expires_at)::bigint AS expires_at,
                          now() > expires_at AS expired
                     FROM public.approval_nonces
                    WHERE nonce = %s
                      FOR UPDATE""",
                (signed.nonce,),
            ).fetchone()
            if nonce is None:
                raise StoreError(409, "unknown nonce; request a new decision challenge")
            if nonce["thread_id"] != thread_id:
                raise StoreError(409, "nonce is bound to a different thread")

            # Lock order 2: approval_requests (§7.6)
            req = conn.execute(
                """SELECT approval_request_id, thread_id, package_version, status,
                          now() >= expires_at AS is_expired
                     FROM public.approval_requests
                    WHERE thread_id = %s
                      FOR UPDATE""",
                (thread_id,),
            ).fetchone()

            # Lock order 3: graph_threads (§7.6)
            thread = conn.execute(
                """SELECT thread_id, state, status, package_version, club_id, decision_payload
                     FROM public.graph_threads
                    WHERE thread_id = %s
                      FOR UPDATE""",
                (thread_id,),
            ).fetchone()
            if thread is None:
                raise StoreError(404, "thread not found")

# SAFE IDEMPOTENT REPLAY PROTOCOL
            if nonce["consumed_at"] is not None:
                audit = conn.execute(
                    """SELECT id, thread_id, package_version, signature_sha256,
                          decision, candidate_id, player_id, owner, selection,
                          approval_request_id
                     FROM public.approval_audit
                    WHERE nonce = %s""",
                    (signed.nonce,),
                ).fetchone()

                # Verify byte-level and semantic identity of replay
                if audit is not None and (
                    audit["thread_id"] == thread_id
                    and audit["package_version"] == signed.package_version
                    and audit["signature_sha256"] == sig_hash
                    and audit["decision"] == intent.decision.value
                    and audit["candidate_id"] == intent.candidate_id
                    and audit["player_id"] == intent.player_id
                    and audit["owner"] == (intent.owner.value if intent.owner else None)
                    and audit["selection"] == (intent.selection.value if intent.selection else None)
                ):
                    replayed_state = dict(thread["state"])
                    existing_dispatch = conn.execute(
                        "SELECT dispatch_id FROM public.dispatches WHERE approval_id = %s",
                        (audit["id"],),
                    ).fetchone()
                    replayed_state.update(
                        approval_audit_id=audit["id"],
                        approval_request_id=str(audit["approval_request_id"]),
                        dispatch_id=str(existing_dispatch["dispatch_id"]) if existing_dispatch else None,
                    )
                    return replayed_state

                raise StoreError(409, "replay: conflicting terminal decision or nonce already used")

            # Inconsistency check
            inconsistency = conn.execute(
                """SELECT 1 FROM public.fc27_approval_inconsistencies
                    WHERE thread_id = %s AND kind <> 'legacy_unlinked'""",
                (thread_id,),
            ).fetchone()
            if inconsistency is not None:
                raise StoreError(409, "inconsistent_state")

            # Expiry and Revocation checks
            if nonce["revoked_at"] is not None:
                raise StoreError(409, "approval_expired")
            if req is None or req["status"] != "awaiting_approval" or req["is_expired"]:
                raise StoreError(409, "approval_expired")
            if nonce["expired"]:
                raise StoreError(409, "approval_expired")

            # Payload matching checks
            if (
                nonce["payload_sha256"] != digest
                or nonce["issued_at"] != signed.issued_at
                or nonce["expires_at"] != signed.expires_at
                or nonce["package_version"] != signed.package_version
            ):
                raise StoreError(409, "signed payload does not match the issued challenge (package_version mismatch)")

            if thread["status"] != "awaiting_approval":
                raise StoreError(409, f"thread is {thread['status']}, not awaiting_approval")
            if thread["package_version"] != signed.package_version:
                raise StoreError(409, "lead package changed since the challenge was issued (stale package)")

            now_row = conn.execute(
                "SELECT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS t"
            ).fetchone()
            if now_row is None:
                raise StoreError(500, "failed to query database time")
            decided_at = str(now_row["t"])

            try:
                new_state = apply_decision_to_state(thread["state"], intent, principal.name, decided_at)
            except DecisionConflict as exc:
                raise StoreError(409, str(exc)) from exc

            # Mark nonce consumed
            conn.execute(
                "UPDATE public.approval_nonces SET consumed_at = now() WHERE nonce = %s AND consumed_at IS NULL",
                (signed.nonce,),
            )

            decision_payload = {**payload, "signature_sha256": sig_hash}

            # Update approval_requests (R4 conditional transition with RETURNING)
            ar_row = conn.execute(
                """UPDATE public.approval_requests
                      SET status = %s, decided_at = now(), version = version + 1
                    WHERE approval_request_id = %s AND status = 'awaiting_approval'
                RETURNING approval_request_id""",
                (new_state["status"], req["approval_request_id"]),
            ).fetchone()
            if ar_row is None:
                raise StoreError(409, "approval request is no longer awaiting approval")

            # Update graph_threads
            row = conn.execute(
                """UPDATE public.graph_threads
                      SET status = %s, state = %s, approved_by = %s, approved_at = now(),
                          decision_payload = %s, updated_at = now()
                    WHERE thread_id = %s AND status = 'awaiting_approval' AND package_version = %s
                RETURNING thread_id, status, approved_at""",
                (new_state["status"], Jsonb(new_state), principal.name, Jsonb(decision_payload), thread_id, signed.package_version),
            ).fetchone()
            if row is None:
                raise StoreError(409, "thread is no longer awaiting approval")

            # Write approval_audit with Section 1A compatibility fields
            audit_row = conn.execute(
                """INSERT INTO public.approval_audit (
                       thread_id, approved_by, decision, notes, owner, selection,
                       candidate_id, player_id, nonce, package_version, signature_sha256,
                       proof_type, correlation_id, approval_request_id,
                       actor_type, authority_basis, authority_role, decision_scope
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s,
                       'hmac', %s, %s,
                       'commissioner', 'primary', 'commissioner', 'club'
                   ) RETURNING id""",
                (
                    thread_id,
                    principal.name,
                    intent.decision.value,
                    intent.notes,
                    intent.owner.value if intent.owner else None,
                    intent.selection.value if intent.selection else None,
                    intent.candidate_id,
                    intent.player_id,
                    signed.nonce,
                    signed.package_version,
                    sig_hash,
                    corr_id,
                    req["approval_request_id"],
                ),
            ).fetchone()
            if audit_row is None:
                raise StoreError(500, "failed to insert approval audit record")
            audit_id = audit_row["id"]

            _safe_record_event(
                conn,
                event_type="approval.decided",
                entity_type="approval_request",
                entity_id=str(req["approval_request_id"]),
                actor=f"commissioner:{principal.name}",
                correlation_id=corr_id,
                visibility="staff",
                scope_club_id=thread["club_id"],
                source_table="approval_audit",
                source_id=str(audit_id),
                from_status="awaiting_approval",
                to_status=new_state["status"],
            )

            dispatch_id: UUID | None = None
            if dispatch_enabled and intent.decision is DecisionKind.APPROVE and dispatch_creator is not None:
                resolved = resolve_candidate(thread["state"], intent)
                candidate = resolved[2] if resolved else None
                try:
                    dispatch_id = dispatch_creator(
                        conn,
                        thread=thread,
                        candidate=candidate,
                        intent=intent,
                        approval_audit_id=audit_id,
                        approval_request_id=req["approval_request_id"],
                        payload_sha256=digest,
                        principal_name=principal.name,
                        correlation_id=corr_id,
                    )
                except NotDispatchable as exc:
                    raise StoreError(409, f"not_dispatchable: {exc}") from exc
                except (DispatchNotFoundError, TransitionConflictError, InvalidTransitionError, InvalidPatchError,
                        UnauthorizedRoleError, Phase1DeliveryBlockedError) as exc:
                    raise StoreError(409, f"dispatch_transition_failed: {exc}") from exc
                except psycopg.Error as exc:
                    raise StoreError(500, f"dispatch_database_error: {exc}") from exc
                except BaseException as exc:
                    raise

            new_state = {
                **new_state,
                "approval_audit_id": audit_id,
                "approval_request_id": str(req["approval_request_id"]),
                "dispatch_id": str(dispatch_id) if dispatch_id else None,
            }

        return new_state

    def apply_webauthn_decision(
        self,
        *,
        evidence_id: UUID | str,
        decision: str,
        thread_id: str | None = None,
        principal: Any = None,
        approval_request_id: UUID | str | None = None,
        package_id: str | None = None,
        package_version: str | None = None,
        notes: str | None = None,
        authority_basis: str = "primary",
        delegation_id: UUID | str | None = None,
        correlation_id: UUID | None = None,
        dispatch_enabled: bool = False,
        dispatch_creator: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Applies a WebAuthn decision in a single atomic transaction (R3 & R4).

        Locks rows in deterministic order:
        1. fc27_decision_evidence FOR UPDATE
        2. approval_requests FOR UPDATE
        3. graph_threads FOR UPDATE
        """
        norm_decision = normalize_decision(decision)
        cid = correlation_id or uuid4()

        with self.connect() as conn, conn.transaction():
            # Lock order 1: fc27_decision_evidence FOR UPDATE
            ev_row = conn.execute(
                """SELECT evidence_id, user_id, session_id, thread_id, approval_request_id,
                          package_id, package_version, package_hash, normalized_decision,
                          canonical_bound_payload_hash, issued_at, expires_at, consumed_at,
                          webauthn_credential_id, challenge_id,
                          now() >= expires_at AS is_expired
                     FROM public.fc27_decision_evidence
                    WHERE evidence_id = %s
                      FOR UPDATE""",
                (str(evidence_id),),
            ).fetchone()
            if ev_row is None:
                raise StoreError(400, "unknown or invalid authentication_evidence_id")
            if ev_row["is_expired"]:
                raise StoreError(400, "decision evidence expired")

            # Cross-validate caller inputs against evidence row
            if approval_request_id is not None and str(approval_request_id) != str(ev_row["approval_request_id"]):
                raise StoreError(400, "caller approval_request_id does not match evidence approval_request_id")
            if thread_id is not None and thread_id != str(ev_row["thread_id"]):
                raise StoreError(400, "caller thread_id does not match evidence thread_id")
            if package_id is not None and package_id != str(ev_row["package_id"]):
                raise StoreError(400, "caller package_id does not match evidence package_id")
            if package_version is not None:
                if (
                    package_version != str(ev_row["package_version"])
                    and package_version.lower() != str(ev_row["package_hash"]).lower()
                ):
                    raise StoreError(400, "caller package_version does not match evidence package parameters")

            # Safe Idempotent Replay Evaluation Under Lock
            if ev_row["consumed_at"] is not None:
                existing_audit = conn.execute(
                    """SELECT a.id, g.state, g.status, a.decision, a.package_version,
                              a.approval_request_id, a.thread_id
                         FROM public.approval_audit a
                         JOIN public.graph_threads g USING (thread_id)
                        WHERE a.authentication_evidence_id = %s
                          AND a.thread_id = %s""",
                    (str(evidence_id), ev_row["thread_id"]),
                ).fetchone()
                if existing_audit and existing_audit["status"].lower() == norm_decision:
                    replayed_state = dict(existing_audit["state"]) if isinstance(existing_audit["state"], dict) else {}
                    return {
                        **replayed_state,
                        "thread_id": ev_row["thread_id"],
                        "status": existing_audit["status"],
                        "package_version": ev_row["package_version"],
                        "approval_audit_id": existing_audit["id"],
                        "approval_request_id": str(ev_row["approval_request_id"]),
                        "dispatch": {
                            "enabled": False,
                            "created": False,
                            "message": "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player.",
                            "dispatch_id": None,
                        },
                        "dispatch_id": None,
                        "replayed": True,
                        "graph_resume": "resumed",
                    }
                raise StoreError(409, "replay: authentication evidence already consumed")

            # Lock order 2: approval_requests FOR UPDATE
            req_id = approval_request_id or ev_row["approval_request_id"]
            req = conn.execute(
                """SELECT approval_request_id, thread_id, package_version, status,
                          expires_at, now() >= expires_at AS is_expired
                     FROM public.approval_requests
                    WHERE approval_request_id = %s
                      FOR UPDATE""",
                (str(req_id),),
            ).fetchone()
            if req is None:
                raise StoreError(404, "approval request not found")
            if req["status"] != "awaiting_approval" or req["is_expired"]:
                raise StoreError(409, "approval request is no longer awaiting approval or expired")

            # Lock order 3: graph_threads FOR UPDATE
            th_id = thread_id or req["thread_id"]
            thread = conn.execute(
                """SELECT thread_id, state, status, package_version, club_id, decision_payload
                     FROM public.graph_threads
                    WHERE thread_id = %s
                      FOR UPDATE""",
                (str(th_id),),
            ).fetchone()
            if thread is None:
                raise StoreError(404, "graph thread not found")
            if thread["status"] != "awaiting_approval":
                raise StoreError(409, f"graph thread is {thread['status']}, not awaiting_approval")

            # Inconsistency check
            inconsistency = conn.execute(
                """SELECT 1 FROM public.fc27_approval_inconsistencies
                    WHERE thread_id = %s AND kind <> 'legacy_unlinked'""",
                (str(th_id),),
            ).fetchone()
            if inconsistency is not None:
                raise StoreError(409, "inconsistent_state")

            # Validate bindings and cross-validate against locked database rows
            p_uid = getattr(principal, "user_id", None)
            if p_uid and str(ev_row["user_id"]) != str(p_uid):
                raise StoreError(403, "evidence bound to different user ID")

            p_sess = getattr(principal, "session_id", None)
            if p_sess and ev_row["session_id"] and str(ev_row["session_id"]) != str(p_sess):
                raise StoreError(403, "evidence bound to different session ID")

            if str(ev_row["approval_request_id"]) != str(req["approval_request_id"]):
                raise StoreError(400, "evidence bound to different approval request ID")
            if approval_request_id is not None and str(req["approval_request_id"]) != str(approval_request_id):
                raise StoreError(400, "caller approval_request_id does not match locked approval request")

            if str(ev_row["thread_id"]) != str(thread["thread_id"]):
                raise StoreError(400, "evidence bound to different thread ID")
            if str(req["thread_id"]) != str(thread["thread_id"]):
                raise StoreError(400, "approval request bound to different thread ID")
            if thread_id is not None and str(thread["thread_id"]) != thread_id:
                raise StoreError(400, "caller thread_id does not match locked graph thread")

            # Package version match: supports both version string matching and hex package_hash matching
            if (
                str(ev_row["package_version"]) != str(req["package_version"])
                and str(ev_row["package_hash"]).lower() != str(req["package_version"]).lower()
            ):
                raise StoreError(400, "evidence bound to different package version")
            if package_version is not None and (
                package_version != str(req["package_version"])
                and package_version.lower() != str(req["package_version"]).lower()
                and package_version != str(ev_row["package_version"])
            ):
                raise StoreError(400, "caller package_version does not match locked approval request")

            if ev_row["normalized_decision"].lower() != norm_decision:
                raise StoreError(403, "evidence bound to different decision (bound decision mismatch)")

            expected_hash = compute_bound_payload_hash(
                approval_request_id=req["approval_request_id"],
                package_id=ev_row["package_id"],
                package_version=ev_row["package_version"],
                package_hash=ev_row["package_hash"],
                decision=norm_decision,
            )
            if ev_row["canonical_bound_payload_hash"].lower() != expected_hash.lower():
                raise StoreError(400, "canonical bound payload hash verification failed")

            # Consume evidence with conditional RETURNING
            ev_consume = conn.execute(
                """UPDATE public.fc27_decision_evidence
                      SET consumed_at = now()
                    WHERE evidence_id = %s
                      AND consumed_at IS NULL
                   RETURNING evidence_id, consumed_at""",
                (str(evidence_id),),
            ).fetchone()
            if ev_consume is None:
                raise StoreError(409, "conflict: evidence already consumed")

            # Transition approval request with conditional RETURNING
            ar_update = conn.execute(
                """UPDATE public.approval_requests
                      SET status = %s, decided_at = now(), version = version + 1
                    WHERE approval_request_id = %s
                      AND status = 'awaiting_approval'
                   RETURNING approval_request_id, status, version""",
                (norm_decision, req["approval_request_id"]),
            ).fetchone()
            if ar_update is None:
                raise StoreError(409, "conflict: not awaiting_approval")

            # Transition graph thread with conditional RETURNING
            row_t = conn.execute(
                "SELECT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS t"
            ).fetchone()
            decided_at = row_t["t"] if (row_t and "t" in row_t) else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            approved_by_name = getattr(principal, "display_name", None) or getattr(principal, "name", None) or "Commissioner"
            is_approved = (norm_decision == "approved")
            is_revision = (norm_decision == "revision_requested")
            summary_str = "APPROVED" if is_approved else ("REVISION_REQUESTED" if is_revision else "REJECTED")

            new_state = copy.deepcopy(thread["state"]) if isinstance(thread["state"], dict) else {}
            new_state.update(
                approved=is_approved,
                status=norm_decision,
                approved_by=approved_by_name,
                commissioner_input=notes or "",
            )
            new_state["audit_log"] = list(new_state.get("audit_log", [])) + [
                {"node": "approval", "ts": decided_at, "summary": summary_str, "approved_by": approved_by_name}
            ]

            decision_payload = {
                "decision": norm_decision,
                "proof_type": "webauthn",
                "authentication_evidence_id": str(evidence_id),
            }

            gt_update = conn.execute(
                """UPDATE public.graph_threads
                      SET status = %s, state = %s, approved_by = %s, approved_at = now(),
                          decision_payload = %s, updated_at = now()
                    WHERE thread_id = %s
                      AND status = 'awaiting_approval'
                   RETURNING thread_id, status, approved_at""",
                (norm_decision, Jsonb(new_state), approved_by_name, Jsonb(decision_payload), str(th_id)),
            ).fetchone()
            if gt_update is None:
                raise StoreError(409, "conflict: thread not awaiting_approval")

            # Insert into approval_audit
            is_delegate = getattr(principal, "is_delegate", False)
            auth_basis_str = authority_basis.value if hasattr(authority_basis, "value") else str(authority_basis)
            actor_type_str = "delegate" if is_delegate else "commissioner"
            auth_role_str = "commissioner_delegate" if is_delegate else "commissioner"
            db_decision = "APPROVE" if norm_decision == "approved" else ("REQUEST_REVISION" if norm_decision == "revision_requested" else "REJECT")

            audit_row = conn.execute(
                """INSERT INTO public.approval_audit (
                       thread_id, approved_by, decision, notes,
                       package_version, proof_type, authentication_evidence_id,
                       actor_type, authority_basis, authority_role, decision_scope,
                       correlation_id, approval_request_id,
                       actor_user_id, webauthn_credential_id, delegation_id,
                       package_id, package_hash
                   ) VALUES (
                       %s, %s, %s, %s,
                       %s, 'webauthn', %s,
                       %s, %s, %s, 'club',
                       %s, %s,
                       %s, %s, %s,
                       %s, %s
                   ) RETURNING id""",
                (
                    str(th_id),
                    approved_by_name,
                    db_decision,
                    notes or "",
                    req["package_version"],
                    str(evidence_id),
                    actor_type_str,
                    auth_basis_str,
                    auth_role_str,
                    cid,
                    req["approval_request_id"],
                    p_uid,
                    ev_row.get("webauthn_credential_id"),
                    delegation_id,
                    ev_row["package_id"],
                    ev_row["package_hash"],
                ),
            ).fetchone()
            if audit_row is None:
                raise StoreError(500, "failed to insert approval audit record")

            # Insert domain events
            _safe_record_event(
                conn,
                event_type="identity.decision_evidence_consumed",
                entity_type="decision_evidence",
                entity_id=str(evidence_id),
                actor=f"user:{p_uid}" if p_uid else approved_by_name,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=thread["club_id"],
                reason=norm_decision,
                source_table="fc27_decision_evidence",
                source_id=str(evidence_id),
            )

            _safe_record_event(
                conn,
                event_type="approval.decided",
                entity_type="approval_request",
                entity_id=str(req["approval_request_id"]),
                actor=f"user:{p_uid}" if p_uid else approved_by_name,
                correlation_id=cid,
                visibility="staff",
                scope_club_id=thread["club_id"],
                source_table="approval_audit",
                source_id=str(audit_row["id"]),
                from_status="awaiting_approval",
                to_status=norm_decision,
            )

            # Respect DISPATCH_ENABLED=false: zero dispatches created

        return {
            **new_state,
            "thread_id": str(th_id),
            "status": norm_decision,
            "package_version": req["package_version"],
            "approval_audit_id": audit_row["id"],
            "approval_request_id": str(req["approval_request_id"]),
            "dispatch": {
                "enabled": False,
                "created": False,
                "message": "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player.",
                "dispatch_id": None,
            },
            "dispatch_id": None,
            "graph_resume": "resumed",
        }

    def expire_due(self, *, thread_id: str | None = None, limit: int = 100) -> list[str]:
        query = """SELECT r.approval_request_id, r.thread_id
                     FROM public.approval_requests r
                     JOIN public.graph_threads g USING (thread_id)
                    WHERE r.status = 'awaiting_approval'
                      AND r.expires_at <= now()"""
        params: list[Any] = []
        if thread_id is not None:
            query += " AND r.thread_id = %s"
            params.append(thread_id)
        query += " ORDER BY r.expires_at ASC LIMIT %s"
        params.append(limit)

        with self.connect() as conn:
            due = conn.execute(query, tuple(params)).fetchall()

        expired_ids: list[str] = []
        for item in due:
            tid = item["thread_id"]
            req_id = item["approval_request_id"]
            with self.connect() as conn, conn.transaction():
                # Lock order 1: open nonces for thread in nonce order (§7.6)
                conn.execute(
                    """SELECT nonce FROM public.approval_nonces
                        WHERE thread_id = %s AND consumed_at IS NULL AND revoked_at IS NULL
                        ORDER BY nonce
                          FOR UPDATE""",
                    (tid,),
                )

                # Lock order 2: approval_requests FOR UPDATE (§7.6)
                req = conn.execute(
                    """SELECT approval_request_id, status, expires_at
                         FROM public.approval_requests
                        WHERE approval_request_id = %s
                          FOR UPDATE""",
                    (req_id,),
                ).fetchone()
                if req is None or req["status"] != "awaiting_approval":
                    continue

                # Re-check expiry condition under row lock
                expiry_row = conn.execute(
                    "SELECT now() >= %s AS is_expired", (req["expires_at"],)
                ).fetchone()
                if expiry_row is None or not expiry_row["is_expired"]:
                    continue

                # Lock order 3: graph_threads FOR UPDATE (§7.6)
                th = conn.execute(
                    """SELECT thread_id, status, club_id
                         FROM public.graph_threads
                        WHERE thread_id = %s
                          FOR UPDATE""",
                    (tid,),
                ).fetchone()
                if th is None or th["status"] != "awaiting_approval":
                    continue

                # Set both records to expired (R4 conditional transition with RETURNING)
                ar_row = conn.execute(
                    """UPDATE public.approval_requests
                          SET status = 'expired', expired_at = now(), version = version + 1
                        WHERE approval_request_id = %s AND status = 'awaiting_approval'
                    RETURNING approval_request_id""",
                    (req_id,),
                ).fetchone()
                if ar_row is None:
                    continue

                gt_row = conn.execute(
                    """UPDATE public.graph_threads
                          SET status = 'expired', updated_at = now()
                        WHERE thread_id = %s AND status = 'awaiting_approval'
                    RETURNING thread_id""",
                    (tid,),
                ).fetchone()
                if gt_row is None:
                    continue

                # Revoke open nonces
                conn.execute(
                    """UPDATE public.approval_nonces
                          SET revoked_at = now()
                        WHERE thread_id = %s AND consumed_at IS NULL AND revoked_at IS NULL""",
                    (tid,),
                )

                _safe_record_event(
                    conn,
                    event_type="approval.expired",
                    entity_type="approval_request",
                    entity_id=str(req_id),
                    actor="system",
                    correlation_id=uuid4(),
                    visibility="staff",
                    scope_club_id=th["club_id"],
                    source_table="approval_requests",
                    source_id=str(req_id),
                    from_status="awaiting_approval",
                    to_status="expired",
                )
                expired_ids.append(tid)

        return expired_ids
