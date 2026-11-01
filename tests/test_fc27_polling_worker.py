"""Exhaustive offline unit and integration tests for `src/fc27_polling_worker.py` (`PART P`, 101 tests).

Covers:
- Policy and Settings (Tests 1–16)
- Feature Gates (Tests 17–22)
- Single Flight (Tests 23–32)
- Interval and Cache (Tests 33–40)
- Provider Cycle (Tests 41–52)
- Persistence (Tests 53–65)
- Analytics (Tests 66–72)
- Worker Lifecycle (Tests 73–82)
- CLI (Tests 83–88)
- Docker and Dashboard (Tests 89–101)

Zero external network calls and zero EA DNS lookups are executed (`NETWORK_GUARD=PASS`).
"""

from __future__ import annotations

import json
import os
import random
import re
import signal
import socket
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scripts.fc27_polling_worker import main as polling_cli_main
from src import settings
from src.fc27_api_rate_limit import FC27CircuitState
from src.fc27_clubs_api_contracts import FC27ContractError, FC27SchemaValidationError
from src.fc27_community_provider import (
    COMMUNITY_PROVIDER_ID,
    LIVE_EA_CLUBS_COMMUNITY_CLIENT,
    FC27CommunityPersistenceApplySummary,
    FC27CommunityProviderAdapter,
    FC27CommunityProviderEnvelope,
    normalize_community_match_records,
    run_analytics_from_persisted_community_records,
)
from src.fc27_demo import FC27DemoStateStore, _pseudonymize_match_ref
from src.fc27_demo_api import create_demo_app
from src.fc27_polling_worker import (
    POLL_NORMALIZATION_VERSION,
    POLL_OPERATION_NAME,
    POLL_RESPONSE_SCHEMA_VERSION,
    FC27PollResult,
    FC27PollingHealth,
    FC27PollingIdentityRejectedError,
    FC27PollingPolicy,
    FC27PollingPrivacyRejectedError,
    FC27PollingProviderBlockedError,
    FC27PollingRuntime,
    FC27PollingSchemaDriftError,
    FC27PollingTableAccessDeniedError,
    FC27PollingTransientFailureError,
    FC27SingleFlightLeaseContext,
    build_fc27_polling_policy_from_env,
    build_poll_cache_key,
    build_single_flight_lock_key,
    open_guarded_polling_connection,
    reset_default_polling_runtime,
    run_fc27_poll_cycle,
    run_fc27_polling_worker,
    select_bounded_poll_interval_seconds,
)
from src.fc27_provider import FC27AuthenticationError, FC27TransportError
from src.fc27_repository import FC27ReplayConflictError
from src.settings import ConfigError
from tests.test_fc27_pandas_view import _make_pandas_shim

REPO_ROOT = Path(__file__).resolve().parents[1]

_EXTERNAL_NETWORK_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _block_external_network_and_reset_polling_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block all non-loopback network calls/DNS and reset polling env/runtime before each test."""
    orig_getaddrinfo = socket.getaddrinfo
    orig_create_connection = socket.create_connection

    def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        h_str = str(host or "").strip().lower()
        if h_str not in ("127.0.0.1", "localhost", "::1"):
            _EXTERNAL_NETWORK_CALLS.append(f"dns:{h_str}")
            raise AssertionError(f"Blocked external DNS resolution in pytest: {host!r}")
        return orig_getaddrinfo(host, *args, **kwargs)

    def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else ""
        h_str = str(host).strip().lower()
        if h_str not in ("127.0.0.1", "localhost", "::1"):
            _EXTERNAL_NETWORK_CALLS.append(f"connect:{h_str}")
            raise AssertionError(f"Blocked external socket connection in pytest: {address!r}")
        return orig_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    try:
        import pandas  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "pandas", _make_pandas_shim())

    for env_key in (
        "FC27_COMMUNITY_POLLING_ENABLED",
        "FC27_COMMUNITY_PROVIDER_ENABLED",
        "FC27_PERSISTENCE_ENABLED",
        "FC27_COMMUNITY_POLL_MIN_SECONDS",
        "FC27_COMMUNITY_POLL_MAX_SECONDS",
        "FC27_COMMUNITY_POLL_MAX_REQUESTS",
        "FC27_COMMUNITY_POLL_MAX_RETRIES",
        "FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY",
        "FC27_COMMUNITY_POLL_SINGLE_FLIGHT",
        "FC27_COMMUNITY_POLL_ONCE",
        "FC27_RECOMMENDATIONS_ENABLED",
        "DISPATCH_ENABLED",
        "FC27_CLUB_ID",
        "FC_PLATFORM",
    ):
        monkeypatch.delenv(env_key, raising=False)

    reset_default_polling_runtime()


def _enable_all_polling_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "true")
    monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")


def _build_synthetic_community_match_records(
    count: int = 10,
    *,
    start_id: int = 991001,
    players_per_match: int = 6,
    goals_override: int | None = None,
) -> list[dict[str, Any]]:
    positions = ("GK", "CB", "CB", "CM", "CAM", "ST")
    records: list[dict[str, Any]] = []
    for i in range(count):
        gf = goals_override if goals_override is not None else (2 if i % 2 == 0 else 1)
        ga = 1 if i % 3 != 0 else 1
        res_code = "1" if gf > ga else ("2" if gf < ga else "0")
        opp_res = "2" if gf > ga else ("1" if gf < ga else "0")
        wins = 1 if gf > ga else 0
        losses = 1 if gf < ga else 0
        ties = 1 if gf == ga else 0
        players: list[dict[str, Any]] = []
        for p_idx in range(players_per_match):
            pos = positions[p_idx % len(positions)]
            players.append(
                {
                    "club_id": "330323",
                    "player_id": f"8800{p_idx + 1}",
                    "player_name": f"StripThisPrivateGamerTag_{p_idx + 1}",
                    "position": pos,
                    "rating": 7.5 + (p_idx * 0.1),
                    "goals": 1 if (p_idx == players_per_match - 1 and gf > 0) else 0,
                    "assists": 1 if (p_idx == players_per_match - 2 and gf > 0) else 0,
                    "shots": 2,
                    "passes_made": 18,
                    "pass_attempts": 22,
                    "tackles_made": 2,
                    "tackle_attempts": 3,
                    "saves": 3 if pos == "GK" else 0,
                    "red_cards": 0,
                    "mom": 1 if p_idx == players_per_match - 1 else 0,
                    "seconds_played": 5400,
                    "archetype_id": p_idx + 1,
                    "event_mask": (1 << 1) | (1 << 4),
                    "raw": {"playername": f"StripThisPrivateGamerTag_{p_idx + 1}"},
                }
            )
        records.append(
            {
                "match_id": str(start_id + i),
                "match_type": "leagueMatch",
                "timestamp": 1761800000 + i * 3600,
                "clubs": [
                    {
                        "club_id": "330323",
                        "club_name": "East Coasters",
                        "goals": gf,
                        "goals_against": ga,
                        "result": res_code,
                        "wins": wins,
                        "ties": ties,
                        "losses": losses,
                        "winner_by_dnf": 0,
                        "region_id": 4464981,
                        "team_id": 112,
                    },
                    {
                        "club_id": f"880{100 + i}",
                        "club_name": f"Opponent Club {i + 1}",
                        "goals": ga,
                        "goals_against": gf,
                        "result": opp_res,
                        "wins": losses,
                        "ties": ties,
                        "losses": wins,
                        "winner_by_dnf": 0,
                        "region_id": 4464981,
                        "team_id": 115,
                    },
                ],
                "players": players,
                "aggregate": {"330323": {"passesmade": 108}},
                "raw": {"matchId": str(start_id + i)},
            }
        )
    return records


def _build_validated_envelope(
    count: int = 10,
    *,
    start_id: int = 991001,
    players_per_match: int = 6,
    goals_override: int | None = None,
) -> tuple[FC27CommunityProviderEnvelope, tuple[Any, ...]]:
    raw = _build_synthetic_community_match_records(
        count=count,
        start_id=start_id,
        players_per_match=players_per_match,
        goals_override=goals_override,
    )
    return normalize_community_match_records(
        raw,
        observed_at="2026-10-31T12:00:00Z",
    )


def _fake_persist_summary(
    inserted: int = 10,
    duplicates: int = 0,
    conflicts: int = 0,
) -> FC27CommunityPersistenceApplySummary:
    return FC27CommunityPersistenceApplySummary(
        live_data_persisted="YES",
        match_rows_inserted=inserted,
        team_stat_rows_inserted=inserted,
        player_stat_rows_inserted=inserted * 6,
        duplicates_ignored=duplicates,
        replay_conflicts=conflicts,
        idempotent_duplicate=(inserted == 0 and duplicates > 0 and conflicts == 0),
        export_id="comm_poll_fake",
    )


# ==============================================================================
# PART P: Policy and Settings (Tests 1–16)
# ==============================================================================


def test_001_polling_disabled_by_default() -> None:
    """1. Polling disabled by default."""
    assert settings.is_fc27_community_polling_enabled() is False


def test_002_malformed_polling_enabled_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2. Malformed FC27_COMMUNITY_POLLING_ENABLED fails closed."""
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "maybe-enable")
    assert settings.is_fc27_community_polling_enabled(strict=False) is False
    with pytest.raises(ConfigError):
        settings.is_fc27_community_polling_enabled(strict=True)


def test_003_default_minimum_interval_is_300_seconds() -> None:
    """3. Default minimum interval is 300 seconds."""
    assert settings.get_fc27_community_poll_min_seconds(strict=True) == 300
    assert FC27PollingPolicy().minimum_interval_seconds == 300


def test_004_default_maximum_interval_is_600_seconds() -> None:
    """4. Default maximum interval is 600 seconds."""
    assert settings.get_fc27_community_poll_max_seconds(strict=True) == 600
    assert FC27PollingPolicy().maximum_interval_seconds == 600


def test_005_minimum_interval_below_300_seconds_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5. Minimum interval below 300 seconds is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(minimum_interval_seconds=299)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MIN_SECONDS", "120")
    with pytest.raises(ConfigError):
        settings.get_fc27_community_poll_min_seconds(strict=True)


def test_006_maximum_interval_below_minimum_interval_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6. Maximum interval below minimum interval is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(minimum_interval_seconds=500, maximum_interval_seconds=400)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MIN_SECONDS", "500")
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_SECONDS", "400")
    with pytest.raises(ConfigError):
        settings.get_fc27_community_poll_max_seconds(strict=True)


def test_007_maximum_interval_above_3600_seconds_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7. Maximum interval above 3600 seconds is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(maximum_interval_seconds=3601)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_SECONDS", "7200")
    with pytest.raises(ConfigError):
        settings.get_fc27_community_poll_max_seconds(strict=True)


def test_008_default_maximum_requests_per_cycle_is_1() -> None:
    """8. Default maximum requests per cycle is 1."""
    assert settings.get_fc27_community_poll_max_requests(strict=True) == 1
    assert FC27PollingPolicy().maximum_requests_per_cycle == 1


def test_009_poll_max_requests_above_1_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """9. FC27_COMMUNITY_POLL_MAX_REQUESTS above 1 is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(maximum_requests_per_cycle=2)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS", "2")
    with pytest.raises(ConfigError):
        settings.get_fc27_community_poll_max_requests(strict=True)


def test_010_default_maximum_retries_is_0() -> None:
    """10. Default maximum retries is 0."""
    assert settings.get_fc27_community_poll_max_retries(strict=True) == 0
    assert FC27PollingPolicy().maximum_retries == 0


def test_011_poll_max_retries_above_0_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11. FC27_COMMUNITY_POLL_MAX_RETRIES above 0 is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(maximum_retries=1)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_RETRIES", "1")
    with pytest.raises(ConfigError):
        settings.get_fc27_community_poll_max_retries(strict=True)


def test_012_analytics_on_new_only_false_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """12. FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY=false is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(analytics_on_new_records_only=False)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY", "false")
    with pytest.raises(ConfigError):
        settings.is_fc27_community_poll_analytics_on_new_only(strict=True)


def test_013_single_flight_false_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """13. FC27_COMMUNITY_POLL_SINGLE_FLIGHT=false is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(single_flight_enabled=False)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_SINGLE_FLIGHT", "false")
    with pytest.raises(ConfigError):
        settings.is_fc27_community_poll_single_flight(strict=True)


def test_014_non_east_coasters_club_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """14. Non-East-Coasters club ID is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(club_id="999999")
    monkeypatch.setenv("FC27_CLUB_ID", "999999")
    with pytest.raises(ConfigError):
        build_fc27_polling_policy_from_env(strict=True)


def test_015_non_common_gen5_platform_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15. Non-common-gen5 platform is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(platform="common-gen4")
    monkeypatch.setenv("FC_PLATFORM", "ps5")
    with pytest.raises(ConfigError):
        build_fc27_polling_policy_from_env(strict=True)


def test_016_non_fc27_game_version_is_rejected() -> None:
    """16. Non-FC27 game version is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(game_version="FC26")


# ==============================================================================
# PART P: Feature Gates (Tests 17–22)
# ==============================================================================


def test_017_poll_cycle_returns_disabled_when_polling_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """17. Poll cycle returns DISABLED when FC27_COMMUNITY_POLLING_ENABLED=false."""
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "false")
    monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    calls = 0

    def _never_called() -> FC27CommunityProviderEnvelope:
        nonlocal calls
        calls += 1
        raise AssertionError("Provider should not be called when disabled")

    res = run_fc27_poll_cycle(confirm_live_polling=True, fetch_matches_fn=_never_called)
    assert res.poll_status == "DISABLED"
    assert res.provider_requests == 0
    assert calls == 0


def test_018_poll_cycle_blocked_when_community_provider_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """18. Poll cycle returns BLOCKED_FEATURE_GATE when FC27_COMMUNITY_PROVIDER_ENABLED=false."""
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "true")
    monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "false")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    res = run_fc27_poll_cycle(confirm_live_polling=True)
    assert res.poll_status == "BLOCKED_FEATURE_GATE"
    assert res.provider_requests == 0


def test_019_poll_cycle_blocked_when_persistence_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """19. Poll cycle returns BLOCKED_FEATURE_GATE when FC27_PERSISTENCE_ENABLED=false."""
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "true")
    monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "false")
    res = run_fc27_poll_cycle(confirm_live_polling=True)
    assert res.poll_status == "BLOCKED_FEATURE_GATE"
    assert res.provider_requests == 0


def test_020_poll_cycle_blocked_without_confirm_live_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """20. Poll cycle returns BLOCKED_FEATURE_GATE without --confirm-live-polling."""
    _enable_all_polling_gates(monkeypatch)
    res = run_fc27_poll_cycle(confirm_live_polling=False)
    assert res.poll_status == "BLOCKED_FEATURE_GATE"
    assert res.provider_requests == 0


def test_021_enabling_demo_mode_does_not_enable_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """21. Enabling FC27_DEMO_MODE=true does not enable polling."""
    monkeypatch.setenv("FC27_DEMO_MODE", "true")
    assert settings.is_fc27_community_polling_enabled() is False
    res = run_fc27_poll_cycle(confirm_live_polling=True)
    assert res.poll_status == "DISABLED"
    assert res.provider_requests == 0


def test_022_enabling_analytics_does_not_enable_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """22. Enabling FC27_ANALYTICS_ENABLED=true does not enable polling."""
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")
    assert settings.is_fc27_community_polling_enabled() is False
    res = run_fc27_poll_cycle(confirm_live_polling=True)
    assert res.poll_status == "DISABLED"
    assert res.provider_requests == 0


# ==============================================================================
# PART P: Single Flight (Tests 23–32)
# ==============================================================================


def test_023_single_flight_lock_acquires_cleanly_when_idle() -> None:
    """23. Single-flight lock acquires cleanly when idle."""
    policy = FC27PollingPolicy()
    with FC27SingleFlightLeaseContext(policy=policy, owner_id="owner-a") as lease:
        assert lease.acquired is True
        assert lease.acquired_at is not None


def test_024_concurrent_poll_cycle_returns_skipped_single_flight_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """24. Concurrent poll cycle returns SKIPPED_SINGLE_FLIGHT_BUSY."""
    _enable_all_polling_gates(monkeypatch)
    policy = FC27PollingPolicy()
    with FC27SingleFlightLeaseContext(policy=policy, owner_id="holder") as held:
        assert held.acquired is True
        res = run_fc27_poll_cycle(
            policy=policy,
            confirm_live_polling=True,
            use_cache=False,
        )
        assert res.poll_status == "SKIPPED_SINGLE_FLIGHT_BUSY"
        assert res.single_flight_acquired is False


def test_025_busy_poll_cycle_makes_zero_provider_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """25. Busy poll cycle makes zero provider requests."""
    _enable_all_polling_gates(monkeypatch)
    policy = FC27PollingPolicy()
    provider_calls = 0

    def _fetch() -> FC27CommunityProviderEnvelope:
        nonlocal provider_calls
        provider_calls += 1
        env, _ = _build_validated_envelope(3)
        return env

    with FC27SingleFlightLeaseContext(policy=policy, owner_id="holder"):
        res = run_fc27_poll_cycle(
            policy=policy,
            confirm_live_polling=True,
            fetch_matches_fn=_fetch,
            use_cache=False,
        )
        assert res.poll_status == "SKIPPED_SINGLE_FLIGHT_BUSY"
        assert res.provider_requests == 0
        assert provider_calls == 0


def test_026_single_flight_lock_releases_after_successful_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """26. Single-flight lock releases after successful cycle."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=3),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    with FC27SingleFlightLeaseContext(policy=FC27PollingPolicy(), owner_id="after") as lease:
        assert lease.acquired is True


def test_027_single_flight_lock_releases_after_provider_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """27. Single-flight lock releases after provider exception."""
    _enable_all_polling_gates(monkeypatch)

    def _boom() -> FC27CommunityProviderEnvelope:
        raise FC27PollingTransientFailureError("socket timeout")

    res = run_fc27_poll_cycle(confirm_live_polling=True, fetch_matches_fn=_boom)
    assert res.poll_status == "TRANSIENT_FAILURE"
    with FC27SingleFlightLeaseContext(policy=FC27PollingPolicy(), owner_id="after") as lease:
        assert lease.acquired is True


def test_028_single_flight_lock_releases_after_persistence_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """28. Single-flight lock releases after persistence exception."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3)

    def _bad_persist(_e: FC27CommunityProviderEnvelope) -> FC27CommunityPersistenceApplySummary:
        raise FC27ReplayConflictError("replay conflict")

    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=_bad_persist,
    )
    assert res.poll_status == "REPLAY_CONFLICT"
    with FC27SingleFlightLeaseContext(policy=FC27PollingPolicy(), owner_id="after") as lease:
        assert lease.acquired is True


def test_029_single_flight_lock_releases_after_analytics_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """29. Single-flight lock releases after analytics exception."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3)

    def _bad_analytics() -> dict[str, Any]:
        raise RuntimeError("analytics failed")

    with pytest.raises(RuntimeError, match="analytics failed"):
        run_fc27_poll_cycle(
            confirm_live_polling=True,
            fetch_matches_fn=lambda: env,
            persist_fn=lambda _e: _fake_persist_summary(inserted=3),
            analytics_fn=_bad_analytics,
        )
    with FC27SingleFlightLeaseContext(policy=FC27PollingPolicy(), owner_id="after") as lease:
        assert lease.acquired is True


def test_030_single_flight_lock_releases_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """30. Single-flight lock releases on shutdown."""
    _enable_all_polling_gates(monkeypatch)
    stop_event = threading.Event()
    stop_event.set()
    res = run_fc27_poll_cycle(confirm_live_polling=True, shutdown_event=stop_event)
    assert res.poll_status == "SHUTDOWN_REQUESTED"
    with FC27SingleFlightLeaseContext(policy=FC27PollingPolicy(), owner_id="after") as lease:
        assert lease.acquired is True


def test_031_single_flight_lock_not_held_during_sleep_between_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """31. Single-flight lock is not held during sleep between cycles."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3)
    lock_free_during_sleep = False

    def _sleep_probe(_seconds: float) -> bool:
        nonlocal lock_free_during_sleep
        with FC27SingleFlightLeaseContext(
            policy=FC27PollingPolicy(), owner_id="probe-during-sleep"
        ) as lease:
            lock_free_during_sleep = lease.acquired
        return True  # request stop after checking

    results = run_fc27_polling_worker(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=3),
        sleep_fn=_sleep_probe,
        register_signals=False,
    )
    assert len(results) == 1
    assert lock_free_during_sleep is True


def test_032_postgres_advisory_lock_key_is_deterministic(pg_db_migrated) -> None:
    """32. PostgreSQL advisory lock key is deterministic for East Coasters recent league matches."""
    key_a = build_single_flight_lock_key(FC27PollingPolicy())
    key_b = build_single_flight_lock_key(FC27PollingPolicy())
    assert key_a == key_b
    assert key_a == (
        "fc27_poll:fc-clubs-api-community-v1",
        "330323:common-gen5:FC27:RECENT_LEAGUE_MATCHES",
    )
    with FC27SingleFlightLeaseContext(
        policy=FC27PollingPolicy(),
        owner_id="pg-owner-1",
        db_uri_or_conn_factory=pg_db_migrated.app,
    ) as lease:
        assert lease.acquired is True
        assert lease.mechanism == "POSTGRES_ADVISORY_LOCK"


# ==============================================================================
# PART P: Interval and Cache (Tests 33–40)
# ==============================================================================


def test_033_poll_interval_selector_returns_values_within_300_600() -> None:
    """33. Poll interval selector returns values within [300, 600]."""
    policy = FC27PollingPolicy(minimum_interval_seconds=300, maximum_interval_seconds=600)
    rng = random.Random(42)
    for _ in range(50):
        val = select_bounded_poll_interval_seconds(policy, rng=rng)
        assert 300 <= val <= 600


def test_034_poll_cycle_before_next_eligible_at_returns_skipped_min_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """34. Poll cycle before next_eligible_at returns SKIPPED_MIN_INTERVAL."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    rt.next_eligible_at = now + timedelta(seconds=300)
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        now_utc_fn=lambda: now + timedelta(seconds=60),
    )
    assert res.poll_status == "SKIPPED_MIN_INTERVAL"


def test_035_skipped_min_interval_makes_zero_provider_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """35. SKIPPED_MIN_INTERVAL makes zero provider requests."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    rt.next_eligible_at = now + timedelta(seconds=300)
    called = 0

    def _fetch() -> FC27CommunityProviderEnvelope:
        nonlocal called
        called += 1
        env, _ = _build_validated_envelope(3)
        return env

    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=_fetch,
        now_utc_fn=lambda: now + timedelta(seconds=10),
    )
    assert res.poll_status == "SKIPPED_MIN_INTERVAL"
    assert res.provider_requests == 0
    assert called == 0


def test_036_fresh_cache_entry_returns_skipped_cache_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """36. Fresh cache entry returns SKIPPED_CACHE_FRESH."""
    _enable_all_polling_gates(monkeypatch)
    now_mono = [1000.0]
    now_utc = [datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)]
    rt = FC27PollingRuntime(clock=lambda: now_mono[0])
    env, _ = _build_validated_envelope(3)

    first = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=3),
        now_utc_fn=lambda: now_utc[0],
        now_epoch_fn=lambda: now_mono[0],
    )
    assert first.poll_status == "NO_NEW_MATCHES"

    # Advance past next_eligible_at or clear next_eligible_at while keeping cache fresh (< 300s)
    rt.next_eligible_at = None
    now_mono[0] = 1100.0
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=3),
        now_utc_fn=lambda: now_utc[0] + timedelta(seconds=100),
        now_epoch_fn=lambda: now_mono[0],
    )
    assert second.poll_status == "SKIPPED_CACHE_FRESH"
    assert second.cache_status == "HIT_FRESH"


def test_037_skipped_cache_fresh_makes_zero_provider_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """37. SKIPPED_CACHE_FRESH makes zero provider requests."""
    _enable_all_polling_gates(monkeypatch)
    now_mono = [1000.0]
    rt = FC27PollingRuntime(clock=lambda: now_mono[0])
    _, spec = build_poll_cache_key(rt.policy)
    rt.cache.put(spec, {"payload_hash": "a" * 64}, ttl_override_seconds=300)

    calls = 0

    def _fetch() -> FC27CommunityProviderEnvelope:
        nonlocal calls
        calls += 1
        env, _ = _build_validated_envelope(3)
        return env

    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=_fetch,
        now_epoch_fn=lambda: now_mono[0],
    )
    assert res.poll_status == "SKIPPED_CACHE_FRESH"
    assert res.provider_requests == 0
    assert calls == 0


def test_038_expired_cache_entry_permits_one_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """38. Expired cache entry permits one provider request."""
    _enable_all_polling_gates(monkeypatch)
    now_mono = [1000.0]
    rt = FC27PollingRuntime(clock=lambda: now_mono[0])
    _, spec = build_poll_cache_key(rt.policy)
    rt.cache.put(spec, {"payload_hash": "a" * 64}, ttl_override_seconds=300)

    # Advance past 300s TTL
    now_mono[0] = 1305.0
    env, _ = _build_validated_envelope(3)
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=3),
        now_epoch_fn=lambda: now_mono[0],
    )
    assert res.poll_status == "NO_NEW_MATCHES"
    assert res.provider_requests == 1


def test_039_failed_or_rejected_responses_are_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """39. Failed or rejected responses are never cached."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    _, spec = build_poll_cache_key(rt.policy)

    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: (_ for _ in ()).throw(
            FC27PollingSchemaDriftError("drift")
        ),
    )
    assert res.poll_status == "SCHEMA_DRIFT"
    assert rt.cache.get(spec) is None


def test_040_cache_key_includes_all_required_dimensions() -> None:
    """40. Cache key includes provider, operation, club ID, platform, game version, match type, count, schema version, and normalization version."""
    digest, spec = build_poll_cache_key(FC27PollingPolicy())
    assert len(digest) == 64
    payload = spec.canonical_payload()
    assert payload["provider_id"] == COMMUNITY_PROVIDER_ID
    assert payload["operation"] == POLL_OPERATION_NAME
    assert payload["club_id"] == "330323"
    assert payload["platform"] == "common-gen5"
    assert payload["game_version"] == "FC27"
    assert payload["response_schema_version"] == POLL_RESPONSE_SCHEMA_VERSION
    assert payload["normalization_version"] == POLL_NORMALIZATION_VERSION
    params_dict = dict(payload["bounded_parameters"])
    assert params_dict["match_type"] == "leagueMatch"
    assert params_dict["count"] == "10"


# ==============================================================================
# PART P: Provider Cycle (Tests 41–52)
# ==============================================================================


def test_041_enabled_poll_cycle_makes_at_most_1_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """41. Enabled poll cycle makes at most 1 provider request."""
    _enable_all_polling_gates(monkeypatch)
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_matches_callable=lambda _mt, _cnt: _build_synthetic_community_match_records(10),
    )
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        adapter=adapter,
        persist_fn=lambda _e: _fake_persist_summary(inserted=10),
        analytics_fn=lambda: {"confidence_tier": "MODERATE_CONFIDENCE"},
    )
    assert res.provider_requests == 1
    assert adapter.total_requests_executed == 1
    assert adapter.identity_requests_executed == 0
    assert adapter.match_requests_executed == 1


def test_042_enabled_poll_cycle_attempts_0_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """42. Enabled poll cycle attempts 0 retries."""
    _enable_all_polling_gates(monkeypatch)
    attempts = 0

    def _fail_once() -> FC27CommunityProviderEnvelope:
        nonlocal attempts
        attempts += 1
        raise FC27TransportError("Community client request timed out")

    res = run_fc27_poll_cycle(confirm_live_polling=True, fetch_matches_fn=_fail_once)
    assert res.poll_status == "TRANSIENT_FAILURE"
    assert res.provider_requests == 1
    assert res.retries_attempted == 0
    assert attempts == 1


def test_043_provider_identity_mismatch_returns_identity_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """43. Provider identity mismatch returns IDENTITY_REJECTED."""
    _enable_all_polling_gates(monkeypatch)
    bad_records = _build_synthetic_community_match_records(1)
    bad_records[0]["clubs"][0]["club_id"] = "999999"
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_matches_callable=lambda _mt, _cnt: bad_records,
    )
    res = run_fc27_poll_cycle(confirm_live_polling=True, adapter=adapter)
    assert res.poll_status == "IDENTITY_REJECTED"


def test_044_unknown_field_or_schema_drift_returns_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """44. Unknown field or schema drift returns SCHEMA_DRIFT."""
    _enable_all_polling_gates(monkeypatch)
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_matches_callable=lambda _mt, _cnt: [{"invalid_schema": True}],
    )
    res = run_fc27_poll_cycle(confirm_live_polling=True, adapter=adapter)
    assert res.poll_status == "SCHEMA_DRIFT"


def test_045_disallowed_privacy_field_returns_privacy_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """45. Disallowed privacy field returns PRIVACY_REJECTED."""
    _enable_all_polling_gates(monkeypatch)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: (_ for _ in ()).throw(
            FC27PollingPrivacyRejectedError("Forbidden persona_id detected")
        ),
    )
    assert res.poll_status == "PRIVACY_REJECTED"


def test_046_timeout_returns_transient_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """46. Timeout returns TRANSIENT_FAILURE without retry."""
    _enable_all_polling_gates(monkeypatch)
    calls = 0

    def _timeout_fn() -> FC27CommunityProviderEnvelope:
        nonlocal calls
        calls += 1
        raise FC27TransportError("Community client request timed out")

    res = run_fc27_poll_cycle(confirm_live_polling=True, fetch_matches_fn=_timeout_fn)
    assert res.poll_status == "TRANSIENT_FAILURE"
    assert res.retries_attempted == 0
    assert calls == 1


def test_047_upstream_http_403_429_challenge_returns_provider_blocked_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """47. Upstream HTTP 403/429/challenge returns PROVIDER_BLOCKED without retry."""
    _enable_all_polling_gates(monkeypatch)
    calls = 0

    def _blocked_fn() -> FC27CommunityProviderEnvelope:
        nonlocal calls
        calls += 1
        raise FC27AuthenticationError("HTTP 403 Forbidden from EA endpoint")

    res = run_fc27_poll_cycle(confirm_live_polling=True, fetch_matches_fn=_blocked_fn)
    assert res.poll_status == "PROVIDER_BLOCKED"
    assert res.retries_attempted == 0
    assert calls == 1


def test_048_repeated_failures_open_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """48. Repeated failures open the circuit breaker."""
    _enable_all_polling_gates(monkeypatch)
    policy = FC27PollingPolicy(circuit_breaker_failure_threshold=2)
    rt = FC27PollingRuntime(policy=policy)

    r1 = run_fc27_poll_cycle(
        policy=policy,
        runtime=rt,
        confirm_live_polling=True,
        use_rate_limiter=False,
        fetch_matches_fn=lambda: (_ for _ in ()).throw(
            FC27TransportError("Community client request timed out")
        ),
    )
    assert r1.poll_status == "TRANSIENT_FAILURE"
    assert rt.circuit_breaker.state is FC27CircuitState.CLOSED

    r2 = run_fc27_poll_cycle(
        policy=policy,
        runtime=rt,
        confirm_live_polling=True,
        use_rate_limiter=False,
        fetch_matches_fn=lambda: (_ for _ in ()).throw(
            FC27TransportError("Community client request timed out")
        ),
    )
    assert r2.poll_status == "TRANSIENT_FAILURE"
    assert rt.circuit_breaker.state is FC27CircuitState.OPEN


def test_049_open_circuit_breaker_blocks_provider_calls_before_network_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """49. Open circuit breaker blocks provider calls before network execution."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    rt.circuit_breaker.record_failure("HTTP_403")
    assert rt.circuit_breaker.state is FC27CircuitState.OPEN

    called = 0

    def _fetch() -> FC27CommunityProviderEnvelope:
        nonlocal called
        called += 1
        env, _ = _build_validated_envelope(3)
        return env

    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=_fetch,
    )
    assert res.poll_status == "PROVIDER_BLOCKED"
    assert res.provider_requests == 0
    assert called == 0


def test_050_schema_drift_trips_circuit_breaker_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """50. Schema drift trips the circuit breaker immediately."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: (_ for _ in ()).throw(
            FC27SchemaValidationError("Unexpected schema drift")
        ),
    )
    assert res.poll_status == "SCHEMA_DRIFT"
    assert rt.circuit_breaker.state is FC27CircuitState.OPEN


def test_051_replay_conflict_trips_circuit_breaker_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """51. Replay conflict trips the circuit breaker immediately."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(3)
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: (_ for _ in ()).throw(
            FC27ReplayConflictError("Replay conflict detected")
        ),
    )
    assert res.poll_status == "REPLAY_CONFLICT"
    assert rt.circuit_breaker.state is FC27CircuitState.OPEN


def test_052_raw_response_body_headers_and_cookies_are_not_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """52. Raw response body, headers, and cookies are not retained."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=3),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
    )
    dumped = json.dumps(res.model_dump(mode="json")).lower()
    for forbidden in ("raw_body", "response_headers", "cookies", "set_cookie", "stripthisprivategamertag"):
        assert forbidden not in dumped


# ==============================================================================
# PART P: Persistence (Tests 53–65)
# ==============================================================================


def test_053_first_valid_10_match_batch_persists_1_club_1_batch_10_matches_60_player_stats(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """53. First valid 10-match batch persists 1 club, 1 batch, 10 matches, and 60 player stat rows."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(10, players_per_match=6)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.inserted_match_count == 10
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_clubs WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT COUNT(*) FROM public.fc27_export_batches WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 10
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_match_stats WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 60


def test_054_first_valid_batch_returns_new_matches_persisted(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """54. First valid batch returns NEW_MATCHES_PERSISTED."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(10, players_per_match=6)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"


def test_055_identical_second_poll_cycle_persists_0_new_matches_and_0_new_player_stats(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """55. Identical second poll cycle persists 0 new matches and 0 new player stat rows."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(10, players_per_match=6)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert second.inserted_match_count == 0
    assert second.duplicate_ignored_count == 10
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 10
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_match_stats WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 60


def test_056_identical_second_poll_cycle_returns_no_new_matches(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """56. Identical second poll cycle returns NO_NEW_MATCHES."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(10, players_per_match=6)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert second.poll_status == "NO_NEW_MATCHES"


def test_057_partial_new_batch_inserts_only_new_matches_and_player_stats(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """57. Partial new batch (8 existing + 2 new matches) inserts only the 2 new matches and their player stat rows."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env_initial, _ = _build_validated_envelope(8, start_id=991001, players_per_match=6)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_initial,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    env_overlap, _ = _build_validated_envelope(10, start_id=991001, players_per_match=6)
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_overlap,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert second.inserted_match_count == 2
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 10
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_match_stats WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 60


def test_058_partial_new_batch_returns_new_matches_persisted_with_2_inserted_8_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """58. Partial new batch returns NEW_MATCHES_PERSISTED with inserted_match_count=2 and duplicate_ignored_count=8."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env_initial, _ = _build_validated_envelope(8, start_id=991001, players_per_match=6)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_initial,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    env_overlap, _ = _build_validated_envelope(10, start_id=991001, players_per_match=6)
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_overlap,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert second.poll_status == "NEW_MATCHES_PERSISTED"
    assert second.inserted_match_count == 2
    assert second.duplicate_ignored_count == 8


def test_059_changed_replay_conflict_rolls_back_transactionally(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """59. Changed replay conflict rolls back transactionally."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env_initial, _ = _build_validated_envelope(5, start_id=991001, players_per_match=6)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_initial,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    env_conflict, _ = _build_validated_envelope(
        6, start_id=991001, players_per_match=6, goals_override=9
    )
    conflict_res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_conflict,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert conflict_res.inserted_match_count == 0
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 5


def test_060_changed_replay_conflict_returns_replay_conflict(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """60. Changed replay conflict returns REPLAY_CONFLICT."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env_initial, _ = _build_validated_envelope(3, start_id=991001, players_per_match=6)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_initial,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    env_conflict, _ = _build_validated_envelope(
        3, start_id=991001, players_per_match=6, goals_override=8
    )
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_conflict,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "REPLAY_CONFLICT"
    assert res.replay_conflict_count > 0


def test_061_changed_replay_conflict_does_not_overwrite_existing_stored_matches(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """61. Changed replay conflict does not overwrite existing stored matches."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env_initial, _ = _build_validated_envelope(3, start_id=991001, players_per_match=6)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_initial,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    env_conflict, _ = _build_validated_envelope(
        3, start_id=991001, players_per_match=6, goals_override=7
    )
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env_conflict,
        use_cache=False,
        use_rate_limiter=False,
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT goals_for FROM public.fc27_matches WHERE external_match_id = '991001'"
            )
            assert cur.fetchone()[0] == 2


def test_062_player_identifiers_are_pseudonymous_plr_ecf_12_hex(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """62. Player identifiers are pseudonymous plr_ecf_<12 lowercase hex>."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, players_per_match=6)
    run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT local_player_reference FROM public.fc27_player_match_stats")
            refs = [r[0] for r in cur.fetchall()]
    assert len(refs) == 6
    for ref in refs:
        assert re.fullmatch(r"plr_ecf_[0-9a-f]{12}", ref)


def test_063_match_references_are_pseudonymous_mref_ecf_12_hex() -> None:
    """63. Match references are pseudonymous mref_ecf_<12 lowercase hex>."""
    env, _ = _build_validated_envelope(3)
    for rec in env.normalized_records:
        mref = _pseudonymize_match_ref(rec["match_id"])
        assert re.fullmatch(r"mref_ecf_[0-9a-f]{12}", mref)


def test_064_display_alias_remains_null_for_all_persisted_rows(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """64. display_alias remains NULL for all persisted rows."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, players_per_match=6)
    run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM public.fc27_player_match_stats WHERE display_alias IS NOT NULL"
            )
            assert cur.fetchone()[0] == 0


def test_065_provenance_fields_equal_community_v1_and_live_community_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """65. Provenance fields equal fc-clubs-api-community-v1 and LIVE_EA_CLUBS_COMMUNITY_CLIENT."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=3),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
    )
    assert res.provider_id == COMMUNITY_PROVIDER_ID
    assert res.source_classification == LIVE_EA_CLUBS_COMMUNITY_CLIENT


# ==============================================================================
# PART P: Analytics (Tests 66–72)
# ==============================================================================


def test_066_search_scout_analyst_execute_when_inserted_match_count_positive(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """66. Search, Scout, and Analyst execute when inserted_match_count > 0."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(10, players_per_match=6)
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.inserted_match_count == 10
    assert res.analytics_executed is True
    assert rt.last_analytics_bundle is not None
    assert len(rt.last_analytics_bundle["search_context_hash"]) == 64
    assert len(rt.last_analytics_bundle["scout_report_hash"]) == 64
    assert len(rt.last_analytics_bundle["analyst_report_hash"]) == 64


def test_067_search_scout_analyst_do_not_execute_when_inserted_match_count_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """67. Search, Scout, and Analyst do not execute when inserted_match_count == 0."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(5)
    analytics_calls = 0

    def _analytics() -> dict[str, Any]:
        nonlocal analytics_calls
        analytics_calls += 1
        return {"confidence_tier": "LOW_CONFIDENCE"}

    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=5),
        analytics_fn=_analytics,
    )
    assert res.poll_status == "NO_NEW_MATCHES"
    assert res.analytics_executed is False
    assert analytics_calls == 0


def test_068_search_scout_analyst_do_not_execute_on_skipped_cache_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """68. Search, Scout, and Analyst do not execute on SKIPPED_CACHE_FRESH."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    _, spec = build_poll_cache_key(rt.policy)
    rt.cache.put(spec, {"payload_hash": "a" * 64}, ttl_override_seconds=300)
    analytics_calls = 0

    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        analytics_fn=lambda: (analytics_calls := analytics_calls + 1) or {},
    )
    assert res.poll_status == "SKIPPED_CACHE_FRESH"
    assert res.analytics_executed is False
    assert analytics_calls == 0


def test_069_search_scout_analyst_do_not_execute_on_skipped_single_flight_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """69. Search, Scout, and Analyst do not execute on SKIPPED_SINGLE_FLIGHT_BUSY."""
    _enable_all_polling_gates(monkeypatch)
    policy = FC27PollingPolicy()
    analytics_calls = 0
    with FC27SingleFlightLeaseContext(policy=policy, owner_id="busy"):
        res = run_fc27_poll_cycle(
            policy=policy,
            confirm_live_polling=True,
            use_cache=False,
            analytics_fn=lambda: (analytics_calls := analytics_calls + 1) or {},
        )
    assert res.poll_status == "SKIPPED_SINGLE_FLIGHT_BUSY"
    assert res.analytics_executed is False
    assert analytics_calls == 0


def test_070_search_scout_analyst_do_not_execute_on_replay_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """70. Search, Scout, and Analyst do not execute on REPLAY_CONFLICT."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3)
    analytics_calls = 0
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: (_ for _ in ()).throw(
            FC27ReplayConflictError("Replay conflict")
        ),
        analytics_fn=lambda: (analytics_calls := analytics_calls + 1) or {},
    )
    assert res.poll_status == "REPLAY_CONFLICT"
    assert res.analytics_executed is False
    assert analytics_calls == 0


def test_071_analyst_output_remains_observational(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """71. Analyst output remains observational (recommendation=False, approvable=False, dispatchable=False, causal_claim=False)."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(10, players_per_match=6)
    run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    _, _, analyst_rep = run_analytics_from_persisted_community_records(pg_db_migrated.app)
    assert len(analyst_rep.findings) > 0
    for finding in analyst_rep.findings:
        assert finding.recommendation is False
        assert finding.approvable is False
        assert finding.dispatchable is False
        assert finding.causal_claim is False


def test_072_ten_persisted_matches_produce_moderate_confidence(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """72. Ten persisted matches produce MODERATE_CONFIDENCE."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(10, players_per_match=6)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.app,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.confidence_tier == "MODERATE_CONFIDENCE"


# ==============================================================================
# PART P: Worker Lifecycle (Tests 73–82)
# ==============================================================================


def test_073_worker_loop_exits_immediately_when_polling_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """73. Worker loop exits immediately when polling is disabled."""
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "false")
    results = run_fc27_polling_worker(confirm_live_polling=True, register_signals=False)
    assert len(results) == 1
    assert results[0].poll_status == "DISABLED"
    assert results[0].provider_requests == 0


def test_074_worker_loop_runs_one_cycle_when_poll_once_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """74. Worker loop runs one cycle when FC27_COMMUNITY_POLL_ONCE=true."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_ONCE", "true")
    env, _ = _build_validated_envelope(3)
    results = run_fc27_polling_worker(
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=3),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
        register_signals=False,
    )
    assert len(results) == 1
    assert results[0].poll_status == "NEW_MATCHES_PERSISTED"


def test_075_worker_loop_stops_cleanly_when_shutdown_event_set_before_cycle_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """75. Worker loop stops cleanly when shutdown_event is set before cycle start."""
    _enable_all_polling_gates(monkeypatch)
    stop_event = threading.Event()
    stop_event.set()
    calls = 0

    results = run_fc27_polling_worker(
        confirm_live_polling=True,
        shutdown_event=stop_event,
        fetch_matches_fn=lambda: (calls := calls + 1) and _build_validated_envelope(1)[0],
        register_signals=False,
    )
    assert results == []
    assert calls == 0


def test_076_worker_loop_stops_cleanly_when_shutdown_event_set_during_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """76. Worker loop stops cleanly when shutdown_event is set during sleep."""
    _enable_all_polling_gates(monkeypatch)
    stop_event = threading.Event()
    env, _ = _build_validated_envelope(3)

    def _sleep_and_stop(_sec: float) -> bool:
        stop_event.set()
        return True

    results = run_fc27_polling_worker(
        confirm_live_polling=True,
        shutdown_event=stop_event,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=3),
        sleep_fn=_sleep_and_stop,
        register_signals=False,
    )
    assert len(results) == 1


def test_077_sigterm_handler_sets_shutdown_state_without_making_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """77. SIGTERM handler sets shutdown state without making another request."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(3)

    def _send_sigterm_during_sleep(_sec: float) -> bool:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return False

    results = run_fc27_polling_worker(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=3),
        sleep_fn=_send_sigterm_during_sleep,
        register_signals=True,
    )
    assert len(results) == 1
    assert rt.shutdown_requested is True
    assert rt.total_provider_requests == 1


def test_078_sigint_handler_sets_shutdown_state_without_making_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """78. SIGINT handler sets shutdown state without making another request."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(3)

    def _send_sigint_during_sleep(_sec: float) -> bool:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        return False

    results = run_fc27_polling_worker(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=3),
        sleep_fn=_send_sigint_during_sleep,
        register_signals=True,
    )
    assert len(results) == 1
    assert rt.shutdown_requested is True
    assert rt.total_provider_requests == 1


def test_079_health_state_updates_after_successful_new_match_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """79. Health state updates after successful new-match cycle."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(4)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=4),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
    )
    health = rt.snapshot_health()
    assert health.last_status == "NEW_MATCHES_PERSISTED"
    assert health.last_attempt_at is not None
    assert health.last_success_at is not None
    assert health.last_new_match_at is not None
    assert health.total_cycles == 1
    assert health.total_provider_requests == 1
    assert health.total_matches_inserted == 4


def test_080_health_state_updates_after_no_new_matches_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """80. Health state updates after no-new-matches cycle."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(4)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=4),
    )
    health = rt.snapshot_health()
    assert health.last_status == "NO_NEW_MATCHES"
    assert health.last_success_at is not None
    assert health.last_new_match_at is None
    assert health.total_duplicates_ignored == 4


def test_081_health_state_increments_consecutive_failures_and_resets_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """81. Health state increments consecutive_failures on failure and resets on success."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        use_rate_limiter=False,
        fetch_matches_fn=lambda: (_ for _ in ()).throw(
            FC27TransportError("Community client request timed out")
        ),
    )
    assert rt.snapshot_health().consecutive_failures == 1

    env, _ = _build_validated_envelope(3)
    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        use_rate_limiter=False,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=3),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
    )
    assert rt.snapshot_health().consecutive_failures == 0


def test_082_health_state_never_contains_raw_payloads_player_names_headers_cookies_or_credentials() -> None:
    """82. Health state never contains raw payloads, player names, headers, cookies, or database credentials."""
    with pytest.raises(ValidationError):
        FC27PollingHealth(
            worker_enabled=False,
            worker_running=False,
            raw_body="forbidden",  # type: ignore[call-arg]
        )
    health = FC27PollingRuntime().snapshot_health()
    dumped = json.dumps(health.model_dump(mode="json")).lower()
    for bad in ("raw_body", "cookies", "password", "postgresql://", "player_name"):
        assert bad not in dumped


# ==============================================================================
# PART P: CLI (Tests 83–88)
# ==============================================================================


def test_083_cli_status_makes_zero_network_and_zero_provider_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """83. --status makes zero network calls and zero provider calls."""
    rc = polling_cli_main(["--status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PROVIDER_ID=fc-clubs-api-community-v1" in out
    assert "WORKER_ENABLED=NO" in out
    assert "PROVIDER_REQUESTS=0" in out


def test_084_cli_once_with_polling_disabled_exits_cleanly_with_disabled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """84. --once with polling disabled exits cleanly with POLL_STATUS=DISABLED."""
    rc = polling_cli_main(["--once"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "POLL_STATUS=DISABLED" in out
    assert "PROVIDER_REQUESTS=0" in out


def test_085_cli_once_with_valid_fake_provider_and_postgres_persists_once_and_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pg_db_migrated,
) -> None:
    """85. --once with valid fake provider and disposable PostgreSQL persists once and exits."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setenv("POSTGRES_URI", pg_db_migrated.app)
    env, _ = _build_validated_envelope(10, players_per_match=6)
    monkeypatch.setattr(
        FC27CommunityProviderAdapter,
        "get_east_coasters_recent_matches_once",
        lambda self, **kwargs: env,
    )
    rc = polling_cli_main(["--once", "--confirm-live-polling"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "POLL_STATUS=NEW_MATCHES_PERSISTED" in out
    assert "INSERTED_MATCH_COUNT=10" in out
    assert "ANALYTICS_EXECUTED=YES" in out


def test_086_cli_once_repeated_with_identical_fake_provider_records_no_new_matches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pg_db_migrated,
) -> None:
    """86. --once repeated with identical fake provider records NO_NEW_MATCHES and ANALYTICS_EXECUTED=NO."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setenv("POSTGRES_URI", pg_db_migrated.app)
    env, _ = _build_validated_envelope(10, players_per_match=6)
    monkeypatch.setattr(
        FC27CommunityProviderAdapter,
        "get_east_coasters_recent_matches_once",
        lambda self, **kwargs: env,
    )
    rt1 = FC27PollingRuntime()
    polling_cli_main(["--once", "--confirm-live-polling"], runtime=rt1)
    capsys.readouterr()

    rt2 = FC27PollingRuntime()
    rc = polling_cli_main(["--once", "--confirm-live-polling"], runtime=rt2)
    out = capsys.readouterr().out
    assert rc == 0
    assert "POLL_STATUS=NO_NEW_MATCHES" in out
    assert "DUPLICATE_IGNORED_COUNT=10" in out
    assert "ANALYTICS_EXECUTED=NO" in out


def test_087_cli_once_without_confirm_live_polling_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """87. --once without --confirm-live-polling is blocked."""
    _enable_all_polling_gates(monkeypatch)
    rc = polling_cli_main(["--once"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "POLL_STATUS=BLOCKED_FEATURE_GATE" in out
    assert "PROVIDER_REQUESTS=0" in out


def test_088_cli_output_is_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pg_db_migrated,
) -> None:
    """88. CLI output is metadata-only."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setenv("POSTGRES_URI", pg_db_migrated.app)
    env, _ = _build_validated_envelope(3, players_per_match=6)
    monkeypatch.setattr(
        FC27CommunityProviderAdapter,
        "get_east_coasters_recent_matches_once",
        lambda self, **kwargs: env,
    )
    polling_cli_main(["--once", "--confirm-live-polling"])
    out = capsys.readouterr().out
    assert "RAW_RESPONSE_RETAINED=NO" in out
    assert "StripThisPrivateGamerTag" not in out
    assert pg_db_migrated.app not in out


# ==============================================================================
# PART P: Docker and Dashboard (Tests 89–101)
# ==============================================================================


def test_089_dockerfile_community_worker_uses_digest_pinned_base_image_and_non_root_user() -> None:
    """89. Dockerfile.community-worker uses a digest-pinned base image and non-root user."""
    dockerfile_text = (REPO_ROOT / "Dockerfile.community-worker").read_text(encoding="utf-8")
    assert "FROM python:3.11.16-slim@sha256:" in dockerfile_text
    assert "USER 10001:65534" in dockerfile_text
    assert "26a5ff12ea793042bd776a858c74f3f44dd427f5" in dockerfile_text


def test_090_compose_community_worker_is_profile_gated_and_disabled_by_default() -> None:
    """90. docker-compose.community-worker.yml is profile-gated (fc27-polling) and disabled by default."""
    compose_data = yaml.safe_load(
        (REPO_ROOT / "docker-compose.community-worker.yml").read_text(encoding="utf-8")
    )
    svc = compose_data["services"]["fc27-community-worker"]
    assert svc["profiles"] == ["fc27-polling"]
    env = svc["environment"]
    assert "false" in str(env["FC27_COMMUNITY_POLLING_ENABLED"])


def test_091_worker_service_exposes_no_public_ports() -> None:
    """91. Worker service exposes no public ports."""
    compose_data = yaml.safe_load(
        (REPO_ROOT / "docker-compose.community-worker.yml").read_text(encoding="utf-8")
    )
    svc = compose_data["services"]["fc27-community-worker"]
    assert "ports" not in svc


def test_092_worker_service_has_hardening_and_resource_limits() -> None:
    """92. Worker service has read_only: true, cap_drop: ALL, no-new-privileges:true, CPU limit, memory limit, PID limit, and stop_grace_period."""
    compose_data = yaml.safe_load(
        (REPO_ROOT / "docker-compose.community-worker.yml").read_text(encoding="utf-8")
    )
    svc = compose_data["services"]["fc27-community-worker"]
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert float(svc["cpus"]) == 0.50
    assert svc["mem_limit"] == "256m"
    assert int(svc["pids_limit"]) == 64
    assert svc["stop_grace_period"] == "30s"


def test_093_worker_service_does_not_mount_docker_sock_or_host_home() -> None:
    """93. Worker service does not mount /var/run/docker.sock or host home directories."""
    raw_compose = (REPO_ROOT / "docker-compose.community-worker.yml").read_text(encoding="utf-8")
    assert "docker.sock" not in raw_compose
    assert "/home/" not in raw_compose
    assert "C:\\Users" not in raw_compose


def test_094_worker_service_does_not_receive_admin_migrator_commissioner_or_dispatch_secrets() -> None:
    """94. Worker service does not receive admin, migrator, commissioner, approval-signing, BFF, or dispatch secrets."""
    compose_data = yaml.safe_load(
        (REPO_ROOT / "docker-compose.community-worker.yml").read_text(encoding="utf-8")
    )
    svc = compose_data["services"]["fc27-community-worker"]
    assert svc["secrets"] == ["fc27_app_uri"]
    env_keys = set(svc.get("environment", {}).keys())
    for forbidden in (
        "POSTGRES_ADMIN_URI",
        "POSTGRES_ADMIN_URI_FILE",
        "FC27_MIGRATOR_PASSWORD_FILE",
        "GRAPH_COMMISSIONER_TOKEN_SHA256",
        "APPROVAL_SIGNING_SECRET",
        "BFF_SERVICE_TOKEN",
        "FC27_DISPATCH_PASSWORD_FILE",
    ):
        assert forbidden not in env_keys


def test_095_worker_database_role_cannot_access_checkpoints_tables(pg_db_migrated) -> None:
    """95. Worker database role cannot access checkpoints, checkpoint_blobs, or checkpoint_writes."""
    with open_guarded_polling_connection(pg_db_migrated.app) as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            with pytest.raises(FC27PollingTableAccessDeniedError):
                conn.execute(f"SELECT COUNT(*) FROM public.{table}")


def test_096_worker_database_role_cannot_access_approval_tables(pg_db_migrated) -> None:
    """96. Worker database role cannot access approval_requests, approval_audit, or approval_nonces."""
    with open_guarded_polling_connection(pg_db_migrated.app) as conn:
        for table in ("approval_requests", "approval_audit", "approval_nonces"):
            with pytest.raises(FC27PollingTableAccessDeniedError):
                conn.execute(f"SELECT COUNT(*) FROM public.{table}")


def test_097_worker_database_role_cannot_access_dispatch_tables(pg_db_migrated) -> None:
    """97. Worker database role cannot access dispatches or dispatch_delivery_attempts."""
    with open_guarded_polling_connection(pg_db_migrated.app) as conn:
        for table in ("dispatches", "dispatch_delivery_attempts"):
            with pytest.raises(FC27PollingTableAccessDeniedError):
                conn.execute(f"SELECT COUNT(*) FROM public.{table}")


def test_098_worker_database_role_cannot_run_ddl(pg_db_migrated) -> None:
    """98. Worker database role cannot run DDL (CREATE TABLE fails)."""
    with open_guarded_polling_connection(pg_db_migrated.app) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE public.unauthorized_worker_ddl (id int)")
        conn.rollback()


def test_099_demo_status_exposes_polling_health_metadata_disabled_by_default() -> None:
    """99. GET /demo/status exposes polling health metadata with polling_enabled=false by default."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        resp = client.get("/demo/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["polling_enabled"] is False
        assert body["worker_running"] is False
        assert body["last_status"] == "DISABLED"
        assert body["consecutive_failures"] == 0
        assert body["total_matches_inserted"] == 0
        assert body["total_duplicates_ignored"] == 0
        assert body["total_replay_conflicts"] == 0
        assert "last_attempt_at" in body
        assert "last_success_at" in body
        assert "last_new_match_at" in body
        assert "next_eligible_at" in body


def test_100_dashboard_renders_polling_disabled_and_has_no_button_to_enable_live_polling() -> None:
    """100. Dashboard renders Polling Disabled when polling is disabled and has no button to enable live polling."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        resp = client.get("/demo")
        assert resp.status_code == 200
        html_text = resp.text
        assert "Polling Disabled" in html_text
        assert "Polling Worker Health (Read-Only)" in html_text
        assert "enable-polling" not in html_text.lower()
        assert "start-polling" not in html_text.lower()
        assert "/demo/poll" not in html_text.lower()


def test_101_zero_external_network_calls_occur_during_entire_offline_test_suite() -> None:
    """101. Zero external network calls occur during the entire offline test suite."""
    assert _EXTERNAL_NETWORK_CALLS == []
