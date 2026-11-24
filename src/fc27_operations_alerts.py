"""Deduplicated durable alerting surface for FC27 operations.

Interacts with the durable public.fc27_operator_alerts table and stored procedures
to atomically record, deduplicate, suppress duplicate notifications for 30 minutes,
and resolve cleared alerts using the authoritative PostgreSQL database clock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import psycopg
from psycopg import sql

from src.fc27_operations_status import (
    FC27OperationsStatusSnapshot,
    HealthClassification,
    classify_operations_health,
)


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


class FC27AlertManager:
    """Manages the evaluation, durable recording, deduplication, and resolution of operator alerts."""

    def __init__(
        self,
        conn_or_uri: psycopg.Connection[Any] | str,
        suppression_seconds: int = 1800,
    ) -> None:
        self._conn_or_uri = conn_or_uri
        self._suppression_seconds = suppression_seconds

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
                    "SELECT public.fc27_resolve_operator_alert(%s, %s);",
                    (alert_type, component),
                )
                row = cur.fetchone()
                was_resolved = bool(row[0]) if row else False
                return AlertResolutionEvent(
                    alert_type=alert_type,
                    component=component,
                    was_resolved=was_resolved,
                )
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
    ) -> list[AlertNotification]:
        """Evaluate operational health and atomically update alerts table, returning notifications to deliver."""
        poll_warn_sec = getattr(config, "operations_poll_warning_seconds", 18000)
        poll_crit_sec = getattr(config, "operations_poll_critical_seconds", 32400)
        job_warn_sec = getattr(config, "operations_job_warning_seconds", 600)
        job_crit_sec = getattr(config, "operations_job_critical_seconds", 1800)
        hb_warn_sec = getattr(config, "operations_heartbeat_warning_seconds", 120)
        hb_crit_sec = getattr(config, "operations_heartbeat_critical_seconds", 300)

        notifications: list[AlertNotification] = []

        # 1. Polling overdue condition
        if snapshot.polling.last_cycle_completed_at is not None:
            poll_age = (snapshot.database_clock - snapshot.polling.last_cycle_completed_at).total_seconds()
            if poll_age > poll_crit_sec:
                notif = self.record_alert(
                    "POLL_OVERDUE",
                    "polling",
                    "CRITICAL",
                    f"Polling overdue by {int(poll_age)}s (critical limit: {poll_crit_sec}s)",
                )
                if notif:
                    notifications.append(notif)
            elif poll_age > poll_warn_sec:
                notif = self.record_alert(
                    "POLL_OVERDUE",
                    "polling",
                    "WARNING",
                    f"Polling delayed by {int(poll_age)}s (warning limit: {poll_warn_sec}s)",
                )
                if notif:
                    notifications.append(notif)
            else:
                self.resolve_alert("POLL_OVERDUE", "polling")
        else:
            self.resolve_alert("POLL_OVERDUE", "polling")

        # 2. Provider budget condition
        used_budget = snapshot.polling.rolling_provider_attempts_used
        limit_budget = snapshot.polling.provider_attempt_limit
        if used_budget >= limit_budget:
            notif = self.record_alert(
                "PROVIDER_BUDGET_EXHAUSTED",
                "polling",
                "CRITICAL",
                f"Provider budget fully exhausted ({used_budget}/{limit_budget})",
            )
            if notif:
                notifications.append(notif)
        elif used_budget >= 15:
            notif = self.record_alert(
                "PROVIDER_BUDGET_HIGH",
                "polling",
                "WARNING",
                f"Provider budget high ({used_budget}/{limit_budget})",
            )
            if notif:
                notifications.append(notif)
        else:
            self.resolve_alert("PROVIDER_BUDGET_EXHAUSTED", "polling")
            self.resolve_alert("PROVIDER_BUDGET_HIGH", "polling")

        # 3. Delivery stalled / dead-letter condition
        dead_letters = snapshot.discord_delivery.jobs_dead_lettered
        failed_jobs = snapshot.discord_delivery.jobs_failed
        if dead_letters >= 3:
            notif = self.record_alert(
                "DELIVERY_DEAD_LETTER",
                "discord_delivery",
                "CRITICAL",
                f"Critical dead-letter Discord delivery jobs ({dead_letters} jobs)",
            )
            if notif:
                notifications.append(notif)
        elif dead_letters >= 1 or failed_jobs >= 1:
            notif = self.record_alert(
                "DELIVERY_FAILURES",
                "discord_delivery",
                "WARNING",
                f"Discord delivery failures present (dead_letter: {dead_letters}, failed: {failed_jobs})",
            )
            if notif:
                notifications.append(notif)
        else:
            self.resolve_alert("DELIVERY_DEAD_LETTER", "discord_delivery")
            self.resolve_alert("DELIVERY_FAILURES", "discord_delivery")

        # 4. Worker heartbeat condition
        w_hb = snapshot.runtime.worker_heartbeat_age_seconds
        if w_hb is not None:
            if w_hb > hb_crit_sec:
                notif = self.record_alert(
                    "HEARTBEAT_MISSING",
                    "community_worker",
                    "CRITICAL",
                    f"Community worker heartbeat missing ({int(w_hb)}s > {hb_crit_sec}s)",
                )
                if notif:
                    notifications.append(notif)
            elif w_hb > hb_warn_sec:
                notif = self.record_alert(
                    "HEARTBEAT_STALE",
                    "community_worker",
                    "WARNING",
                    f"Community worker heartbeat stale ({int(w_hb)}s > {hb_warn_sec}s)",
                )
                if notif:
                    notifications.append(notif)
            else:
                self.resolve_alert("HEARTBEAT_MISSING", "community_worker")
                self.resolve_alert("HEARTBEAT_STALE", "community_worker")

        # 5. Discord bot heartbeat condition
        b_hb = snapshot.runtime.bot_heartbeat_age_seconds
        if b_hb is not None:
            if b_hb > hb_crit_sec:
                notif = self.record_alert(
                    "HEARTBEAT_MISSING",
                    "discord_bot",
                    "CRITICAL",
                    f"Discord bot heartbeat missing ({int(b_hb)}s > {hb_crit_sec}s)",
                )
                if notif:
                    notifications.append(notif)
            elif b_hb > hb_warn_sec:
                notif = self.record_alert(
                    "HEARTBEAT_STALE",
                    "discord_bot",
                    "WARNING",
                    f"Discord bot heartbeat stale ({int(b_hb)}s > {hb_warn_sec}s)",
                )
                if notif:
                    notifications.append(notif)
            else:
                self.resolve_alert("HEARTBEAT_MISSING", "discord_bot")
                self.resolve_alert("HEARTBEAT_STALE", "discord_bot")

        return notifications


def format_alert_notification_markdown(notification: AlertNotification) -> str:
    """Format an operator alert notification into clean, sanitized Discord markdown."""
    icon = "🔴" if notification.severity == "CRITICAL" else "🟡"
    header = f"{icon} **FC27 Operator Alert: {notification.alert_type}** (`{notification.severity}`)"
    comp = f"**Component**: `{notification.component}`"
    summary = f"**Summary**: {notification.summary}"
    meta = f"**Notification #{notification.notification_count}** (new: {str(notification.is_new).lower()})"
    return f"{header}\n{comp}\n{summary}\n{meta}"
