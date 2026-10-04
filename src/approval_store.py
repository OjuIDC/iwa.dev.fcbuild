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

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from . import approval
from .decision import DecisionConflict, apply_decision_to_state, package_version, resolve_candidate
from .events import record_event
from .schemas import DecisionIntent, DecisionKind, SignedDecision
from .security import Principal, Role
from .thread_ids import allocate_thread_id, is_valid_club_id, parse_thread_id

MAX_APPROVAL_TTL_SECONDS = 300


def _safe_record_event(conn: psycopg.Connection, **kwargs) -> int:
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

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)

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
        version = package_version(state.get("lead_packages")) if status == "awaiting_approval" else None
        if status == "awaiting_approval" and version is None:
            status = "failed"
            state = {**state, "status": "failed", "errors": list(state.get("errors", [])) + ["no lead packages produced"]}
        state = {**state, "package_version": version}

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

            # status == 'awaiting_approval' with version
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

            try:
                resolve_candidate(thread["state"], intent)
            except DecisionConflict as exc:
                raise StoreError(409, str(exc)) from exc

            now_epoch = conn.execute("SELECT extract(epoch FROM now())::bigint AS t").fetchone()["t"]
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

        payload = signed.model_dump(mode="json", exclude={"signature"})
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
                """SELECT state, status, package_version, club_id, decision_payload
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
                raise StoreError(409, "signed payload does not match the issued challenge")

            if thread["status"] != "awaiting_approval":
                raise StoreError(409, f"thread is {thread['status']}, not awaiting_approval")
            if thread["package_version"] != signed.package_version:
                raise StoreError(409, "lead package changed since the challenge was issued (stale package)")

            decided_at = conn.execute(
                "SELECT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS t"
            ).fetchone()["t"]

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

            # Update approval_requests
            conn.execute(
                """UPDATE public.approval_requests
                      SET status = %s, decided_at = now(), version = version + 1
                    WHERE approval_request_id = %s AND status = 'awaiting_approval'""",
                (new_state["status"], req["approval_request_id"]),
            )

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
                candidate = resolve_candidate(thread["state"], intent)
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
                except Exception as exc:
                    if exc.__class__.__name__ == "NotDispatchable":
                        raise StoreError(409, f"not_dispatchable: {exc}") from exc
                    raise

            new_state = {
                **new_state,
                "approval_audit_id": audit_id,
                "approval_request_id": str(req["approval_request_id"]),
                "dispatch_id": str(dispatch_id) if dispatch_id else None,
            }

        return new_state

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
                now_check = conn.execute(
                    "SELECT now() >= %s AS is_expired", (req["expires_at"],)
                ).fetchone()["is_expired"]
                if not now_check:
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

                # Set both records to expired
                conn.execute(
                    """UPDATE public.approval_requests
                          SET status = 'expired', expired_at = now(), version = version + 1
                        WHERE approval_request_id = %s""",
                    (req_id,),
                )
                conn.execute(
                    """UPDATE public.graph_threads
                          SET status = 'expired', updated_at = now()
                        WHERE thread_id = %s AND status = 'awaiting_approval'""",
                    (tid,),
                )

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
