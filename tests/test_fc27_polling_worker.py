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
from src.fc27_discord_bot import FC27DiscordBotConfig, FC27DiscordBotService
from src.fc27_privacy import pseudonymize_match_ref as _pseudonymize_match_ref
from src.fc27_polling_worker import (
    APPROVED_MATCH_TYPES,
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
    recover_polling_state_from_postgres,
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
        "FC27_COMMUNITY_POLL_INTERVAL_SECONDS",
        "FC27_COMMUNITY_POLL_MIN_SECONDS",
        "FC27_COMMUNITY_POLL_MAX_SECONDS",
        "FC27_COMMUNITY_POLL_MAX_REQUESTS",
        "FC27_COMMUNITY_POLL_MAX_RETRIES",
        "FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY",
        "FC27_COMMUNITY_POLL_SINGLE_FLIGHT",
        "FC27_COMMUNITY_POLL_ONCE",
        "FC27_APPROVED_MATCH_TYPES",
        "FC27_ALIAS_HMAC_KEY",
        "FC27_ALIAS_HMAC_KEY_FILE",
        "FC27_POLL_WRITER_URI",
        "FC27_POLL_WRITER_URI_FILE",
        "FC27_PLAYER_DEV_WRITER_URI",
        "FC27_PLAYER_DEV_WRITER_URI_FILE",
        "FC27_DISCORD_READER_URI",
        "FC27_DISCORD_READER_URI_FILE",
        "FC27_RECOMMENDATIONS_ENABLED",
        "DISPATCH_ENABLED",
        "FC27_CLUB_ID",
        "FC_PLATFORM",
    ):
        monkeypatch.delenv(env_key, raising=False)

    reset_default_polling_runtime()


TEST_ALIAS_HMAC_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _enable_all_polling_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "true")
    monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("FC27_ALIAS_HMAC_KEY", TEST_ALIAS_HMAC_KEY)


def _build_synthetic_community_match_records(
    count: int = 10,
    *,
    start_id: int = 991001,
    players_per_match: int = 6,
    goals_override: int | None = None,
    match_type: str = "leagueMatch",
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
                "match_type": match_type,
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
    match_type: str = "leagueMatch",
    alias_hmac_key: bytes | str | None = TEST_ALIAS_HMAC_KEY,
) -> tuple[FC27CommunityProviderEnvelope, tuple[Any, ...]]:
    raw = _build_synthetic_community_match_records(
        count=count,
        start_id=start_id,
        players_per_match=players_per_match,
        goals_override=goals_override,
        match_type=match_type,
    )
    return normalize_community_match_records(
        raw,
        expected_match_type=match_type,
        observed_at="2026-10-31T12:00:00Z",
        alias_hmac_key=alias_hmac_key,
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


def test_003_default_minimum_interval_is_43200_seconds() -> None:
    """3. Default minimum interval is 43200 seconds (12 hours)."""
    assert settings.get_fc27_community_poll_min_seconds(strict=True) == 43200
    assert settings.get_fc27_community_poll_interval_seconds(strict=True) == 43200
    assert FC27PollingPolicy().minimum_interval_seconds == 43200
    assert FC27PollingPolicy().poll_interval_seconds == 43200


def test_004_default_maximum_interval_is_43200_seconds() -> None:
    """4. Default maximum interval is 43200 seconds (12 hours)."""
    assert settings.get_fc27_community_poll_max_seconds(strict=True) == 43200
    assert FC27PollingPolicy().maximum_interval_seconds == 43200
    assert FC27PollingPolicy().polling_frequency == "ONCE_EVERY_12_HOURS"
    assert FC27PollingPolicy().max_provider_requests_per_24_hours == 2


def test_005_minimum_interval_below_43200_seconds_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5. Minimum interval below 43200 seconds is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(minimum_interval_seconds=299)
    with pytest.raises(ValidationError):
        FC27PollingPolicy(minimum_interval_seconds=3600)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MIN_SECONDS", "120")
    with pytest.raises(ConfigError):
        settings.get_fc27_community_poll_min_seconds(strict=True)


def test_006_maximum_interval_below_minimum_interval_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6. Maximum interval below minimum interval is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(minimum_interval_seconds=43200, maximum_interval_seconds=400)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MIN_SECONDS", "43200")
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_SECONDS", "400")
    with pytest.raises(ConfigError):
        settings.get_fc27_community_poll_max_seconds(strict=True)


def test_007_maximum_interval_above_43200_seconds_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7. Maximum interval above 43200 seconds is rejected."""
    with pytest.raises(ValidationError):
        FC27PollingPolicy(maximum_interval_seconds=86400)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_SECONDS", "86400")
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
        db_uri_or_conn_factory=pg_db_migrated.poll_writer,
    ) as lease:
        assert lease.acquired is True
        assert lease.mechanism == "POSTGRES_ADVISORY_LOCK"


# ==============================================================================
# PART P: Interval and Cache (Tests 33–40)
# ==============================================================================


def test_033_poll_interval_selector_returns_43200_seconds() -> None:
    """33. Poll interval selector returns 43200 seconds (12 hours)."""
    policy = FC27PollingPolicy(minimum_interval_seconds=43200, maximum_interval_seconds=43200)
    rng = random.Random(42)
    for _ in range(50):
        val = select_bounded_poll_interval_seconds(policy, rng=rng)
        assert val == 43200


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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_initial,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    env_overlap, _ = _build_validated_envelope(10, start_id=991001, players_per_match=6)
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_initial,
        use_cache=False,
        use_rate_limiter=False,
    )
    rt.next_eligible_at = None
    env_overlap, _ = _build_validated_envelope(10, start_id=991001, players_per_match=6)
    second = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    _, _, analyst_rep = run_analytics_from_persisted_community_records(pg_db_migrated.poll_writer)
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
        db_conn_or_uri=pg_db_migrated.poll_writer,
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
    monkeypatch.setenv("POSTGRES_URI", pg_db_migrated.poll_writer)
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
    monkeypatch.setenv("POSTGRES_URI", pg_db_migrated.poll_writer)
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
    monkeypatch.setenv("POSTGRES_URI", pg_db_migrated.poll_writer)
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
    assert pg_db_migrated.poll_writer not in out


# ==============================================================================
# PART P: Docker and Dashboard (Tests 89–101)
# ==============================================================================


def test_089_dockerfile_community_worker_uses_digest_pinned_base_image_and_non_root_user() -> None:
    """89. Dockerfile.community-worker uses a digest-pinned base image and non-root user."""
    dockerfile_text = (REPO_ROOT / "Dockerfile.community-worker").read_text(encoding="utf-8")
    assert (
        "FROM python:3.11.18-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545"
        in dockerfile_text
    )
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
    assert set(svc["secrets"]) == {"fc27_poll_writer_uri", "fc27_alias_hmac_key"}
    env_keys = set(svc.get("environment", {}).keys())
    for forbidden in (
        "POSTGRES_ADMIN_URI",
        "POSTGRES_ADMIN_URI_FILE",
        "FC27_MIGRATOR_PASSWORD_FILE",
        "GRAPH_COMMISSIONER_TOKEN_SHA256",
        "APPROVAL_SIGNING_SECRET",
        "BFF_SERVICE_TOKEN",
        "FC27_DISPATCH_PASSWORD_FILE",
        "FC27_PLAYER_DEV_WRITER_URI_FILE",
        "FC27_DISCORD_READER_URI_FILE",
        "FC27_POLL_WRITER_PASSWORD_FILE",
    ):
        assert forbidden not in env_keys


def test_095_worker_database_role_cannot_access_checkpoints_tables(pg_db_migrated) -> None:
    """95. Worker database role cannot access checkpoints, checkpoint_blobs, or checkpoint_writes."""
    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            with pytest.raises(FC27PollingTableAccessDeniedError):
                conn.execute(f"SELECT COUNT(*) FROM public.{table}")


def test_096_worker_database_role_cannot_access_approval_tables(pg_db_migrated) -> None:
    """96. Worker database role cannot access approval, decision-evidence, or identity/WebAuthn tables."""
    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as conn:
        for table in (
            "approval_requests",
            "approval_audit",
            "approval_nonces",
            "fc27_decision_evidence",
            "fc27_users",
            "fc27_sessions",
            "fc27_webauthn_credentials",
            "fc27_challenges",
            "fc27_recovery_codes",
            "fc27_totp_credentials",
            "fc27_credentials",
        ):
            with pytest.raises(FC27PollingTableAccessDeniedError):
                conn.execute(f"SELECT COUNT(*) FROM public.{table}")


def test_097_worker_database_role_cannot_access_dispatch_tables(pg_db_migrated) -> None:
    """97. Worker database role cannot access dispatches, dispatch_delivery_attempts, domain_events, or outbox."""
    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as conn:
        for table in (
            "dispatches",
            "dispatch_delivery_attempts",
            "dispatch_plans",
            "dispatch_worker_claims",
            "domain_events",
            "outbox",
        ):
            with pytest.raises(FC27PollingTableAccessDeniedError):
                conn.execute(f"SELECT COUNT(*) FROM public.{table}")


def test_098_worker_database_role_cannot_run_ddl(pg_db_migrated) -> None:
    """98. Worker database role cannot run DDL (CREATE TABLE fails)."""
    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE public.unauthorized_worker_ddl (id int)")
        conn.rollback()


def test_099_discord_status_exposes_polling_health_metadata_disabled_by_default() -> None:
    """99. /fc27 status exposes polling health metadata with worker_enabled=false by default."""
    rt = FC27PollingRuntime()
    health = rt.snapshot_health()
    assert health.worker_enabled is False
    assert health.worker_running is False
    assert health.last_status == "DISABLED"
    assert health.consecutive_failures == 0
    assert health.total_matches_inserted == 0
    assert health.total_duplicates_ignored == 0
    assert health.total_replay_conflicts == 0

    svc = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"123456789012345678"}),
            allowed_channel_ids=frozenset({"234567890123456789"}),
        ),
        polling_runtime=rt,
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id="123456789012345678",
        channel_id="234567890123456789",
    )
    assert resp.status == "OK"
    fields = {k: v for emb in resp.embeds for k, v in emb.fields.items()}
    assert fields["worker_enabled"] == "false"
    assert fields["worker_running"] == "false"
    assert fields["poll_status"] == "DISABLED"
    assert fields["consecutive_failures"] == "0"
    assert fields["matches_inserted"] == "0"
    assert fields["duplicates_ignored"] == "0"
    assert fields["replay_conflicts"] == "0"


def test_100_discord_polling_status_remains_read_only_with_zero_provider_calls() -> None:
    """100. Discord /fc27 status is read-only and never triggers live polling."""
    rt = FC27PollingRuntime()
    svc = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"123456789012345678"}),
            allowed_channel_ids=frozenset({"234567890123456789"}),
        ),
        polling_runtime=rt,
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id="123456789012345678",
        channel_id="234567890123456789",
    )
    assert resp.status == "OK"
    assert resp.provider_requests_executed == 0
    assert svc.config.database_write_access is False



def test_101_zero_external_network_calls_occur_during_entire_offline_test_suite() -> None:
    """101. Zero external network calls occur during the entire offline test suite."""
    assert _EXTERNAL_NETWORK_CALLS == []


# ==============================================================================
# PHASE 1A, 1B, 1D, 2A, 2B: 12-Hour Schedule, Recovery, Continuity & Durability
# ==============================================================================


def test_102_default_poll_interval_is_43200_and_frequency_is_once_every_12_hours() -> None:
    """102. Default poll interval is 43200 seconds and frequency is ONCE_EVERY_12_HOURS."""
    policy = FC27PollingPolicy()
    assert policy.poll_interval_seconds == 43200
    assert policy.minimum_interval_seconds == 43200
    assert policy.maximum_interval_seconds == 43200
    assert policy.polling_frequency == "ONCE_EVERY_12_HOURS"
    assert policy.max_provider_requests_per_24_hours == 2
    assert policy.catch_up_bursts_allowed is False
    assert policy.approval_enabled is False


def test_103_poll_interval_below_or_above_43200_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """103. Poll interval below or above 43200 seconds is rejected."""
    for invalid in (300, 600, 3600, 43199, 43201, 86400):
        with pytest.raises(ValidationError):
            FC27PollingPolicy(poll_interval_seconds=invalid)
        monkeypatch.setenv("FC27_COMMUNITY_POLL_INTERVAL_SECONDS", str(invalid))
        with pytest.raises(ConfigError):
            settings.get_fc27_community_poll_interval_seconds(strict=True)


def test_104_normal_12_hour_cycle_schedules_next_eligible_at_43200s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """104. Normal 12-hour cycle schedules next_eligible_at = finished_at + 43200s."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(10)
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=10),
        analytics_fn=lambda: {"confidence_tier": "MODERATE_CONFIDENCE"},
        now_utc_fn=lambda: t0,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.poll_interval_seconds == 43200
    assert res.polling_frequency == "ONCE_EVERY_12_HOURS"
    assert res.max_provider_requests_per_24_hours == 2
    assert res.next_eligible_at == t0 + timedelta(seconds=43200)
    assert res.schedule_lag_seconds == 0
    assert res.missed_intervals == 0
    assert res.catch_up_cycles_suppressed is True


def test_105_worker_container_restart_with_elapsed_under_43200_skips_poll(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """105. Worker container restart with elapsed < 43200s skips poll and preserves last_attempt_at."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    env, _ = _build_validated_envelope(10, players_per_match=6)

    # First worker instance runs and persists at t0
    rt1 = FC27PollingRuntime()
    res1 = run_fc27_poll_cycle(
        runtime=rt1,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res1.poll_status == "NEW_MATCHES_PERSISTED"

    # Simulate container restart 2 hours later (7200s < 43200s) with fresh in-memory runtime
    t_restart = t0 + timedelta(hours=2)
    rt2 = FC27PollingRuntime()
    provider_calls = 0

    def _should_not_call() -> FC27CommunityProviderEnvelope:
        nonlocal provider_calls
        provider_calls += 1
        return env

    res2 = run_fc27_poll_cycle(
        runtime=rt2,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=_should_not_call,
        now_utc_fn=lambda: t_restart,
        use_cache=False,
        use_rate_limiter=False,
        recover_state_from_db=True,
    )
    assert res2.poll_status == "SKIPPED_MIN_INTERVAL"
    assert res2.provider_requests == 0
    assert provider_calls == 0
    assert rt2.last_attempt_at == t0
    assert rt2.last_success_at == t0
    assert rt2.next_eligible_at == t0 + timedelta(seconds=43200)


def test_106_docker_daemon_restart_recovers_persisted_state_and_skips_when_under_12h(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """106. Docker daemon restart recovers persisted state in worker loop and skips when < 12h."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 6, 0, 0, tzinfo=timezone.utc)
    env, _ = _build_validated_envelope(10, players_per_match=6)

    run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )

    # Fresh runtime on Docker daemon restart at t0 + 6 hours (21600s < 43200s)
    rt_daemon_restart = FC27PollingRuntime()
    results = run_fc27_polling_worker(
        runtime=rt_daemon_restart,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: (_ for _ in ()).throw(AssertionError("Must not poll")),
        now_utc_fn=lambda: t0 + timedelta(hours=6),
        max_cycles=1,
        register_signals=False,
        use_cache=False,
        use_rate_limiter=False,
        recover_state_from_db=True,
    )
    assert len(results) == 1
    assert results[0].poll_status == "SKIPPED_MIN_INTERVAL"
    assert results[0].provider_requests == 0


def test_107_host_reboot_simulation_with_persisted_postgres_state_preserves_schedule(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """107. Host reboot simulation recovers known_match_ids, anchor, and schedule from PostgreSQL."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 8, 0, 0, tzinfo=timezone.utc)
    env, _ = _build_validated_envelope(10, start_id=995001, players_per_match=6)

    run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )

    # Host reboot at t0 + 11 hours (39600s < 43200s): skipped
    rt_reboot = FC27PollingRuntime()
    early = run_fc27_poll_cycle(
        runtime=rt_reboot,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        now_utc_fn=lambda: t0 + timedelta(hours=11),
        use_cache=False,
        use_rate_limiter=False,
        recover_state_from_db=True,
    )
    assert early.poll_status == "SKIPPED_MIN_INTERVAL"
    assert len(rt_reboot.known_match_ids) == 10

    # At t0 + 12 hours (43200s): executes 1 cycle cleanly
    due = run_fc27_poll_cycle(
        runtime=rt_reboot,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        now_utc_fn=lambda: t0 + timedelta(hours=12),
        use_cache=False,
        use_rate_limiter=False,
        recover_state_from_db=True,
    )
    assert due.poll_status == "NO_NEW_MATCHES"
    assert due.provider_requests == 1
    assert due.possible_history_gap is False


def test_108_host_sleep_hibernate_over_43200_executes_single_cycle_no_catch_up_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """108. Host sleep/hibernate with elapsed > 43200s executes 1 cycle only and suppresses catch-up bursts."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(10)

    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=10),
        analytics_fn=lambda: {"confidence_tier": "MODERATE_CONFIDENCE"},
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )

    # Host sleeps for 30 hours (108000s > 43200s; lag = 64800s, 1 missed interval)
    t_wake = t0 + timedelta(hours=30)
    wake_res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=10),
        now_utc_fn=lambda: t_wake,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert wake_res.poll_status == "NO_NEW_MATCHES"
    assert wake_res.provider_requests == 1
    assert wake_res.schedule_lag_seconds == 64800
    assert wake_res.missed_intervals == 1
    assert wake_res.catch_up_cycles_suppressed is True
    assert wake_res.next_eligible_at == t_wake + timedelta(seconds=43200)

    # Immediate follow-up poll 1 minute after wake is skipped (no catch-up burst)
    follow_up = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=10),
        now_utc_fn=lambda: t_wake + timedelta(minutes=1),
        use_cache=False,
        use_rate_limiter=False,
    )
    assert follow_up.poll_status == "SKIPPED_MIN_INTERVAL"
    assert follow_up.provider_requests == 0


def test_109_dst_spring_forward_and_fall_back_have_zero_interval_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """109. Daylight Saving Time transitions have zero effect on UTC 12-hour interval calculations."""
    _enable_all_polling_gates(monkeypatch)
    # Simulate non-UTC timezone offsets across a DST change (-04:00 vs -05:00)
    edt = timezone(timedelta(hours=-4))
    est = timezone(timedelta(hours=-5))
    # 2026-11-01 00:30:00 EDT == 2026-11-01 04:30:00 UTC
    t_before_fallback = datetime(2026, 11, 1, 0, 30, 0, tzinfo=edt)
    # 12 real UTC hours later is 2026-11-01 16:30:00 UTC == 2026-11-01 11:30:00 EST
    t_after_11h_real = datetime(2026, 11, 1, 10, 30, 0, tzinfo=est)
    t_after_12h_real = datetime(2026, 11, 1, 11, 30, 0, tzinfo=est)

    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(5)

    r1 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=5),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
        now_utc_fn=lambda: t_before_fallback,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.started_at.tzinfo == timezone.utc
    assert r1.next_eligible_at == t_before_fallback.astimezone(timezone.utc) + timedelta(seconds=43200)

    # 11 real hours later -> still skipped
    r_early = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=5),
        now_utc_fn=lambda: t_after_11h_real,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r_early.poll_status == "SKIPPED_MIN_INTERVAL"

    # Exactly 12 real UTC hours later -> allowed
    r_due = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=0, duplicates=5),
        now_utc_fn=lambda: t_after_12h_real,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r_due.poll_status == "NO_NEW_MATCHES"
    assert r_due.provider_requests == 1


def test_110_three_day_outage_executes_1_cycle_only_and_suppresses_catch_up_bursts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """110. 3-day outage executes 1 cycle only, records missed intervals, and resets next_eligible_at."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    rt = FC27PollingRuntime()
    env_initial, _ = _build_validated_envelope(10, start_id=991001)

    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env_initial,
        persist_fn=lambda _e: _fake_persist_summary(inserted=10),
        analytics_fn=lambda: {"confidence_tier": "MODERATE_CONFIDENCE"},
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )

    # 3 days (72 hours = 259200s) later -> lag beyond next_eligible_at (12h) is 60h = 216000s (5 missed 12h intervals)
    t_outage_end = t0 + timedelta(days=3)
    env_overlap, _ = _build_validated_envelope(10, start_id=991003)  # 8 overlap, 2 new
    r_outage = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env_overlap,
        persist_fn=lambda _e: _fake_persist_summary(inserted=2, duplicates=8),
        analytics_fn=lambda: {"confidence_tier": "MODERATE_CONFIDENCE"},
        now_utc_fn=lambda: t_outage_end,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r_outage.poll_status == "NEW_MATCHES_PERSISTED"
    assert r_outage.provider_requests == 1
    assert r_outage.schedule_lag_seconds == 216000
    assert r_outage.missed_intervals == 5
    assert r_outage.catch_up_cycles_suppressed is True
    assert r_outage.next_eligible_at == t_outage_end + timedelta(seconds=43200)
    assert r_outage.possible_history_gap is False


def test_111_postgresql_unavailable_at_poll_time_records_failure_without_analytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """111. PostgreSQL unavailable at poll time records failure, does not advance last_success_at, and blocks analytics."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(10)
    analytics_called = False

    def _analytics() -> dict[str, Any]:
        nonlocal analytics_called
        analytics_called = True
        return {}

    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        fetch_matches_fn=lambda: env,
        persist_fn=lambda _e: (_ for _ in ()).throw(
            psycopg.OperationalError("connection to PostgreSQL failed")
        ),
        analytics_fn=_analytics,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "TRANSIENT_FAILURE"
    assert res.error_category == "PERSISTENCE_FAILURE"
    assert res.inserted_match_count == 0
    assert res.analytics_executed is False
    assert analytics_called is False
    assert rt.last_success_at is None
    assert rt.consecutive_failures == 1


def test_112_partial_or_mid_transaction_persistence_failure_rolls_back_and_blocks_analytics(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """112. Mid-transaction persistence failure rolls back atomically and prevents analytics from running."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(10, players_per_match=6)
    analytics_called = False

    import src.fc27_polling_worker as pw_mod

    def _fail_touch(*args: Any, **kwargs: Any) -> None:
        raise psycopg.DatabaseError("simulated mid-transaction failure before commit")

    monkeypatch.setattr(pw_mod, "_touch_persisted_polling_timestamps", _fail_touch)

    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        analytics_fn=lambda: (analytics_called := True) or {},
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "TRANSIENT_FAILURE"
    assert res.error_category == "PERSISTENCE_FAILURE"
    assert res.analytics_executed is False
    assert analytics_called is False

    # Verify transaction rolled back completely (0 matches, 0 player stats in DB)
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_match_stats WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 0


def test_113_overlapping_manual_and_scheduled_poll_skips_second_via_single_flight(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """113. Overlapping manual and scheduled poll skips the second via PostgreSQL advisory single-flight lock."""
    _enable_all_polling_gates(monkeypatch)
    policy = FC27PollingPolicy()
    with FC27SingleFlightLeaseContext(
        policy=policy,
        owner_id="scheduled-worker",
        db_uri_or_conn_factory=pg_db_migrated.poll_writer,
    ) as lease:
        assert lease.acquired is True
        manual_res = run_fc27_poll_cycle(
            policy=policy,
            confirm_live_polling=True,
            db_conn_or_uri=pg_db_migrated.poll_writer,
            use_cache=False,
            use_rate_limiter=False,
        )
        assert manual_res.poll_status == "SKIPPED_SINGLE_FLIGHT_BUSY"
        assert manual_res.provider_requests == 0


def test_114_ten_new_matches_after_existing_matches_flags_possible_history_gap(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """114. 10 new matches with 0 overlap against existing persisted matches sets possible_history_gap=True and review_required=True without extra provider calls."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    rt = FC27PollingRuntime()
    env_batch_1, _ = _build_validated_envelope(10, start_id=991001, players_per_match=6)

    r1 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_batch_1,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.poll_status == "NEW_MATCHES_PERSISTED"
    assert r1.possible_history_gap is False
    assert r1.review_required is False

    # 12 hours later: 10 completely new matches (992001..992010), 0 overlap with 991001..991010
    t1 = t0 + timedelta(seconds=43200)
    env_batch_2, _ = _build_validated_envelope(10, start_id=992001, players_per_match=6)
    r2 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_batch_2,
        now_utc_fn=lambda: t1,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.poll_status == "NEW_MATCHES_PERSISTED"
    assert r2.inserted_match_count == 10
    assert r2.duplicate_ignored_count == 0
    assert r2.possible_history_gap is True
    assert r2.review_required is True
    assert r2.history_gap_reason == "FULL_WINDOW_TURNOVER_NO_OVERLAP"
    assert r2.provider_requests == 1


def test_115_eight_existing_and_two_new_matches_verifies_continuity_without_gap(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """115. 8 existing + 2 new matches verifies continuity with possible_history_gap=False."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    rt = FC27PollingRuntime()
    all_12_raw = _build_synthetic_community_match_records(12, start_id=991001, players_per_match=6)
    env_batch_1, _ = normalize_community_match_records(
        all_12_raw[:10],
        observed_at="2026-10-31T12:00:00Z",
    )

    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_batch_1,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )

    t1 = t0 + timedelta(seconds=43200)
    env_batch_2, _ = normalize_community_match_records(
        all_12_raw[2:12],
        observed_at="2026-11-01T12:00:00Z",
    )
    r2 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_batch_2,
        now_utc_fn=lambda: t1,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.poll_status == "NEW_MATCHES_PERSISTED"
    assert r2.inserted_match_count == 2
    assert r2.duplicate_ignored_count == 8
    assert r2.possible_history_gap is False
    assert r2.review_required is False
    assert r2.history_gap_reason is None
    assert r2.continuity_last_verified_at == t1


def test_116_ten_identical_matches_verifies_continuity_without_gap(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """116. 10 identical matches returns NO_NEW_MATCHES, possible_history_gap=False, analytics_executed=False."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(10, start_id=991001, players_per_match=6)

    run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )

    t1 = t0 + timedelta(seconds=43200)
    r2 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        now_utc_fn=lambda: t1,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.poll_status == "NO_NEW_MATCHES"
    assert r2.inserted_match_count == 0
    assert r2.duplicate_ignored_count == 10
    assert r2.analytics_executed is False
    assert r2.possible_history_gap is False
    assert r2.continuity_last_verified_at == t1


def test_117_persistence_before_analytics_verified_by_execution_order(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """117. Every accepted normalized match is durably committed in PostgreSQL before analytics executes."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(10, start_id=998001, players_per_match=6)
    persisted_count_during_analytics = 0

    def _verify_committed_before_analytics() -> dict[str, Any]:
        nonlocal persisted_count_during_analytics
        # Open an independent PostgreSQL connection to prove rows are already committed
        with psycopg.connect(pg_db_migrated.admin) as verify_conn:
            with verify_conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = '330323'"
                )
                persisted_count_during_analytics = int(cur.fetchone()[0])
        return {"confidence_tier": "MODERATE_CONFIDENCE"}

    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        analytics_fn=_verify_committed_before_analytics,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.persistence_before_analytics is True
    assert persisted_count_during_analytics == 10


# ==============================================================================
# STAGE C1 MOCKED PROVIDER TEST MATRIX (Items 1–42 -> Tests 118–159)
# ==============================================================================


def test_118_c1_matrix_01_league_match_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 Matrix 1: leagueMatch accepted."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, start_id=910001, match_type="leagueMatch")
    assert env.provenance["match_type"] == "leagueMatch"
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda mt: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=3),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
        match_type_override="leagueMatch",
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.match_type == "leagueMatch"


def test_119_c1_matrix_02_playoff_match_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 Matrix 2: playoffMatch accepted."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, start_id=910101, match_type="playoffMatch")
    assert env.provenance["match_type"] == "playoffMatch"
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda mt: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=3),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
        match_type_override="playoffMatch",
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.match_type == "playoffMatch"


def test_120_c1_matrix_03_friendly_match_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 Matrix 3: friendlyMatch accepted."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, start_id=910201, match_type="friendlyMatch")
    assert env.provenance["match_type"] == "friendlyMatch"
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda mt: env,
        persist_fn=lambda _e: _fake_persist_summary(inserted=3),
        analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
        match_type_override="friendlyMatch",
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.match_type == "friendlyMatch"


def test_121_c1_matrix_04_unapproved_match_type_rejected_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 Matrix 4: Unknown or unapproved match_type rejected before transport."""
    _enable_all_polling_gates(monkeypatch)
    transport_calls = 0

    def _never_called(_mt: str) -> FC27CommunityProviderEnvelope:
        nonlocal transport_calls
        transport_calls += 1
        return _build_validated_envelope(1)[0]

    for bad_type in ("cupMatch", "practiceMatch", "unknownMatch", "ALL", ""):
        with pytest.raises((FC27ContractError, FC27PollingSchemaDriftError, ValueError)):
            normalize_community_match_records(
                _build_synthetic_community_match_records(1),
                match_type=bad_type,
            )
        with pytest.raises(FC27PollingSchemaDriftError):
            run_fc27_poll_cycle(
                confirm_live_polling=True,
                fetch_matches_fn=_never_called,
                match_type_override=bad_type,
            )
    assert transport_calls == 0


def test_122_c1_matrix_05_mismatched_returned_match_type_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 Matrix 5: Mismatched returned match_type vs requested match_type rejected."""
    _enable_all_polling_gates(monkeypatch)
    raw_playoff = _build_synthetic_community_match_records(
        2, start_id=910301, match_type="playoffMatch"
    )
    with pytest.raises(FC27SchemaValidationError, match="does not match requested match_type"):
        normalize_community_match_records(
            raw_playoff,
            match_type="leagueMatch",
            alias_hmac_key=TEST_ALIAS_HMAC_KEY,
        )

    env_playoff, _ = _build_validated_envelope(2, start_id=910301, match_type="playoffMatch")
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        fetch_matches_fn=lambda _mt: env_playoff,
        match_type_override="leagueMatch",
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "SCHEMA_DRIFT"
    assert res.inserted_match_count == 0


def test_123_c1_matrix_06_rotation_order_league_playoff_friendly_league(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 Matrix 6: Rotation order leagueMatch -> playoffMatch -> friendlyMatch -> leagueMatch."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    seen_types: list[str] = []

    def _fetch_for_type(mt: str) -> FC27CommunityProviderEnvelope:
        seen_types.append(mt)
        env, _ = _build_validated_envelope(
            2, start_id=911000 + len(seen_types) * 10, match_type=mt
        )
        return env

    for step in range(4):
        res = run_fc27_poll_cycle(
            runtime=rt,
            confirm_live_polling=True,
            fetch_matches_fn=_fetch_for_type,
            persist_fn=lambda _e: _fake_persist_summary(inserted=2),
            analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
            now_utc_fn=lambda s=step: t0 + timedelta(seconds=43200 * s),
            use_cache=False,
            use_rate_limiter=False,
        )
        assert res.poll_status == "NEW_MATCHES_PERSISTED"

    assert seen_types == [
        "leagueMatch",
        "playoffMatch",
        "friendlyMatch",
        "leagueMatch",
    ]


def test_124_c1_matrix_07_one_provider_request_per_cycle_across_all_match_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 Matrix 7: One provider request per cycle across all match types."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)

    for step, expected_mt in enumerate(APPROVED_MATCH_TYPES):
        calls_in_cycle = 0

        def _fetch(mt: str) -> FC27CommunityProviderEnvelope:
            nonlocal calls_in_cycle
            calls_in_cycle += 1
            return _build_validated_envelope(1, start_id=912000 + step * 10, match_type=mt)[0]

        res = run_fc27_poll_cycle(
            runtime=rt,
            confirm_live_polling=True,
            fetch_matches_fn=_fetch,
            persist_fn=lambda _e: _fake_persist_summary(inserted=1),
            analytics_fn=lambda: {"confidence_tier": "LOW_CONFIDENCE"},
            now_utc_fn=lambda s=step: t0 + timedelta(seconds=43200 * s),
            use_cache=False,
            use_rate_limiter=False,
        )
        assert res.match_type == expected_mt
        assert res.provider_requests == 1
        assert calls_in_cycle == 1


def test_125_c1_matrix_08_12_hour_interval_and_max_2_requests_per_24h_preserved() -> None:
    """C1 Matrix 8: 12-hour interval (43200s) and max 2 requests per 24 hours preserved."""
    pol = FC27PollingPolicy()
    assert pol.poll_interval_seconds == 43200
    assert pol.minimum_interval_seconds == 43200
    assert pol.maximum_interval_seconds == 43200
    assert pol.max_provider_requests_per_24_hours == 2
    assert pol.maximum_requests_per_cycle == 1
    assert pol.approved_match_types == ("leagueMatch", "playoffMatch", "friendlyMatch")


def test_126_c1_matrix_09_zero_retries_and_zero_catch_up_bursts_preserved() -> None:
    """C1 Matrix 9: Zero retries and zero catch-up bursts preserved."""
    pol = FC27PollingPolicy()
    assert pol.maximum_retries == 0
    assert pol.catch_up_bursts_allowed is False


def test_127_c1_matrix_10_restart_before_43200s_performs_zero_provider_requests(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 10: Restart before 43200s performs zero provider requests."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 2, 0, 0, 0, tzinfo=timezone.utc)
    env, _ = _build_validated_envelope(2, start_id=913001, match_type="leagueMatch")
    r1 = run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.poll_status == "NEW_MATCHES_PERSISTED"

    rt_restarted = FC27PollingRuntime()
    r2 = run_fc27_poll_cycle(
        runtime=rt_restarted,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: (_ for _ in ()).throw(AssertionError("Must not call provider")),
        now_utc_fn=lambda: t0 + timedelta(seconds=43199),
        use_cache=False,
        use_rate_limiter=False,
        recover_state_from_db=True,
    )
    assert r2.poll_status == "SKIPPED_MIN_INTERVAL"
    assert r2.provider_requests == 0


def test_128_c1_matrix_11_restart_after_43200s_polls_next_match_type_in_rotation_once(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 11: Restart after >= 43200s polls the next match type in rotation once."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 2, 0, 0, 0, tzinfo=timezone.utc)
    env_league, _ = _build_validated_envelope(2, start_id=914001, match_type="leagueMatch")
    r1 = run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env_league,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.poll_status == "NEW_MATCHES_PERSISTED"
    assert r1.match_type == "leagueMatch"

    polled_types: list[str] = []

    def _fetch_after_restart(mt: str) -> FC27CommunityProviderEnvelope:
        polled_types.append(mt)
        return _build_validated_envelope(2, start_id=914101, match_type=mt)[0]

    rt_restarted = FC27PollingRuntime()
    r2 = run_fc27_poll_cycle(
        runtime=rt_restarted,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=_fetch_after_restart,
        now_utc_fn=lambda: t0 + timedelta(seconds=43200),
        use_cache=False,
        use_rate_limiter=False,
        recover_state_from_db=True,
    )
    assert r2.poll_status == "NEW_MATCHES_PERSISTED"
    assert polled_types == ["playoffMatch"]
    assert r2.match_type == "playoffMatch"
    assert r2.next_match_type == "friendlyMatch"
    assert r2.provider_requests == 1


def test_129_c1_matrix_12_zero_match_response_advances_rotation_cleanly_without_inserts_or_jobs(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 12: 0-match response for a match type advances rotation cleanly without error, without inserting match rows, and without enqueueing Discord jobs."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    empty_env, _ = normalize_community_match_records(
        [],
        match_type="playoffMatch",
        observed_at="2026-11-02T12:00:00Z",
        alias_hmac_key=TEST_ALIAS_HMAC_KEY,
    )
    rt = FC27PollingRuntime()
    rt.current_rotation_match_type = "playoffMatch"
    res = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: empty_env,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NO_NEW_MATCHES"
    assert res.match_type == "playoffMatch"
    assert res.next_match_type == "friendlyMatch"
    assert res.inserted_match_count == 0
    assert res.discord_jobs_inserted_count == 0
    assert res.fingerprints_inserted_count == 0

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
            assert cur.fetchone()[0] == 0

    # Verify restart durability of the 0-match rotation advance
    rt_recovered = recover_polling_state_from_postgres(pg_db_migrated.poll_writer)
    assert rt_recovered.last_successful_match_type == "playoffMatch"
    assert rt_recovered.current_rotation_match_type == "friendlyMatch"


def test_130_c1_matrix_13_valid_alias_and_hmac_key_produces_deterministic_64_hex() -> None:
    """C1 Matrix 13: Valid alias + valid HMAC key produces deterministic 64-character lowercase hex HMAC."""
    from src.fc27_privacy import compute_alias_hmac_fingerprint

    d1 = compute_alias_hmac_fingerprint("CaptainEast99", TEST_ALIAS_HMAC_KEY)
    d2 = compute_alias_hmac_fingerprint("CaptainEast99", TEST_ALIAS_HMAC_KEY)
    assert d1 == d2
    assert re.fullmatch(r"^[0-9a-f]{64}$", d1)


def test_131_c1_matrix_14_equivalent_aliases_with_whitespace_and_case_produce_identical_hmac() -> None:
    """C1 Matrix 14: Equivalent aliases with whitespace/case differences produce identical HMAC."""
    from src.fc27_privacy import compute_alias_hmac_fingerprint

    d1 = compute_alias_hmac_fingerprint("  East   Coaster_Pro  ", TEST_ALIAS_HMAC_KEY)
    d2 = compute_alias_hmac_fingerprint("east coaster_pro", TEST_ALIAS_HMAC_KEY)
    d3 = compute_alias_hmac_fingerprint("EAST COASTER_PRO", TEST_ALIAS_HMAC_KEY)
    assert d1 == d2 == d3


def test_132_c1_matrix_15_missing_empty_or_short_hmac_key_fails_closed_before_polling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C1 Matrix 15: Missing, empty, or < 32-byte HMAC key fails closed before polling persistence."""
    _enable_all_polling_gates(monkeypatch)
    provider_calls = 0

    def _fetch(_mt: str) -> FC27CommunityProviderEnvelope:
        nonlocal provider_calls
        provider_calls += 1
        return _build_validated_envelope(1)[0]

    # Case A: missing file
    monkeypatch.setenv("FC27_ALIAS_HMAC_KEY_FILE", str(tmp_path / "does_not_exist.key"))
    monkeypatch.delenv("FC27_ALIAS_HMAC_KEY", raising=False)
    res_missing = run_fc27_poll_cycle(confirm_live_polling=True, fetch_matches_fn=_fetch)
    assert res_missing.poll_status == "BLOCKED_FEATURE_GATE"
    assert res_missing.error_category == "ALIAS_HMAC_KEY_MISSING_OR_INVALID"

    # Case B: empty file
    empty_file = tmp_path / "empty.key"
    empty_file.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("FC27_ALIAS_HMAC_KEY_FILE", str(empty_file))
    res_empty = run_fc27_poll_cycle(confirm_live_polling=True, fetch_matches_fn=_fetch)
    assert res_empty.poll_status == "BLOCKED_FEATURE_GATE"
    assert res_empty.error_category == "ALIAS_HMAC_KEY_MISSING_OR_INVALID"

    # Case C: short (< 32 bytes) key
    short_file = tmp_path / "short.key"
    short_file.write_text("too-short-key-only-20b", encoding="utf-8")
    monkeypatch.setenv("FC27_ALIAS_HMAC_KEY_FILE", str(short_file))
    res_short = run_fc27_poll_cycle(confirm_live_polling=True, fetch_matches_fn=_fetch)
    assert res_short.poll_status == "BLOCKED_FEATURE_GATE"
    assert res_short.error_category == "ALIAS_HMAC_KEY_MISSING_OR_INVALID"
    assert provider_calls == 0


def test_133_c1_matrix_16_same_pseudonym_and_hmac_updates_timestamps_without_duplicate_rows(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 16: Same (pseudonym, gamertag_hmac) updates last_seen_at and updated_at without duplicate rows."""
    _enable_all_polling_gates(monkeypatch)
    t0 = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=43200)
    rt = FC27PollingRuntime()
    env1, _ = _build_validated_envelope(
        2, start_id=915001, players_per_match=6, match_type="leagueMatch"
    )
    env2, _ = _build_validated_envelope(
        2, start_id=915101, players_per_match=6, match_type="playoffMatch"
    )

    r1 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env1,
        now_utc_fn=lambda: t0,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.fingerprints_inserted_count == 6
    assert r1.fingerprints_refreshed_count == 0
    assert r1.fingerprint_duplicate_observations_count == 6

    r2 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env2,
        now_utc_fn=lambda: t1,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.fingerprints_inserted_count == 0
    assert r2.fingerprints_refreshed_count == 6
    assert r2.fingerprint_duplicate_observations_count == 6

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), BOOL_AND(updated_at >= created_at), BOOL_AND(last_seen_at >= created_at) FROM public.fc27_player_alias_fingerprints"
            )
            cnt, upd_ok, seen_ok = cur.fetchone()
            assert cnt == 6
            assert upd_ok is True
            assert seen_ok is True


def test_134_c1_matrix_17_existing_pseudonym_with_different_hmac_fails_closed_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 17: Existing pseudonym with different gamertag_hmac fails closed and rolls back."""
    from src.fc27_community_provider import FC27AliasFingerprintObservation

    _enable_all_polling_gates(monkeypatch)
    env1, _ = _build_validated_envelope(2, start_id=916001, players_per_match=6)
    r1 = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env1,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.poll_status == "NEW_MATCHES_PERSISTED"
    existing_pseudo = env1.alias_fingerprints[0].pseudonym

    env2_base, _ = _build_validated_envelope(
        2, start_id=916101, players_per_match=6, match_type="playoffMatch"
    )
    colliding_fps = (
        FC27AliasFingerprintObservation(
            pseudonym=existing_pseudo,
            gamertag_hmac="f" * 64,
        ),
    )
    env2 = env2_base.model_copy(update={"alias_fingerprints": colliding_fps})

    rt2 = FC27PollingRuntime()
    rt2.current_rotation_match_type = "playoffMatch"
    r2 = run_fc27_poll_cycle(
        runtime=rt2,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env2,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.poll_status == "PRIVACY_REJECTED"
    assert r2.error_category == "FINGERPRINT_COLLISION"
    assert r2.fingerprint_collision_count == 1
    assert r2.inserted_match_count == 0

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 2
            cur.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
            assert cur.fetchone()[0] == 2


def test_135_c1_matrix_18_existing_hmac_with_different_pseudonym_fails_closed_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 18: Existing gamertag_hmac with different pseudonym fails closed and rolls back."""
    from src.fc27_community_provider import FC27AliasFingerprintObservation

    _enable_all_polling_gates(monkeypatch)
    env1, _ = _build_validated_envelope(2, start_id=917001, players_per_match=6)
    run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env1,
        use_cache=False,
        use_rate_limiter=False,
    )
    existing_hmac = env1.alias_fingerprints[0].gamertag_hmac

    env2_base, _ = _build_validated_envelope(
        2, start_id=917101, players_per_match=6, match_type="playoffMatch"
    )
    colliding_fps = (
        FC27AliasFingerprintObservation(
            pseudonym="plr_ecf_deadbeef0123",
            gamertag_hmac=existing_hmac,
        ),
    )
    env2 = env2_base.model_copy(update={"alias_fingerprints": colliding_fps})

    rt2 = FC27PollingRuntime()
    rt2.current_rotation_match_type = "playoffMatch"
    r2 = run_fc27_poll_cycle(
        runtime=rt2,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env2,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.poll_status == "PRIVACY_REJECTED"
    assert r2.error_category == "FINGERPRINT_COLLISION"
    assert r2.inserted_match_count == 0

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 2


def test_136_c1_matrix_19_fingerprints_table_never_stores_plaintext_gamertags_raw_ids_or_hmac_key(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 19: fc27_player_alias_fingerprints never stores plaintext gamertags, raw EA IDs, or the HMAC key."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(2, start_id=918001, players_per_match=6)
    run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pseudonym, gamertag_hmac FROM public.fc27_player_alias_fingerprints"
            )
            rows = cur.fetchall()
    assert len(rows) == 6
    for pseudo, ghmac in rows:
        assert re.fullmatch(r"^plr_ecf_[0-9a-f]{12}$", pseudo)
        assert re.fullmatch(r"^[0-9a-f]{64}$", ghmac)
        assert "StripThisPrivateGamerTag" not in pseudo
        assert "StripThisPrivateGamerTag" not in ghmac
        assert TEST_ALIAS_HMAC_KEY not in ghmac


def test_137_c1_matrix_20_poll_writer_can_select_insert_and_update_timestamps_on_fingerprints(
    pg_db_migrated,
) -> None:
    """C1 Matrix 20: fc27_poll_writer can SELECT, INSERT, and UPDATE (last_seen_at, updated_at) on fc27_player_alias_fingerprints."""
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    later = now + timedelta(hours=12)
    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_player_alias_fingerprints (
                pseudonym, gamertag_hmac, last_seen_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            ("plr_ecf_001122334455", "a" * 64, now, now, now),
        )
        conn.execute(
            """
            UPDATE public.fc27_player_alias_fingerprints
               SET last_seen_at = %s, updated_at = %s
             WHERE pseudonym = %s
            """,
            (later, later, "plr_ecf_001122334455"),
        )
        cur = conn.execute(
            "SELECT pseudonym, gamertag_hmac FROM public.fc27_player_alias_fingerprints WHERE pseudonym = %s",
            ("plr_ecf_001122334455",),
        )
        assert cur.fetchone() == ("plr_ecf_001122334455", "a" * 64)
        conn.commit()


def test_138_c1_matrix_21_poll_writer_cannot_delete_or_update_immutable_columns_on_fingerprints(
    pg_db_migrated,
) -> None:
    """C1 Matrix 21: fc27_poll_writer cannot DELETE from fc27_player_alias_fingerprints or update immutable columns."""
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_player_alias_fingerprints (
                pseudonym, gamertag_hmac, last_seen_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            ("plr_ecf_aabbccddeeff", "b" * 64, now, now, now),
        )
        conn.commit()
        with pytest.raises(FC27PollingTableAccessDeniedError):
            conn.execute(
                "DELETE FROM public.fc27_player_alias_fingerprints WHERE pseudonym = 'plr_ecf_aabbccddeeff'"
            )
        with pytest.raises(FC27PollingTableAccessDeniedError):
            conn.execute(
                "UPDATE public.fc27_player_alias_fingerprints SET gamertag_hmac = %s WHERE pseudonym = %s",
                ("c" * 64, "plr_ecf_aabbccddeeff"),
            )

    # Also verify PostgreSQL role enforcement directly without the Python SQL guard
    with psycopg.connect(pg_db_migrated.poll_writer) as raw_conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw_conn.execute(
                "DELETE FROM public.fc27_player_alias_fingerprints WHERE pseudonym = 'plr_ecf_aabbccddeeff'"
            )
        raw_conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw_conn.execute(
                "UPDATE public.fc27_player_alias_fingerprints SET gamertag_hmac = %s WHERE pseudonym = %s",
                ("c" * 64, "plr_ecf_aabbccddeeff"),
            )
        raw_conn.rollback()


def test_139_c1_matrix_22_23_new_match_enqueues_one_pending_job_with_deterministic_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 22 & 23: New persisted match enqueues one post_new_results job with state='pending', attempt_count=0, and idempotency_key='fc27:post_new_results:<match_ref>'."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(1, start_id=919001, players_per_match=6)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.discord_jobs_inserted_count == 1
    expected_mref = _pseudonymize_match_ref("919001")

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_type, state, attempt_count, idempotency_key, payload, claim_owner, claimed_at, completed_at FROM public.fc27_discord_jobs"
            )
            rows = cur.fetchall()
    assert len(rows) == 1
    job_type, state, attempts, ikey, payload, owner, claimed, completed = rows[0]
    assert job_type == "post_new_results"
    assert state == "pending"
    assert attempts == 0
    assert ikey == f"fc27:post_new_results:{expected_mref}"
    assert payload["match_ref"] == expected_mref
    assert owner is None
    assert claimed is None
    assert completed is None


def test_140_c1_matrix_24_batch_of_3_new_matches_enqueues_3_jobs(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 24: Batch of 3 new matches enqueues 3 jobs with distinct deterministic idempotency keys."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, start_id=920001, players_per_match=6)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.discord_jobs_inserted_count == 3
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT idempotency_key FROM public.fc27_discord_jobs ORDER BY job_id ASC")
            keys = [r[0] for r in cur.fetchall()]
    assert len(keys) == 3
    assert len(set(keys)) == 3
    for mid in ("920001", "920002", "920003"):
        assert f"fc27:post_new_results:{_pseudonymize_match_ref(mid)}" in keys


def test_141_c1_matrix_25_partial_new_batch_8_existing_2_new_enqueues_only_2_new_jobs(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 25: Partial new batch (8 existing + 2 new) enqueues only 2 new jobs."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env_8, _ = _build_validated_envelope(8, start_id=921001, players_per_match=6)
    r1 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_8,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.discord_jobs_inserted_count == 8

    rt.next_eligible_at = None
    env_10, _ = _build_validated_envelope(10, start_id=921001, players_per_match=6)
    r2 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_10,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.inserted_match_count == 2
    assert r2.discord_jobs_inserted_count == 2
    assert r2.discord_jobs_existing_count == 8

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
            assert cur.fetchone()[0] == 10


def test_142_c1_matrix_26_27_duplicate_batch_enqueues_0_new_jobs_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 26 & 27: Duplicate batch (0 new matches) enqueues 0 new jobs and re-running identical match batch is idempotent."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env, _ = _build_validated_envelope(4, start_id=922001, players_per_match=6)
    r1 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.discord_jobs_inserted_count == 4

    rt.next_eligible_at = None
    r2 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.poll_status == "NO_NEW_MATCHES"
    assert r2.discord_jobs_inserted_count == 0
    assert r2.discord_jobs_existing_count == 4

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
            assert cur.fetchone()[0] == 4


def test_143_c1_matrix_28_conflicting_payload_for_existing_idempotency_key_fails_closed_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 28: Conflicting payload for an existing idempotency_key fails closed and rolls back."""
    _enable_all_polling_gates(monkeypatch)
    mref = _pseudonymize_match_ref("923001")
    ikey = f"fc27:post_new_results:{mref}"

    # Pre-seed a conflicting payload under the same idempotency_key
    with psycopg.connect(pg_db_migrated.admin) as conn:
        conn.execute(
            """
            INSERT INTO public.fc27_discord_jobs (
                job_type, idempotency_key, payload, state, attempt_count
            ) VALUES (
                'post_new_results', %s, %s::jsonb, 'pending', 0
            )
            """,
            (
                ikey,
                json.dumps(
                    {
                        "club_id": "330323",
                        "export_id": "comm_poll_conflict",
                        "game_version": "FC27",
                        "goals_against": 99,
                        "goals_for": 0,
                        "match_hash": "0" * 64,
                        "match_ref": mref,
                        "match_type": "leagueMatch",
                        "opponent_name": "Tampered Club",
                        "platform": "common-gen5",
                        "played_at": "2026-10-31T00:00:00Z",
                        "result": "LOSS",
                    }
                ),
            ),
        )
        conn.commit()

    env, _ = _build_validated_envelope(2, start_id=923001, players_per_match=6)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "REPLAY_CONFLICT"
    assert res.error_category == "DISCORD_JOB_IDEMPOTENCY_CONFLICT"
    assert res.discord_job_conflict_count == 1
    assert res.inserted_match_count == 0

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_alias_fingerprints")
            assert cur.fetchone()[0] == 0


def test_144_c1_matrix_29_job_payload_passes_safe_bounded_jsonb_and_privacy_checks(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 29: Job payload passes fc27_is_safe_bounded_jsonb, contains no sensitive keys, mentions, or URLs, and stays <= 4096 bytes."""
    from src.fc27_privacy import contains_discord_mention, contains_prohibited_url, scan_for_sensitive_fields

    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, start_id=924001, players_per_match=6)
    run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM public.fc27_discord_jobs")
            payloads = [r[0] for r in cur.fetchall()]
    assert len(payloads) == 3
    for p in payloads:
        raw_bytes = json.dumps(p, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert len(raw_bytes) <= 4096
        assert scan_for_sensitive_fields(p) == []
        assert not contains_discord_mention(json.dumps(p))
        assert not contains_prohibited_url(json.dumps(p))


def test_145_c1_matrix_30_31_32_poll_writer_is_producer_only_on_discord_jobs(
    pg_db_migrated,
) -> None:
    """C1 Matrix 30, 31, 32: Polling worker never claims/completes/fails/retries/deletes jobs; fc27_poll_writer can SELECT and INSERT but cannot UPDATE or DELETE fc27_discord_jobs."""
    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as conn:
        with pytest.raises(FC27PollingTableAccessDeniedError):
            conn.execute("UPDATE public.fc27_discord_jobs SET state = 'claimed'")
        with pytest.raises(FC27PollingTableAccessDeniedError):
            conn.execute("DELETE FROM public.fc27_discord_jobs")

    with psycopg.connect(pg_db_migrated.poll_writer) as raw_conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw_conn.execute("UPDATE public.fc27_discord_jobs SET state = 'completed'")
        raw_conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw_conn.execute("DELETE FROM public.fc27_discord_jobs")
        raw_conn.rollback()


def test_146_c1_matrix_33_34_atomic_commit_and_zero_discord_network_calls(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 33 & 34: Match insert, player stat insert, fingerprint upsert, and Discord job insert commit together on success with zero Discord calls."""
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, start_id=925001, players_per_match=6)
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.inserted_match_count == 3
    assert res.fingerprints_inserted_count == 6
    assert res.discord_jobs_inserted_count == 3
    assert _EXTERNAL_NETWORK_CALLS == []

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_match_stats")
            assert cur.fetchone()[0] == 18
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_alias_fingerprints")
            assert cur.fetchone()[0] == 6
            cur.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
            assert cur.fetchone()[0] == 3


def test_147_c1_matrix_35_simulated_failure_during_fingerprint_persistence_rolls_back_all_writes(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 35: Simulated failure during fingerprint persistence rolls back match, player stat, batch, and Discord job writes."""
    import src.fc27_repository as repo_mod

    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, start_id=926001, players_per_match=6)
    monkeypatch.setattr(
        repo_mod,
        "persist_player_alias_fingerprints_in_tx",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            psycopg.DatabaseError("simulated fingerprint persistence failure")
        ),
    )
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "TRANSIENT_FAILURE"
    assert res.error_category == "PERSISTENCE_FAILURE"
    assert res.inserted_match_count == 0

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            for tbl in (
                "fc27_export_batches",
                "fc27_matches",
                "fc27_player_match_stats",
                "fc27_player_alias_fingerprints",
                "fc27_discord_jobs",
            ):
                cur.execute(f"SELECT COUNT(*) FROM public.{tbl}")
                assert cur.fetchone()[0] == 0


def test_148_c1_matrix_36_simulated_failure_during_discord_job_enqueue_rolls_back_all_writes(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 36: Simulated failure during Discord job enqueue rolls back match, player stat, batch, and fingerprint writes."""
    import src.fc27_repository as repo_mod

    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(3, start_id=927001, players_per_match=6)
    monkeypatch.setattr(
        repo_mod,
        "enqueue_post_new_results_jobs_in_tx",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            psycopg.DatabaseError("simulated discord job enqueue failure")
        ),
    )
    res = run_fc27_poll_cycle(
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda _mt: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "TRANSIENT_FAILURE"
    assert res.error_category == "PERSISTENCE_FAILURE"
    assert res.inserted_match_count == 0

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            for tbl in (
                "fc27_export_batches",
                "fc27_matches",
                "fc27_player_match_stats",
                "fc27_player_alias_fingerprints",
                "fc27_discord_jobs",
            ):
                cur.execute(f"SELECT COUNT(*) FROM public.{tbl}")
                assert cur.fetchone()[0] == 0


def test_149_c1_matrix_37_replay_conflict_rolls_back_match_stats_batch_fingerprint_and_job_writes(
    monkeypatch: pytest.MonkeyPatch,
    pg_db_migrated,
) -> None:
    """C1 Matrix 37: Replay conflict rolls back match, player stat, batch, fingerprint, and Discord job writes."""
    _enable_all_polling_gates(monkeypatch)
    rt = FC27PollingRuntime()
    env_initial, _ = _build_validated_envelope(2, start_id=928001, players_per_match=4)
    r1 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_initial,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r1.inserted_match_count == 2
    assert r1.fingerprints_inserted_count == 4
    assert r1.discord_jobs_inserted_count == 2

    rt.next_eligible_at = None
    env_conflict, _ = _build_validated_envelope(
        3, start_id=928001, players_per_match=6, goals_override=7
    )
    r2 = run_fc27_poll_cycle(
        runtime=rt,
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda: env_conflict,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert r2.poll_status == "REPLAY_CONFLICT"
    assert r2.inserted_match_count == 0
    assert r2.fingerprints_inserted_count == 0
    assert r2.discord_jobs_inserted_count == 0

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches")
            assert cur.fetchone()[0] == 2
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_alias_fingerprints")
            assert cur.fetchone()[0] == 4
            cur.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
            assert cur.fetchone()[0] == 2


def test_150_c1_matrix_38_39_40_role_boundaries_for_poll_writer_discord_reader_and_app(
    pg_db_migrated,
) -> None:
    """C1 Matrix 38, 39, 40: Verify fc27_poll_writer, fc27_discord_reader, and fc27_app role boundaries on Stage C tables."""
    # 38: fc27_poll_writer cannot access player_dev or manual match tables
    with open_guarded_polling_connection(pg_db_migrated.poll_writer) as gconn:
        for forbidden_tbl in (
            "fc27_player_links",
            "fc27_build_catalog",
            "fc27_build_catalog_audit",
            "fc27_player_suggestions",
            "fc27_suggestion_assessments",
            "fc27_manual_matches",
            "fc27_manual_match_players",
            "fc27_manual_match_events",
            "fc27_manual_match_audit",
        ):
            with pytest.raises(FC27PollingTableAccessDeniedError):
                gconn.execute(f"SELECT COUNT(*) FROM public.{forbidden_tbl}")

    # 39: fc27_discord_reader is read-only on fingerprints and discord_jobs
    with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
        with rconn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_player_alias_fingerprints")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
            assert cur.fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rconn.execute(
                "INSERT INTO public.fc27_player_alias_fingerprints (pseudonym, gamertag_hmac) VALUES ('plr_ecf_112233445566', %s)",
                ("d" * 64,),
            )
        rconn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rconn.execute(
                "INSERT INTO public.fc27_discord_jobs (job_type, idempotency_key, payload) VALUES ('post_new_results', 'k1', '{}'::jsonb)"
            )
        rconn.rollback()

    # 40: fc27_app has no access to fingerprints or discord_jobs
    with psycopg.connect(pg_db_migrated.app) as aconn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            aconn.execute("SELECT COUNT(*) FROM public.fc27_player_alias_fingerprints")
        aconn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            aconn.execute("SELECT COUNT(*) FROM public.fc27_discord_jobs")
        aconn.rollback()


def test_151_c1_matrix_41_42_status_surfaces_expose_c1_metadata_and_zero_external_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """C1 Matrix 41 & 42: /fc27 status and --status remain offline/read-only and expose C1 rotation, fingerprint, and job-production metadata without secrets or raw aliases."""
    rt = FC27PollingRuntime()
    rc = polling_cli_main(["--status"], runtime=rt)
    out = capsys.readouterr().out
    assert rc == 0
    assert "APPROVED_MATCH_TYPES=leagueMatch,playoffMatch,friendlyMatch" in out
    assert "CURRENT_ROTATION_MATCH_TYPE=leagueMatch" in out
    assert "NEXT_ROTATION_MATCH_TYPE=playoffMatch" in out
    assert "TOTAL_FINGERPRINTS_INSERTED=0" in out
    assert "TOTAL_DISCORD_JOBS_INSERTED=0" in out

    svc = FC27DiscordBotService(
        FC27DiscordBotConfig(
            enabled=True,
            allowed_guild_ids=frozenset({"123456789012345678"}),
            allowed_channel_ids=frozenset({"234567890123456789"}),
        ),
        polling_runtime=rt,
    )
    resp = svc.handle_slash_command(
        "status",
        guild_id="123456789012345678",
        channel_id="234567890123456789",
    )
    assert resp.status == "OK"
    fields = {k: v for emb in resp.embeds for k, v in emb.fields.items()}
    assert fields["approved_match_types"] == "leagueMatch,playoffMatch,friendlyMatch"
    assert fields["current_rotation_match_type"] == "leagueMatch"
    assert fields["next_rotation_match_type"] == "playoffMatch"
    assert fields["fingerprints_inserted"] == "0"
    assert fields["discord_jobs_inserted"] == "0"
    assert _EXTERNAL_NETWORK_CALLS == []
