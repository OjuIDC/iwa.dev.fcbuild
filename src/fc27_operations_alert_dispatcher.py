"""Destination-aware Discord operations alert dispatcher for FC27.

Dispatches queued operator alert delivery jobs to Discord channels
registered with `message_kind = 'OPERATIONS_ALERT'`.
Guarantees:
- Destination-aware dispatch strictly bound to symbolic destination keys.
- Durable idempotency via PostgreSQL stored procedures.
- Send-and-fetch verification ensuring authorship, channel, and non-sensitive marker tags match.
- Ambiguous-send recovery preventing duplicate notifications across retries.
- Bounded retry with dead-lettering.
- Zero EA Clubs provider calls and zero changes to Match Results domain tables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from src import settings
from src.fc27_match_results import (
    open_guarded_match_results_writer_connection,
)
from src.fc27_operations_alerts import (
    build_operations_alert_marker,
    format_operations_alert_message,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FC27OperationsAlertDispatchConfig:
    """Runtime configuration for FC27 operations alert dispatch."""

    enabled: bool = False
    destination_key: str = ""
    interval_seconds: int = 30
    batch_size: int = 5
    max_attempts: int = 3
    lease_seconds: int = 300
    claim_owner: str = ""

    @classmethod
    def from_env(cls) -> FC27OperationsAlertDispatchConfig:
        """Construct configuration from environment and settings helpers."""
        enabled = settings.is_fc27_operations_alert_dispatch_enabled(strict=False)
        dest_key = settings.get_fc27_operations_alert_destination(strict=False)
        interval = settings.get_fc27_operations_alert_dispatch_interval_seconds(strict=False)
        batch = settings.get_fc27_operations_alert_dispatch_batch_size(strict=False)
        max_att = settings.get_fc27_operations_alert_dispatch_max_attempts(strict=False)
        owner = f"{socket.gethostname()}-{os.getpid()}"
        return cls(
            enabled=enabled,
            destination_key=dest_key,
            interval_seconds=interval,
            batch_size=batch,
            max_attempts=max_att,
            lease_seconds=300,
            claim_owner=owner,
        )


@dataclass(frozen=True)
class FC27ClaimedAlertDelivery:
    """Represents a claimed operator alert delivery row."""

    delivery_id: int
    alert_id: str
    lifecycle_event: str
    destination_key: str
    attempt_count: int
    marker_digest: str
    guild_id: str
    channel_id: str
    alert_type: str
    component: str
    severity: str
    summary: str
    details: Mapping[str, Any]
    opened_at: datetime | None
    resolved_at: datetime | None
    notification_count: int


@dataclass
class FC27AlertDispatchCycleResult:
    """Summary of operations alert dispatch cycle execution."""

    claimed_count: int = 0
    delivered_count: int = 0
    reconciled_count: int = 0
    failed_count: int = 0
    dead_letter_count: int = 0
    skipped_reason: str | None = None


def claim_operator_alert_deliveries(
    conn: psycopg.Connection[Any],
    destination_key: str,
    claim_owner: str,
    batch_size: int = 5,
    lease_seconds: int = 300,
    max_attempts: int = 3,
) -> list[FC27ClaimedAlertDelivery]:
    """Claim a batch of pending or retryable alert deliveries via stored procedure."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT delivery_id, alert_id, lifecycle_event, destination_key,
                   attempt_count, marker_digest, guild_id, channel_id,
                   alert_type, component, severity, summary, details,
                   opened_at, resolved_at, notification_count
              FROM public.fc27_claim_operator_alert_deliveries(
                  %s, %s, %s, %s, %s
              );
            """,
            (destination_key, claim_owner, batch_size, lease_seconds, max_attempts),
        )
        rows = cur.fetchall()
        deliveries: list[FC27ClaimedAlertDelivery] = []
        for r in rows:
            raw_details = r[12]
            if isinstance(raw_details, str):
                details_dict = json.loads(raw_details)
            elif isinstance(raw_details, dict):
                details_dict = raw_details
            else:
                details_dict = {}
            deliveries.append(
                FC27ClaimedAlertDelivery(
                    delivery_id=int(r[0]),
                    alert_id=str(r[1]),
                    lifecycle_event=str(r[2]),
                    destination_key=str(r[3]),
                    attempt_count=int(r[4]),
                    marker_digest=str(r[5]),
                    guild_id=str(r[6]),
                    channel_id=str(r[7]),
                    alert_type=str(r[8]),
                    component=str(r[9]),
                    severity=str(r[10]),
                    summary=str(r[11]),
                    details=details_dict,
                    opened_at=r[13],
                    resolved_at=r[14],
                    notification_count=int(r[15]),
                )
            )
        return deliveries


def finalize_operator_alert_delivery(
    conn: psycopg.Connection[Any],
    delivery_id: int,
    claim_owner: str,
    discord_message_id: str,
    discord_channel_id: str,
) -> bool:
    """Mark an alert delivery as DELIVERED."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT public.fc27_finalize_operator_alert_delivery(%s, %s, %s, %s);
            """,
            (delivery_id, claim_owner, discord_message_id, discord_channel_id),
        )
        row = cur.fetchone()
        return bool(row[0]) if row else False


def fail_operator_alert_delivery(
    conn: psycopg.Connection[Any],
    delivery_id: int,
    claim_owner: str,
    error_category: str,
    max_attempts: int = 3,
) -> str:
    """Record delivery failure or transition to DEAD_LETTER."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT public.fc27_fail_operator_alert_delivery(%s, %s, %s, %s);
            """,
            (delivery_id, claim_owner, error_category, max_attempts),
        )
        row = cur.fetchone()
        return str(row[0]) if row else "FAILED"


def reconcile_operator_alert_delivery_by_marker(
    conn: psycopg.Connection[Any],
    destination_key: str,
    marker_digest: str,
    discord_message_id: str,
    discord_channel_id: str,
) -> tuple[bool, int | None, str | None]:
    """Reconcile a previously delivered message by its marker digest without resending."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT reconciled, delivery_id, delivery_state
              FROM public.fc27_reconcile_operator_alert_delivery_by_marker(%s, %s, %s, %s);
            """,
            (destination_key, marker_digest, discord_message_id, discord_channel_id),
        )
        row = cur.fetchone()
        if not row:
            return False, None, None
        return bool(row[0]), int(row[1]) if row[1] is not None else None, str(row[2]) if row[2] is not None else None


def get_operator_alert_delivery_summary(
    conn: psycopg.Connection[Any],
) -> dict[str, int]:
    """Retrieve counts of operator alert deliveries across all states."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pending_count, claimed_count, delivered_count, failed_count, dead_letter_count
              FROM public.fc27_get_operator_alert_delivery_summary();
            """
        )
        row = cur.fetchone()
        if not row:
            return {"pending": 0, "claimed": 0, "delivered": 0, "failed": 0, "dead_letter": 0}
        return {
            "pending": int(row[0]),
            "claimed": int(row[1]),
            "delivered": int(row[2]),
            "failed": int(row[3]),
            "dead_letter": int(row[4]),
        }


def _verify_snapshot_matches(
    snapshot: Any,
    expected_channel_id: str,
    expected_marker_tag: str,
) -> bool:
    """Verify that a fetched message snapshot matches expected channel, bot authorship, and marker."""
    if snapshot is None:
        return False
    if not getattr(snapshot, "is_bot_authored", False):
        return False
    if str(getattr(snapshot, "channel_id", "")) != str(expected_channel_id):
        return False
    content = getattr(snapshot, "content", "") or ""
    return expected_marker_tag in content


def run_operations_alert_dispatch_cycle(
    config: FC27OperationsAlertDispatchConfig,
    transport: Any,
    conn_or_uri: psycopg.Connection[Any] | str,
    *,
    strict_live_check: bool = False,
) -> FC27AlertDispatchCycleResult:
    """Execute a single bounded operations alert dispatch cycle.

    Steps:
    1. Check config enabled; fail closed if disabled.
    2. Check symbolic destination key; reject numeric snowflake IDs.
    3. Verify transport capabilities.
    4. Claim batch of pending deliveries using FOR UPDATE SKIP LOCKED.
    5. For each delivery:
       a. Build marker tag and verify marker digest.
       b. Check for existing message with discriminator tag to prevent duplicate send.
       c. If found and verified, reconcile without resending.
       d. If not found, format and post message.
       e. Fetch snapshot and verify authorship, channel, and marker tag.
       f. If verified, finalize delivery as DELIVERED.
       g. If ambiguous or failed, handle recovery or fail/dead-letter.
    """
    if not config.enabled:
        return FC27AlertDispatchCycleResult(skipped_reason="DISPATCH_DISABLED")

    dest_key = (config.destination_key or "").strip()
    if not dest_key:
        return FC27AlertDispatchCycleResult(skipped_reason="DESTINATION_KEY_EMPTY")
    if dest_key.isdecimal():
        return FC27AlertDispatchCycleResult(skipped_reason="DESTINATION_KEY_MUST_BE_SYMBOLIC")

    if not hasattr(transport, "post_text") or not hasattr(transport, "fetch_notification_snapshot"):
        return FC27AlertDispatchCycleResult(skipped_reason="TRANSPORT_NOT_CAPABLE")

    if strict_live_check and not getattr(transport, "is_live", False):
        return FC27AlertDispatchCycleResult(skipped_reason="LIVE_TRANSPORT_REQUIRED")

    result = FC27AlertDispatchCycleResult()

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=False
    ) as guarded:
        conn = getattr(guarded, "_conn", guarded)
        claim_owner = config.claim_owner or f"{socket.gethostname()}-{os.getpid()}"
        deliveries = claim_operator_alert_deliveries(
            conn,
            destination_key=dest_key,
            claim_owner=claim_owner,
            batch_size=config.batch_size,
            lease_seconds=config.lease_seconds,
            max_attempts=config.max_attempts,
        )
        result.claimed_count = len(deliveries)
        if not deliveries:
            return result

        find_fn = getattr(transport, "find_existing_text", None)

        for delivery in deliveries:
            # 1. Build marker tag and verify digest
            marker_tag, computed_digest = build_operations_alert_marker(
                alert_id=delivery.alert_id,
                lifecycle_event=delivery.lifecycle_event,
                alert_type=delivery.alert_type,
                component=delivery.component,
                destination_key=delivery.destination_key,
            )
            if computed_digest != delivery.marker_digest:
                fail_operator_alert_delivery(
                    conn,
                    delivery.delivery_id,
                    claim_owner,
                    "MARKER_DIGEST_MISMATCH",
                    config.max_attempts,
                )
                result.failed_count += 1
                continue

            # 2. Ambiguous send check / pre-send duplicate reconciliation
            existing_msg_id: str | None = None
            if callable(find_fn):
                try:
                    existing_msg_id = find_fn(
                        channel_id=delivery.channel_id,
                        content="",
                        discriminator=marker_tag,
                    )
                except Exception as ex:  # noqa: BLE001 - defensive fallback on transport search error
                    logger.warning("Error searching existing text: %s", ex)
                    existing_msg_id = None

            if existing_msg_id:
                snap = transport.fetch_notification_snapshot(
                    channel_id=delivery.channel_id,
                    message_id=str(existing_msg_id),
                )
                if _verify_snapshot_matches(snap, delivery.channel_id, marker_tag):
                    reconcile_operator_alert_delivery_by_marker(
                        conn,
                        delivery.destination_key,
                        delivery.marker_digest,
                        str(snap.message_id),
                        str(snap.channel_id),
                    )
                    result.reconciled_count += 1
                    continue

            # 3. Format message
            msg_content = format_operations_alert_message(
                alert_type=delivery.alert_type,
                component=delivery.component,
                severity=delivery.severity,
                summary=delivery.summary,
                lifecycle_event=delivery.lifecycle_event,
                opened_at=delivery.opened_at,
                resolved_at=delivery.resolved_at,
                marker_tag=marker_tag,
                details=delivery.details,
            )

            # 4. Post message
            posted_msg_id: str | None = None
            posted_chan_id: str | None = None
            try:
                posted_msg_id, posted_chan_id = transport.post_text(
                    channel_id=delivery.channel_id,
                    content=msg_content,
                )
            except Exception as post_exc:  # noqa: BLE001 - catch arbitrary transport/network errors for safe retry
                logger.warning(
                    "Error posting operations alert delivery %d: %s",
                    delivery.delivery_id,
                    post_exc,
                )
                # Recover if post actually succeeded despite exception
                if callable(find_fn):
                    try:
                        recovered_id = find_fn(
                            channel_id=delivery.channel_id,
                            content="",
                            discriminator=marker_tag,
                        )
                    except Exception:  # noqa: BLE001 - ignore search errors during recovery
                        recovered_id = None
                    if recovered_id:
                        snap = transport.fetch_notification_snapshot(
                            channel_id=delivery.channel_id,
                            message_id=str(recovered_id),
                        )
                        if _verify_snapshot_matches(snap, delivery.channel_id, marker_tag):
                            reconcile_operator_alert_delivery_by_marker(
                                conn,
                                delivery.destination_key,
                                delivery.marker_digest,
                                str(snap.message_id),
                                str(snap.channel_id),
                            )
                            result.reconciled_count += 1
                            continue

                err_cat = type(post_exc).__name__
                new_state = fail_operator_alert_delivery(
                    conn,
                    delivery.delivery_id,
                    claim_owner,
                    err_cat,
                    config.max_attempts,
                )
                if new_state == "DEAD_LETTER":
                    result.dead_letter_count += 1
                else:
                    result.failed_count += 1
                continue

            # 5. Send-and-fetch verification
            snap = transport.fetch_notification_snapshot(
                channel_id=str(posted_chan_id or delivery.channel_id),
                message_id=str(posted_msg_id),
            )
            if _verify_snapshot_matches(snap, delivery.channel_id, marker_tag):
                finalize_operator_alert_delivery(
                    conn,
                    delivery.delivery_id,
                    claim_owner,
                    str(snap.message_id),
                    str(snap.channel_id),
                )
                result.delivered_count += 1
            else:
                logger.warning(
                    "Post-send verification failed for delivery %d (msg_id: %s)",
                    delivery.delivery_id,
                    posted_msg_id,
                )
                new_state = fail_operator_alert_delivery(
                    conn,
                    delivery.delivery_id,
                    claim_owner,
                    "SNAPSHOT_VERIFICATION_FAILED",
                    config.max_attempts,
                )
                if new_state == "DEAD_LETTER":
                    result.dead_letter_count += 1
                else:
                    result.failed_count += 1

    return result


async def run_operations_alert_dispatcher_loop(
    config: FC27OperationsAlertDispatchConfig,
    transport: Any,
    conn_or_uri_factory: Callable[[], psycopg.Connection[Any] | str],
    stop_event: asyncio.Event,
) -> None:
    """Run recurring operations alert dispatch cycles until stop_event is set."""
    if not config.enabled:
        logger.info("Operations alert dispatch is disabled; loop exiting.")
        return

    logger.info(
        "Starting operations alert dispatcher loop (interval: %ds, destination: %s)",
        config.interval_seconds,
        config.destination_key,
    )

    while not stop_event.is_set():
        try:
            conn_or_uri = conn_or_uri_factory()
            cycle_result = await asyncio.to_thread(
                run_operations_alert_dispatch_cycle,
                config,
                transport,
                conn_or_uri,
            )
            if cycle_result.claimed_count > 0:
                logger.info(
                    "Alert dispatch cycle: claimed=%d, delivered=%d, reconciled=%d, failed=%d, dead_letter=%d",
                    cycle_result.claimed_count,
                    cycle_result.delivered_count,
                    cycle_result.reconciled_count,
                    cycle_result.failed_count,
                    cycle_result.dead_letter_count,
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Unexpected error in alert dispatch cycle")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(config.interval_seconds))
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            break

    logger.info("Operations alert dispatcher loop stopped.")
