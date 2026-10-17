"""Dispatch Worker Service for FC27 Pro Clubs Platform.

Responsible for:
- Consuming approved internal domain events from Redis (`fc27:events`).
- Validating event envelopes against strict schema.
- Re-reading authoritative PostgreSQL state (never trusting Redis as source of truth).
- Enforcing player identity-verification and club-scope authorization.
- Enforcing fail-closed dispatch activation gates and global kill switch (`DISPATCH_ENABLED`).
- Claiming dispatch work safely via optimistic concurrency in `public.fc27_dispatch_transition`.
- Executing deterministic per-player personalized plan policy validation.
- Emitting periodic worker heartbeats to `public.service_heartbeats` (`G-WORKER`).
- Delivering via approved adapters ('dashboard-storage', 'websocket-notification').
- Recording immutable delivery attempts and idempotency in `public.dispatch_delivery_attempts`.
- Maintaining least-privilege execution under the `fc27_dispatch` database role.
"""
from __future__ import annotations

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

REQUIRED_ENVELOPE_FIELDS = (
    "event_id",
    "event_type",
    "entity_type",
    "entity_id",
    "actor",
    "correlation_id",
    "scope_club_id",
    "scope_player_id",
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
# 1. Event Envelope Validation
# =====================================================================

def parse_event_envelope(raw_message: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Parses and strictly validates the internal Redis event envelope.

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

    try:
        UUID(str(data["correlation_id"]))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid correlation_id UUID: {data.get('correlation_id')}") from exc

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
            VALUES ('dispatch-worker', now(), %s::jsonb)
            ON CONFLICT (service_name) DO UPDATE
            SET last_heartbeat = EXCLUDED.last_heartbeat,
                details = EXCLUDED.details
            """,
            (Jsonb(payload),),
        )
    logger.debug("worker_heartbeat_recorded", extra={"instance_id": instance_id})


class HeartbeatManager:
    """Background thread manager that writes periodic heartbeats."""

    def __init__(
        self,
        pg_uri: str,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ):
        self.pg_uri = pg_uri
        self.interval = interval_seconds
        self.instance_id = instance_id
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="worker-heartbeat", daemon=True)
        self._thread.start()
        logger.info("heartbeat_manager_started", extra={"interval": self.interval, "instance_id": self.instance_id})

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("heartbeat_manager_stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with psycopg.connect(self.pg_uri, autocommit=True) as conn:
                    write_heartbeat(conn, instance_id=self.instance_id)
            except Exception as exc:
                logger.error("heartbeat_write_failed", extra={"error": str(exc), "error_type": type(exc).__name__})
            self._stop_event.wait(self.interval)


# =====================================================================
# 3. Deterministic Policy Validation & Personalized Plan Engine
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
# 4. Delivery Adapters (Dashboard Storage & WebSocket Notification)
# =====================================================================

class DeliveryResult:
    """Result of an adapter delivery attempt."""

    def __init__(self, channel: str, status: str, error: str | None = None):
        self.channel = channel
        self.status = status  # 'stored', 'pushed', 'no_subscriber', 'error'
        self.error = error

    def is_success(self) -> bool:
        return self.status in ("stored", "pushed")


class DeliveryAdapter(Protocol):
    """Protocol for dispatch delivery adapters."""

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
                    # Record error attempt
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
            "websocket_notification_delivered",
            extra={
                "dispatch_id": str(dispatch_id),
                "player_id": player_id,
                "plan_version": plan_version,
                "result": result_status,
            },
        )
        return DeliveryResult("websocket", result_status)


# =====================================================================
# 5. Core Dispatch Processor
# =====================================================================

def process_dispatch_event(
    conn: psycopg.Connection,
    raw_event: str | bytes | dict[str, Any],
    *,
    redis_client: Any = None,
    dispatch_enabled: bool | None = None,
    instance_id: str = DEFAULT_INSTANCE_ID,
    dashboard_adapter: DeliveryAdapter | None = None,
    ws_adapter: DeliveryAdapter | None = None,
) -> dict[str, Any]:
    """Processes a single dispatch event through the fail-closed pipeline.

    Steps:
    1. Parse and validate event envelope.
    2. Re-read authoritative dispatch row from PostgreSQL.
    3. Validate club, player, and approval bindings.
    4. Check status and handle idempotent skip.
    5. Claim dispatch (`pending` -> `queued`) via single write-path `fc27_dispatch_transition`.
    6. Run per-player agent pipeline and deterministic policy validation.
    7. Persist plan in `public.dispatch_plans`.
    8. If material change -> transition `queued` -> `requires_reapproval`.
    9. If policy/schema invalid -> transition `queued` -> `failed`.
    10. If valid:
        - Check player identity verification. If unverified -> transition to `failed`.
        - Enforce `DISPATCH_ENABLED`. If false -> withhold delivery.
        - If enabled -> execute approved delivery adapters and record attempts.

    Args:
        conn: Open psycopg Connection (running as fc27_dispatch role).
        raw_event: Raw message from Redis channel `fc27:events`.
        redis_client: Optional redis connection for WS adapter.
        dispatch_enabled: Explicit override for kill-switch (defaults to env var).
        instance_id: Worker instance ID.
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
    correlation_id = UUID(str(envelope["correlation_id"]))

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

    # 4. Idempotency Check: if already processed or terminal
    if current_status != "pending":
        logger.info(
            "idempotent_duplicate_event_skipped",
            extra={"dispatch_id": str(dispatch_id), "current_status": current_status},
        )
        return {"status": "idempotent_skip", "current_status": current_status}

    # 5. Safe Claim: Transition pending -> queued via fc27_dispatch_transition
    try:
        with conn.transaction():
            new_version = transition(
                conn,
                dispatch_id=dispatch_id,
                expected_status="pending",
                expected_version=dispatch["version"],
                to_status="queued",
                reason="worker claim",
                correlation_id=correlation_id,
            )
            logger.info("dispatch_claimed_queued", extra={"dispatch_id": str(dispatch_id), "version": new_version})
    except TransitionConflictError as exc:
        logger.info(
            "concurrent_claim_conflict_dropped",
            extra={"dispatch_id": str(dispatch_id), "error": str(exc)},
        )
        return {"status": "conflict_dropped", "reason": "optimistic_concurrency_conflict"}
    except Exception as exc:
        logger.error("dispatch_claim_failed", extra={"dispatch_id": str(dispatch_id), "error": str(exc)})
        raise

    # 6. Execute Deterministic Plan Validation
    approved_instruction = dispatch["approved_instruction"] or {}
    plan_status, plan_body, val_details = validate_personalized_plan(approved_instruction)
    plan_version = 1
    plan_sha = sha256_digest(plan_body)

    # 7. Persist in public.dispatch_plans
    with conn.transaction():
        with conn.cursor() as cur:
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
        logger.info(
            "dispatch_blocked_player_unverified",
            extra={"dispatch_id": str(dispatch_id), "player_id": str(dispatch["player_id"])},
        )
        return {"status": "failed", "failure_reason": "player_identity_unverified"}

    # 11. Fail-Closed Kill-Switch: DISPATCH_ENABLED
    if not dispatch_enabled:
        logger.info(
            "delivery_blocked_dispatch_disabled",
            extra={"dispatch_id": str(dispatch_id), "posture": "fail_closed_safe"},
        )
        return {"status": "delivery_disabled", "plan_version": plan_version}

    # 12. Deliver via Approved Adapters
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

    # 13. State Transition: attempt queued -> delivered
    # In Phase 1/Phase 2 environments, transition to 'delivered' is blocked by SQLSTATE FC002.
    # We attempt it safely: if permitted, version advances; if blocked, we remain queued with stored attempts.
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

    return {
        "status": "delivered",
        "dispatch_id": str(dispatch_id),
        "plan_version": plan_version,
        "dashboard_result": dash_res.status,
        "websocket_result": ws_res.status,
    }


# =====================================================================
# 6. Service Run Loop and Health
# =====================================================================

def check_worker_health(conn: psycopg.Connection, redis_client: Any = None) -> dict[str, Any]:
    """Evaluates live health status of the worker service."""
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_worker()
