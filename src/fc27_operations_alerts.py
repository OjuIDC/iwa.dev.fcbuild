"""Deduplicated durable alerting surface for FC27 operations.

Interacts with the durable public.fc27_operator_alerts table and stored procedures
to atomically record, deduplicate, suppress duplicate notifications for 30 minutes,
and resolve cleared alerts using the authoritative PostgreSQL database clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from src.fc27_operations_status import (
    FC27OperationsStatusSnapshot,
)

ALERT_STATE_ENGINE_IMPLEMENTED = "YES"
ALERT_DISPATCH_IMPLEMENTED = "YES"
OPERATIONS_ALERT_MESSAGE_KIND = "OPERATIONS_ALERT"
OPERATIONS_ALERT_TAG_PREFIX = "fc27-ops-"
OPERATIONS_ALERT_TAG_LENGTH = 25


def build_operations_alert_marker(
    alert_id: str,
    lifecycle_event: str,
    alert_type: str,
    component: str,
    destination_key: str,
) -> tuple[str, str]:
    """Build a deterministic, non-sensitive Discord marker tag and its DB digest.

    Marker tag format: 'fc27-ops-{sha256(canonical)[:16]}' (25 chars).
    Returns (marker_tag, marker_digest).
    """
    canonical = f"{alert_id}:{lifecycle_event}:{alert_type}:{component}:{destination_key}:{OPERATIONS_ALERT_MESSAGE_KIND}"
    tag_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    marker_tag = f"{OPERATIONS_ALERT_TAG_PREFIX}{tag_hash}"
    marker_digest = hashlib.sha256(marker_tag.encode("utf-8")).hexdigest()
    return marker_tag, marker_digest


def extract_operations_alert_tag(content: str) -> str | None:
    """Extract an operations alert tag from message text if present."""
    match = re.search(r"(?:^|\s)(fc27-ops-[0-9a-f]{16})(?:\s|$)", content)
    if match:
        return match.group(1)
    return None


def format_operations_alert_message(
    alert_type: str,
    component: str,
    severity: str,
    summary: str,
    lifecycle_event: str,
    opened_at: datetime | str | None = None,
    resolved_at: datetime | str | None = None,
    marker_tag: str | None = None,
) -> str:
    """Format an operator alert into clean, privacy-safe Discord markdown under 2000 characters."""
    event = lifecycle_event.upper()
    if event == "RESOLVED":
        header = f"🟢 **FC27 Operations Alert Resolved: {alert_type}**"
        lines = [
            header,
            f"**Component**: `{component}`",
            f"**Summary**: {summary}",
        ]
        if resolved_at:
            ts_str = resolved_at.isoformat() if isinstance(resolved_at, datetime) else str(resolved_at)
            lines.append(f"**Resolved**: `{ts_str}`")
    else:
        icon = "🔴" if severity.upper() == "CRITICAL" else "🟡"
        header = f"{icon} **FC27 Operations Alert: {alert_type}** (`{severity.upper()}`)"
        lines = [
            header,
            f"**Component**: `{component}`",
            f"**Summary**: {summary}",
        ]
        if opened_at:
            ts_str = opened_at.isoformat() if isinstance(opened_at, datetime) else str(opened_at)
            lines.append(f"**Opened**: `{ts_str}`")

    if marker_tag:
        lines.append(f"\n{marker_tag.strip()}")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1850] + "...\n" + (marker_tag.strip() if marker_tag else "")
    return msg


@dataclass(frozen=True)
class OperatorAlertRecord:
    """Durable record of an operator alert."""

    alert_id: str
    alert_type: str
    component: str
    severity: str
    status: str
    summary: str
    details: Mapping[str, Any]
    opened_at: datetime
    last_notified_at: datetime
    notification_count: int


@dataclass(frozen=True)
class AlertNotification:
    """Notification event emitted for a newly opened or re-notified alert."""

    alert_id: str
    alert_type: str
    component: str
    severity: str
    summary: str
    is_new: bool
    notification_count: int


@dataclass(frozen=True)
class AlertResolutionEvent:
    """Event emitted when an active alert condition resolves."""

    alert_type: str
    component: str
    was_resolved: bool
    alert_id: str | None = None


class FC27AlertManager:
    """Manages the evaluation, durable recording, deduplication, and resolution of operator alerts."""

    def __init__(
        self,
        conn_or_uri: psycopg.Connection[Any] | str,
        suppression_seconds: int = 1800,
        destination_key: str = "",
    ) -> None:
        self._conn_or_uri = conn_or_uri
        self._suppression_seconds = suppression_seconds
        self._destination_key = destination_key

    def _get_connection(self) -> psycopg.Connection[Any]:
        if isinstance(self._conn_or_uri, str):
            return psycopg.connect(self._conn_or_uri)
        return self._conn_or_uri

    def record_alert(
        self,
        alert_type: str,
        component: str,
        severity: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> AlertNotification | None:
        """Atomically record or update an alert, suppressing notifications if within the suppression window."""
        payload_json = json.dumps(details or {})
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT alert_id, should_notify, is_new, notification_count
                      FROM public.fc27_record_operator_alert(
                          %s, %s, %s, %s, %s::jsonb, %s
                      );
                    """,
                    (alert_type, component, severity, summary, payload_json, self._suppression_seconds),
                )
                row = cur.fetchone()
                if not row:
                    return None
                alert_id, should_notify, is_new, count = row
                if not should_notify:
                    return None
                return AlertNotification(
                    alert_id=str(alert_id),
                    alert_type=alert_type,
                    component=component,
                    severity=severity,
                    summary=summary,
                    is_new=bool(is_new),
                    notification_count=int(count),
                )
        finally:
            if isinstance(self._conn_or_uri, str):
                conn.close()

    def resolve_alert(self, alert_type: str, component: str) -> AlertResolutionEvent:
        """Resolve any active alert matching alert_type and component."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT was_resolved, alert_id FROM public.fc27_resolve_operator_alert(%s, %s);",
                    (alert_type, component),
                )
                row = cur.fetchone()
                was_resolved = bool(row[0]) if row else False
                alert_id = str(row[1]) if (row and row[1] is not None) else None
                return AlertResolutionEvent(
                    alert_type=alert_type,
                    component=component,
                    was_resolved=was_resolved,
                    alert_id=alert_id,
                )
        finally:
            if isinstance(self._conn_or_uri, str):
                conn.close()

    def reserve_alert_delivery(
        self,
        alert_id: str,
        lifecycle_event: str,
        destination_key: str,
        marker_digest: str,
    ) -> tuple[int, bool, str] | None:
        """Reserve an alert delivery row idempotently via stored procedure.

        Returns (delivery_id, is_new, delivery_state) or None on failure.
        """
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT delivery_id, is_new, delivery_state
                      FROM public.fc27_reserve_operator_alert_delivery(
                          %s, %s, %s, %s
                      );
                    """,
                    (alert_id, lifecycle_event, destination_key, marker_digest),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return int(row[0]), bool(row[1]), str(row[2])
        finally:
            if isinstance(self._conn_or_uri, str):
                conn.close()

    def get_active_alerts(self) -> list[OperatorAlertRecord]:
        """Query all currently OPEN or ACKNOWLEDGED operator alerts."""
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM public.fc27_get_active_operator_alerts();")
                rows = cur.fetchall()
                results: list[OperatorAlertRecord] = []
                for r in rows:
                    raw_details = r[6]
                    if isinstance(raw_details, str):
                        det = json.loads(raw_details)
                    else:
                        det = raw_details or {}
                    results.append(
                        OperatorAlertRecord(
                            alert_id=str(r[0]),
                            alert_type=str(r[1]),
                            component=str(r[2]),
                            severity=str(r[3]),
                            status=str(r[4]),
                            summary=str(r[5]),
                            details=det,
                            opened_at=r[7],
                            last_notified_at=r[8],
                            notification_count=int(r[9]),
                        )
                    )
                return results
        finally:
            if isinstance(self._conn_or_uri, str):
                conn.close()

    def evaluate_and_sync(
        self,
        snapshot: FC27OperationsStatusSnapshot,
        config: Any = None,
        destination_key: str | None = None,
    ) -> list[AlertNotification]:
        """Evaluate operational health and atomically update alerts table, returning notifications to deliver."""
        poll_warn_sec = getattr(config, "operations_poll_warning_seconds", 18000)
        poll_crit_sec = getattr(config, "operations_poll_critical_seconds", 32400)
        hb_warn_sec = getattr(config, "operations_heartbeat_warning_seconds", 120)
        hb_crit_sec = getattr(config, "operations_heartbeat_critical_seconds", 300)

        target_destination = (
            destination_key
            if destination_key is not None
            else getattr(config, "operations_alert_destination", self._destination_key)
        )

        notifications: list[AlertNotification] = []

        def _record(a_type: str, comp: str, sev: str, summ: str) -> None:
            notif = self.record_alert(a_type, comp, sev, summ)
            if notif:
                notifications.append(notif)
                if notif.is_new and target_destination:
                    _, digest = build_operations_alert_marker(
                        notif.alert_id, "OPEN", a_type, comp, target_destination
                    )
                    self.reserve_alert_delivery(
                        notif.alert_id, "OPEN", target_destination, digest
                    )

        def _resolve(a_type: str, comp: str) -> None:
            res = self.resolve_alert(a_type, comp)
            if res.was_resolved and res.alert_id and target_destination:
                _, digest = build_operations_alert_marker(
                    res.alert_id, "RESOLVED", a_type, comp, target_destination
                )
                self.reserve_alert_delivery(
                    res.alert_id, "RESOLVED", target_destination, digest
                )

        # 1. Polling overdue condition
        if snapshot.polling.last_cycle_completed_at is not None:
            poll_age = (snapshot.database_clock - snapshot.polling.last_cycle_completed_at).total_seconds()
            if poll_age > poll_crit_sec:
                _record(
                    "POLL_OVERDUE",
                    "polling",
                    "CRITICAL",
                    f"Polling overdue by {int(poll_age)}s (critical limit: {poll_crit_sec}s)",
                )
            elif poll_age > poll_warn_sec:
                _record(
                    "POLL_OVERDUE",
                    "polling",
                    "WARNING",
                    f"Polling delayed by {int(poll_age)}s (warning limit: {poll_warn_sec}s)",
                )
            else:
                _resolve("POLL_OVERDUE", "polling")
        else:
            _resolve("POLL_OVERDUE", "polling")

        # 2. Provider budget condition
        used_budget = snapshot.polling.rolling_provider_attempts_used
        limit_budget = snapshot.polling.provider_attempt_limit
        if used_budget >= limit_budget:
            _record(
                "PROVIDER_BUDGET_EXHAUSTED",
                "polling",
                "CRITICAL",
                f"Provider budget fully exhausted ({used_budget}/{limit_budget})",
            )
        elif used_budget >= 15:
            _record(
                "PROVIDER_BUDGET_HIGH",
                "polling",
                "WARNING",
                f"Provider budget high ({used_budget}/{limit_budget})",
            )
        else:
            _resolve("PROVIDER_BUDGET_EXHAUSTED", "polling")
            _resolve("PROVIDER_BUDGET_HIGH", "polling")

        # 3. Delivery stalled / dead-letter condition
        dead_letters = snapshot.discord_delivery.jobs_dead_lettered
        failed_jobs = snapshot.discord_delivery.jobs_failed
        if dead_letters >= 3:
            _record(
                "DELIVERY_DEAD_LETTER",
                "discord_delivery",
                "CRITICAL",
                f"Critical dead-letter Discord delivery jobs ({dead_letters} jobs)",
            )
        elif dead_letters >= 1 or failed_jobs >= 1:
            _record(
                "DELIVERY_FAILURES",
                "discord_delivery",
                "WARNING",
                f"Discord delivery failures present (dead_letter: {dead_letters}, failed: {failed_jobs})",
            )
        else:
            _resolve("DELIVERY_DEAD_LETTER", "discord_delivery")
            _resolve("DELIVERY_FAILURES", "discord_delivery")

        # 4. Worker heartbeat condition
        w_hb = snapshot.runtime.worker_heartbeat_age_seconds
        if w_hb is not None:
            if w_hb > hb_crit_sec:
                _record(
                    "HEARTBEAT_MISSING",
                    "community_worker",
                    "CRITICAL",
                    f"Community worker heartbeat missing ({int(w_hb)}s > {hb_crit_sec}s)",
                )
            elif w_hb > hb_warn_sec:
                _record(
                    "HEARTBEAT_STALE",
                    "community_worker",
                    "WARNING",
                    f"Community worker heartbeat stale ({int(w_hb)}s > {hb_warn_sec}s)",
                )
            else:
                _resolve("HEARTBEAT_MISSING", "community_worker")
                _resolve("HEARTBEAT_STALE", "community_worker")

        # 5. Discord bot heartbeat condition
        b_hb = snapshot.runtime.bot_heartbeat_age_seconds
        if b_hb is not None:
            if b_hb > hb_crit_sec:
                _record(
                    "HEARTBEAT_MISSING",
                    "discord_bot",
                    "CRITICAL",
                    f"Discord bot heartbeat missing ({int(b_hb)}s > {hb_crit_sec}s)",
                )
            elif b_hb > hb_warn_sec:
                _record(
                    "HEARTBEAT_STALE",
                    "discord_bot",
                    "WARNING",
                    f"Discord bot heartbeat stale ({int(b_hb)}s > {hb_warn_sec}s)",
                )
            else:
                _resolve("HEARTBEAT_MISSING", "discord_bot")
                _resolve("HEARTBEAT_STALE", "discord_bot")

        return notifications


def format_alert_notification_markdown(notification: AlertNotification) -> str:
    """Format an operator alert notification into clean, sanitized Discord markdown."""
    icon = "🔴" if notification.severity == "CRITICAL" else "🟡"
    header = f"{icon} **FC27 Operator Alert: {notification.alert_type}** (`{notification.severity}`)"
    comp = f"**Component**: `{notification.component}`"
    summary = f"**Summary**: {notification.summary}"
    meta = f"**Notification #{notification.notification_count}** (new: {str(notification.is_new).lower()})"
    return f"{header}\n{comp}\n{summary}\n{meta}"
