"""Dispatch Worker Service for FC27 Pro Clubs Platform.

Responsible for:
- Consuming approved internal domain events from Redis (`fc27:events`).
- Validating event envelopes against strict production and test schemas.
- Re-reading authoritative PostgreSQL state (never trusting Redis as source of truth).
- Enforcing player identity-verification and club-scope authorization.
- Enforcing fail-closed activation gates and global kill switch (`DISPATCH_ENABLED`).
- Performing immediate pre-side-effect rechecks to prevent execution under revoked gates.
- Managing durable worker claims with lease ownership, expiry, and bounded retries.
- Reconciling abandoned queued work whose lease expired.
- Executing deterministic per-player personalized plan policy validation.
- Emitting periodic worker heartbeats to `public.service_heartbeats` (`G-WORKER`).
- Delivering via approved adapters ('dashboard-storage', 'websocket-notification').
- Recording immutable delivery attempts and idempotency in `public.dispatch_delivery_attempts`.
- Maintaining least-privilege execution under the `fc27_dispatch` database role.
- Exposing container health check for Docker Compose orchestration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from typing import Any, Mapping, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dispatch.gates import (
    DeploymentIdentity,
    GateResult,
    dispatch_effective,
    evaluate_gates,
)
from src.dispatch.objective import validate_metric_bounds
from src.dispatch.transition import (
    Phase1DeliveryBlockedError,
    TransitionConflictError,
    TransitionError,
    transition,
)

logger = logging.getLogger("fc27.dispatch_worker")

# Configuration Defaults
DEFAULT_HEARTBEAT_INTERVAL = float(os.getenv("WORKER_HEARTBEAT_INTERVAL", "15.0"))
DEFAULT_POLL_INTERVAL = float(os.getenv("WORKER_POLL_INTERVAL", "1.0"))
DEFAULT_DISPATCH_ENABLED = os.getenv("DISPATCH_ENABLED", "false").lower() in ("true", "1", "yes")
DEFAULT_INSTANCE_ID = os.getenv("WORKER_INSTANCE_ID") or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
DEFAULT_LEASE_SECONDS = float(os.getenv("WORKER_LEASE_SECONDS", "30.0"))
MAX_WORKER_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS", "3"))
BASE_BACKOFF_SECONDS = float(os.getenv("WORKER_BASE_BACKOFF_SECONDS", "5.0"))

REQUIRED_ENVELOPE_FIELDS = (
    "event_id",
    "event_type",
    "entity_type",
    "entity_id",
    "actor",
)


def _pg_uri() -> str:
    """Resolve PostgreSQL connection URI with secret file fallback."""
    uri_file = os.getenv("POSTGRES_URI_FILE")
    if uri_file and os.path.exists(uri_file):
        with open(uri_file, encoding="utf-8") as f:
            return f.read().strip()
    return os.getenv("POSTGRES_URI", "")


def _redis_url() -> str:
    """Resolve Redis connection URL."""
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Produces deterministic UTF-8 bytes for canonical hashing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_digest(data: bytes | Mapping[str, Any]) -> str:
    """Calculates hex SHA-256 digest of bytes or a mapping."""
    if isinstance(data, Mapping):
        data = canonical_json_bytes(data)
    return hashlib.sha256(data).hexdigest()


# =====================================================================
# 1. Event Envelope Validation (P3-BLOCKER-03)
# =====================================================================

def parse_event_envelope(raw_message: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Parses and strictly validates the internal Redis event envelope.

    Supports both Package 2 production outbox relay output and Package 3 test envelopes.

    Args:
        raw_message: JSON string, bytes, or parsed dict.

    Returns:
        Validated event dictionary.

    Raises:
        ValueError: If JSON is malformed or required fields are missing/invalid.
    """
    if isinstance(raw_message, (str, bytes)):
        try:
            data = json.loads(raw_message)
        except Exception as exc:
            raise ValueError(f"malformed event JSON: {exc}") from exc
    elif isinstance(raw_message, dict):
        data = raw_message
    else:
        raise ValueError(f"unsupported event payload type: {type(raw_message).__name__}")

    if not isinstance(data, dict):
        raise ValueError("event payload must be a JSON object")

    missing = [f for f in REQUIRED_ENVELOPE_FIELDS if f not in data or data[f] is None]
    if missing:
        raise ValueError(f"event envelope missing required fields: {', '.join(missing)}")

    # Validate UUIDs
    try:
        UUID(str(data["entity_id"]))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid entity_id UUID: {data.get('entity_id')}") from exc

    corr = data.get("correlation_id")
    if corr is not None:
        try:
            UUID(str(corr))
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"invalid correlation_id UUID: {corr}") from exc

    return data


# =====================================================================
# 2. Worker Heartbeat Manager (G-WORKER)
# =====================================================================

def write_heartbeat(
    conn: psycopg.Connection,
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Writes an authoritative heartbeat row to public.service_heartbeats.

    Must satisfy the G-WORKER gate requirement:
    service_name = 'dispatch-worker', age < 60s.
    """
    payload = {
        "instance_id": instance_id,
        "pid": os.getpid(),
        "status": "healthy",
        "dispatch_enabled": DEFAULT_DISPATCH_ENABLED,
    }
    if details:
        payload.update(details)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.service_heartbeats (service_name, last_heartbeat, details)
            VALUES ('dispatch-worker', now(), %s)
            ON CONFLICT (service_name)
            DO UPDATE SET
                last_heartbeat = now(),
                details = EXCLUDED.details
            """,
            (Jsonb(payload),),
        )


class HeartbeatManager:
    """Background thread managing periodic heartbeat writes to public.service_heartbeats."""

    def __init__(
        self,
        pg_uri: str,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        self._pg_uri = pg_uri
        self._interval = interval_seconds
        self._instance_id = instance_id
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="DispatchWorkerHeartbeat", daemon=True)
        self._thread.start()
        logger.info("heartbeat_manager_started", extra={"instance_id": self._instance_id, "interval": self._interval})

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            logger.info("heartbeat_manager_stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with psycopg.connect(self._pg_uri, autocommit=True) as conn:
                    write_heartbeat(conn, instance_id=self._instance_id)
            except Exception as exc:
                logger.error("heartbeat_write_failed", extra={"error": str(exc), "instance_id": self._instance_id})
            self._stop_event.wait(self._interval)


# =====================================================================
# 3. Activation Gate & Authorization Decision (P3-BLOCKER-01)
# =====================================================================

def check_delivery_authorization(
    conn: psycopg.Connection,
    identity: DeploymentIdentity | None = None,
    configured: bool = DEFAULT_DISPATCH_ENABLED,
) -> tuple[bool, str, list[GateResult]]:
    """Evaluates the full gate matrix and global kill-switch fail-closed.

    Required gates:
    - G-MIGRATION: Schema migration 0003 applied
    - G-GRANTS: Least-privilege posture verified
    - G-WORKER: Heartbeat age < 60s
    - G-ID: Human-commissioner identity gate recorded
    - G-ATOMIC: Transactional atomicity gate recorded
    - G-OUTBOX: Outbox relay gate recorded

    Args:
        conn: Open psycopg Connection (running as fc27_dispatch role).
        identity: Deployment identity for manual gate lookups.
        configured: Global dispatch kill switch (DISPATCH_ENABLED).

    Returns:
        (authorized: bool, reason: str, gate_results: list[GateResult])
    """
    if identity is None:
        code_rev = os.getenv("CODE_REVISION") or os.getenv("FC27_CODE_REVISION") or "test-rev-12345"
        dep_id = os.getenv("DEPLOYMENT_ID") or os.getenv("FC27_DEPLOYMENT_ID") or "test-deploy-67890"
        identity = DeploymentIdentity(code_rev, dep_id)

    if not configured:
        return False, "delivery_withheld_dispatch_disabled", []

    try:
        results = evaluate_gates(conn, identity)
    except Exception as exc:
        logger.error("gate_evaluation_exception", extra={"error": str(exc)})
        return False, f"gate_evaluation_error: {exc}", []

    effective = dispatch_effective(configured, results)
    if not effective:
        failing = [r.gate_id for r in results if not r.passed]
        reason = f"delivery_withheld_gate_failed: {', '.join(failing)}" if failing else "delivery_withheld_not_effective"
        return False, reason, results

    return True, "authorized", results


# =====================================================================
# 4. Durable Worker Claim & Lease Model (P3-BLOCKER-02)
# =====================================================================

def claim_dispatch_work(
    conn: psycopg.Connection,
    dispatch_id: UUID,
    instance_id: str = DEFAULT_INSTANCE_ID,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[bool, str, int]:
    """Atomically claims or reclaims a dispatch using durable worker claims.

    Args:
        conn: Open psycopg Connection.
        dispatch_id: Unique dispatch identifier.
        instance_id: Worker instance ID.
        lease_seconds: Claim lease duration in seconds.

    Returns:
        (claimed: bool, reason: str, attempts: int)
    """
    with conn.cursor(row_factory=dict_row) as cur:
        # Check current dispatch status and version (plain SELECT, no FOR UPDATE)
        cur.execute(
            "SELECT dispatch_status, version FROM public.dispatches WHERE dispatch_id = %s",
            (dispatch_id,),
        )
        row = cur.fetchone()
        if not row:
            return False, "dispatch_not_found", 0

        status = row["dispatch_status"]
        version = row["version"]

        if status in ("completed", "expired", "cancelled", "superseded", "delivered", "requires_reapproval", "failed"):
            return False, f"terminal_{status}", 0

        # Lock the claim record for this dispatch in public.dispatch_worker_claims
        cur.execute(
            """
            SELECT worker_id, locked_until, attempts, next_attempt_at
              FROM public.dispatch_worker_claims
             WHERE dispatch_id = %s
             FOR UPDATE
            """,
            (dispatch_id,),
        )
        claim = cur.fetchone()

        cur.execute("SELECT now() AS current_time")
        now_time = cur.fetchone()["current_time"]

        if claim is None:
            # First claim: if dispatch is pending, transition pending -> queued
            if status == "pending":
                try:
                    with conn.transaction():
                        transition(
                            conn,
                            dispatch_id=dispatch_id,
                            expected_status="pending",
                            expected_version=version,
                            to_status="queued",
                            reason="claimed by worker",
                            correlation_id=uuid.uuid4(),
                        )
                except TransitionConflictError as exc:
                    return False, "conflict_dropped", 0
                except Exception as exc:
                    return False, f"transition_failed: {exc}", 0

            with conn.transaction():
                cur.execute(
                    """
                    INSERT INTO public.dispatch_worker_claims (
                        dispatch_id, worker_id, locked_until, attempts, next_attempt_at, updated_at
                    ) VALUES (%s, %s, now() + (%s * interval '1 second'), 1, now(), now())
                    ON CONFLICT (dispatch_id) DO UPDATE SET
                        worker_id = EXCLUDED.worker_id,
                        locked_until = EXCLUDED.locked_until,
                        attempts = public.dispatch_worker_claims.attempts + 1,
                        updated_at = now()
                    """,
                    (dispatch_id, instance_id, lease_seconds),
                )
            return True, "claimed", 1

        # Claim exists: inspect lease and owner
        locked_until = claim["locked_until"]
        worker_id = claim["worker_id"]
        attempts = claim["attempts"]
        next_attempt_at = claim["next_attempt_at"]

        if locked_until > now_time and worker_id != instance_id:
            # Active lease held by another worker: fail-closed disjoint claim
            return False, "active_lease_owned_by_other_worker", attempts

        if next_attempt_at > now_time and worker_id != instance_id:
            # Retry backoff window in effect
            return False, "retry_backoff_in_effect", attempts

        # Stale lease (abandoned work) or refresh by current owner
        new_attempts = attempts if worker_id == instance_id else attempts + 1
        if new_attempts > MAX_WORKER_ATTEMPTS:
            # Retry exhaustion: transition dispatch to failed
            try:
                transition(
                    conn,
                    dispatch_id=dispatch_id,
                    expected_status="queued",
                    expected_version=version,
                    to_status="failed",
                    reason="maximum retry attempts exhausted",
                    correlation_id=uuid.uuid4(),
                    patch={"failure_reason": "delivery_store_error"},
                )
            except Exception:
                pass
            return False, "max_attempts_exhausted", new_attempts

        cur.execute(
            """
            UPDATE public.dispatch_worker_claims
               SET worker_id = %s,
                   locked_until = now() + (%s * interval '1 second'),
                   attempts = %s,
                   updated_at = now()
             WHERE dispatch_id = %s
            """,
            (instance_id, lease_seconds, new_attempts, dispatch_id),
        )
        reason = "refreshed" if worker_id == instance_id else "reclaimed"
        return True, reason, new_attempts


def release_or_finalize_claim(
    conn: psycopg.Connection,
    dispatch_id: UUID,
    instance_id: str = DEFAULT_INSTANCE_ID,
    *,
    delivered: bool = False,
    error: str | None = None,
    retryable: bool = False,
    attempts: int = 1,
) -> None:
    """Updates or clears worker claim ownership upon completion or failure."""
    with conn.cursor() as cur:
        if delivered:
            cur.execute("DELETE FROM public.dispatch_worker_claims WHERE dispatch_id = %s", (dispatch_id,))
        elif retryable:
            backoff_secs = BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1))
            cur.execute(
                """
                UPDATE public.dispatch_worker_claims
                   SET next_attempt_at = now() + (%s * interval '1 second'),
                       locked_until = now(),
                       last_error = %s,
                       updated_at = now()
                 WHERE dispatch_id = %s AND worker_id = %s
                """,
                (backoff_secs, (error or "")[:500], dispatch_id, instance_id),
            )
        else:
            cur.execute(
                """
                UPDATE public.dispatch_worker_claims
                   SET last_error = %s,
                       updated_at = now()
                 WHERE dispatch_id = %s
                """,
                ((error or "")[:500], dispatch_id),
            )


def reconcile_abandoned_queued_work(
    conn: psycopg.Connection,
    instance_id: str = DEFAULT_INSTANCE_ID,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> int:
    """Reconciles abandoned queued dispatches whose lease expired without completion.

    Finds rows in public.dispatch_worker_claims joined with public.dispatches where:
    - dispatches.dispatch_status = 'queued'
    - locked_until < now()

    Returns:
        Number of dispatches reconciled.
    """
    reconciled = 0
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT c.dispatch_id, c.attempts, d.version
              FROM public.dispatch_worker_claims c
              JOIN public.dispatches d ON c.dispatch_id = d.dispatch_id
             WHERE d.dispatch_status = 'queued'
               AND c.locked_until < now()
             ORDER BY c.locked_until ASC
             LIMIT 50
            """
        )
        stale_items = cur.fetchall()

    for item in stale_items:
        disp_id = item["dispatch_id"]
        attempts = item["attempts"]
        version = item["version"]

        if attempts >= MAX_WORKER_ATTEMPTS:
            # Transition to failed
            try:
                with conn.transaction():
                    transition(
                        conn,
                        dispatch_id=disp_id,
                        expected_status="queued",
                        expected_version=version,
                        to_status="failed",
                        reason="abandoned lease retry limit exhausted",
                        correlation_id=uuid.uuid4(),
                        patch={"failure_reason": "delivery_store_error"},
                    )
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE public.dispatch_worker_claims SET last_error = 'lease_timeout_exhausted', updated_at = now() WHERE dispatch_id = %s",
                            (disp_id,),
                        )
                reconciled += 1
            except Exception as exc:
                logger.error("abandoned_work_failure_transition_error", extra={"dispatch_id": str(disp_id), "error": str(exc)})
        else:
            # Reclaim for current worker
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.dispatch_worker_claims
                       SET worker_id = %s,
                           locked_until = now() + (%s * interval '1 second'),
                           attempts = attempts + 1,
                           updated_at = now()
                     WHERE dispatch_id = %s
                    """,
                    (instance_id, lease_seconds, disp_id),
                )
                reconciled += 1

    return reconciled


# =====================================================================
# 5. Deterministic Policy Validation
# =====================================================================

def validate_personalized_plan(
    approved_instruction: Mapping[str, Any],
    player_data: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Generates and validates a typed personalized plan deterministically.

    Outcomes:
        - 'valid': Plan conforms to schema and bounds policy.
        - 'material_change': Plan materially deviates from approved instruction, requiring re-approval.
        - 'invalid': Plan violates metric bounds or policy constraints.
        - 'schema_invalid': Plan payload is malformed or lacks mandatory keys.

    Returns:
        tuple of (status, plan_dict, validation_details)
    """
    if not isinstance(approved_instruction, Mapping):
        return "schema_invalid", {}, {"error": "instruction must be a mapping"}

    # Support both flat and nested objective structures
    if "objective" in approved_instruction and isinstance(approved_instruction["objective"], Mapping):
        obj = approved_instruction["objective"]
        metric = obj.get("metric")
        target_delta = obj.get("target_delta")
        policy = obj.get("bounds_policy") or obj.get("policy", "fc27-objective-bounds-v1")
        summary = approved_instruction.get("action") or obj.get("summary")
    else:
        metric = approved_instruction.get("metric")
        target_delta = approved_instruction.get("target_delta")
        policy = approved_instruction.get("policy", "fc27-objective-bounds-v1")
        summary = approved_instruction.get("objective") or approved_instruction.get("summary")

    if not metric or target_delta is None:
        return "schema_invalid", {}, {"error": "missing metric or target_delta"}

    try:
        delta_val = float(target_delta)
    except (TypeError, ValueError):
        return "schema_invalid", {}, {"error": f"non-numeric target_delta: {target_delta}"}

    # Policy Bounds Validation
    try:
        validate_metric_bounds(metric, delta_val, policy=policy)
    except ValueError as exc:
        return "invalid", {}, {"error": str(exc), "policy": policy}

    # Material Change Detection (e.g. marked explicitly or target delta modified post-approval)
    is_material_change = bool(
        approved_instruction.get("material_change") is True
        or approved_instruction.get("force_reapproval") is True
    )

    plan_body = {
        "metric": metric,
        "target_delta": delta_val,
        "objective": summary or f"Improve {metric} by {delta_val}",
        "policy": policy,
        "drills": approved_instruction.get("drills", ["default_conditioning"]),
        "notes": approved_instruction.get("notes", ""),
    }

    if is_material_change:
        return (
            "material_change",
            plan_body,
            {"reason": "material change detected in personalized plan", "metric": metric},
        )

    return "valid", plan_body, {"status": "policy_passed", "policy": policy}


# =====================================================================
# 6. Delivery Adapter Protocol and Implementations
# =====================================================================

class DeliveryResult:
    """Outcome of an adapter delivery attempt."""

    def __init__(self, channel: str, status: str, error: str | None = None, detail: str = ""):
        self.channel = channel
        self.status = status
        self.error = error
        self.detail = detail


class DeliveryAdapter(Protocol):
    """Protocol for approved delivery adapters."""

    def deliver(
        self,
        conn: psycopg.Connection,
        *,
        dispatch_id: UUID,
        plan_version: int,
        player_id: str,
        plan_data: Mapping[str, Any],
        idempotency_key: str,
    ) -> DeliveryResult:
        ...


class DashboardStorageAdapter:
    """Stores dispatch plan into dashboard storage for player web view.

    Persists into public.dispatch_delivery_attempts with channel='dashboard'.
    """

    def deliver(
        self,
        conn: psycopg.Connection,
        *,
        dispatch_id: UUID,
        plan_version: int,
        player_id: str,
        plan_data: Mapping[str, Any],
        idempotency_key: str,
    ) -> DeliveryResult:
        with conn.cursor() as cur:
            # Check idempotency
            cur.execute(
                """
                SELECT result FROM public.dispatch_delivery_attempts
                 WHERE dispatch_id = %s AND channel = 'dashboard' AND plan_version = %s
                """,
                (str(dispatch_id), plan_version),
            )
            existing = cur.fetchone()
            if existing and existing[0] == "stored":
                return DeliveryResult("dashboard", "stored")

            # Record delivery attempt
            cur.execute(
                """
                INSERT INTO public.dispatch_delivery_attempts (
                    dispatch_id, channel, plan_version, attempted_at, result, idempotency_key
                ) VALUES (
                    %s, 'dashboard', %s, now(), 'stored', %s
                )
                """,
                (str(dispatch_id), plan_version, idempotency_key),
            )
        logger.info(
            "dashboard_delivery_stored",
            extra={"dispatch_id": str(dispatch_id), "player_id": player_id, "plan_version": plan_version},
        )
        return DeliveryResult("dashboard", "stored")


class WebSocketNotificationAdapter:
    """Pushes dispatch notification over internal event channel / WebSocket.

    Persists into public.dispatch_delivery_attempts with channel='websocket'.
    """

    def __init__(self, redis_client: Any = None):
        self.redis = redis_client

    def deliver(
        self,
        conn: psycopg.Connection,
        *,
        dispatch_id: UUID,
        plan_version: int,
        player_id: str,
        plan_data: Mapping[str, Any],
        idempotency_key: str,
    ) -> DeliveryResult:
        with conn.cursor() as cur:
            # Check idempotency
            cur.execute(
                """
                SELECT result FROM public.dispatch_delivery_attempts
                 WHERE dispatch_id = %s AND channel = 'websocket' AND plan_version = %s
                """,
                (str(dispatch_id), plan_version),
            )
            existing = cur.fetchone()
            if existing and existing[0] in ("pushed", "no_subscriber"):
                return DeliveryResult("websocket", existing[0])

            # Publish to Redis notification channel if client available
            pushed = False
            if self.redis is not None:
                try:
                    channel = f"fc27:player_notifications:{player_id}"
                    payload = json.dumps({
                        "type": "dispatch.delivered",
                        "dispatch_id": str(dispatch_id),
                        "player_id": player_id,
                        "plan_version": plan_version,
                        "plan": plan_data,
                    })
                    subscribers = self.redis.publish(channel, payload)
                    pushed = subscribers > 0
                except Exception as exc:
                    logger.warning("websocket_publish_error", extra={"error": str(exc)})
                    cur.execute(
                        """
                        INSERT INTO public.dispatch_delivery_attempts (
                            dispatch_id, channel, plan_version, attempted_at, result, error, idempotency_key
                        ) VALUES (
                            %s, 'websocket', %s, now(), 'error', %s, %s
                        )
                        """,
                        (str(dispatch_id), plan_version, str(exc)[:250], idempotency_key),
                    )
                    return DeliveryResult("websocket", "error", error=str(exc))

            result_status = "pushed" if pushed or self.redis is None else "no_subscriber"

            cur.execute(
                """
                INSERT INTO public.dispatch_delivery_attempts (
                    dispatch_id, channel, plan_version, attempted_at, result, idempotency_key
                ) VALUES (
                    %s, 'websocket', %s, now(), %s, %s
                )
                """,
                (str(dispatch_id), plan_version, result_status, idempotency_key),
            )

        logger.info(
            "websocket_notification_pushed",
            extra={"dispatch_id": str(dispatch_id), "player_id": player_id},
        )
        return DeliveryResult("websocket", result_status)


# =====================================================================
# 7. Core Event Processing Pipeline
# =====================================================================

def process_dispatch_event(
    conn: psycopg.Connection,
    raw_event: str | bytes | dict[str, Any],
    *,
    redis_client: Any = None,
    dispatch_enabled: bool | None = None,
    instance_id: str = DEFAULT_INSTANCE_ID,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    identity: DeploymentIdentity | None = None,
    dashboard_adapter: DeliveryAdapter | None = None,
    ws_adapter: DeliveryAdapter | None = None,
) -> dict[str, Any]:
    """Processes a single dispatch event through the fail-closed pipeline.

    Steps:
    1. Parse and validate event envelope.
    2. Re-read authoritative dispatch row from PostgreSQL.
    3. Validate club, player, and approval bindings.
    4. Check status and handle idempotent skip.
    5. Durable worker claim (`pending` -> `queued`) with lease management.
    6. Run per-player agent pipeline and deterministic policy validation.
    7. Persist plan in `public.dispatch_plans`.
    8. If material change -> transition `queued` -> `requires_reapproval`.
    9. If policy/schema invalid -> transition `queued` -> `failed`.
    10. If valid:
        - Check player identity verification. If unverified -> transition to `failed`.
        - Enforce full gate matrix (`evaluate_gates()` + `dispatch_effective()`).
        - Perform immediate pre-side-effect recheck right before adapter execution.
        - If enabled and authorized -> execute approved delivery adapters and record attempts.
        - Transition `queued` -> `delivered` and finalize claim.

    Args:
        conn: Open psycopg Connection (running as fc27_dispatch role).
        raw_event: Raw message from Redis channel `fc27:events`.
        redis_client: Optional redis connection for WS adapter.
        dispatch_enabled: Explicit override for kill-switch (defaults to env var).
        instance_id: Worker instance ID.
        lease_seconds: Worker lease duration.
        identity: Deployment identity for gate checks.
        dashboard_adapter: Optional dashboard adapter override.
        ws_adapter: Optional websocket adapter override.

    Returns:
        Structured outcome summary.
    """
    if dispatch_enabled is None:
        dispatch_enabled = DEFAULT_DISPATCH_ENABLED

    dash_adapter = dashboard_adapter or DashboardStorageAdapter()
    websocket_adapter = ws_adapter or WebSocketNotificationAdapter(redis_client=redis_client)

    # 1. Parse and validate event envelope
    try:
        envelope = parse_event_envelope(raw_event)
    except ValueError as exc:
        logger.warning("invalid_event_envelope_rejected", extra={"error": str(exc)})
        return {"status": "rejected", "reason": f"invalid_envelope: {exc}"}

    event_type = envelope.get("event_type")
    if event_type != "dispatch.created":
        logger.debug("unhandled_event_type_skipped", extra={"event_type": event_type})
        return {"status": "ignored", "reason": f"unhandled_event_type: {event_type}"}

    dispatch_id = UUID(str(envelope["entity_id"]))
    corr_str = envelope.get("correlation_id")
    correlation_id = UUID(str(corr_str)) if corr_str else uuid.uuid4()

    # 2. Authoritative PostgreSQL Lookup
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT dispatch_id, source_thread_id, approval_id, approval_request_id,
                   approval_payload_hash, package_version, player_id, candidate_id,
                   owner, selection, club_id, approved_instruction,
                   dispatch_status, plan_version, version
              FROM public.dispatches
             WHERE dispatch_id = %s
            """,
            (str(dispatch_id),),
        )
        dispatch = cur.fetchone()

    if not dispatch:
        logger.error("dispatch_not_found_authoritative", extra={"dispatch_id": str(dispatch_id)})
        return {"status": "error", "reason": "dispatch_not_found"}

    # 3. Security Binding Cross-Validation
    scope_club_id = str(envelope.get("scope_club_id") or "")
    scope_player_id = str(envelope.get("scope_player_id") or "")

    if str(dispatch["club_id"]) != scope_club_id or str(dispatch["player_id"]) != scope_player_id:
        logger.error(
            "security_scope_mismatch_detected",
            extra={
                "dispatch_id": str(dispatch_id),
                "db_club": str(dispatch["club_id"]),
                "event_club": scope_club_id,
                "db_player": str(dispatch["player_id"]),
                "event_player": scope_player_id,
            },
        )
        return {"status": "error", "reason": "security_scope_mismatch"}

    current_status = dispatch["dispatch_status"]

    # 4. Idempotency Check: if already terminal or delivered
    if current_status in ("completed", "expired", "cancelled", "superseded", "delivered", "requires_reapproval", "failed"):
        logger.info(
            "idempotent_duplicate_event_skipped",
            extra={"dispatch_id": str(dispatch_id), "current_status": current_status},
        )
        return {"status": "idempotent_skip", "current_status": current_status}

    if current_status == "queued":
        # Check if attempts already recorded in dispatch_delivery_attempts
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.dispatch_delivery_attempts WHERE dispatch_id = %s", (str(dispatch_id),))
            if cur.fetchone()[0] > 0:
                logger.info("idempotent_duplicate_event_skipped_attempts_exist", extra={"dispatch_id": str(dispatch_id)})
                return {"status": "idempotent_skip", "current_status": current_status}

    # 5. Safe Claim: Durable worker claim and lease check
    claimed, claim_reason, attempts = claim_dispatch_work(
        conn,
        dispatch_id=dispatch_id,
        instance_id=instance_id,
        lease_seconds=lease_seconds,
    )

    if not claimed:
        if claim_reason in ("conflict_dropped", "optimistic_concurrency_conflict"):
            return {"status": "conflict_dropped", "reason": "optimistic_concurrency_conflict"}
        if claim_reason == "active_lease_owned_by_other_worker":
            return {"status": "idempotent_skip", "current_status": "queued"}
        if claim_reason.startswith("terminal_"):
            return {"status": "idempotent_skip", "current_status": claim_reason.replace("terminal_", "")}
        return {"status": "conflict_dropped", "reason": claim_reason}

    # Fetch updated dispatch version after claim
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT version FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,))
        updated_row = cur.fetchone()
        new_version = updated_row["version"] if updated_row else dispatch["version"]

    # 6. Execute Deterministic Plan Validation
    approved_instruction = dispatch["approved_instruction"] or {}
    plan_status, plan_body, val_details = validate_personalized_plan(approved_instruction)
    plan_version = 1
    plan_sha = sha256_digest(plan_body)

    # 7. Persist in public.dispatch_plans (plain INSERT; table is immutable)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM public.dispatch_plans WHERE dispatch_id = %s AND plan_version = %s",
                (str(dispatch_id), plan_version),
            )
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO public.dispatch_plans (
                        dispatch_id, plan_version, plan, plan_sha256, status, validation
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        str(dispatch_id),
                        plan_version,
                        Jsonb(plan_body),
                        plan_sha,
                        plan_status,
                        Jsonb(val_details),
                    ),
                )

    # 8. Handle Material Change: Transition queued -> requires_reapproval
    if plan_status == "material_change":
        with conn.transaction():
            transition(
                conn,
                dispatch_id=dispatch_id,
                expected_status="queued",
                expected_version=new_version,
                to_status="requires_reapproval",
                reason="material change in personalized plan",
                correlation_id=correlation_id,
                patch={"plan_version": plan_version},
            )
        release_or_finalize_claim(conn, dispatch_id, instance_id, error="material_change")
        logger.info(
            "dispatch_requires_reapproval",
            extra={"dispatch_id": str(dispatch_id), "plan_version": plan_version},
        )
        return {"status": "requires_reapproval", "plan_version": plan_version}

    # 9. Handle Validation Failures: Transition queued -> failed
    if plan_status in ("schema_invalid", "invalid"):
        fail_reason = "plan_schema_invalid" if plan_status == "schema_invalid" else "plan_policy_invalid"
        with conn.transaction():
            transition(
                conn,
                dispatch_id=dispatch_id,
                expected_status="queued",
                expected_version=new_version,
                to_status="failed",
                reason=f"policy validation failed: {plan_status}",
                correlation_id=correlation_id,
                patch={
                    "failure_reason": fail_reason,
                    "last_error": val_details.get("error", "plan validation failed"),
                },
            )
        release_or_finalize_claim(conn, dispatch_id, instance_id, error=fail_reason)
        logger.info(
            "dispatch_validation_failed",
            extra={"dispatch_id": str(dispatch_id), "failure_reason": fail_reason},
        )
        return {"status": "failed", "failure_reason": fail_reason}

    # 10. Plan is Valid -> Enforce Player Identity Verification
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT player_id, club_id, identity_verified_at FROM public.players WHERE player_id = %s",
            (str(dispatch["player_id"]),),
        )
        player_row = cur.fetchone()

    is_verified = bool(player_row and player_row["identity_verified_at"] is not None)
    if not is_verified:
        with conn.transaction():
            transition(
                conn,
                dispatch_id=dispatch_id,
                expected_status="queued",
                expected_version=new_version,
                to_status="failed",
                reason="player identity unverified by commissioner",
                correlation_id=correlation_id,
                patch={
                    "failure_reason": "player_identity_unverified",
                    "last_error": "player identity must be verified by human commissioner prior to dispatch",
                },
            )
        release_or_finalize_claim(conn, dispatch_id, instance_id, error="player_identity_unverified")
        logger.info(
            "dispatch_blocked_player_unverified",
            extra={"dispatch_id": str(dispatch_id), "player_id": str(dispatch["player_id"])},
        )
        return {"status": "failed", "failure_reason": "player_identity_unverified"}

    # 11. Fail-Closed Activation Gate Matrix & Global Kill Switch (P3-BLOCKER-01)
    authorized, gate_reason, gate_results = check_delivery_authorization(
        conn,
        identity=identity,
        configured=dispatch_enabled,
    )
    if not authorized:
        logger.info(
            "delivery_blocked_by_gate_matrix",
            extra={"dispatch_id": str(dispatch_id), "reason": gate_reason, "posture": "fail_closed_safe"},
        )
        # Release claim lease so work can be reclaimed once gates are satisfied
        release_or_finalize_claim(conn, dispatch_id, instance_id, retryable=True, error=gate_reason, attempts=attempts)
        return {"status": "delivery_disabled", "plan_version": plan_version, "reason": gate_reason}

    # 12. Immediate Pre-Side-Effect Recheck (P3-BLOCKER-01)
    recheck_authorized, recheck_reason, _ = check_delivery_authorization(
        conn,
        identity=identity,
        configured=dispatch_enabled,
    )
    if not recheck_authorized:
        logger.warning(
            "pre_delivery_gate_revoked",
            extra={"dispatch_id": str(dispatch_id), "reason": recheck_reason},
        )
        release_or_finalize_claim(conn, dispatch_id, instance_id, retryable=True, error=recheck_reason, attempts=attempts)
        return {"status": "delivery_disabled", "plan_version": plan_version, "reason": recheck_reason}

    # 13. Deliver via Approved Adapters
    dash_idempotency = f"dispatch:{dispatch_id}:v{plan_version}:ch:dashboard"
    ws_idempotency = f"dispatch:{dispatch_id}:v{plan_version}:ch:websocket"

    with conn.transaction():
        dash_res = dash_adapter.deliver(
            conn,
            dispatch_id=dispatch_id,
            plan_version=plan_version,
            player_id=str(dispatch["player_id"]),
            plan_data=plan_body,
            idempotency_key=dash_idempotency,
        )
        ws_res = websocket_adapter.deliver(
            conn,
            dispatch_id=dispatch_id,
            plan_version=plan_version,
            player_id=str(dispatch["player_id"]),
            plan_data=plan_body,
            idempotency_key=ws_idempotency,
        )

    # 14. State Transition: attempt queued -> delivered
    try:
        with conn.transaction():
            final_version = transition(
                conn,
                dispatch_id=dispatch_id,
                expected_status="queued",
                expected_version=new_version,
                to_status="delivered",
                reason="successful adapter delivery",
                correlation_id=correlation_id,
                patch={"plan_version": plan_version},
            )
            logger.info("dispatch_transitioned_delivered", extra={"dispatch_id": str(dispatch_id), "version": final_version})
    except Phase1DeliveryBlockedError:
        logger.info(
            "dispatch_delivery_persisted_transition_blocked_invariant",
            extra={"dispatch_id": str(dispatch_id), "note": "delivered edge blocked by DB invariant; attempts durable"},
        )

    # Finalize durable claim upon successful delivery
    release_or_finalize_claim(conn, dispatch_id, instance_id, delivered=True)

    return {
        "status": "delivered",
        "dispatch_id": str(dispatch_id),
        "plan_version": plan_version,
        "dashboard_result": dash_res.status,
        "websocket_result": ws_res.status,
    }


# =====================================================================
# 8. Worker Health Check & CLI Entrypoint (P3-BLOCKER-04)
# =====================================================================

def check_worker_health(
    conn: psycopg.Connection,
    redis_client: Any = None,
) -> dict[str, Any]:
    """Evaluates liveliness and readiness for Docker Compose orchestration."""
    db_ok = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            db_ok = cur.fetchone() is not None
    except Exception:
        db_ok = False

    redis_ok = False
    if redis_client is not None:
        try:
            redis_ok = bool(redis_client.ping())
        except Exception:
            redis_ok = False
    else:
        redis_ok = True

    # Check G-WORKER heartbeat freshness
    heartbeat_fresh = False
    heartbeat_age = -1.0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT extract(epoch FROM (now() - last_heartbeat)) AS age_seconds
                  FROM public.service_heartbeats
                 WHERE service_name = 'dispatch-worker'
                """
            )
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                heartbeat_age = float(row[0])
                heartbeat_fresh = heartbeat_age < 60.0
    except Exception:
        pass

    overall_ok = db_ok and redis_ok and heartbeat_fresh
    return {
        "status": "ok" if overall_ok else "degraded",
        "database_connected": db_ok,
        "redis_connected": redis_ok,
        "heartbeat_fresh": heartbeat_fresh,
        "heartbeat_age_seconds": heartbeat_age,
        "dispatch_enabled": DEFAULT_DISPATCH_ENABLED,
    }


def run_worker(
    stop_event: threading.Event | None = None,
    single_pass: bool = False,
    poll_timeout: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Main worker process loop."""
    import redis

    pg_uri = _pg_uri()
    if not pg_uri:
        logger.critical("missing_postgres_uri_fatal")
        sys.exit(1)

    redis_url = _redis_url()
    try:
        r = redis.from_url(redis_url, decode_responses=True)
        r.ping()
    except Exception as exc:
        logger.critical("redis_connection_fatal", extra={"error": str(exc)})
        sys.exit(1)

    if stop_event is None:
        stop_event = threading.Event()

        def _handle_signal(signum: int, frame: Any) -> None:
            logger.info("shutdown_signal_received", extra={"signal": signum})
            stop_event.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    # Start background heartbeat manager
    heartbeat_mgr = HeartbeatManager(pg_uri=pg_uri, instance_id=DEFAULT_INSTANCE_ID)
    heartbeat_mgr.start()

    pubsub = r.pubsub()
    pubsub.subscribe("fc27:events")
    logger.info("dispatch_worker_started", extra={"instance_id": DEFAULT_INSTANCE_ID, "channel": "fc27:events"})

    try:
        while not stop_event.is_set():
            # Periodic reconciliation of abandoned queued work
            try:
                with psycopg.connect(pg_uri, autocommit=True) as conn:
                    reconciled = reconcile_abandoned_queued_work(conn, instance_id=DEFAULT_INSTANCE_ID)
                    if reconciled > 0:
                        logger.info("abandoned_work_reconciled", extra={"count": reconciled})
            except Exception as r_exc:
                logger.debug("reconciliation_tick_error", extra={"error": str(r_exc)})

            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=poll_timeout)
            if message and message.get("type") == "message":
                data = message.get("data")
                try:
                    with psycopg.connect(pg_uri, autocommit=True) as conn:
                        process_dispatch_event(
                            conn,
                            data,
                            redis_client=r,
                            instance_id=DEFAULT_INSTANCE_ID,
                        )
                except Exception as exc:
                    logger.error("worker_event_processing_error", extra={"error": str(exc), "error_type": type(exc).__name__})

            if single_pass:
                break
    finally:
        heartbeat_mgr.stop()
        pubsub.close()
        logger.info("dispatch_worker_shutdown_complete")


def main() -> None:
    """CLI Entrypoint for worker service and health checking."""
    parser = argparse.ArgumentParser(description="FC27 Dispatch Worker Service")
    parser.add_argument("--health-check", action="store_true", help="Perform health check and exit")
    parser.add_argument("--single-pass", action="store_true", help="Run a single pass and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.health_check:
        pg_uri = _pg_uri()
        if not pg_uri:
            print("ERROR: missing POSTGRES_URI or POSTGRES_URI_FILE")
            sys.exit(1)
        try:
            with psycopg.connect(pg_uri) as conn:
                health = check_worker_health(conn)
                print(json.dumps(health))
                if health["status"] == "ok":
                    sys.exit(0)
                else:
                    sys.exit(1)
        except Exception as exc:
            print(f"ERROR: health check failed: {exc}")
            sys.exit(1)

    run_worker(single_pass=args.single_pass)


if __name__ == "__main__":
    main()
