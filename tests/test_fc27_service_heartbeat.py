"""Unit tests for durable FC27 service heartbeats (no database)."""

from __future__ import annotations

import threading
import time

import pytest

from src.fc27_operations_status import (
    HEARTBEAT_CRITICAL_SECONDS,
    HEARTBEAT_WARNING_SECONDS,
    HealthClassification,
    classify_heartbeat_state,
    classify_operations_health,
    parse_operations_status_snapshot,
    render_operations_status_markdown,
)
from src.fc27_service_heartbeat import (
    ALLOWED_RUNTIME_STATUSES,
    ALLOWED_SERVICE_KEYS,
    HeartbeatConfig,
    ServiceHeartbeat,
    build_service_heartbeat_from_env,
    generate_instance_digest,
    is_valid_instance_digest,
    load_heartbeat_config,
    read_uri_file,
)

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.offline]


VALID_DIGEST = "a" * 64


class _FakeCursor:
    def __init__(self, log: list, *, fail: bool = False, gate: threading.Event | None = None, on_execute=None):
        self._log = log
        self._fail = fail
        self._gate = gate
        self._on_execute = on_execute

    def execute(self, sql, params=None):
        if self._on_execute is not None:
            self._on_execute()
        if self._gate is not None:
            self._gate.wait(timeout=2.0)
        if self._fail:
            raise RuntimeError("postgresql://user:secret@host/db boom")
        self._log.append((sql, params))

    def fetchone(self):
        return (None,)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, log: list, *, fail: bool = False, gate: threading.Event | None = None, on_execute=None):
        self._log = log
        self._fail = fail
        self._gate = gate
        self._on_execute = on_execute

    def cursor(self):
        return _FakeCursor(self._log, fail=self._fail, gate=self._gate, on_execute=self._on_execute)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_connect(log: list, **kwargs):
    def _connect(uri, connect_timeout=None, options=None):
        return _FakeConn(log, **kwargs)

    return _connect


def test_01_only_two_service_keys_accepted() -> None:
    assert ALLOWED_SERVICE_KEYS == {"fc27-community-worker", "fc27-discord-bot"}


def test_02_unknown_service_key_rejected() -> None:
    with pytest.raises(ValueError):
        ServiceHeartbeat("fc27-bogus", "postgresql://x", instance_digest=VALID_DIGEST)


def test_03_invalid_status_rejected() -> None:
    hb = ServiceHeartbeat("fc27-discord-bot", None, instance_digest=VALID_DIGEST)
    with pytest.raises(ValueError):
        hb.set_status("EXPLODE")


def test_04_invalid_digest_rejected() -> None:
    with pytest.raises(ValueError):
        ServiceHeartbeat("fc27-discord-bot", "postgresql://x", instance_digest="nothex")


def test_05_generated_digest_shape_and_uniqueness() -> None:
    first = generate_instance_digest()
    second = generate_instance_digest()
    assert is_valid_instance_digest(first)
    assert len(first) == 64
    assert first != second
    assert first == first.lower()


def test_06_config_bounds_clamped() -> None:
    cfg = load_heartbeat_config(
        {
            "FC27_SERVICE_HEARTBEAT_INTERVAL_SECONDS": "5",
            "FC27_SERVICE_HEARTBEAT_CONNECT_TIMEOUT_SECONDS": "99",
            "FC27_SERVICE_HEARTBEAT_STATEMENT_TIMEOUT_MS": "1",
        }
    )
    assert cfg.interval_seconds == 30
    assert cfg.connect_timeout_seconds == 5
    assert cfg.statement_timeout_ms == 500
    assert HeartbeatConfig().interval_seconds == 60


def test_07_read_uri_file(tmp_path) -> None:
    assert read_uri_file(None) is None
    assert read_uri_file(tmp_path / "missing") is None
    f = tmp_path / "uri"
    f.write_text("postgresql://u:p@h/db\n", encoding="utf-8")
    assert read_uri_file(f) == "postgresql://u:p@h/db"


def test_08_records_starting_then_ready(monkeypatch) -> None:
    log: list = []
    hb = ServiceHeartbeat(
        "fc27-community-worker",
        "postgresql://u@h/db",
        config=HeartbeatConfig(interval_seconds=300),
        instance_digest=VALID_DIGEST,
        connect_fn=_fake_connect(log),
    )
    hb.start("STARTING")
    hb.set_status("READY")
    hb._record("READY")
    hb.stop(join_timeout_seconds=2.0)
    statuses = [params[2] for _, params in log]
    assert "STARTING" in statuses
    assert "READY" in statuses
    assert statuses[-1] == "STOPPING"
    for sql, _ in log:
        assert "fc27_record_service_heartbeat" in sql


def test_09_instance_digest_stable_across_records() -> None:
    log: list = []
    hb = ServiceHeartbeat(
        "fc27-discord-bot",
        "postgresql://u@h/db",
        instance_digest=VALID_DIGEST,
        connect_fn=_fake_connect(log),
    )
    hb._record("READY")
    hb._record("READY")
    digests = {params[1] for _, params in log}
    assert digests == {VALID_DIGEST}


def test_10_no_overlapping_records() -> None:
    log: list = []
    lock = threading.Lock()
    active = {"now": 0, "max": 0}

    def _enter():
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.02)
        with lock:
            active["now"] -= 1

    hb = ServiceHeartbeat(
        "fc27-community-worker",
        "postgresql://u@h/db",
        config=HeartbeatConfig(interval_seconds=300),
        instance_digest=VALID_DIGEST,
        connect_fn=_fake_connect(log, on_execute=_enter),
    )
    hb.start("STARTING")
    for _ in range(5):
        hb.set_status("READY")
    time.sleep(0.1)
    hb.stop(join_timeout_seconds=2.0)
    assert active["max"] == 1
    assert len(log) >= 2


def test_11_failure_isolation() -> None:
    def _boom(uri, connect_timeout=None, options=None):
        raise RuntimeError("postgresql://u:p@h/db connection refused")

    hb = ServiceHeartbeat(
        "fc27-discord-bot",
        "postgresql://u@h/db",
        instance_digest=VALID_DIGEST,
        connect_fn=_boom,
    )
    # Must not raise.
    hb._record("READY")


def test_12_rate_limited_logging() -> None:
    warnings: list[str] = []

    def _boom(uri, connect_timeout=None, options=None):
        raise RuntimeError("postgresql://u:p@h/db down")

    hb = ServiceHeartbeat(
        "fc27-discord-bot",
        "postgresql://u@h/db",
        instance_digest=VALID_DIGEST,
        connect_fn=_boom,
        logger=warnings.append,
    )
    for _ in range(5):
        hb._record("READY")
    assert len(warnings) == 1
    assert "FC27_SERVICE_HEARTBEAT_WARNING" in warnings[0]
    assert "postgresql://" not in warnings[0]


def test_13_disabled_without_uri_records_nothing() -> None:
    log: list = []
    hb = build_service_heartbeat_from_env(
        "fc27-community-worker",
        uri_file=None,
        connect_fn=_fake_connect(log),
    )
    hb.start("STARTING")
    hb.set_status("READY")
    hb.stop()
    assert log == []


def test_14_classify_states() -> None:
    assert classify_heartbeat_state(None, None, False) == "UNKNOWN"
    assert classify_heartbeat_state(10, "READY", True) == "HEALTHY"
    assert (
        classify_heartbeat_state(HEARTBEAT_WARNING_SECONDS + 1, "READY", True)
        == "DEGRADED"
    )
    assert (
        classify_heartbeat_state(HEARTBEAT_CRITICAL_SECONDS + 1, "READY", True)
        == "CRITICAL"
    )


def test_15_unknown_never_healthy() -> None:
    state = classify_heartbeat_state(None, "READY", False)
    assert state == "UNKNOWN"
    assert state != HealthClassification.HEALTHY.value


def test_16_fresh_degraded_status_stays_degraded() -> None:
    assert classify_heartbeat_state(5, "DEGRADED", True) == "DEGRADED"
    assert classify_heartbeat_state(5, "STARTING", True) == "DEGRADED"
    assert classify_heartbeat_state(5, "STOPPING", True) == "DEGRADED"


def _raw_snapshot(worker_age, bot_age, worker_known=False, bot_known=False, worker_rt=None, bot_rt=None):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "database_clock": now.isoformat(),
        "snapshot_generated_at": now.isoformat(),
        "polling": {
            "last_cycle_completed_at": now.isoformat(),
            "last_cycle_outcome": "SUCCEEDED",
            "match_types_attempted": ["leagueMatch", "playoffMatch", "friendlyMatch"],
            "per_type_outcome": {},
            "polling_interval_seconds": 14400,
            "single_flight_state": "free",
            "rolling_provider_attempts_used": 0,
            "provider_attempt_limit": 18,
            "remaining_provider_capacity": 18,
        },
        "ingestion": {},
        "discord_delivery": {},
        "runtime": {
            "worker_heartbeat_age_seconds": worker_age,
            "bot_heartbeat_age_seconds": bot_age,
            "worker_heartbeat_known": worker_known,
            "bot_heartbeat_known": bot_known,
            "worker_runtime_status": worker_rt,
            "bot_runtime_status": bot_rt,
            "current_migration_version": "0028_fc27_operations_alert_canary.sql",
            "grants_policy_version": 29,
            "active_backfill_request_count": 0,
        },
    }


def test_17_parse_durable_fields() -> None:
    snap = parse_operations_status_snapshot(
        _raw_snapshot(30.0, 20.0, True, True, "READY", "READY")
    )
    assert snap.runtime.worker_heartbeat_age_seconds == 30.0
    assert snap.runtime.worker_heartbeat_known is True
    assert snap.runtime.worker_heartbeat_state == "HEALTHY"
    assert snap.runtime.bot_heartbeat_state == "HEALTHY"


def test_18_status_healthy_for_fresh_ready() -> None:
    snap = parse_operations_status_snapshot(_raw_snapshot(30.0, 20.0, True, True, "READY", "READY"))
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.HEALTHY


def test_19_status_degraded_after_warning_threshold() -> None:
    snap = parse_operations_status_snapshot(_raw_snapshot(200.0, 20.0, True, True, "READY", "READY"))
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.DEGRADED
    assert any("Worker heartbeat stale" in r for r in reasons)


def test_20_status_critical_after_critical_threshold() -> None:
    snap = parse_operations_status_snapshot(_raw_snapshot(400.0, 20.0, True, True, "READY", "READY"))
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.CRITICAL
    assert any("Worker heartbeat missing" in r for r in reasons)


def test_21_status_unknown_with_no_row() -> None:
    snap = parse_operations_status_snapshot(_raw_snapshot(None, None, False, False))
    classification, reasons = classify_operations_health(snap)
    assert classification == HealthClassification.UNKNOWN
    assert any("heartbeat source unknown" in r.lower() for r in reasons)


def test_22_render_healthy_heartbeat_lines() -> None:
    snap = parse_operations_status_snapshot(_raw_snapshot(34.0, 21.0, True, True, "READY", "READY"))
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    assert "heartbeat 34s" in text
    assert "heartbeat 21s" in text
    assert len(text) < 2000


def test_23_render_never_leaks_digest_or_container(monkeypatch) -> None:
    digest = "b" * 64
    snap = parse_operations_status_snapshot(_raw_snapshot(10.0, 10.0, True, True, "READY", "READY"))
    text = render_operations_status_markdown(snap, HealthClassification.HEALTHY)
    assert digest not in text
    assert "container" not in text.lower()
    assert "pid" not in text.lower()
    assert "postgresql://" not in text


def test_24_render_stale_and_missing_and_unknown() -> None:
    stale = parse_operations_status_snapshot(_raw_snapshot(200.0, 20.0, True, True, "READY", "READY"))
    stale_text = render_operations_status_markdown(stale, HealthClassification.DEGRADED)
    assert "stale (200s)" in stale_text
    missing = parse_operations_status_snapshot(_raw_snapshot(400.0, 20.0, True, True, "READY", "READY"))
    missing_text = render_operations_status_markdown(missing, HealthClassification.CRITICAL)
    assert "heartbeat missing (400s)" in missing_text
    unknown = parse_operations_status_snapshot(_raw_snapshot(None, None, False, False))
    unknown_text = render_operations_status_markdown(unknown, HealthClassification.UNKNOWN)
    assert "Worker: unknown" in unknown_text


def test_25_allowed_statuses_constant() -> None:
    assert ALLOWED_RUNTIME_STATUSES == {"READY", "STARTING", "DEGRADED", "STOPPING"}
