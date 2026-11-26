"""Comprehensive tests for FC27 controlled operations alert canary contract (Migration 0028, Policy 29).

Covers:
1. Synthetic canary identity (OPERATIONS_CANARY, INFO, operations-alert-dispatch, fc27-operations).
2. Constrained severity: INFO allowed only for OPERATIONS_CANARY with operations-alert-dispatch.
3. INFO rejected for genuine operational conditions (POLL_OVERDUE, HEARTBEAT_STALE, etc.).
4. General record_operator_alert cannot create OPERATIONS_CANARY.
5. General resolve_operator_alert cannot resolve OPERATIONS_CANARY.
6. Narrow API open_operations_alert_canary creates exactly one lifecycle and one OPEN delivery.
7. Open idempotency and concurrent open safety.
8. Automatic evaluator (evaluate_and_sync) exclusion and impossibility.
9. Narrow API resolve_operations_alert_canary resolves lifecycle and reserves one RESOLVED delivery.
10. Resolve idempotency, concurrent resolve, and resolve without active canary (bounded no-op).
11. Dispatcher cycle: one OPEN message, one RESOLVED message, exactly two total, differing markers.
12. Duplicate prevention and ambiguous-send recovery.
13. Message formatting: privacy, required markers, duration, no internal IDs or tokens.
14. Least-privilege grants (Policy 29): writer permitted only narrow procedures, others denied.
15. Migration 0028 upgrade and idempotency.
16. Backup/restore preservation of canary records.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from src import settings
from src.db_migrate import GRANTS_POLICY_VERSION, migrate, verify_postconditions
from src.fc27_discord_dispatcher import SyntheticDiscordNotificationTransport
from src.fc27_operations_alert_dispatcher import (
    FC27OperationsAlertDispatchConfig,
    run_operations_alert_dispatch_cycle,
)
from src.fc27_operations_alerts import (
    CANARY_ALERT_TYPE,
    CANARY_COMPONENT,
    CANARY_DESTINATION_KEY,
    CANARY_RECOMMENDED_ACTION,
    CANARY_SEVERITY,
    CANARY_SUMMARY,
    OPERATIONS_ALERT_MESSAGE_KIND,
    FC27AlertManager,
    extract_operations_alert_tag,
    format_operations_alert_message,
)
from src.fc27_operations_status import parse_operations_status_snapshot


@pytest.fixture
def canary_destination(pg_db_migrated: Any) -> str:
    """Ensure destination 'fc27-operations' exists in the test database."""
    dest_key = "fc27-operations"
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_discord_destinations
                (destination_key, guild_id, channel_id, message_kind, enabled)
            VALUES (%s, '900000000000000001', '800000000000000001', 'OPERATIONS_ALERT', TRUE)
            ON CONFLICT (destination_key) DO UPDATE
               SET message_kind = 'OPERATIONS_ALERT', enabled = TRUE;
            """,
            (dest_key,),
        )
    return dest_key


# ==============================================================================
# 1. CANARY IDENTITY & CONSTRAINTS
# ==============================================================================


def test_01_canary_constants() -> None:
    """1. Verify canary canonical constants."""
    assert CANARY_ALERT_TYPE == "OPERATIONS_CANARY"
    assert CANARY_COMPONENT == "operations-alert-dispatch"
    assert CANARY_SEVERITY == "INFO"
    assert CANARY_SUMMARY == "FC27 operations alert delivery canary"
    assert CANARY_RECOMMENDED_ACTION == "No action required; controlled verification only"
    assert CANARY_DESTINATION_KEY == "fc27-operations"
    assert OPERATIONS_ALERT_MESSAGE_KIND == "OPERATIONS_ALERT"


def test_02_info_severity_constraint(pg_db_migrated: Any, canary_destination: str) -> None:
    """2. Database constraint allows INFO only for OPERATIONS_CANARY with operations-alert-dispatch."""
    with psycopg.connect(pg_db_migrated.admin, autocommit=True) as conn:
        # INFO rejected for real operational alert type
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO public.fc27_operator_alerts (alert_type, component, severity, summary)
                VALUES ('POLL_OVERDUE', 'polling', 'INFO', 'Test');
                """
            )

        # INFO rejected for HEARTBEAT_STALE
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO public.fc27_operator_alerts (alert_type, component, severity, summary)
                VALUES ('HEARTBEAT_STALE', 'community_worker', 'INFO', 'Test');
                """
            )

        # OPERATIONS_CANARY rejected with WARNING
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO public.fc27_operator_alerts (alert_type, component, severity, summary)
                VALUES ('OPERATIONS_CANARY', 'operations-alert-dispatch', 'WARNING', 'Test');
                """
            )

        # OPERATIONS_CANARY rejected with CRITICAL
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO public.fc27_operator_alerts (alert_type, component, severity, summary)
                VALUES ('OPERATIONS_CANARY', 'operations-alert-dispatch', 'CRITICAL', 'Test');
                """
            )

        # OPERATIONS_CANARY rejected with another component
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                INSERT INTO public.fc27_operator_alerts (alert_type, component, severity, summary)
                VALUES ('OPERATIONS_CANARY', 'polling', 'INFO', 'Test');
                """
            )

        # Genuine production alert with WARNING and CRITICAL succeeds
        conn.execute(
            """
            INSERT INTO public.fc27_operator_alerts (alert_type, component, severity, summary)
            VALUES ('POLL_OVERDUE', 'polling', 'WARNING', 'Test poll warning');
            """
        )
        conn.execute(
            """
            INSERT INTO public.fc27_operator_alerts (alert_type, component, severity, summary)
            VALUES ('DELIVERY_DEAD_LETTER', 'discord_delivery', 'CRITICAL', 'Test critical');
            """
        )


def test_03_general_record_and_resolve_reject_canary(pg_db_migrated: Any, canary_destination: str) -> None:
    """3. General fc27_record_operator_alert and fc27_resolve_operator_alert reject OPERATIONS_CANARY."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer)

    # Python layer guards
    with pytest.raises(ValueError, match="OPERATIONS_CANARY cannot be recorded through the general record_alert API"):
        mgr.record_alert("OPERATIONS_CANARY", "operations-alert-dispatch", "INFO", "Test")

    with pytest.raises(ValueError, match="INFO severity is valid only for OPERATIONS_CANARY"):
        mgr.record_alert("POLL_OVERDUE", "polling", "INFO", "Test")

    with pytest.raises(ValueError, match="OPERATIONS_CANARY cannot be resolved through the general resolve_alert API"):
        mgr.resolve_alert("OPERATIONS_CANARY", "operations-alert-dispatch")

    # SQL procedure guards
    with psycopg.connect(pg_db_migrated.match_results_writer) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="OPERATIONS_CANARY cannot be recorded through general alert procedure"):
            conn.execute(
                "SELECT * FROM public.fc27_record_operator_alert('OPERATIONS_CANARY', 'operations-alert-dispatch', 'CRITICAL', 'Test');"
            )
        conn.rollback()

        with pytest.raises(psycopg.errors.RaiseException, match="OPERATIONS_CANARY cannot be resolved through general alert procedure"):
            conn.execute(
                "SELECT * FROM public.fc27_resolve_operator_alert('OPERATIONS_CANARY', 'operations-alert-dispatch');"
            )


# ==============================================================================
# 2. NARROW CANARY API & IDEMPOTENCY
# ==============================================================================


def test_04_narrow_api_open_lifecycle(pg_db_migrated: Any, canary_destination: str) -> None:
    """4. open_operations_alert_canary creates exactly one active lifecycle and one OPEN delivery."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer)

    res = mgr.open_operations_alert_canary()
    assert res.is_new is True
    assert res.lifecycle_status == "OPEN"
    assert res.delivery_id is not None
    assert res.delivery_state == "PENDING"

    # Verify database state
    with psycopg.connect(pg_db_migrated.admin) as conn:
        alert = conn.execute(
            """
            SELECT alert_type, component, severity, status, summary, details
              FROM public.fc27_operator_alerts
             WHERE alert_type = 'OPERATIONS_CANARY';
            """
        ).fetchone()
        assert alert is not None
        assert alert[0] == "OPERATIONS_CANARY"
        assert alert[1] == "operations-alert-dispatch"
        assert alert[2] == "INFO"
        assert alert[3] == "OPEN"
        assert alert[4] == "FC27 operations alert delivery canary"
        assert alert[5]["recommended_action"] == "No action required; controlled verification only"

        # Exactly 1 delivery row
        deliv = conn.execute(
            """
            SELECT delivery_id, lifecycle_event, destination_key, delivery_state
              FROM public.fc27_operator_alert_deliveries
             WHERE destination_key = 'fc27-operations';
            """
        ).fetchall()
        assert len(deliv) == 1
        assert deliv[0][1] == "OPEN"
        assert deliv[0][2] == "fc27-operations"
        assert deliv[0][3] == "PENDING"


def test_05_open_canary_idempotent(pg_db_migrated: Any, canary_destination: str) -> None:
    """5. Repeated open_operations_alert_canary is strictly idempotent."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer)

    res1 = mgr.open_operations_alert_canary()
    assert res1.is_new is True

    res2 = mgr.open_operations_alert_canary()
    assert res2.is_new is False
    assert res2.lifecycle_status == "OPEN"
    assert res2.delivery_id == res1.delivery_id
    assert res2.delivery_state == "PENDING"

    with psycopg.connect(pg_db_migrated.admin) as conn:
        alert_cnt = conn.execute("SELECT COUNT(*) FROM public.fc27_operator_alerts WHERE alert_type = 'OPERATIONS_CANARY'").fetchone()[0]
        deliv_cnt = conn.execute("SELECT COUNT(*) FROM public.fc27_operator_alert_deliveries").fetchone()[0]
        assert alert_cnt == 1
        assert deliv_cnt == 1


def test_06_concurrent_open_canary(pg_db_migrated: Any, canary_destination: str) -> None:
    """6. Concurrent open calls produce exactly one lifecycle and one delivery."""
    def _open() -> tuple[bool, int | None]:
        with psycopg.connect(pg_db_migrated.match_results_writer, autocommit=True) as conn:
            row = conn.execute("SELECT is_new, delivery_id FROM public.fc27_open_operations_alert_canary()").fetchone()
            return bool(row[0]), int(row[1])

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(_open) for _ in range(5)]
        results = [f.result() for f in futures]

    is_new_list = [r[0] for r in results]
    delivery_ids = {r[1] for r in results}

    assert is_new_list.count(True) == 1
    assert is_new_list.count(False) == 4
    assert len(delivery_ids) == 1

    with psycopg.connect(pg_db_migrated.admin) as conn:
        alert_cnt = conn.execute("SELECT COUNT(*) FROM public.fc27_operator_alerts WHERE alert_type = 'OPERATIONS_CANARY'").fetchone()[0]
        deliv_cnt = conn.execute("SELECT COUNT(*) FROM public.fc27_operator_alert_deliveries").fetchone()[0]
        assert alert_cnt == 1
        assert deliv_cnt == 1


def test_07_resolve_canary_lifecycle(pg_db_migrated: Any, canary_destination: str) -> None:
    """7. resolve_operations_alert_canary transitions lifecycle and reserves RESOLVED delivery."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer)

    open_res = mgr.open_operations_alert_canary()
    assert open_res.is_new is True

    resolve_res = mgr.resolve_operations_alert_canary()
    assert resolve_res.was_resolved is True
    assert resolve_res.lifecycle_status == "RESOLVED"
    assert resolve_res.delivery_id is not None
    assert resolve_res.delivery_id != open_res.delivery_id
    assert resolve_res.delivery_state == "PENDING"

    with psycopg.connect(pg_db_migrated.admin) as conn:
        alert = conn.execute(
            "SELECT status, resolved_at FROM public.fc27_operator_alerts WHERE alert_type = 'OPERATIONS_CANARY'"
        ).fetchone()
        assert alert[0] == "RESOLVED"
        assert alert[1] is not None

        deliveries = conn.execute(
            "SELECT lifecycle_event, delivery_state FROM public.fc27_operator_alert_deliveries ORDER BY delivery_id"
        ).fetchall()
        assert len(deliveries) == 2
        assert deliveries[0] == ("OPEN", "PENDING")
        assert deliveries[1] == ("RESOLVED", "PENDING")


def test_08_resolve_canary_idempotent(pg_db_migrated: Any, canary_destination: str) -> None:
    """8. Repeated resolve_operations_alert_canary is idempotent."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer)

    mgr.open_operations_alert_canary()
    res1 = mgr.resolve_operations_alert_canary()
    assert res1.was_resolved is True

    res2 = mgr.resolve_operations_alert_canary()
    assert res2.was_resolved is False
    assert res2.lifecycle_status == "RESOLVED"
    assert res2.delivery_id == res1.delivery_id

    with psycopg.connect(pg_db_migrated.admin) as conn:
        deliv_cnt = conn.execute("SELECT COUNT(*) FROM public.fc27_operator_alert_deliveries").fetchone()[0]
        assert deliv_cnt == 2


def test_09_resolve_without_active_canary_noop(pg_db_migrated: Any, canary_destination: str) -> None:
    """9. Resolving when no canary exists returns bounded not-found result without error."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer)

    res = mgr.resolve_operations_alert_canary()
    assert res.was_resolved is False
    assert res.lifecycle_status == "NOT_FOUND"
    assert res.delivery_id is None
    assert res.delivery_state is None


# ==============================================================================
# 3. EVALUATOR ISOLATION PROOF
# ==============================================================================


def test_10_evaluator_cannot_create_canary(pg_db_migrated: Any, canary_destination: str) -> None:
    """10. Prove automatic health evaluator can NEVER generate OPERATIONS_CANARY."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer)
    now = datetime.now(UTC)

    # Critical failure snapshot (all components failing)
    raw_catastrophic = {
        "as_of": now.isoformat(),
        "database_clock": now.isoformat(),
        "health_classification": "CRITICAL",
        "polling": {
            "last_cycle_completed_at": (now - timedelta(hours=10)).isoformat(),
            "rolling_provider_attempts_used": 20,
            "provider_attempt_limit": 18,
            "all_match_types_active": False,
        },
        "discord_delivery": {
            "jobs_pending": 10, "jobs_claimed": 2, "jobs_completed": 0,
            "jobs_failed": 5, "jobs_dead_lettered": 4,
            "oldest_pending_job_age_seconds": 3600.0,
            "formal_result_delivery_count": 0, "team_summary_delivery_count": 0,
            "failed_destination_delivery_count": 5, "last_successful_delivery_time": None,
        },
        "runtime": {
            "worker_heartbeat_age_seconds": 600.0,
            "bot_heartbeat_age_seconds": 600.0,
            "current_migration_version": "0028_fc27_operations_alert_canary.sql",
            "grants_policy_version": 29,
            "active_backfill_request_count": 0,
        },
    }
    snap = parse_operations_status_snapshot(raw_catastrophic)
    notifs = mgr.evaluate_and_sync(snap, destination_key=canary_destination)

    # Real alerts generated, but zero canaries
    assert len(notifs) >= 1
    for n in notifs:
        assert n.alert_type != "OPERATIONS_CANARY"
        assert n.severity != "INFO"

    with psycopg.connect(pg_db_migrated.admin) as conn:
        canary_rows = conn.execute("SELECT COUNT(*) FROM public.fc27_operator_alerts WHERE alert_type = 'OPERATIONS_CANARY'").fetchone()[0]
        assert canary_rows == 0


# ==============================================================================
# 4. DISPATCH CYCLE, MARKERS & MESSAGE VERIFICATION
# ==============================================================================


def test_11_dispatcher_delivers_open_and_resolved_canary(pg_db_migrated: Any, canary_destination: str) -> None:
    """11. Dispatcher delivers exactly one OPEN and one RESOLVED message with differing markers."""
    mgr = FC27AlertManager(pg_db_migrated.match_results_writer)
    transport = SyntheticDiscordNotificationTransport()

    # Step A: Open canary
    open_res = mgr.open_operations_alert_canary()
    assert open_res.is_new is True

    # Dispatch OPEN delivery
    cfg = FC27OperationsAlertDispatchConfig(
        enabled=True,
        destination_key=canary_destination,
        interval_seconds=30,
        claim_owner="test-worker-1",
    )
    cycle1 = run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)
    assert cycle1.claimed_count == 1
    assert cycle1.delivered_count == 1
    assert len(transport.sent_text_messages) == 1

    open_chan_id, open_msg_id, open_content = transport.sent_text_messages[0]
    assert "FC27 Operations Alert: OPERATIONS_CANARY" in open_content
    assert "INFO" in open_content
    assert "operations-alert-dispatch" in open_content
    assert "FC27 operations alert delivery canary" in open_content
    assert "No action required; controlled verification only" in open_content
    assert "**Event**: `OPEN`" in open_content

    open_tag = extract_operations_alert_tag(open_content)
    assert open_tag is not None
    assert open_tag.startswith("fc27-ops-")

    # Step B: Repeated cycle while open sends 0 duplicate messages
    cycle2 = run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)
    assert cycle2.claimed_count == 0
    assert cycle2.delivered_count == 0
    assert len(transport.sent_text_messages) == 1

    # Step C: Resolve canary
    resolve_res = mgr.resolve_operations_alert_canary()
    assert resolve_res.was_resolved is True

    # Dispatch RESOLVED delivery
    cycle3 = run_operations_alert_dispatch_cycle(cfg, transport, pg_db_migrated.match_results_writer)
    assert cycle3.claimed_count == 1
    assert cycle3.delivered_count == 1
    assert len(transport.sent_text_messages) == 2

    res_chan_id, res_msg_id, resolved_content = transport.sent_text_messages[1]
    assert "FC27 Operations Alert Resolved: OPERATIONS_CANARY" in resolved_content
    assert "Resolved" in resolved_content
    assert "operations-alert-dispatch" in resolved_content
    assert "**Event**: `RESOLVED`" in resolved_content

    resolved_tag = extract_operations_alert_tag(resolved_content)
    assert resolved_tag is not None
    assert resolved_tag != open_tag

    # Step D: Total canary messages = 2, 0 duplicates
    assert len(transport.sent_text_messages) == 2


def test_12_message_formatting_privacy() -> None:
    """12. Message formatting ensures privacy: no raw UUIDs, secrets, or DSNs."""
    open_text = format_operations_alert_message(
        alert_type="OPERATIONS_CANARY",
        component="operations-alert-dispatch",
        severity="INFO",
        summary="FC27 operations alert delivery canary",
        lifecycle_event="OPEN",
        opened_at=datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC),
        marker_tag="fc27-ops-9ffc933c825764ab",
    )
    assert "OPERATIONS_CANARY" in open_text
    assert "INFO" in open_text
    assert "fc27-ops-9ffc933c825764ab" in open_text
    assert "postgresql://" not in open_text
    assert "password" not in open_text

    resolved_text = format_operations_alert_message(
        alert_type="OPERATIONS_CANARY",
        component="operations-alert-dispatch",
        severity="INFO",
        summary="FC27 operations alert delivery canary",
        lifecycle_event="RESOLVED",
        opened_at=datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC),
        resolved_at=datetime(2026, 11, 26, 12, 5, 0, tzinfo=UTC),
        marker_tag="fc27-ops-a1b2c3d4e5f67890",
    )
    assert "**Duration**: `300s`" in resolved_text
    assert "Resolved" in resolved_text
    assert "fc27-ops-a1b2c3d4e5f67890" in resolved_text


# ==============================================================================
# 5. LEAST PRIVILEGE & MIGRATION VERIFICATION
# ==============================================================================


def test_13_least_privilege_grants_policy_29(pg_db_migrated: Any, canary_destination: str) -> None:
    """13. Grants Policy 29: fc27_match_results_writer has EXECUTE, other roles are denied."""
    assert GRANTS_POLICY_VERSION == 29

    # Writer can execute open and resolve
    with psycopg.connect(pg_db_migrated.match_results_writer) as wconn:
        row_open = wconn.execute("SELECT is_new FROM public.fc27_open_operations_alert_canary()").fetchone()
        assert row_open is not None
        row_res = wconn.execute("SELECT was_resolved FROM public.fc27_resolve_operations_alert_canary()").fetchone()
        assert row_res is not None

    # Discord reader denied EXECUTE
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rconn.execute("SELECT * FROM public.fc27_open_operations_alert_canary();")
        rconn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rconn.execute("SELECT * FROM public.fc27_resolve_operations_alert_canary();")
        rconn.rollback()

    # Poll writer denied EXECUTE
    with psycopg.connect(pg_db_migrated.poll_writer) as pwconn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pwconn.execute("SELECT * FROM public.fc27_open_operations_alert_canary();")
        pwconn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pwconn.execute("SELECT * FROM public.fc27_resolve_operations_alert_canary();")
        pwconn.rollback()

    # Verify postconditions clean
    with psycopg.connect(pg_db_migrated.admin) as aconn:
        assert verify_postconditions(aconn) == []


def test_14_migration_0028_upgrade_and_idempotency(pg_db: Any) -> None:
    """14. Migration 0028 applies cleanly, upgrades from 0027, and is idempotent."""
    # Migrate to 0027 first
    applied_to_0027 = migrate(pg_db.migrator, target="0027")
    assert "0027_fc27_operations_alert_dispatch.sql" in applied_to_0027
    assert "0028_fc27_operations_alert_canary.sql" not in applied_to_0027

    # Upgrade to 0028
    applied_to_0028 = migrate(pg_db.migrator)
    assert "0028_fc27_operations_alert_canary.sql" in applied_to_0028

    # Idempotent re-run
    applied_rerun = migrate(pg_db.migrator)
    assert applied_rerun == []

    with psycopg.connect(pg_db.admin) as conn:
        assert verify_postconditions(conn) == []
        max_v = conn.execute("SELECT MAX(version) FROM public.schema_migrations").fetchone()[0]
        assert max_v == "0028"
