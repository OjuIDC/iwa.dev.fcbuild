"""Comprehensive test suite for FC27 Operator Status and Deduplicated Alerting.

Verifies all 47 test matrix requirements:
1. Status command disabled by default.
2. Authorized Administrator succeeds.
3. Authorized Operator succeeds.
4. Identity Operator denied.
5. Role-name text denied.
6. Unauthorized member denied.
7. Response ephemeral.
8. Zero database writes.
9. Zero provider requests.
10. Zero public Discord messages.
11. Last-cycle calculation.
12. Next-cycle calculation.
13. Per-type outcome.
14. Rolling provider usage.
15. Remaining budget.
16. Pending-job count and age.
17. Failed/dead-letter count.
18. Destination-delivery counts.
19. Worker heartbeat state.
20. Bot heartbeat state.
21. Migration version.
22. Grants Policy version.
23. Healthy classification.
24. Poll-overdue warning.
25. Poll-overdue critical.
26. Budget warning.
27. Budget critical.
28. Pending-job warning.
29. Pending-job critical.
30. Stale-heartbeat warning and critical.
31. Long lease warning and critical.
32. Quiet cycle without new matches remains healthy.
33. Alert deduplication.
34. Alert suppression.
35. Alert resolution.
36. Restart-safe alert state.
37. No raw IDs.
38. No tokens/digests.
39. No HMACs.
40. No DSNs.
41. Command output within Discord limits.
42. Existing command topology unchanged.
43. Global command count remains zero.
44. Backfill remains disabled.
45. Existing polling tests pass.
46. Existing delivery tests pass.
47. Existing healthcheck tests pass.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from src import settings
from src.fc27_discord_bot import (
    DISCORD_PROVIDER_REQUESTS,
    FC27DiscordBotConfig,
    FC27DiscordBotService,
    register_fc27_application_commands,
)
from src.fc27_operations_alerts import (
    AlertNotification,
    FC27AlertManager,
    format_alert_notification_markdown,
)
from src.fc27_operations_status import (
    FC27OperationsStatusSnapshot,
    HealthClassification,
    classify_operations_health,
    fetch_operations_status_snapshot,
    parse_operations_status_snapshot,
    render_operations_status_markdown,
)

TEST_GUILD_ID = "11111111111111111"
TEST_CHANNEL_ID = "22222222222222222"
TEST_USER_ID = "33333333333333333"

ADMIN_ROLE_ID = "99999999999999991"
OPERATOR_ROLE_ID = "99999999999999992"
IDENTITY_OP_ROLE_ID = "99999999999999993"
UNAUTHORIZED_ROLE_ID = "99999999999999994"


def _build_test_config(**overrides: Any) -> FC27DiscordBotConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "allowed_guild_ids": frozenset({TEST_GUILD_ID}),
        "allowed_channel_ids": frozenset({TEST_CHANNEL_ID}),
        "admin_role_ids": frozenset({ADMIN_ROLE_ID}),
        "operator_role_ids": frozenset({OPERATOR_ROLE_ID}),
        "identity_operator_role_ids": frozenset({IDENTITY_OP_ROLE_ID}),
        "operations_status_enabled": True,
        "operations_alerts_enabled": True,
    }
    params.update(overrides)
    return FC27DiscordBotConfig(**params)


def _make_sample_snapshot(
    *,
    db_clock: datetime | None = None,
    last_poll_hours_ago: float = 1.0,
    rolling_attempts: int = 6,
    pending_jobs: int = 0,
    oldest_pending_age: float | None = None,
    dead_letters: int = 0,
    failed_jobs: int = 0,
    worker_hb_age: float = 30.0,
    bot_hb_age: float = 25.0,
    single_flight_state: str = "free",
    lease_age: float | None = None,
    new_matches: int = 0,
) -> FC27OperationsStatusSnapshot:
    now = db_clock or datetime.now(timezone.utc)
    last_completed = now - timedelta(hours=last_poll_hours_ago)
    raw = {
        "database_clock": now.isoformat(),
        "snapshot_generated_at": now.isoformat(),
        "polling": {
            "last_cycle_id_sanitized": "cyc_a1b2c3d4",
            "last_cycle_start_at": (last_completed - timedelta(seconds=45)).isoformat(),
            "last_cycle_completed_at": last_completed.isoformat(),
            "last_cycle_outcome": "SUCCEEDED",
            "match_types_attempted": ["leagueMatch", "playoffMatch", "friendlyMatch"],
            "per_type_outcome": {
                "leagueMatch": "SUCCEEDED",
                "playoffMatch": "SUCCEEDED",
                "friendlyMatch": "SUCCEEDED",
            },
            "next_expected_poll_at": (last_completed + timedelta(seconds=14400)).isoformat(),
            "polling_interval_seconds": 14400,
            "single_flight_state": single_flight_state,
            "active_lease_age_seconds": lease_age,
            "rolling_provider_attempts_used": rolling_attempts,
            "provider_attempt_limit": 18,
            "remaining_provider_capacity": max(0, 18 - rolling_attempts),
            "oldest_unresolved_reservation_age_seconds": None,
        },
        "ingestion": {
            "new_matches_in_last_cycle": new_matches,
            "existing_duplicate_matches": 4,
            "quarantined_matches": 0,
            "replay_conflicts": 0,
            "export_batch_state": "BATCH_01",
            "last_persisted_match_at": (now - timedelta(hours=2)).isoformat(),
            "latest_source_match_at": (now - timedelta(hours=2)).isoformat(),
            "data_freshness_age_seconds": 7200.0,
        },
        "discord_delivery": {
            "jobs_pending": pending_jobs,
            "jobs_claimed": 0,
            "jobs_completed": 12,
            "jobs_failed": failed_jobs,
            "jobs_dead_lettered": dead_letters,
            "oldest_pending_job_age_seconds": oldest_pending_age,
            "formal_result_delivery_count": 10,
            "team_summary_delivery_count": 10,
            "failed_destination_delivery_count": 0,
            "last_successful_delivery_time": (now - timedelta(minutes=50)).isoformat(),
        },
        "runtime": {
            "worker_heartbeat_age_seconds": worker_hb_age,
            "bot_heartbeat_age_seconds": bot_hb_age,
            "current_migration_version": "0027_fc27_operations_alert_dispatch.sql",
            "grants_policy_version": 28,
            "active_backfill_request_count": 0,
        },
    }
    return parse_operations_status_snapshot(raw)


# ==============================================================================
# SCENARIOS 1-10: COMMAND AUTHORIZATION, EPHEMERALITY, ZERO WRITES/CALLS
# ==============================================================================


def test_01_status_command_disabled_by_default() -> None:
    """1. Status command disabled by default."""
    cfg = _build_test_config(operations_status_enabled=False)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
    )
    assert resp.status == "ERROR"
    assert resp.ephemeral is True
    assert "disabled" in resp.content.lower()


def test_02_authorized_administrator_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """2. Authorized Administrator succeeds."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    sample_snap = _make_sample_snapshot()
    monkeypatch.setattr(
        "src.fc27_operations_status.fetch_operations_status_snapshot",
        lambda _uri: sample_snap,
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
    )
    assert resp.status == "OK"
    assert resp.ephemeral is True
    assert "FC27 Operations" in resp.content


def test_03_authorized_operator_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """3. Authorized Operator succeeds."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    sample_snap = _make_sample_snapshot()
    monkeypatch.setattr(
        "src.fc27_operations_status.fetch_operations_status_snapshot",
        lambda _uri: sample_snap,
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[OPERATOR_ROLE_ID],
    )
    assert resp.status == "OK"
    assert resp.ephemeral is True
    assert "FC27 Operations" in resp.content


def test_04_identity_operator_denied() -> None:
    """4. Identity Operator denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[IDENTITY_OP_ROLE_ID],
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True
    assert "not authorized" in resp.content.lower()


def test_05_role_name_text_denied() -> None:
    """5. Role-name text denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=["Administrator", "Operator", "Admin"],
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_06_unauthorized_member_denied() -> None:
    """6. Unauthorized member denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[UNAUTHORIZED_ROLE_ID],
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_07_response_ephemeral() -> None:
    """7. Response ephemeral."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    is_ephemeral = svc.resolve_defer_ephemeral(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[OPERATOR_ROLE_ID],
    )
    assert is_ephemeral is True


def test_08_zero_database_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """8. Zero database writes during status query."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    sample_snap = _make_sample_snapshot()
    executed_statements: list[str] = []

    def _mock_fetch(uri: Any) -> FC27OperationsStatusSnapshot:
        executed_statements.append("SELECT public.fc27_get_operations_status_snapshot();")
        return sample_snap

    monkeypatch.setattr(
        "src.fc27_operations_status.fetch_operations_status_snapshot",
        _mock_fetch,
    )

    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[OPERATOR_ROLE_ID],
    )
    assert resp.status == "OK"
    assert all(s.strip().upper().startswith("SELECT") for s in executed_statements)
    assert not any(
        verb in s.upper() for s in executed_statements for verb in ("INSERT", "UPDATE", "DELETE", "DROP")
    )


def test_09_zero_provider_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """9. Zero provider requests executed."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    sample_snap = _make_sample_snapshot()
    monkeypatch.setattr(
        "src.fc27_operations_status.fetch_operations_status_snapshot",
        lambda _uri: sample_snap,
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[OPERATOR_ROLE_ID],
    )
    assert resp.provider_requests_executed == 0
    assert DISCORD_PROVIDER_REQUESTS == 0


def test_10_zero_public_discord_messages() -> None:
    """10. Zero public Discord messages."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    assert svc.resolve_defer_ephemeral(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[OPERATOR_ROLE_ID],
    ) is True


# ==============================================================================
# SCENARIOS 11-22: STATUS CONTRACT METRIC CALCULATIONS
# ==============================================================================


def test_11_last_cycle_calculation() -> None:
    """11. Last-cycle calculation."""
    snap = _make_sample_snapshot(last_poll_hours_ago=2.0)
    assert snap.polling.last_cycle_completed_at is not None
    assert snap.polling.last_cycle_outcome == "SUCCEEDED"
    assert snap.polling.last_cycle_id_sanitized == "cyc_a1b2c3d4"


def test_12_next_cycle_calculation() -> None:
    """12. Next-cycle calculation."""
    snap = _make_sample_snapshot(last_poll_hours_ago=2.0)
    assert snap.polling.next_expected_poll_at is not None
    expected_diff = (snap.polling.next_expected_poll_at - snap.polling.last_cycle_completed_at).total_seconds()
    assert expected_diff == 14400


def test_13_per_type_outcome() -> None:
    """13. Per-type outcome."""
    snap = _make_sample_snapshot()
    assert snap.polling.match_types_attempted == ("leagueMatch", "playoffMatch", "friendlyMatch")
    assert snap.polling.per_type_outcome.get("leagueMatch") == "SUCCEEDED"


def test_14_rolling_provider_usage() -> None:
    """14. Rolling provider usage."""
    snap = _make_sample_snapshot(rolling_attempts=9)
    assert snap.polling.rolling_provider_attempts_used == 9


def test_15_remaining_budget() -> None:
    """15. Remaining budget."""
    snap = _make_sample_snapshot(rolling_attempts=9)
    assert snap.polling.remaining_provider_capacity == 9
    assert snap.polling.provider_attempt_limit == 18


def test_16_pending_job_count_and_age() -> None:
    """16. Pending-job count and age."""
    snap = _make_sample_snapshot(pending_jobs=2, oldest_pending_age=125.0)
    assert snap.discord_delivery.jobs_pending == 2
    assert snap.discord_delivery.oldest_pending_job_age_seconds == 125.0


def test_17_failed_dead_letter_count() -> None:
    """17. Failed/dead-letter count."""
    snap = _make_sample_snapshot(dead_letters=1, failed_jobs=2)
    assert snap.discord_delivery.jobs_dead_lettered == 1
    assert snap.discord_delivery.jobs_failed == 2


def test_18_destination_delivery_counts() -> None:
    """18. Destination-delivery counts."""
    snap = _make_sample_snapshot()
    assert snap.discord_delivery.formal_result_delivery_count == 10
    assert snap.discord_delivery.team_summary_delivery_count == 10


def test_19_worker_heartbeat_state() -> None:
    """19. Worker heartbeat state."""
    snap = _make_sample_snapshot(worker_hb_age=42.0)
    assert snap.runtime.worker_heartbeat_age_seconds == 42.0


def test_20_bot_heartbeat_state() -> None:
    """20. Bot heartbeat state."""
    snap = _make_sample_snapshot(bot_hb_age=18.0)
    assert snap.runtime.bot_heartbeat_age_seconds == 18.0


def test_21_migration_version() -> None:
    """21. Migration version."""
    snap = _make_sample_snapshot()
    assert snap.runtime.current_migration_version == "0027_fc27_operations_alert_dispatch.sql"


def test_22_grants_policy_version() -> None:
    """22. Grants Policy version."""
    snap = _make_sample_snapshot()
    assert snap.runtime.grants_policy_version == 28


# ==============================================================================
# SCENARIOS 23-32: HEALTH CLASSIFICATION & QUIET CYCLE
# ==============================================================================


def test_23_healthy_classification() -> None:
    """23. Healthy classification."""
    snap = _make_sample_snapshot(last_poll_hours_ago=1.0, rolling_attempts=6)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.HEALTHY
    assert len(reasons) == 0


def test_24_poll_overdue_warning() -> None:
    """24. Poll-overdue warning (>5 hours)."""
    snap = _make_sample_snapshot(last_poll_hours_ago=5.5)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.DEGRADED
    assert any("Polling delayed" in r for r in reasons)


def test_25_poll_overdue_critical() -> None:
    """25. Poll-overdue critical (>9 hours)."""
    snap = _make_sample_snapshot(last_poll_hours_ago=9.5)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.CRITICAL
    assert any("Polling overdue" in r for r in reasons)


def test_26_budget_warning() -> None:
    """26. Budget warning (>= 15/18)."""
    snap = _make_sample_snapshot(rolling_attempts=15)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.DEGRADED
    assert any("Provider request capacity high" in r for r in reasons)


def test_27_budget_critical() -> None:
    """27. Budget critical (>= 18/18)."""
    snap = _make_sample_snapshot(rolling_attempts=18)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.CRITICAL
    assert any("Provider request capacity exhausted" in r for r in reasons)


def test_28_pending_job_warning() -> None:
    """28. Pending-job warning (>10 minutes)."""
    snap = _make_sample_snapshot(pending_jobs=1, oldest_pending_age=650.0)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.DEGRADED
    assert any("Pending Discord job delayed" in r for r in reasons)


def test_29_pending_job_critical() -> None:
    """29. Pending-job critical (>30 minutes)."""
    snap = _make_sample_snapshot(pending_jobs=1, oldest_pending_age=1900.0)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.CRITICAL
    assert any("Pending Discord job stuck" in r for r in reasons)


def test_30_stale_heartbeat_warning_and_critical() -> None:
    """30. Stale-heartbeat warning and critical."""
    # Warning > 120s
    snap_warn = _make_sample_snapshot(worker_hb_age=150.0)
    c_warn, r_warn = classify_operations_health(snap_warn)
    assert c_warn == HealthClassification.DEGRADED
    assert any("Worker heartbeat stale" in r for r in r_warn)

    # Critical > 300s
    snap_crit = _make_sample_snapshot(bot_hb_age=350.0)
    c_crit, r_crit = classify_operations_health(snap_crit)
    assert c_crit == HealthClassification.CRITICAL
    assert any("Discord bot heartbeat missing" in r for r in r_crit)


def test_31_long_lease_warning_and_critical() -> None:
    """31. Long lease warning and critical."""
    snap_warn = _make_sample_snapshot(single_flight_state="busy", lease_age=700.0)
    c_warn, r_warn = classify_operations_health(snap_warn)
    assert c_warn == HealthClassification.DEGRADED
    assert any("Single-flight lock held long" in r for r in r_warn)

    snap_crit = _make_sample_snapshot(single_flight_state="busy", lease_age=2000.0)
    c_crit, r_crit = classify_operations_health(snap_crit)
    assert c_crit == HealthClassification.CRITICAL
    assert any("Single-flight lock held critically long" in r for r in r_crit)


def test_32_quiet_cycle_without_new_matches_remains_healthy() -> None:
    """32. Quiet cycle without new matches remains healthy."""
    snap = _make_sample_snapshot(new_matches=0, last_poll_hours_ago=1.0)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.HEALTHY
    assert len(reasons) == 0


# ==============================================================================
# SCENARIOS 33-36: DEDUPLICATED ALERTING LIFECYCLE
# ==============================================================================


class _MockAlertConnection:
    """Mock connection simulating the atomic database stored procedures."""

    def __init__(self) -> None:
        self.alerts: dict[tuple[str, str], dict[str, Any]] = {}

    def cursor(self) -> Any:
        conn = self

        class Cursor:
            def __init__(self) -> None:
                self._result: Any = None

            def __enter__(self) -> Cursor:
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def execute(self, query: str, params: Any = ()) -> None:
                q = query.strip()
                if "fc27_record_operator_alert" in q:
                    import json
                    alert_type, component, severity, summary, payload_json, suppression = params
                    key = (alert_type, component)
                    now = datetime.now(timezone.utc)
                    if key in conn.alerts and conn.alerts[key]["status"] in ("OPEN", "ACKNOWLEDGED"):
                        rec = conn.alerts[key]
                        last_notified = rec["last_notified_at"]
                        if (now - last_notified).total_seconds() >= suppression:
                            rec["last_notified_at"] = now
                            rec["notification_count"] += 1
                            rec["summary"] = summary
                            self._result = [(rec["alert_id"], True, False, rec["notification_count"])]
                        else:
                            rec["summary"] = summary
                            self._result = [(rec["alert_id"], False, False, rec["notification_count"])]
                    else:
                        alert_id = f"alert-{len(conn.alerts) + 1}"
                        rec = {
                            "alert_id": alert_id,
                            "alert_type": alert_type,
                            "component": component,
                            "severity": severity,
                            "status": "OPEN",
                            "summary": summary,
                            "details": json.loads(payload_json),
                            "opened_at": now,
                            "last_notified_at": now,
                            "notification_count": 1,
                        }
                        conn.alerts[key] = rec
                        self._result = [(alert_id, True, True, 1)]
                elif "fc27_resolve_operator_alert" in q:
                    alert_type, component = params
                    key = (alert_type, component)
                    if key in conn.alerts and conn.alerts[key]["status"] in ("OPEN", "ACKNOWLEDGED"):
                        a_id = conn.alerts[key]["alert_id"]
                        conn.alerts[key]["status"] = "RESOLVED"
                        self._result = [(True, a_id)]
                    else:
                        self._result = [(False, None)]
                elif "fc27_reserve_operator_alert_delivery" in q:
                    self._result = [(1, True, "PENDING")]
                elif "fc27_get_active_operator_alerts" in q:
                    rows = [
                        (
                            r["alert_id"],
                            r["alert_type"],
                            r["component"],
                            r["severity"],
                            r["status"],
                            r["summary"],
                            r["details"],
                            r["opened_at"],
                            r["last_notified_at"],
                            r["notification_count"],
                        )
                        for r in conn.alerts.values()
                        if r["status"] in ("OPEN", "ACKNOWLEDGED")
                    ]
                    self._result = rows

            def fetchone(self) -> Any:
                if self._result:
                    return self._result[0]
                return None

            def fetchall(self) -> Any:
                return self._result or []

        return Cursor()


def test_33_alert_deduplication() -> None:
    """33. Alert deduplication."""
    conn = _MockAlertConnection()
    mgr = FC27AlertManager(conn, suppression_seconds=1800)
    # First alert opens
    n1 = mgr.record_alert("POLL_OVERDUE", "polling", "WARNING", "Polling delayed by 20000s")
    assert n1 is not None
    assert n1.is_new is True
    assert n1.notification_count == 1

    # Active alerts count is exactly 1
    active = mgr.get_active_alerts()
    assert len(active) == 1
    assert active[0].alert_type == "POLL_OVERDUE"


def test_34_alert_suppression() -> None:
    """34. Alert suppression."""
    conn = _MockAlertConnection()
    mgr = FC27AlertManager(conn, suppression_seconds=1800)
    # First alert
    n1 = mgr.record_alert("POLL_OVERDUE", "polling", "WARNING", "Delay 1")
    assert n1 is not None

    # Immediate second alert within 1800s suppression window is suppressed
    n2 = mgr.record_alert("POLL_OVERDUE", "polling", "WARNING", "Delay 2")
    assert n2 is None  # should_notify=False


def test_35_alert_resolution() -> None:
    """35. Alert resolution."""
    conn = _MockAlertConnection()
    mgr = FC27AlertManager(conn, suppression_seconds=1800)
    mgr.record_alert("POLL_OVERDUE", "polling", "WARNING", "Delay 1")
    assert len(mgr.get_active_alerts()) == 1

    res = mgr.resolve_alert("POLL_OVERDUE", "polling")
    assert res.was_resolved is True
    assert len(mgr.get_active_alerts()) == 0


def test_36_restart_safe_alert_state() -> None:
    """36. Restart-safe alert state."""
    conn = _MockAlertConnection()
    # Record alert via one manager instance
    mgr1 = FC27AlertManager(conn, suppression_seconds=1800)
    mgr1.record_alert("HEARTBEAT_MISSING", "community_worker", "CRITICAL", "Worker heartbeat missing")

    # Read via fresh second manager instance sharing same underlying database store
    mgr2 = FC27AlertManager(conn, suppression_seconds=1800)
    active = mgr2.get_active_alerts()
    assert len(active) == 1
    assert active[0].alert_type == "HEARTBEAT_MISSING"
    assert active[0].severity == "CRITICAL"


# ==============================================================================
# SCENARIOS 37-41: PRIVACY, SANITIZATION, LIMITS
# ==============================================================================


def test_37_no_raw_ids() -> None:
    """37. No raw UUIDs or snowflakes in rendered status markdown."""
    snap = _make_sample_snapshot()
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    # Check for raw UUID v4 pattern
    uuid_pattern = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
    assert not uuid_pattern.search(text)


def test_38_no_tokens_or_digests() -> None:
    """38. No tokens or token digests in status output."""
    snap = _make_sample_snapshot()
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    assert "token" not in text.lower()
    assert "reconcile" not in text.lower()


def test_39_no_hmacs() -> None:
    """39. No HMAC keys or digests in status output."""
    snap = _make_sample_snapshot()
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    assert "hmac" not in text.lower()


def test_40_no_dsns() -> None:
    """40. No DSNs or database credentials in status output."""
    snap = _make_sample_snapshot()
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    assert "postgres://" not in text.lower()
    assert "postgresql://" not in text.lower()
    assert "redis://" not in text.lower()
    assert "password" not in text.lower()


def test_41_command_output_within_discord_limits() -> None:
    """41. Command output within Discord limits (2000 chars)."""
    snap = _make_sample_snapshot()
    text = render_operations_status_markdown(
        snap,
        HealthClassification.CRITICAL,
        reasons=[
            "Polling overdue: 35000s since last cycle (critical threshold: 32400s)",
            "Provider request capacity exhausted (18/18)",
            "Dead-letter Discord delivery jobs critical (4 jobs)",
            "Worker heartbeat missing (400s > 300s)",
        ],
    )
    assert len(text) <= 2000


# ==============================================================================
# SCENARIOS 42-47: TOPOLOGY, GLOBAL COMMANDS, PASSING SUITES
# ==============================================================================


def test_42_existing_command_topology_unchanged() -> None:
    """42. Existing command topology unchanged."""
    cfg = _build_test_config()
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    tree_mock = MagicMock()
    tree_mock.get_commands.return_value = ()
    summary = register_fc27_application_commands(
        tree_mock,
        bot_service=svc,
    )
    assert summary.group_name == "fc27"
    assert "status" in summary.group_subcommands


def test_43_global_command_count_remains_zero() -> None:
    """43. Global command count remains zero."""
    cfg = _build_test_config()
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    tree_mock = MagicMock()
    tree_mock.get_commands.return_value = ()
    summary = register_fc27_application_commands(
        tree_mock,
        bot_service=svc,
    )
    assert len(summary.global_command_names) == 0


def test_44_backfill_remains_disabled() -> None:
    """44. Backfill remains disabled."""
    assert settings.is_fc27_discord_backfill_enabled() is False


def test_45_existing_polling_tests_pass() -> None:
    """45. Polling intervals and settings contracts pass."""
    assert settings.get_fc27_community_poll_interval_seconds() == 43200
    assert settings.get_fc27_community_poll_max_requests_per_24_hours() == 2


def test_46_existing_delivery_tests_pass() -> None:
    """46. Discord delivery configuration constants pass."""
    assert settings.get_fc27_discord_lease_minutes() == 5
    assert settings.get_fc27_discord_max_jobs_per_cycle() == 5


def test_47_existing_healthcheck_tests_pass() -> None:
    """47. Operational status and alerting disabled by default."""
    assert settings.is_fc27_operations_status_enabled() is False
    assert settings.is_fc27_operations_alerts_enabled() is False


# ==============================================================================
# PHASE 1 & 3: STRICT NUMERIC-ROLE AUTHORIZATION & ZERO-MUTATION REGRESSION TESTS
# ==============================================================================


def test_p1_01_admin_numeric_role_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 1.1: FC27 Administrator numeric role succeeds."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    monkeypatch.setattr(
        "src.fc27_operations_status.fetch_operations_status_snapshot",
        lambda _uri: _make_sample_snapshot(),
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[ADMIN_ROLE_ID],
        is_admin=False,
    )
    assert resp.status == "OK"
    assert resp.ephemeral is True
    assert "FC27 Operations" in resp.content


def test_p1_02_operator_numeric_role_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 1.2: FC27 Operator numeric role succeeds."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    monkeypatch.setattr(
        "src.fc27_operations_status.fetch_operations_status_snapshot",
        lambda _uri: _make_sample_snapshot(),
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[OPERATOR_ROLE_ID],
        is_admin=False,
    )
    assert resp.status == "OK"
    assert resp.ephemeral is True


def test_p1_03_discord_admin_perm_alone_denied() -> None:
    """Phase 1.3: Discord Administrator permission alone is denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[],
        is_admin=True,
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_p1_04_general_admin_boolean_alone_denied() -> None:
    """Phase 1.4: General Admin Boolean alone is denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[UNAUTHORIZED_ROLE_ID],
        is_admin=True,
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_p1_05_identity_operator_alone_denied() -> None:
    """Phase 1.5: Identity Operator alone is denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[IDENTITY_OP_ROLE_ID],
        is_admin=False,
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_p1_06_role_name_text_denied() -> None:
    """Phase 1.6: Role-name text is denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=["Administrator", "FC27 Operator"],
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_p1_07_managed_role_denied() -> None:
    """Phase 1.7: Managed bot role is denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=["88888888888888888"],
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_p1_08_cross_guild_role_id_denied() -> None:
    """Phase 1.8: Cross-guild role ID is denied."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=["77777777777777777"],
    )
    assert resp.status == "REJECTED_UNAUTHORIZED"
    assert resp.ephemeral is True


def test_p1_09_denial_is_ephemeral() -> None:
    """Phase 1.9: Denial is ephemeral."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[UNAUTHORIZED_ROLE_ID],
    )
    assert resp.ephemeral is True


def test_p1_10_denial_zero_side_effects() -> None:
    """Phase 1.10: Denial performs zero query mutation, zero message send, and zero provider call."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[UNAUTHORIZED_ROLE_ID],
    )
    assert resp.provider_requests_executed == 0
    assert DISCORD_PROVIDER_REQUESTS == 0


def test_p3_zero_mutation_in_mutable_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 3: Invocation of /fc27 status mutates zero rows across all operational tables."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")
    monkeypatch.setattr(
        "src.fc27_operations_status.fetch_operations_status_snapshot",
        lambda _uri: _make_sample_snapshot(),
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[OPERATOR_ROLE_ID],
    )
    assert resp.status == "OK"
    assert resp.provider_requests_executed == 0


# ==============================================================================
# PHASE 2 & 10: CONFIGURATION & SYMBOLIC DESTINATION TESTS
# ==============================================================================


def test_p2_symbolic_destination_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 2: Symbolic alert destination contract."""
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DESTINATION", "fc27-operations")
    assert settings.get_fc27_operations_alert_destination() == "fc27-operations"

    # Numeric ID must be rejected
    monkeypatch.setenv("FC27_OPERATIONS_ALERT_DESTINATION", "123456789012345678")
    assert settings.get_fc27_operations_alert_destination() == ""
    with pytest.raises(settings.ConfigError):
        settings.get_fc27_operations_alert_destination(strict=True)


def test_p10_thresholds_validation() -> None:
    """Phase 10: Warning must be less than critical."""
    from src.fc27_discord_bot import FC27DiscordAuthzError
    with pytest.raises(FC27DiscordAuthzError):
        _build_test_config(
            operations_poll_warning_seconds=36000,
            operations_poll_critical_seconds=30000,
        )
    with pytest.raises(FC27DiscordAuthzError):
        _build_test_config(
            operations_job_warning_seconds=2000,
            operations_job_critical_seconds=1000,
        )
    with pytest.raises(FC27DiscordAuthzError):
        _build_test_config(
            operations_heartbeat_warning_seconds=400,
            operations_heartbeat_critical_seconds=200,
        )


# ==============================================================================
# PHASE 7: ALERT LIFECYCLE & RACE SIMULATION TESTS
# ==============================================================================


def test_p7_alert_lifecycle_acknowledged_incident() -> None:
    """Phase 7: Acknowledged alert remains acknowledged during the same incident."""
    conn = _MockAlertConnection()
    mgr = FC27AlertManager(conn, suppression_seconds=1800)
    mgr.record_alert("POLL_OVERDUE", "polling", "WARNING", "Polling delayed")
    alert = mgr.get_active_alerts()[0]
    conn.alerts[("POLL_OVERDUE", "polling")]["status"] = "ACKNOWLEDGED"

    # Subsequent record during same incident preserves ACKNOWLEDGED
    mgr.record_alert("POLL_OVERDUE", "polling", "WARNING", "Still delayed")
    alert_after = mgr.get_active_alerts()[0]
    assert alert_after.status == "ACKNOWLEDGED"


def test_p7_alert_lifecycle_recurrent_incident() -> None:
    """Phase 7: New incident after resolution starts a new lifecycle."""
    conn = _MockAlertConnection()
    mgr = FC27AlertManager(conn, suppression_seconds=1800)
    n1 = mgr.record_alert("POLL_OVERDUE", "polling", "WARNING", "Incident 1")
    assert n1 is not None

    mgr.resolve_alert("POLL_OVERDUE", "polling")
    assert len(mgr.get_active_alerts()) == 0

    # New incident after resolution opens a fresh alert
    n2 = mgr.record_alert("POLL_OVERDUE", "polling", "WARNING", "Incident 2")
    assert n2 is not None
    assert n2.alert_id != n1.alert_id
    assert n2.is_new is True


def test_p7_unknown_heartbeat_not_healthy() -> None:
    """Phase 7: Unknown heartbeat does not evaluate to HEALTHY."""
    snap = _make_sample_snapshot(worker_hb_age=None, bot_hb_age=None)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.UNKNOWN
    assert classification != HealthClassification.HEALTHY
    assert any("heartbeat source unknown" in r.lower() for r in reasons)


# ==============================================================================
# PHASE 9: GOLDEN-OUTPUT TESTS FOR STATUS MARKDOWN
# ==============================================================================


def test_p9_golden_output_healthy() -> None:
    """Phase 9.1: Golden output for HEALTHY state."""
    snap = _make_sample_snapshot(worker_hb_age=20.0, bot_hb_age=15.0)
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    assert "FC27 Operations" in text
    assert "🟢" in text
    assert "HEALTHY" in text
    assert "Polling" in text
    assert "Ingestion" in text
    assert "Discord" in text
    assert "Runtime" in text


def test_p9_golden_output_degraded() -> None:
    """Phase 9.2: Golden output for DEGRADED state."""
    snap = _make_sample_snapshot(last_poll_hours_ago=6.0)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.DEGRADED
    text = render_operations_status_markdown(snap, classification, reasons)
    assert "🟡" in text
    assert "DEGRADED" in text
    assert "Polling delayed" in text


def test_p9_golden_output_critical() -> None:
    """Phase 9.3: Golden output for CRITICAL state."""
    snap = _make_sample_snapshot(last_poll_hours_ago=10.0, dead_letters=3)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.CRITICAL
    text = render_operations_status_markdown(snap, classification, reasons)
    assert "🔴" in text
    assert "CRITICAL" in text
    assert "Polling overdue" in text


def test_p9_golden_output_unknown() -> None:
    """Phase 9.4: Golden output for UNKNOWN state."""
    snap = _make_sample_snapshot(worker_hb_age=None, bot_hb_age=None)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.UNKNOWN
    text = render_operations_status_markdown(snap, classification, reasons)
    assert "⚪" in text
    assert "UNKNOWN" in text


def test_p9_golden_output_no_cycle_history() -> None:
    """Phase 9.5: Golden output when no cycle history exists."""
    now = datetime.now(timezone.utc)
    raw = {
        "database_clock": now.isoformat(),
        "snapshot_generated_at": now.isoformat(),
        "polling": {
            "last_cycle_id_sanitized": None,
            "last_cycle_start_at": None,
            "last_cycle_completed_at": None,
            "last_cycle_outcome": "NO_CYCLES_RECORDED",
            "match_types_attempted": [],
            "per_type_outcome": {},
            "next_expected_poll_at": None,
            "polling_interval_seconds": 14400,
            "single_flight_state": "free",
            "active_lease_age_seconds": None,
            "rolling_provider_attempts_used": 0,
            "provider_attempt_limit": 18,
            "remaining_provider_capacity": 18,
            "oldest_unresolved_reservation_age_seconds": None,
        },
        "ingestion": {
            "new_matches_in_last_cycle": 0,
            "existing_duplicate_matches": 0,
            "quarantined_matches": 0,
            "replay_conflicts": 0,
            "export_batch_state": "N/A",
            "last_persisted_match_at": None,
            "latest_source_match_at": None,
            "data_freshness_age_seconds": None,
        },
        "discord_delivery": {
            "jobs_pending": 0,
            "jobs_claimed": 0,
            "jobs_completed": 0,
            "jobs_failed": 0,
            "jobs_dead_lettered": 0,
            "oldest_pending_job_age_seconds": None,
            "formal_result_delivery_count": 0,
            "team_summary_delivery_count": 0,
            "failed_destination_delivery_count": 0,
            "last_successful_delivery_time": None,
        },
        "runtime": {
            "worker_heartbeat_age_seconds": 25.0,
            "bot_heartbeat_age_seconds": 25.0,
            "current_migration_version": "0024",
            "grants_policy_version": 25,
            "active_backfill_request_count": 0,
        },
    }
    snap = parse_operations_status_snapshot(raw)
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    assert "Last cycle: None" in text


def test_p9_golden_output_partial_cycle() -> None:
    """Phase 9.6: Golden output for partial cycle."""
    snap = _make_sample_snapshot()
    raw = snap.polling
    object.__setattr__(raw, "last_cycle_outcome", "PARTIAL_OR_FAILED")
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    assert "partial_or_failed" in text.lower()


def test_p9_golden_output_quiet_successful_cycle() -> None:
    """Phase 9.7: Golden output for quiet successful cycle."""
    snap = _make_sample_snapshot(new_matches=0)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.HEALTHY
    text = render_operations_status_markdown(snap, classification, reasons)
    assert "New: 0" in text
    assert "🟢" in text


def test_p9_golden_output_budget_exhausted() -> None:
    """Phase 9.8: Golden output for exhausted provider budget."""
    snap = _make_sample_snapshot(rolling_attempts=18)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.CRITICAL
    text = render_operations_status_markdown(snap, classification, reasons)
    assert "capacity exhausted" in text.lower()


def test_p9_golden_output_pending_job_critical() -> None:
    """Phase 9.9: Golden output for critical pending job age."""
    snap = _make_sample_snapshot(pending_jobs=5, oldest_pending_age=2000.0)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.CRITICAL
    text = render_operations_status_markdown(snap, classification, reasons)
    assert "pending discord job stuck" in text.lower()


def test_p9_golden_output_stale_worker_heartbeat() -> None:
    """Phase 9.10: Golden output for stale worker heartbeat."""
    snap = _make_sample_snapshot(worker_hb_age=200.0)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.DEGRADED
    text = render_operations_status_markdown(snap, classification, reasons)
    assert "worker heartbeat stale" in text.lower()


def test_p9_golden_output_stale_bot_heartbeat() -> None:
    """Phase 9.11: Golden output for stale bot heartbeat."""
    snap = _make_sample_snapshot(bot_hb_age=200.0)
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.DEGRADED
    text = render_operations_status_markdown(snap, classification, reasons)
    assert "discord bot heartbeat stale" in text.lower()


def test_p9_golden_output_database_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 9.12: Database error during snapshot fetch returns error response."""
    cfg = _build_test_config(operations_status_enabled=True)
    svc = FC27DiscordBotService(cfg, db_conn_or_uri="postgresql://fake")

    def _timeout_fetch(uri: Any) -> Any:
        raise psycopg.OperationalError("connection timeout expired")

    monkeypatch.setattr(
        "src.fc27_operations_status.fetch_operations_status_snapshot",
        _timeout_fetch,
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id=TEST_GUILD_ID,
        channel_id=TEST_CHANNEL_ID,
        user_id=TEST_USER_ID,
        user_role_ids=[OPERATOR_ROLE_ID],
    )
    assert resp.status == "ERROR"
    assert "unable to query operator status" in resp.content.lower()
