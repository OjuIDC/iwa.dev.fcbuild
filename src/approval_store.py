"""PostgreSQL persistence for runs and Commissioner decisions.

Invariants enforced here (and backed by constraints/triggers in migration 0002):

* A thread id is created once. Existing threads are never overwritten.
* Only ``running`` threads can become ``awaiting_approval``/``failed``.
* A decision is one transaction: nonce validation + consumption, conditional
  ``awaiting_approval -> approved|rejected`` update, and the audit insert.
  Any failure rolls back all three.
* Nonces are issued per thread, bound to the exact decision payload and
  package version, single-use, and short-lived.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import approval
from .decision import DecisionConflict, apply_decision_to_state, package_version, resolve_candidate
from .schemas import DecisionIntent, SignedDecision
from .security import Principal, Role
from .thread_ids import allocate_thread_id, is_valid_club_id, parse_thread_id

MAX_APPROVAL_TTL_SECONDS = 300


@dataclass
class StoreError(Exception):
    status_code: int
    detail: str

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.status_code}: {self.detail}"


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ApprovalStore:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    # ------------------------------------------------------------------ runs
    def create_run(self, *, club_id: str, thread_id: str | None, initial_state: Callable[[str], dict[str, Any]], req_hash: str) -> dict[str, Any]:
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
            state["status"] = "running"
            row = conn.execute(
                """INSERT INTO graph_threads(thread_id, state, status, club_id, request_hash)
                   VALUES (%s, %s, 'running', %s, %s)
                   ON CONFLICT (thread_id) DO NOTHING
                   RETURNING thread_id""",
                (thread_id, Jsonb(state), club_id, req_hash),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    "SELECT status, request_hash FROM graph_threads WHERE thread_id = %s", (thread_id,)
                ).fetchone()
                same = existing is not None and existing["request_hash"] == req_hash
                raise StoreError(
                    409,
                    f"thread {thread_id} already exists (status={existing['status'] if existing else 'unknown'}"
                    f"{', identical request' if same else ''}); reruns must use a new thread_id",
                )
        return state

    def complete_run(self, thread_id: str, state: dict[str, Any]) -> dict[str, Any]:
        status = state.get("status")
        if status not in ("awaiting_approval", "failed"):
            raise StoreError(500, "pipeline returned an invalid status")
        version = package_version(state.get("lead_packages")) if status == "awaiting_approval" else None
        if status == "awaiting_approval" and version is None:
            status = "failed"
            state = {**state, "status": "failed", "errors": list(state.get("errors", [])) + ["no lead packages produced"]}
        state = {**state, "package_version": version}
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """UPDATE graph_threads
                      SET state = %s, status = %s, package_version = %s, updated_at = now()
                    WHERE thread_id = %s AND status = 'running'
                RETURNING thread_id""",
                (Jsonb(state), status, version, thread_id),
            ).fetchone()
            if row is None:
                raise StoreError(409, f"thread {thread_id} is no longer running")
        return state

    def load(self, thread_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT thread_id, state, status, package_version, approved_by, approved_at, decision_payload
                     FROM graph_threads WHERE thread_id = %s""",
                (thread_id,),
            ).fetchone()

    # ------------------------------------------------------------- decisions
    def issue_challenge(self, thread_id: str, intent: DecisionIntent, principal: Principal, ttl_seconds: int) -> dict[str, Any]:
        if principal.role is not Role.commissioner:
            raise StoreError(403, "only the commissioner can request a decision challenge")
        ttl = max(10, min(int(ttl_seconds), MAX_APPROVAL_TTL_SECONDS))
        with self.connect() as conn, conn.transaction():
            thread = conn.execute(
                "SELECT state, status, package_version FROM graph_threads WHERE thread_id = %s FOR SHARE",
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
            now = conn.execute("SELECT extract(epoch FROM now())::bigint AS t").fetchone()["t"]
            payload = {
                "thread_id": thread_id,
                **intent.model_dump(mode="json"),
                "package_version": thread["package_version"],
                "nonce": secrets.token_urlsafe(32),
                "issued_at": int(now),
                "expires_at": int(now) + ttl,
            }
            conn.execute(
                """INSERT INTO approval_nonces(nonce, thread_id, payload_sha256, package_version, issued_at, expires_at, created_by)
                   VALUES (%s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s)""",
                (payload["nonce"], thread_id, approval.payload_sha256(payload), thread["package_version"],
                 payload["issued_at"], payload["expires_at"], principal.name),
            )
        return payload

    def apply_decision(self, thread_id: str, signed: SignedDecision, principal: Principal, signing_secret: str) -> dict[str, Any]:
        if principal.role is not Role.commissioner:
            raise StoreError(403, "only the commissioner can decide")
        if signed.thread_id != thread_id:
            raise StoreError(400, "signed thread_id does not match the URL")
        payload = signed.model_dump(mode="json", exclude={"signature"})
        try:
            approval.verify_signature(payload, signed.signature, signing_secret)
        except approval.ApprovalSignatureError as exc:
            raise StoreError(403, str(exc)) from exc
        digest = approval.payload_sha256(payload)
        intent = DecisionIntent.model_validate({k: payload[k] for k in DecisionIntent.model_fields})

        with self.connect() as conn, conn.transaction():
            nonce = conn.execute(
                """SELECT thread_id, payload_sha256, package_version, consumed_at,
                          extract(epoch FROM issued_at)::bigint AS issued_at,
                          extract(epoch FROM expires_at)::bigint AS expires_at,
                          now() > expires_at AS expired
                     FROM approval_nonces WHERE nonce = %s FOR UPDATE""",
                (signed.nonce,),
            ).fetchone()
            if nonce is None:
                raise StoreError(409, "unknown nonce; request a new decision challenge")
            if nonce["thread_id"] != thread_id:
                raise StoreError(409, "nonce is bound to a different thread")
            if nonce["consumed_at"] is not None:
                raise StoreError(409, "nonce already used (replay rejected)")
            if nonce["expired"]:
                raise StoreError(409, "decision challenge expired; request a new one")
            if (nonce["payload_sha256"] != digest or nonce["issued_at"] != signed.issued_at
                    or nonce["expires_at"] != signed.expires_at or nonce["package_version"] != signed.package_version):
                raise StoreError(409, "signed payload does not match the issued challenge")

            thread = conn.execute(
                "SELECT state, status, package_version FROM graph_threads WHERE thread_id = %s FOR UPDATE",
                (thread_id,),
            ).fetchone()
            if thread is None:
                raise StoreError(404, "thread not found")
            if thread["status"] != "awaiting_approval":
                raise StoreError(409, f"thread is {thread['status']}, not awaiting_approval")
            if thread["package_version"] != signed.package_version:
                raise StoreError(409, "lead package changed since the challenge was issued (stale package)")

            decided_at = conn.execute("SELECT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS t").fetchone()["t"]
            try:
                new_state = apply_decision_to_state(thread["state"], intent, principal.name, decided_at)
            except DecisionConflict as exc:
                raise StoreError(409, str(exc)) from exc

            conn.execute("UPDATE approval_nonces SET consumed_at = now() WHERE nonce = %s AND consumed_at IS NULL", (signed.nonce,))
            decision_payload = {**payload, "signature_sha256": hashlib.sha256(signed.signature.encode()).hexdigest()}
            row = conn.execute(
                """UPDATE graph_threads
                      SET status = %s, state = %s, approved_by = %s, approved_at = now(),
                          decision_payload = %s, updated_at = now()
                    WHERE thread_id = %s AND status = 'awaiting_approval' AND package_version = %s
                RETURNING thread_id, status, approved_at""",
                (new_state["status"], Jsonb(new_state), principal.name, Jsonb(decision_payload), thread_id, signed.package_version),
            ).fetchone()
            if row is None:
                raise StoreError(409, "thread is no longer awaiting approval")
            conn.execute(
                """INSERT INTO approval_audit(thread_id, approved_by, decision, notes, owner, selection,
                                              candidate_id, player_id, nonce, package_version, signature_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (thread_id, principal.name, intent.decision.value, intent.notes,
                 intent.owner.value if intent.owner else None,
                 intent.selection.value if intent.selection else None,
                 intent.candidate_id, intent.player_id, signed.nonce, signed.package_version,
                 decision_payload["signature_sha256"]),
            )
        return new_state
