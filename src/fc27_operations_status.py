"""Read-only FC27 operations status contract, snapshot loader, and health evaluation.

Provides sanitized operational metrics across Polling, Ingestion, Discord Delivery,
and Container Runtime without exposing raw internal IDs, secrets, tokens, or DSNs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence, cast

import psycopg
from psycopg import sql


class HealthClassification(str, Enum):
    """Overall operational health classification."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


HEARTBEAT_WARNING_SECONDS = 120
HEARTBEAT_CRITICAL_SECONDS = 300


def classify_heartbeat_state(
    age_seconds: float | None,
    runtime_status: str | None,
    known: bool,
    *,
    warning_seconds: float = HEARTBEAT_WARNING_SECONDS,
    critical_seconds: float = HEARTBEAT_CRITICAL_SECONDS,
) -> str:
    """Classify one service heartbeat using database-clock age and runtime status.

    - No row (not known / no age): UNKNOWN
    - age > critical: CRITICAL
    - runtime_status != READY: DEGRADED
    - age > warning: DEGRADED
    - otherwise: HEALTHY
    UNKNOWN never maps to HEALTHY.
    """
    if not known or age_seconds is None:
        return HealthClassification.UNKNOWN.value
    if age_seconds > critical_seconds:
        return HealthClassification.CRITICAL.value
    status = (runtime_status or "READY").strip().upper()
    if status != "READY":
        return HealthClassification.DEGRADED.value
    if age_seconds > warning_seconds:
        return HealthClassification.DEGRADED.value
    return HealthClassification.HEALTHY.value


@dataclass(frozen=True)
class PollingStatusSnapshot:
    """Sanitized operational metrics for four-hour all-match-types polling."""

    last_cycle_id_sanitized: str | None
    last_cycle_start_at: datetime | None
    last_cycle_completed_at: datetime | None
    last_cycle_outcome: str
    match_types_attempted: tuple[str, ...]
    per_type_outcome: Mapping[str, str]
    next_expected_poll_at: datetime | None
    polling_interval_seconds: int
    single_flight_state: str  # "free" or "busy"
    active_lease_age_seconds: float | None
    rolling_provider_attempts_used: int
    provider_attempt_limit: int
    remaining_provider_capacity: int
    oldest_unresolved_reservation_age_seconds: float | None


@dataclass(frozen=True)
class IngestionStatusSnapshot:
    """Sanitized operational metrics for match and stat ingestion."""

    new_matches_in_last_cycle: int
    existing_duplicate_matches: int
    quarantined_matches: int
    replay_conflicts: int
    export_batch_state: str
    last_persisted_match_at: datetime | None
    latest_source_match_at: datetime | None
    data_freshness_age_seconds: float | None


@dataclass(frozen=True)
class DiscordDeliveryStatusSnapshot:
    """Sanitized operational metrics for asynchronous Discord delivery."""

    jobs_pending: int
    jobs_claimed: int
    jobs_completed: int
    jobs_failed: int
    jobs_dead_lettered: int
    oldest_pending_job_age_seconds: float | None
    formal_result_delivery_count: int
    team_summary_delivery_count: int
    failed_destination_delivery_count: int
    last_successful_delivery_time: datetime | None


@dataclass(frozen=True)
class RuntimeStatusSnapshot:
    """Sanitized container, migration, and feature state."""

    worker_heartbeat_age_seconds: float | None
    bot_heartbeat_age_seconds: float | None
    current_migration_version: str | None
    grants_policy_version: int | None
    active_backfill_request_count: int
    # Durable PostgreSQL heartbeat contract (Migration 0026).
    worker_heartbeat_known: bool = False
    bot_heartbeat_known: bool = False
    worker_runtime_status: str | None = None
    bot_runtime_status: str | None = None
    worker_started_age_seconds: float | None = None
    bot_started_age_seconds: float | None = None

    def _known(self, explicit: bool, age: float | None) -> bool:
        # Legacy snapshots only carried an age; absence of a durable flag with a
        # present age is treated as known for backward compatibility.
        return bool(explicit or age is not None)

    @property
    def worker_heartbeat_state(self) -> str:
        return classify_heartbeat_state(
            self.worker_heartbeat_age_seconds,
            self.worker_runtime_status,
            self._known(self.worker_heartbeat_known, self.worker_heartbeat_age_seconds),
        )

    @property
    def bot_heartbeat_state(self) -> str:
        return classify_heartbeat_state(
            self.bot_heartbeat_age_seconds,
            self.bot_runtime_status,
            self._known(self.bot_heartbeat_known, self.bot_heartbeat_age_seconds),
        )


@dataclass(frozen=True)
class FC27OperationsStatusSnapshot:
    """Complete sanitized operational health snapshot."""

    database_clock: datetime
    snapshot_generated_at: datetime
    polling: PollingStatusSnapshot
    ingestion: IngestionStatusSnapshot
    discord_delivery: DiscordDeliveryStatusSnapshot
    runtime: RuntimeStatusSnapshot


def _parse_iso_or_none(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        s = str(val).strip()
        if not s:
            return None
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def parse_operations_status_snapshot(raw_data: Mapping[str, Any]) -> FC27OperationsStatusSnapshot:
    """Parse raw JSONB snapshot payload from database into strongly typed dataclasses."""
    db_clock = _parse_iso_or_none(raw_data.get("database_clock")) or datetime.now(timezone.utc)
    gen_at = _parse_iso_or_none(raw_data.get("snapshot_generated_at")) or db_clock

    p_raw = raw_data.get("polling") or {}
    polling = PollingStatusSnapshot(
        last_cycle_id_sanitized=p_raw.get("last_cycle_id_sanitized"),
        last_cycle_start_at=_parse_iso_or_none(p_raw.get("last_cycle_start_at")),
        last_cycle_completed_at=_parse_iso_or_none(p_raw.get("last_cycle_completed_at")),
        last_cycle_outcome=str(p_raw.get("last_cycle_outcome") or "NO_CYCLES_RECORDED"),
        match_types_attempted=tuple(str(x) for x in p_raw.get("match_types_attempted") or ()),
        per_type_outcome={str(k): str(v) for k, v in (p_raw.get("per_type_outcome") or {}).items()},
        next_expected_poll_at=_parse_iso_or_none(p_raw.get("next_expected_poll_at")),
        polling_interval_seconds=int(p_raw.get("polling_interval_seconds") or 14400),
        single_flight_state=str(p_raw.get("single_flight_state") or "free"),
        active_lease_age_seconds=(
            float(p_raw["active_lease_age_seconds"])
            if p_raw.get("active_lease_age_seconds") is not None
            else None
        ),
        rolling_provider_attempts_used=int(p_raw.get("rolling_provider_attempts_used") or 0),
        provider_attempt_limit=int(p_raw.get("provider_attempt_limit") or 18),
        remaining_provider_capacity=int(p_raw.get("remaining_provider_capacity") or 18),
        oldest_unresolved_reservation_age_seconds=(
            float(p_raw["oldest_unresolved_reservation_age_seconds"])
            if p_raw.get("oldest_unresolved_reservation_age_seconds") is not None
            else None
        ),
    )

    i_raw = raw_data.get("ingestion") or {}
    ingestion = IngestionStatusSnapshot(
        new_matches_in_last_cycle=int(i_raw.get("new_matches_in_last_cycle") or 0),
        existing_duplicate_matches=int(i_raw.get("existing_duplicate_matches") or 0),
        quarantined_matches=int(i_raw.get("quarantined_matches") or 0),
        replay_conflicts=int(i_raw.get("replay_conflicts") or 0),
        export_batch_state=str(i_raw.get("export_batch_state") or "N/A"),
        last_persisted_match_at=_parse_iso_or_none(i_raw.get("last_persisted_match_at")),
        latest_source_match_at=_parse_iso_or_none(i_raw.get("latest_source_match_at")),
        data_freshness_age_seconds=(
            float(i_raw["data_freshness_age_seconds"])
            if i_raw.get("data_freshness_age_seconds") is not None
            else None
        ),
    )

    d_raw = raw_data.get("discord_delivery") or {}
    discord_delivery = DiscordDeliveryStatusSnapshot(
        jobs_pending=int(d_raw.get("jobs_pending") or 0),
        jobs_claimed=int(d_raw.get("jobs_claimed") or 0),
        jobs_completed=int(d_raw.get("jobs_completed") or 0),
        jobs_failed=int(d_raw.get("jobs_failed") or 0),
        jobs_dead_lettered=int(d_raw.get("jobs_dead_lettered") or 0),
        oldest_pending_job_age_seconds=(
            float(d_raw["oldest_pending_job_age_seconds"])
            if d_raw.get("oldest_pending_job_age_seconds") is not None
            else None
        ),
        formal_result_delivery_count=int(d_raw.get("formal_result_delivery_count") or 0),
        team_summary_delivery_count=int(d_raw.get("team_summary_delivery_count") or 0),
        failed_destination_delivery_count=int(d_raw.get("failed_destination_delivery_count") or 0),
        last_successful_delivery_time=_parse_iso_or_none(d_raw.get("last_successful_delivery_time")),
    )

    r_raw = raw_data.get("runtime") or {}
    runtime = RuntimeStatusSnapshot(
        worker_heartbeat_age_seconds=(
            float(r_raw["worker_heartbeat_age_seconds"])
            if r_raw.get("worker_heartbeat_age_seconds") is not None
            else None
        ),
        bot_heartbeat_age_seconds=(
            float(r_raw["bot_heartbeat_age_seconds"])
            if r_raw.get("bot_heartbeat_age_seconds") is not None
            else None
        ),
        current_migration_version=r_raw.get("current_migration_version"),
        grants_policy_version=(
            int(r_raw["grants_policy_version"])
            if r_raw.get("grants_policy_version") is not None
            else None
        ),
        active_backfill_request_count=int(r_raw.get("active_backfill_request_count") or 0),
        worker_heartbeat_known=bool(r_raw.get("worker_heartbeat_known") or False),
        bot_heartbeat_known=bool(r_raw.get("bot_heartbeat_known") or False),
        worker_runtime_status=r_raw.get("worker_runtime_status"),
        bot_runtime_status=r_raw.get("bot_runtime_status"),
        worker_started_age_seconds=(
            float(r_raw["worker_started_age_seconds"])
            if r_raw.get("worker_started_age_seconds") is not None
            else None
        ),
        bot_started_age_seconds=(
            float(r_raw["bot_started_age_seconds"])
            if r_raw.get("bot_started_age_seconds") is not None
            else None
        ),
    )

    return FC27OperationsStatusSnapshot(
        database_clock=db_clock,
        snapshot_generated_at=gen_at,
        polling=polling,
        ingestion=ingestion,
        discord_delivery=discord_delivery,
        runtime=runtime,
    )


def fetch_operations_status_snapshot(
    conn_or_uri: psycopg.Connection[Any] | str,
) -> FC27OperationsStatusSnapshot:
    """Execute the stored procedure public.fc27_get_operations_status_snapshot() and return typed snapshot."""
    if isinstance(conn_or_uri, str):
        with psycopg.connect(conn_or_uri) as conn:
            return _execute_snapshot_proc(conn)
    return _execute_snapshot_proc(conn_or_uri)


def _execute_snapshot_proc(conn: psycopg.Connection[Any]) -> FC27OperationsStatusSnapshot:
    with conn.cursor() as cur:
        cur.execute("SELECT public.fc27_get_operations_status_snapshot();")
        row = cur.fetchone()
        if not row or row[0] is None:
            raise RuntimeError("public.fc27_get_operations_status_snapshot() returned NULL")
        raw_val = row[0]
        if isinstance(raw_val, str):
            raw_dict = json.loads(raw_val)
        else:
            raw_dict = cast(dict[str, Any], raw_val)
        _merge_durable_heartbeats(conn, cur, raw_dict)
        return parse_operations_status_snapshot(raw_dict)


def _merge_durable_heartbeats(
    conn: psycopg.Connection[Any],
    cur: Any,
    raw_dict: dict[str, Any],
) -> None:
    """Overlay durable PostgreSQL heartbeat ages/status onto the snapshot.

    The legacy snapshot procedure may return ages from ``service_heartbeats``.
    When the Migration 0026 durable source is present it is authoritative; when
    the helper is absent (pre-0026 database) the legacy values are retained.
    Best-effort: any failure leaves the legacy snapshot untouched.
    """
    try:
        cur.execute(
            "SELECT to_regprocedure('public.fc27_get_service_heartbeat_status()')"
        )
        exists = cur.fetchone()
        if not exists or exists[0] is None:
            return
        cur.execute("SELECT public.fc27_get_service_heartbeat_status();")
        hb_row = cur.fetchone()
        if not hb_row or hb_row[0] is None:
            return
        hb_val = hb_row[0]
        hb = json.loads(hb_val) if isinstance(hb_val, str) else hb_val
        if not isinstance(hb, Mapping):
            return
        runtime = raw_dict.setdefault("runtime", {})
        for service in ("worker", "bot"):
            info = hb.get(service) or {}
            if info.get("known"):
                runtime[f"{service}_heartbeat_age_seconds"] = info.get(
                    "heartbeat_age_seconds"
                )
                runtime[f"{service}_started_age_seconds"] = info.get(
                    "started_age_seconds"
                )
                runtime[f"{service}_runtime_status"] = info.get("runtime_status")
                runtime[f"{service}_heartbeat_known"] = True
            else:
                # Durable source present but no row: authoritative UNKNOWN.
                runtime[f"{service}_heartbeat_age_seconds"] = None
                runtime[f"{service}_started_age_seconds"] = None
                runtime[f"{service}_runtime_status"] = None
                runtime[f"{service}_heartbeat_known"] = False
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass



def classify_operations_health(
    snapshot: FC27OperationsStatusSnapshot,
    config: Any = None,
) -> tuple[HealthClassification, list[str]]:
    """Evaluate operational metrics against configured or default thresholds.

    Rules:
    - Quiet cycle (zero new matches in last cycle) is normal and HEALTHY.
    - Polling overdue: warning > 5h (18000s), critical > 9h (32400s).
    - Provider rolling budget: warning >= 15/18, critical >= 18/18.
    - Pending jobs: warning oldest > 10m (600s), critical > 30m (1800s).
    - Failed/dead-letter jobs: warning >= 1, critical >= 3.
    - Worker/bot heartbeat: warning > 2m (120s), critical > 5m (300s).
    - Single-flight active lease: warning > 10m (600s), critical > 30m (1800s).
    """
    poll_warn_sec = getattr(config, "operations_poll_warning_seconds", 18000)
    poll_crit_sec = getattr(config, "operations_poll_critical_seconds", 32400)
    job_warn_sec = getattr(config, "operations_job_warning_seconds", 600)
    job_crit_sec = getattr(config, "operations_job_critical_seconds", 1800)
    hb_warn_sec = getattr(config, "operations_heartbeat_warning_seconds", 120)
    hb_crit_sec = getattr(config, "operations_heartbeat_critical_seconds", 300)

    critical_reasons: list[str] = []
    warning_reasons: list[str] = []

    # 1. Polling checks
    if snapshot.polling.last_cycle_completed_at is not None:
        poll_age = (snapshot.database_clock - snapshot.polling.last_cycle_completed_at).total_seconds()
        if poll_age > poll_crit_sec:
            critical_reasons.append(
                f"Polling overdue: {int(poll_age)}s since last cycle (critical threshold: {poll_crit_sec}s)"
            )
        elif poll_age > poll_warn_sec:
            warning_reasons.append(
                f"Polling delayed: {int(poll_age)}s since last cycle (warning threshold: {poll_warn_sec}s)"
            )

    used_budget = snapshot.polling.rolling_provider_attempts_used
    limit_budget = snapshot.polling.provider_attempt_limit
    if used_budget >= limit_budget:
        critical_reasons.append(f"Provider request capacity exhausted ({used_budget}/{limit_budget})")
    elif used_budget >= 15:
        warning_reasons.append(f"Provider request capacity high ({used_budget}/{limit_budget})")

    if snapshot.polling.single_flight_state == "busy":
        lease_age = snapshot.polling.active_lease_age_seconds
        if lease_age is not None:
            if lease_age > job_crit_sec:
                critical_reasons.append(f"Single-flight lock held critically long ({int(lease_age)}s)")
            elif lease_age > job_warn_sec:
                warning_reasons.append(f"Single-flight lock held long ({int(lease_age)}s)")

    unres_age = snapshot.polling.oldest_unresolved_reservation_age_seconds
    if unres_age is not None:
        if unres_age > job_crit_sec:
            critical_reasons.append(f"Provider reservation stuck ({int(unres_age)}s)")
        elif unres_age > job_warn_sec:
            warning_reasons.append(f"Provider reservation lingering ({int(unres_age)}s)")

    # 2. Discord delivery checks
    dead_letters = snapshot.discord_delivery.jobs_dead_lettered
    failed_jobs = snapshot.discord_delivery.jobs_failed
    if dead_letters >= 3:
        critical_reasons.append(f"Dead-letter Discord delivery jobs critical ({dead_letters} jobs)")
    elif dead_letters >= 1 or failed_jobs >= 1:
        warning_reasons.append(
            f"Delivery issues detected (dead-letter: {dead_letters}, failed: {failed_jobs})"
        )

    oldest_job_age = snapshot.discord_delivery.oldest_pending_job_age_seconds
    if oldest_job_age is not None:
        if oldest_job_age > job_crit_sec:
            critical_reasons.append(f"Pending Discord job stuck ({int(oldest_job_age)}s)")
        elif oldest_job_age > job_warn_sec:
            warning_reasons.append(f"Pending Discord job delayed ({int(oldest_job_age)}s)")

    # 3. Runtime heartbeat checks (durable PostgreSQL heartbeat, DB clock)
    unknown_reasons: list[str] = []
    w_age = snapshot.runtime.worker_heartbeat_age_seconds
    w_known = snapshot.runtime.worker_heartbeat_known or w_age is not None
    w_state = classify_heartbeat_state(
        w_age,
        snapshot.runtime.worker_runtime_status,
        w_known,
        warning_seconds=hb_warn_sec,
        critical_seconds=hb_crit_sec,
    )
    if w_state == HealthClassification.CRITICAL.value:
        w_reported = int(w_age) if w_age is not None else 0
        critical_reasons.append(
            f"Worker heartbeat missing ({w_reported}s > {hb_crit_sec}s)"
        )
    elif w_state == HealthClassification.DEGRADED.value:
        if w_age is not None and w_age > hb_warn_sec:
            warning_reasons.append(
                f"Worker heartbeat stale ({int(w_age)}s > {hb_warn_sec}s)"
            )
        else:
            warning_reasons.append(
                f"Worker reporting {snapshot.runtime.worker_runtime_status or 'UNKNOWN'}"
            )
    elif w_state == HealthClassification.UNKNOWN.value:
        unknown_reasons.append("Worker heartbeat source unknown")

    b_age = snapshot.runtime.bot_heartbeat_age_seconds
    b_known = snapshot.runtime.bot_heartbeat_known or b_age is not None
    b_state = classify_heartbeat_state(
        b_age,
        snapshot.runtime.bot_runtime_status,
        b_known,
        warning_seconds=hb_warn_sec,
        critical_seconds=hb_crit_sec,
    )
    if b_state == HealthClassification.CRITICAL.value:
        b_reported = int(b_age) if b_age is not None else 0
        critical_reasons.append(
            f"Discord bot heartbeat missing ({b_reported}s > {hb_crit_sec}s)"
        )
    elif b_state == HealthClassification.DEGRADED.value:
        if b_age is not None and b_age > hb_warn_sec:
            warning_reasons.append(
                f"Discord bot heartbeat stale ({int(b_age)}s > {hb_warn_sec}s)"
            )
        else:
            warning_reasons.append(
                f"Discord bot reporting {snapshot.runtime.bot_runtime_status or 'UNKNOWN'}"
            )
    elif b_state == HealthClassification.UNKNOWN.value:
        unknown_reasons.append("Discord bot heartbeat source unknown")

    if critical_reasons:
        return HealthClassification.CRITICAL, critical_reasons + warning_reasons + unknown_reasons
    if warning_reasons:
        return HealthClassification.DEGRADED, warning_reasons + unknown_reasons
    if unknown_reasons:
        return HealthClassification.UNKNOWN, unknown_reasons
    return HealthClassification.HEALTHY, []


def render_operations_status_markdown(
    snapshot: FC27OperationsStatusSnapshot,
    classification: HealthClassification,
    reasons: Sequence[str] = (),
) -> str:
    """Format sanitized operations status markdown fitting within Discord's 2000 character limit."""
    emoji_map = {
        HealthClassification.HEALTHY: "🟢",
        HealthClassification.DEGRADED: "🟡",
        HealthClassification.CRITICAL: "🔴",
        HealthClassification.UNKNOWN: "⚪",
    }
    status_icon = emoji_map.get(classification, "⚪")

    last_cycle_str = "None"
    if snapshot.polling.last_cycle_completed_at is not None:
        time_part = snapshot.polling.last_cycle_completed_at.strftime("%H:%M UTC")
        outcome_part = "completed" if snapshot.polling.last_cycle_outcome == "SUCCEEDED" else snapshot.polling.last_cycle_outcome.lower()
        last_cycle_str = f"{time_part}, {outcome_part}"

    next_expected_str = "None"
    if snapshot.polling.next_expected_poll_at is not None:
        next_expected_str = snapshot.polling.next_expected_poll_at.strftime("%H:%M UTC")

    types_count = len(snapshot.polling.match_types_attempted)
    types_str = f"{types_count}/3" if types_count > 0 else "0/3"
    budget_str = f"{snapshot.polling.rolling_provider_attempts_used}/{snapshot.polling.provider_attempt_limit}"

    oldest_pending_str = (
        f"{int(snapshot.discord_delivery.oldest_pending_job_age_seconds)}s"
        if snapshot.discord_delivery.oldest_pending_job_age_seconds is not None
        else "none"
    )

    last_delivery_str = (
        snapshot.discord_delivery.last_successful_delivery_time.strftime("%H:%M UTC")
        if snapshot.discord_delivery.last_successful_delivery_time is not None
        else "none"
    )

    def _heartbeat_line(age: float | None, runtime_status: str | None, state: str) -> str:
        if state == HealthClassification.UNKNOWN.value:
            return "unknown"
        if state == HealthClassification.CRITICAL.value and age is not None:
            return f"heartbeat missing ({int(age)}s)"
        if state == HealthClassification.DEGRADED.value:
            if age is not None and age > HEARTBEAT_WARNING_SECONDS:
                return f"stale ({int(age)}s)"
            return f"degraded ({(runtime_status or 'unknown').lower()})"
        if age is not None:
            return f"healthy, heartbeat {int(age)}s"
        return "healthy"

    worker_status = _heartbeat_line(
        snapshot.runtime.worker_heartbeat_age_seconds,
        snapshot.runtime.worker_runtime_status,
        snapshot.runtime.worker_heartbeat_state,
    )
    bot_status = _heartbeat_line(
        snapshot.runtime.bot_heartbeat_age_seconds,
        snapshot.runtime.bot_runtime_status,
        snapshot.runtime.bot_heartbeat_state,
    )

    migration_str = snapshot.runtime.current_migration_version or "N/A"
    if migration_str.endswith(".sql"):
        migration_str = migration_str.split("_")[0]

    grants_str = str(snapshot.runtime.grants_policy_version or "N/A")

    lines = [
        f"**FC27 Operations** {status_icon} `{classification.value}`",
        "",
        "**Polling**",
        f"Last cycle: {last_cycle_str}",
        f"Next expected: {next_expected_str}",
        f"Types: {types_str}",
        f"Budget: {budget_str}",
        f"Single-flight: {snapshot.polling.single_flight_state}",
        "",
        "**Ingestion**",
        f"New: {snapshot.ingestion.new_matches_in_last_cycle}",
        f"Existing: {snapshot.ingestion.existing_duplicate_matches}",
        f"Quarantined: {snapshot.ingestion.quarantined_matches}",
        f"Replay conflicts: {snapshot.ingestion.replay_conflicts}",
        "",
        "**Discord**",
        f"Pending: {snapshot.discord_delivery.jobs_pending}",
        f"Failed: {snapshot.discord_delivery.jobs_failed}",
        f"Oldest pending: {oldest_pending_str}",
        f"Last delivery: {last_delivery_str}",
        "",
        "**Runtime**",
        f"Worker: {worker_status}",
        f"Bot: {bot_status}",
        f"Migration: {migration_str}",
        f"Grants Policy: {grants_str}",
    ]

    if reasons:
        lines.append("")
        lines.append("**Active Items**")
        for r in reasons[:4]:
            lines.append(f"• {r}")

    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1890] + "..."
    return text
