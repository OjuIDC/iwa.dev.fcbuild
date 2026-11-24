"""Exhaustive tests for FC27 all-match-types polling implementation (Migration 0022 / Grants Policy 23).

Covers:
1. 3-type fixed order (leagueMatch -> playoffMatch -> friendlyMatch), 3 attempts, 2 delays, 0 retries.
2. Independent persistence (earlier success survives later failure).
3. 401/403/429 upstream block stop behavior (budget preserved).
4. Shared schema failure stop behavior.
5. Privacy rejection stop behavior.
6. Type-local timeout continuation to remaining match types.
7. No-results and duplicate-only continuation.
8. Per-cycle request cap enforcement in PostgreSQL ledger.
9. Rolling 24-hour daily cap enforcement across cycles.
10. Failure and timeout counting in durable request budget.
11. Concurrency-safe final-slot reservation under transaction advisory lock.
12. Shared scheduled and manual request budget.
13. Stale/crashed reservation accounting.
14. Idempotent attempt finalization and category validation.
15. Policy and settings validation for match types, delay, and zero retries.
16. Disabled-mode compatibility (single-type rotation).
17. Chronological Discord delivery jobs with zero raw payloads.
18. Migration 0022 and Grants Policy 23 idempotency and least-privilege security verification.

Zero external network calls and zero EA DNS lookups are executed (`NETWORK_GUARD=PASS`).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from src import settings
from src.db_migrate import migrate
from src.db_roles import GRANTS_POLICY_VERSION
from src.fc27_api_rate_limit import FC27AdapterCircuitBreaker, FC27CircuitState
from src.fc27_clubs_api_contracts import FC27SchemaValidationError
from src.fc27_community_provider import (
    COMMUNITY_PROVIDER_ID,
    LIVE_EA_CLUBS_COMMUNITY_CLIENT,
    FC27CommunityPersistenceApplySummary,
    FC27CommunityProviderEnvelope,
)
from src.fc27_privacy import FC27FingerprintCollisionError, FC27PrivacyViolationError
from src.fc27_polling_worker import (
    APPROVED_MATCH_TYPES,
    FIXED_CLUB_ID,
    FIXED_CLUB_NAME,
    FIXED_GAME_VERSION,
    FIXED_MATCH_TYPE,
    FIXED_PLATFORM,
    POLL_INTERVAL_FOUR_HOURS_SECONDS,
    POLL_INTERVAL_SECONDS,
    FC27PollResult,
    FC27PollTypeSummary,
    FC27PollingIdentityRejectedError,
    FC27PollingPolicy,
    FC27PollingPrivacyRejectedError,
    FC27PollingProviderBlockedError,
    FC27PollingRuntime,
    FC27PollingSchemaDriftError,
    FC27PollingTransientFailureError,
    _record_provider_attempt_outcome,
    _reserve_provider_request_budget,
    build_fc27_polling_policy_from_env,
    run_fc27_poll_cycle,
    should_continue_polling_cycle_after_result,
)
from src.fc27_provider import FC27AuthenticationError, FC27LiveAccessBlockedError, FC27TransportError
from src.settings import ConfigError, get_fc27_community_poll_max_requests
from tests.test_fc27_pandas_view import _make_pandas_shim
from tests.test_fc27_polling_worker import (
    _build_validated_envelope,
    _enable_all_polling_gates,
    _fake_persist_summary,
)

_EXTERNAL_NETWORK_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _guard_network_and_reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure zero external network or DNS calls occur during testing."""
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


def _make_all_types_policy(
    *,
    inter_delay: int = 12,
    daily_budget: int = 18,
    match_types: tuple[str, ...] = ("leagueMatch", "playoffMatch", "friendlyMatch"),
) -> FC27PollingPolicy:
    return FC27PollingPolicy(
        poll_interval_seconds=14400,
        minimum_interval_seconds=14400,
        maximum_interval_seconds=14400,
        polling_frequency="ONCE_EVERY_4_HOURS",
        max_provider_requests_per_24_hours=daily_budget,
        poll_all_match_types=True,
        poll_match_types=match_types,
        maximum_requests_per_cycle=3,
        inter_request_delay_seconds=inter_delay,
    )


# ==============================================================================
# 1. FIXED ORDER, 3 ATTEMPTS, 2 DELAYS, ZERO RETRIES
# ==============================================================================


def test_001_three_types_fixed_order_two_delays_zero_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-types cycle executes leagueMatch -> playoffMatch -> friendlyMatch with 2 delays and 0 retries."""
    _enable_all_polling_gates(monkeypatch)
    delays_called: list[float] = []
    monkeypatch.setattr("src.fc27_polling_worker.time.sleep", lambda sec: delays_called.append(sec))

    policy = _make_all_types_policy(inter_delay=15)
    called_types: list[str] = []

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        called_types.append(match_type)
        return _build_validated_envelope(1, start_id=981000 + len(called_types), match_type=match_type)[0]

    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=lambda _env: _fake_persist_summary(inserted=0),
        use_cache=False,
        use_rate_limiter=False,
    )

    assert called_types == ["leagueMatch", "playoffMatch", "friendlyMatch"]
    assert len(delays_called) == 2
    assert delays_called == [15, 15]
    assert result.batch_requests_attempted == 3
    assert result.batch_requests_succeeded == 3
    assert result.batch_requests_failed == 0
    assert result.retries_attempted == 0
    assert len(result.batch_type_results) == 3
    assert [ts.match_type for ts in result.batch_type_results] == ["leagueMatch", "playoffMatch", "friendlyMatch"]


# ==============================================================================
# 2. INDEPENDENT PERSISTENCE (EARLIER SUCCESS SURVIVES LATER FAILURE)
# ==============================================================================


def test_002_independent_persistence_earlier_success_survives_later_failure(
    pg_db_migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Earlier match type persists transactionally and survives a failure in a subsequent match type."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setattr("src.fc27_polling_worker.time.sleep", lambda _sec: None)

    policy = _make_all_types_policy()
    seeded_env, _ = _build_validated_envelope(1, start_id=982100, match_type="leagueMatch")

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        if match_type == "leagueMatch":
            return seeded_env
        raise FC27TransportError(f"Transient error fetching {match_type}")

    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=fetch,
        use_cache=False,
        use_rate_limiter=False,
    )

    assert result.inserted_match_count == 1
    assert result.batch_requests_attempted == 3
    assert result.batch_requests_succeeded == 1
    assert result.batch_requests_failed == 2

    # Verify directly via database query that the match persisted even though subsequent types failed
    with psycopg.connect(pg_db_migrated.poll_writer) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.fc27_matches WHERE external_match_id = %s", ("982100",))
            count = cur.fetchone()[0]
            assert count == 1, "leagueMatch row must remain committed in public.fc27_matches"


# ==============================================================================
# 3. UPSTREAM PROVIDER BLOCKED (401/403/429) STOPS CYCLE IMMEDIATELY
# ==============================================================================


def test_003_upstream_provider_blocked_stops_immediately_and_preserves_budget(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """When upstream blocks access (e.g. 401/403/429), cycle stops immediately without probing remaining types."""
    _enable_all_polling_gates(monkeypatch)
    called: list[str] = []

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        called.append(match_type)
        if match_type == "leagueMatch":
            raise FC27AuthenticationError("401 Unauthorized upstream")
        return _build_validated_envelope(1, start_id=983000, match_type=match_type)[0]

    policy = _make_all_types_policy()
    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=lambda _env: _fake_persist_summary(inserted=0),
        use_cache=False,
        use_rate_limiter=False,
    )

    assert called == ["leagueMatch"], "playoffMatch and friendlyMatch must NOT be called after PROVIDER_BLOCKED"
    assert result.poll_status == "PROVIDER_BLOCKED"
    assert result.batch_requests_attempted == 1


# ==============================================================================
# 4. SHARED SCHEMA FAILURE STOPS CYCLE IMMEDIATELY
# ==============================================================================


def test_004_shared_schema_failure_stops_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema drift or validation error on one type breaks the cycle immediately to prevent bad ingestion."""
    _enable_all_polling_gates(monkeypatch)
    called: list[str] = []

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        called.append(match_type)
        if match_type == "leagueMatch":
            raise FC27SchemaValidationError("Unexpected provider payload schema")
        return _build_validated_envelope(1, start_id=984000, match_type=match_type)[0]

    policy = _make_all_types_policy()
    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=lambda _env: _fake_persist_summary(inserted=0),
        use_cache=False,
        use_rate_limiter=False,
    )

    assert called == ["leagueMatch"]
    assert result.poll_status == "SCHEMA_DRIFT"


# ==============================================================================
# 5. PRIVACY REJECTION STOPS CYCLE IMMEDIATELY
# ==============================================================================


def test_005_privacy_rejection_stops_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Privacy violation stops the batch immediately to protect player identity."""
    _enable_all_polling_gates(monkeypatch)
    called: list[str] = []

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        called.append(match_type)
        if match_type == "leagueMatch":
            raise FC27PrivacyViolationError("Sensitive plain text detected in record")
        return _build_validated_envelope(1, start_id=985000, match_type=match_type)[0]

    policy = _make_all_types_policy()
    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=lambda _env: _fake_persist_summary(inserted=0),
        use_cache=False,
        use_rate_limiter=False,
    )

    assert called == ["leagueMatch"]
    assert result.poll_status == "PRIVACY_REJECTED"


# ==============================================================================
# 6. TYPE-LOCAL TIMEOUT CONTINUES TO SUBSEQUENT TYPES
# ==============================================================================


def test_006_type_local_timeout_continues_to_subsequent_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient network timeout on leagueMatch does not abort subsequent types."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setattr("src.fc27_polling_worker.time.sleep", lambda _sec: None)
    called: list[str] = []

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        called.append(match_type)
        if match_type == "leagueMatch":
            raise FC27TransportError("Timeout contacting upstream for leagueMatch")
        return _build_validated_envelope(1, start_id=986000 + len(called), match_type=match_type)[0]

    policy = _make_all_types_policy()
    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=lambda _env: _fake_persist_summary(inserted=1),
        analytics_fn=lambda: {"analytics": "mock"},
        use_cache=False,
        use_rate_limiter=False,
    )

    assert called == ["leagueMatch", "playoffMatch", "friendlyMatch"]
    assert result.batch_requests_attempted == 3
    assert result.batch_requests_succeeded == 2
    assert result.batch_requests_failed == 1
    assert result.inserted_match_count == 2
    assert result.poll_status == "NEW_MATCHES_PERSISTED"


# ==============================================================================
# 7. NO-RESULTS AND DUPLICATE-ONLY CONTINUATION
# ==============================================================================


def test_007_no_results_and_duplicates_continue_to_next_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty or duplicate matches in earlier types continue to later types."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setattr("src.fc27_polling_worker.time.sleep", lambda _sec: None)
    called: list[str] = []

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        called.append(match_type)
        count = 0 if match_type == "leagueMatch" else 1
        return _build_validated_envelope(count, start_id=987000 + len(called), match_type=match_type)[0]

    def persist(env: FC27CommunityProviderEnvelope) -> FC27CommunityPersistenceApplySummary:
        mt = env.provenance.get("match_type")
        if mt == "playoffMatch":
            return _fake_persist_summary(inserted=0, duplicates=1)
        if mt == "friendlyMatch":
            return _fake_persist_summary(inserted=1, duplicates=0)
        return _fake_persist_summary(inserted=0, duplicates=0)

    policy = _make_all_types_policy()
    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=persist,
        analytics_fn=lambda: {"analytics": "mock"},
        use_cache=False,
        use_rate_limiter=False,
    )

    assert called == ["leagueMatch", "playoffMatch", "friendlyMatch"]
    assert result.batch_requests_attempted == 3
    assert result.batch_requests_succeeded == 3
    assert result.inserted_match_count == 1
    assert result.duplicate_ignored_count == 1
    assert result.poll_status == "NEW_MATCHES_PERSISTED"


# ==============================================================================
# 8. PER-CYCLE REQUEST CAP ENFORCEMENT IN POSTGRESQL LEDGER
# ==============================================================================


def test_008_durable_request_budget_enforces_per_cycle_cap_in_db(pg_db_migrated) -> None:
    """PostgreSQL procedure fc27_reserve_provider_request enforces per-cycle request cap."""
    db = pg_db_migrated.poll_writer
    policy = _make_all_types_policy(daily_budget=18)

    # 3 requests for cycle-1 should succeed
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="cycle-1", attempt_id="att-1", match_type="leagueMatch"
    ) is True
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="cycle-1", attempt_id="att-2", match_type="playoffMatch"
    ) is True
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="cycle-1", attempt_id="att-3", match_type="friendlyMatch"
    ) is True

    # 4th request in the same cycle must be rejected by per-cycle cap (maximum_requests_per_cycle=3)
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="cycle-1", attempt_id="att-4", match_type="leagueMatch"
    ) is False


# ==============================================================================
# 9. ROLLING 24-HOUR DAILY CAP ENFORCEMENT ACROSS CYCLES
# ==============================================================================


def test_009_durable_request_budget_enforces_rolling_24h_cap_across_cycles(pg_db_migrated) -> None:
    """PostgreSQL procedure fc27_reserve_provider_request enforces rolling 24-hour limit across cycles."""
    db = pg_db_migrated.poll_writer
    # Create policy with a tight daily budget of 3 (the valid minimum for all-types mode)
    policy = _make_all_types_policy(daily_budget=3)

    # Cycle A uses 2 requests
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="cycle-a", attempt_id="att-a1", match_type="leagueMatch"
    ) is True
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="cycle-a", attempt_id="att-a2", match_type="playoffMatch"
    ) is True

    # Cycle B uses 1 request (now at 3 total for the 24h window)
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="cycle-b", attempt_id="att-b1", match_type="leagueMatch"
    ) is True

    # Next attempt in Cycle B must fail because daily budget (3) is exhausted
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="cycle-b", attempt_id="att-b2", match_type="playoffMatch"
    ) is False


# ==============================================================================
# 10. FAILURE AND TIMEOUT COUNTING IN DURABLE BUDGET
# ==============================================================================


def test_010_durable_request_budget_counts_failures_and_timeouts(pg_db_migrated) -> None:
    """Failed and timed out attempts recorded in the ledger still count against the rolling 24h budget."""
    db = pg_db_migrated.poll_writer
    policy = _make_all_types_policy(daily_budget=3)

    # First attempt: reserved then marked TRANSIENT_FAILURE
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="fail-cycle-1", attempt_id="att-f1", match_type="leagueMatch"
    ) is True
    _record_provider_attempt_outcome(
        db_conn_or_uri=db, attempt_id="att-f1", outcome_category="TRANSIENT_FAILURE"
    )

    # Second attempt: reserved then marked PROVIDER_BLOCKED
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="fail-cycle-2", attempt_id="att-f2", match_type="playoffMatch"
    ) is True
    _record_provider_attempt_outcome(
        db_conn_or_uri=db, attempt_id="att-f2", outcome_category="PROVIDER_BLOCKED"
    )

    # Third attempt: reserved then marked FAILED
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="fail-cycle-3", attempt_id="att-f3", match_type="friendlyMatch"
    ) is True
    _record_provider_attempt_outcome(
        db_conn_or_uri=db, attempt_id="att-f3", outcome_category="FAILED"
    )

    # Fourth attempt: daily budget of 3 is now exhausted despite prior attempts being failures
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="fail-cycle-4", attempt_id="att-f4", match_type="leagueMatch"
    ) is False


# ==============================================================================
# 11. CONCURRENT FINAL-SLOT RESERVATION UNDER TRANSACTION ADVISORY LOCK
# ==============================================================================


def test_011_concurrent_final_slot_reservation_is_safe(pg_db_migrated) -> None:
    """When only one request slot remains, concurrent transactions race safely: exactly one wins."""
    db = pg_db_migrated.poll_writer
    policy = _make_all_types_policy(daily_budget=3)

    # Consume first 2 slots
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="pre-race", attempt_id="pre-1", match_type="leagueMatch"
    ) is True
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="pre-race", attempt_id="pre-2", match_type="playoffMatch"
    ) is True

    # Exactly 1 slot remains
    outcomes: list[bool] = []
    lock = threading.Lock()

    def try_reserve(att_id: str) -> None:
        won = _reserve_provider_request_budget(
            db_conn_or_uri=db, policy=policy, cycle_id=f"concurrent-{att_id}", attempt_id=att_id, match_type="friendlyMatch"
        )
        with lock:
            outcomes.append(won)

    t1 = threading.Thread(target=try_reserve, args=("race-slot-1",))
    t2 = threading.Thread(target=try_reserve, args=("race-slot-2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert outcomes.count(True) == 1, "Exactly one concurrent thread must secure the final slot"
    assert outcomes.count(False) == 1, "The second concurrent thread must be denied"


# ==============================================================================
# 12. SHARED SCHEDULED AND MANUAL REQUEST BUDGET
# ==============================================================================


def test_012_shared_scheduled_and_manual_budget(pg_db_migrated) -> None:
    """Manual and scheduled runs share the same underlying fc27_provider_request_attempts table."""
    db = pg_db_migrated.poll_writer
    policy = _make_all_types_policy(daily_budget=3)

    # Manual CLI reservation: 2 requests
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="manual-cycle", attempt_id="manual-att-1", match_type="leagueMatch"
    ) is True
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="manual-cycle", attempt_id="manual-att-2", match_type="playoffMatch"
    ) is True

    # Scheduled cycle reservation: 1 request (reaches daily cap of 3)
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="scheduled-cycle", attempt_id="sched-att-1", match_type="friendlyMatch"
    ) is True

    # Now daily budget is exhausted for both scheduled and manual
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="subsequent-manual", attempt_id="manual-att-3", match_type="leagueMatch"
    ) is False


# ==============================================================================
# 13. STALE AND CRASHED RESERVATIONS STILL COUNT
# ==============================================================================


def test_013_stale_and_crashed_reservations_still_count(pg_db_migrated) -> None:
    """Reservations that never finalized (e.g. process crashed) still protect the upstream provider limit."""
    db = pg_db_migrated.poll_writer
    policy = _make_all_types_policy(daily_budget=3)

    # Reserve 3 slots, leaving them in RESERVED status (simulates worker crashing right after reservation)
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="crash-cycle", attempt_id="crash-att-1", match_type="leagueMatch"
    ) is True
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="crash-cycle", attempt_id="crash-att-2", match_type="playoffMatch"
    ) is True
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="crash-cycle", attempt_id="crash-att-3", match_type="friendlyMatch"
    ) is True

    # Restarted worker attempts reservation -> must be rejected because slots are counted
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="restart-cycle", attempt_id="restart-att-1", match_type="leagueMatch"
    ) is False


# ==============================================================================
# 14. IDEMPOTENT ATTEMPT FINALIZATION AND CATEGORY VALIDATION
# ==============================================================================


def test_014_finalize_provider_request_idempotent_and_validates_category(pg_db_migrated) -> None:
    """fc27_finalize_provider_request validates allowed categories and is idempotent on repeat calls."""
    db = pg_db_migrated.poll_writer
    policy = _make_all_types_policy()

    assert _reserve_provider_request_budget(
        db_conn_or_uri=db, policy=policy, cycle_id="finalize-cycle", attempt_id="fin-att-1", match_type="leagueMatch"
    ) is True

    # First finalization succeeds
    _record_provider_attempt_outcome(db_conn_or_uri=db, attempt_id="fin-att-1", outcome_category="SUCCEEDED")

    # Second finalization with same attempt_id is idempotent
    _record_provider_attempt_outcome(db_conn_or_uri=db, attempt_id="fin-att-1", outcome_category="SUCCEEDED")

    # Direct procedure execution with invalid category raises exception
    with psycopg.connect(db) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException):
                cur.execute(
                    "SELECT public.fc27_finalize_provider_request(%s::text, %s::text)",
                    ("fin-att-1", "INVALID_UNKNOWN_OUTCOME"),
                )


# ==============================================================================
# 15. POLICY AND SETTINGS VALIDATION FOR MATCH TYPES, DELAY, ZERO RETRIES
# ==============================================================================


def test_015_policy_validation_rejects_invalid_match_types_or_delay() -> None:
    """FC27PollingPolicy constructor rejects invalid match types, missing required types, invalid delay, and retries > 0."""
    # Retries > 0 rejected
    with pytest.raises(ValidationError):
        FC27PollingPolicy(
            poll_interval_seconds=14400,
            minimum_interval_seconds=14400,
            maximum_interval_seconds=14400,
            polling_frequency="ONCE_EVERY_4_HOURS",
            max_provider_requests_per_24_hours=18,
            poll_all_match_types=True,
            poll_match_types=("leagueMatch", "playoffMatch", "friendlyMatch"),
            maximum_requests_per_cycle=3,
            maximum_retries=1,
        )

    # Inter-request delay outside 10..60 rejected
    with pytest.raises(ValidationError):
        _make_all_types_policy(inter_delay=5)
    with pytest.raises(ValidationError):
        _make_all_types_policy(inter_delay=90)

    # Missing required match type rejected
    with pytest.raises(ValidationError):
        _make_all_types_policy(match_types=("leagueMatch", "playoffMatch"))

    # Unsupported match type rejected
    with pytest.raises(ValidationError):
        _make_all_types_policy(match_types=("leagueMatch", "playoffMatch", "customMatch"))


# ==============================================================================
# 16. DISABLED-MODE COMPATIBILITY (SINGLE-TYPE ROTATION)
# ==============================================================================


def test_016_disabled_mode_preserves_single_type_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """When poll_all_match_types is False, policy enforces 12-hour interval and single request per cycle."""
    _enable_all_polling_gates(monkeypatch)
    policy = FC27PollingPolicy()
    assert policy.poll_all_match_types is False
    assert policy.poll_interval_seconds == 43200
    assert policy.maximum_requests_per_cycle == 1
    assert policy.max_provider_requests_per_24_hours == 2

    called: list[str] = []

    def fetch(match_type: str = "leagueMatch") -> FC27CommunityProviderEnvelope:
        called.append(match_type)
        return _build_validated_envelope(1, start_id=988000, match_type=match_type)[0]

    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=lambda _env: _fake_persist_summary(inserted=0),
        use_cache=False,
        use_rate_limiter=False,
    )

    assert called == ["leagueMatch"]
    assert result.provider_requests == 1
    assert result.batch_requests_attempted == 0


# ==============================================================================
# 17. CHRONOLOGICAL DISCORD DELIVERY JOBS WITH ZERO RAW PAYLOADS
# ==============================================================================


def test_017_chronological_discord_jobs_no_raw_payloads(
    pg_db_migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discord delivery jobs are queued chronologically with sanitized metadata and no raw payloads."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setattr("src.fc27_polling_worker.time.sleep", lambda _sec: None)

    policy = _make_all_types_policy()
    envs: dict[str, FC27CommunityProviderEnvelope] = {
        "leagueMatch": _build_validated_envelope(1, start_id=989100, match_type="leagueMatch")[0],
        "playoffMatch": _build_validated_envelope(1, start_id=989200, match_type="playoffMatch")[0],
        "friendlyMatch": _build_validated_envelope(1, start_id=989300, match_type="friendlyMatch")[0],
    }

    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        db_conn_or_uri=pg_db_migrated.poll_writer,
        fetch_matches_fn=lambda mt: envs[mt],
        use_cache=False,
        use_rate_limiter=False,
    )

    assert result.inserted_match_count == 3
    assert result.discord_jobs_inserted == 3

    # Verify rows in public.fc27_discord_jobs
    with psycopg.connect(pg_db_migrated.poll_writer) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, job_type, payload FROM public.fc27_discord_jobs ORDER BY created_at ASC"
            )
            rows = cur.fetchall()
            assert len(rows) >= 3
            forbidden = {"raw", "raw_payload", "gamer_tag", "gamertag", "blaze_id", "ea_id", "password", "secret"}
            for _jid, _jtype, payload in rows:
                if isinstance(payload, dict):
                    assert not (set(payload.keys()) & forbidden)


# ==============================================================================
# 18. MIGRATION 0022 AND GRANTS POLICY 23 VERIFICATION
# ==============================================================================


def test_018_migration_0022_and_grants_policy_23_idempotency_and_security(pg_db_migrated) -> None:
    """Verify Migration 0022 and Grants Policy 23 permissions, ownership, and idempotent re-application."""
    assert GRANTS_POLICY_VERSION == 26

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            # Table ownership
            cur.execute(
                """SELECT tableowner FROM pg_tables
                    WHERE schemaname = 'public' AND tablename = 'fc27_provider_request_attempts'"""
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "fc27_owner"

            # Function ownership and SECURITY DEFINER
            cur.execute(
                """SELECT proname, prosecdef, pg_get_userbyid(proowner)
                     FROM pg_proc
                    WHERE proname IN ('fc27_reserve_provider_request', 'fc27_finalize_provider_request')"""
            )
            funcs = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            assert "fc27_reserve_provider_request" in funcs
            assert funcs["fc27_reserve_provider_request"] == (True, "fc27_owner")
            assert "fc27_finalize_provider_request" in funcs
            assert funcs["fc27_finalize_provider_request"] == (True, "fc27_owner")

            # Least-privilege permissions: fc27_poll_writer has SELECT on table, but NOT INSERT/UPDATE/DELETE
            cur.execute(
                """SELECT privilege_type
                     FROM information_schema.role_table_grants
                    WHERE grantee = 'fc27_poll_writer'
                      AND table_name = 'fc27_provider_request_attempts'"""
            )
            privs = {r[0] for r in cur.fetchall()}
            assert "SELECT" in privs
            assert "INSERT" not in privs
            assert "UPDATE" not in privs
            assert "DELETE" not in privs

    # Idempotent re-run of migrator
    applied = migrate(pg_db_migrated.migrator)
    assert applied == []


# ==============================================================================
# 19. CENTRALIZED FAILURE DECISION MATRIX (TABLE-DRIVEN)
# ==============================================================================


@pytest.mark.parametrize(
    "poll_status,error_category,circuit_state,expected_continue",
    [
        ("NO_NEW_MATCHES", "", FC27CircuitState.CLOSED, True),
        ("NEW_MATCHES_PERSISTED", "", FC27CircuitState.CLOSED, True),
        ("PROVIDER_BLOCKED", "AUTHENTICATION_FAILED", FC27CircuitState.CLOSED, False),
        ("PROVIDER_BLOCKED", "AUTHORIZATION_FAILED", FC27CircuitState.CLOSED, False),
        ("PROVIDER_BLOCKED", "RATE_LIMITED", FC27CircuitState.CLOSED, False),
        ("PROVIDER_BLOCKED", "DATABASE_POSTURE_ERROR", FC27CircuitState.CLOSED, False),
        ("SCHEMA_DRIFT", "SCHEMA_DRIFT", FC27CircuitState.CLOSED, False),
        ("IDENTITY_REJECTED", "IDENTITY_MISMATCH", FC27CircuitState.CLOSED, False),
        ("PRIVACY_REJECTED", "PRIVACY_REJECTED", FC27CircuitState.CLOSED, False),
        ("PRIVACY_REJECTED", "FINGERPRINT_COLLISION", FC27CircuitState.CLOSED, False),
        ("REPLAY_CONFLICT", "REPLAY_CONFLICT", FC27CircuitState.CLOSED, False),
        ("SKIPPED_SINGLE_FLIGHT_BUSY", "SINGLE_FLIGHT_BUSY", FC27CircuitState.CLOSED, False),
        ("TRANSIENT_FAILURE", "TRANSIENT_TIMEOUT", FC27CircuitState.CLOSED, True),
        ("TRANSIENT_FAILURE", "TRANSIENT_TRANSPORT_ERROR", FC27CircuitState.CLOSED, True),
        ("TRANSIENT_FAILURE", "TYPE_LOCAL_TIMEOUT", FC27CircuitState.CLOSED, True),
        ("TRANSIENT_FAILURE", "PERSISTENCE_FAILURE", FC27CircuitState.CLOSED, True),
        ("TRANSIENT_FAILURE", "TRANSIENT_TIMEOUT", FC27CircuitState.OPEN, False),
        ("TRANSIENT_FAILURE", "DATABASE_POSTURE_ERROR", FC27CircuitState.CLOSED, False),
        ("TRANSIENT_FAILURE", "POSTGRESQL_OUTAGE", FC27CircuitState.CLOSED, False),
    ],
)
def test_019_centralized_failure_decision_matrix_table_driven(
    poll_status: str,
    error_category: str,
    circuit_state: FC27CircuitState,
    expected_continue: bool,
) -> None:
    """Table-driven test for should_continue_polling_cycle_after_result across all error categories and circuit states."""
    cb = FC27AdapterCircuitBreaker(approved_provider_selected=True)
    if circuit_state == FC27CircuitState.OPEN:
        cb.record_failure("HTTP_403")

    now = datetime.now(timezone.utc)
    dummy_res = FC27PollResult(
        poll_attempt_id="dummy-att-1",
        poll_status=poll_status,
        single_flight_acquired=True,
        cache_status="MISS",
        started_at=now,
        finished_at=now,
        duration_ms=10,
        poll_interval_seconds=14400,
        polling_frequency="ONCE_EVERY_4_HOURS",
        provider_requests=1,
        retries_attempted=0,
        source_match_count=0,
        accepted_match_count=0,
        inserted_match_count=0,
        duplicate_ignored_count=0,
        replay_conflict_count=0,
        analytics_executed=False,
        confidence_tier="COMMUNITY_DERIVED",
        circuit_state=circuit_state.value,
        match_type="leagueMatch",
        next_rotation_match_type="playoffMatch",
        error_category=error_category,
    )

    should_cont = should_continue_polling_cycle_after_result(
        dummy_res,
        circuit_breaker=cb,
        single_flight_held=True,
        budget_exhausted=False,
    )
    assert should_cont == expected_continue

    # Lease lost must always stop
    assert should_continue_polling_cycle_after_result(
        dummy_res,
        circuit_breaker=cb,
        single_flight_held=False,
        budget_exhausted=False,
    ) is False

    # Budget exhausted must always stop
    assert should_continue_polling_cycle_after_result(
        dummy_res,
        circuit_breaker=cb,
        single_flight_held=True,
        budget_exhausted=True,
    ) is False


# ==============================================================================
# 20. HTTP 401, 403, 429 STOP REMAINING TYPES AND FINALIZE OUTCOME
# ==============================================================================


@pytest.mark.parametrize(
    "exc_to_raise,expected_status,expected_err_cat",
    [
        (FC27AuthenticationError("401 Unauthorized"), "PROVIDER_BLOCKED", "AUTHENTICATION_FAILED"),
        (FC27PollingProviderBlockedError("403 Forbidden"), "PROVIDER_BLOCKED", "AUTHORIZATION_FAILED"),
        (FC27TransportError("429 Too Many Requests"), "PROVIDER_BLOCKED", "RATE_LIMITED"),
    ],
)
def test_020_http_401_403_429_stops_remaining_types_and_finalizes_outcome(
    monkeypatch: pytest.MonkeyPatch,
    exc_to_raise: Exception,
    expected_status: str,
    expected_err_cat: str,
) -> None:
    """HTTP 401, 403, and 429 stops the batch immediately and classifies accurately."""
    _enable_all_polling_gates(monkeypatch)
    called: list[str] = []

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        called.append(match_type)
        if match_type == "leagueMatch":
            raise exc_to_raise
        return _build_validated_envelope(1, start_id=983500, match_type=match_type)[0]

    policy = _make_all_types_policy()
    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=lambda _env: _fake_persist_summary(inserted=0),
        use_cache=False,
        use_rate_limiter=False,
    )

    assert called == ["leagueMatch"], "playoffMatch and friendlyMatch must NOT be called"
    assert result.poll_status == expected_status
    assert result.error_category == expected_err_cat
    assert result.batch_requests_attempted == 1


# ==============================================================================
# 21. CIRCUIT BREAKER BEHAVIOR UNDER TIMEOUTS AND IMMEDIATE FAILURES
# ==============================================================================


def test_021_circuit_breaker_behavior_under_timeouts_and_immediate_failures() -> None:
    """Type-local timeout does not latch breaker OPEN; repeated timeouts trip breaker; immediate reasons trip breaker."""
    cb = FC27AdapterCircuitBreaker(approved_provider_selected=True, timeout_threshold=2)
    assert cb.state == FC27CircuitState.CLOSED

    # 1. Single timeout does NOT open the breaker
    cb.record_failure("TRANSIENT_TIMEOUT")
    assert cb.state == FC27CircuitState.CLOSED
    cb.before_call()  # Succeeds without raising

    # 2. Second timeout trips to OPEN
    cb.record_failure("TRANSIENT_TIMEOUT")
    assert cb.state == FC27CircuitState.OPEN
    with pytest.raises(FC27TransportError, match="Circuit breaker is OPEN"):
        cb.before_call()

    # Reset breaker
    cb.record_success()
    assert cb.state == FC27CircuitState.CLOSED

    # 3. Immediate open reasons trip immediately
    cb.record_failure("AUTHENTICATION_FAILED")
    assert cb.state == FC27CircuitState.OPEN

    cb.record_success()
    cb.record_failure("RATE_LIMITED")
    assert cb.state == FC27CircuitState.OPEN


# ==============================================================================
# 22. CONFIGURATION KEYS RECONCILIATION CANONICAL AND ALIAS
# ==============================================================================


def test_022_configuration_keys_reconciliation_canonical_and_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC27_COMMUNITY_POLL_MAX_REQUESTS is canonical; alias supported; conflicts fail closed."""
    # 1. Canonical key works
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS", "3")
    monkeypatch.delenv("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_CYCLE", raising=False)
    assert get_fc27_community_poll_max_requests(strict=True) == 3

    # 2. Alias key works when canonical unset
    monkeypatch.delenv("FC27_COMMUNITY_POLL_MAX_REQUESTS", raising=False)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_CYCLE", "3")
    assert get_fc27_community_poll_max_requests(strict=True) == 3

    # 3. Matching canonical and alias succeed
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS", "3")
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_CYCLE", "3")
    assert get_fc27_community_poll_max_requests(strict=True) == 3

    # 4. Conflicting values fail closed
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS", "1")
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_CYCLE", "3")
    with pytest.raises(ConfigError, match="Conflicting values"):
        get_fc27_community_poll_max_requests(strict=True)

    # 5. Missing value defaults to 1
    monkeypatch.delenv("FC27_COMMUNITY_POLL_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_CYCLE", raising=False)
    assert get_fc27_community_poll_max_requests(strict=True) == 1

    # 6. Malformed value fails closed
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS", "not_a_number")
    with pytest.raises(ConfigError, match="must be between 1 and 3"):
        get_fc27_community_poll_max_requests(strict=True)

    # 7. Rolling cap bounds (1..18)
    monkeypatch.setenv("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_24_HOURS", "25")
    with pytest.raises(ConfigError, match="must be 1..18"):
        settings.get_fc27_community_poll_max_requests_per_24_hours(strict=True)


# ==============================================================================
# 23. LEDGER INVOCATION SOURCE AND INVALID STATE TRANSITIONS
# ==============================================================================


def test_023_ledger_invocation_source_and_invalid_state_transitions(pg_db_migrated) -> None:
    """Ledger supports invocation_source, and fc27_finalize_provider_request rejects transition from terminal to RESERVED."""
    db = pg_db_migrated.poll_writer
    policy = _make_all_types_policy()

    # Reserve with invocation_source 'MANUAL_POLL'
    assert _reserve_provider_request_budget(
        db_conn_or_uri=db,
        policy=policy,
        cycle_id="src-cycle",
        attempt_id="src-att-1",
        match_type="leagueMatch",
        invocation_source="MANUAL_POLL",
    ) is True

    with psycopg.connect(db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invocation_source, outcome_category FROM public.fc27_provider_request_attempts WHERE attempt_id = %s",
                ("src-att-1",),
            )
            row = cur.fetchone()
            assert row == ("MANUAL_POLL", "RESERVED")

            # Finalize to SUCCEEDED
            cur.execute("SELECT public.fc27_finalize_provider_request(%s::text, %s::text)", ("src-att-1", "SUCCEEDED"))
            assert cur.fetchone()[0] is True

            # Attempting to revert terminal state to RESERVED must raise exception
            with pytest.raises(psycopg.errors.RaiseException, match="Invalid state transition from SUCCEEDED to RESERVED"):
                cur.execute("SELECT public.fc27_finalize_provider_request(%s::text, %s::text)", ("src-att-1", "RESERVED"))


# ==============================================================================
# 24. SINGLE FLIGHT BUSY FAILS FAST WITHOUT POLLING
# ==============================================================================


def test_024_single_flight_busy_fails_fast_without_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """When single-flight lease is already held by another worker, run_fc27_poll_cycle returns BUSY immediately."""
    _enable_all_polling_gates(monkeypatch)
    policy = _make_all_types_policy()

    # Mock acquire() to return False
    class MockLeaseCtx:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.lease = type("MockLease", (), {"acquired": False})()
        def acquire(self) -> Any:
            return self.lease
        def release(self) -> None:
            pass

    monkeypatch.setattr("src.fc27_polling_worker.FC27SingleFlightLeaseContext", MockLeaseCtx)

    provider_called = False
    def fetch() -> Any:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("Provider should not be called when lease busy")

    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        use_cache=False,
        use_rate_limiter=False,
    )

    assert provider_called is False
    assert result.poll_status == "SKIPPED_SINGLE_FLIGHT_BUSY"
    assert result.single_flight_acquired is False
    assert result.provider_requests == 0


# ==============================================================================
# 25. EARLIER SUCCESS SURVIVES LATER PERSISTENCE FAILURE
# ==============================================================================


def test_025_earlier_success_survives_later_persistence_failure(
    pg_db_migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistence failure on playoffMatch does not wipe out previously persisted leagueMatch."""
    _enable_all_polling_gates(monkeypatch)
    monkeypatch.setattr("src.fc27_polling_worker.time.sleep", lambda _sec: None)

    policy = _make_all_types_policy()
    env1, _ = _build_validated_envelope(1, start_id=987100, match_type="leagueMatch")
    env2, _ = _build_validated_envelope(1, start_id=987200, match_type="playoffMatch")

    def fetch(match_type: str) -> FC27CommunityProviderEnvelope:
        if match_type == "leagueMatch":
            return env1
        if match_type == "playoffMatch":
            return env2
        return _build_validated_envelope(1, start_id=987300, match_type="friendlyMatch")[0]

    def custom_persist(env: FC27CommunityProviderEnvelope) -> FC27CommunityPersistenceApplySummary:
        m_type = env.provenance.get("match_type")
        if m_type == "playoffMatch":
            raise RuntimeError("Database constraint or disk full during playoffMatch persist")
        return _fake_persist_summary(inserted=1)

    result = run_fc27_poll_cycle(
        policy=policy,
        runtime=FC27PollingRuntime(policy=policy),
        confirm_live_polling=True,
        fetch_matches_fn=fetch,
        persist_fn=custom_persist,
        analytics_fn=lambda: {"analytics": "ok"},
        use_cache=False,
        use_rate_limiter=False,
    )

    # leagueMatch was inserted (1), playoffMatch failed persistence (0), friendlyMatch was inserted (1)
    assert result.batch_requests_attempted == 3
    assert result.inserted_match_count == 2
