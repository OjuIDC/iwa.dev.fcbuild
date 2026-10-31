"""Comprehensive test suite for the East Coasters FC 27 Loopback Ingestion and Analytics Demo (`PART M`).

Covers all 80 required verification cases:
- Demo defaults, loopback bind, ephemeral port, and non-loopback/wildcard rejection (Tests 1-6)
- East Coasters identity validation and scope mismatch rejection (Tests 7-10)
- Operations and endpoint/path/operation allowlists (Tests 11-17)
- JSON parsing, schema drift, sensitive field rejection, size/timeout/redirect/HTML/401/403 (Tests 18-30)
- Pseudonymization, privacy invariants, and zero raw response retention/caching (Tests 31-36)
- Normalized cache, rate limiter, request budget, single-request/zero-retry bounds, and circuit breaker (Tests 37-42)
- Duplicate match idempotency and changed replay conflict rejection (Tests 43-44)
- Search, Scout, Analyst execution, populated SHA-256 hashes, and determinism (Tests 45-52)
- Read-only Demo API (`/demo/status`, `/demo/matches`, `/demo/analysis`, `/demo/ingest-fixture`) and HTML view (Tests 53-59, 80)
- Optional PostgreSQL persistence gate, transactional idempotency, conflict rollback, and table isolation (Tests 60-68)
- Feature-gate isolation, zero external network/EA DNS, CLI-only live probe boundary, and metadata-only logs (Tests 69-79)
"""
from __future__ import annotations

import json
import re
import socket
from datetime import datetime, timezone
from typing import Any, List, Tuple

import psycopg
import pytest
from fastapi.testclient import TestClient

from scripts.fc27_demo_fixture_server import (
    EphemeralFixtureServerContext,
    FC27DemoFixtureHTTPServer,
    build_synthetic_matches_payload,
    validate_loopback_bind_host,
)
from scripts.fc27_demo_ingest import main as demo_cli_main
from src import settings
from src.fc27_api_cache import FC27BoundedNormalizedCache, build_fc27_cache_key
from src.fc27_api_rate_limit import (
    FC27AdapterCircuitBreaker,
    FC27CircuitState,
    FC27ProviderTokenBucketLimiter,
    FC27RequestBudget,
    FC27RequestBudgetExceededError,
)
from src.fc27_clubs_api_contracts import (
    FC27BoundEndpointRequest,
    FC27ClubsEndpointOperation,
    FC27ContractError,
    FC27SchemaValidationError,
    build_east_coasters_endpoint_request,
)
from src.fc27_demo import (
    DEMO_AUTH_VERSION,
    DEMO_DEFAULT_GENERATED_AT,
    DEMO_FIXTURE_LOOPBACK,
    DEMO_PROVIDER_ID,
    LIVE_PROBE_EXECUTED,
    FC27DemoDisabledError,
    FC27DemoLoopbackTransport,
    FC27DemoLoopbackTransportPolicy,
    FC27DemoStateStore,
    FC27LiveProbeConfig,
    FC27PersistenceGateError,
    _build_export_envelope_for_persistence,
    run_fc27_demo_ingestion,
    run_fc27_demo_ingestion_with_artifacts,
    validate_demo_host_strict,
    validate_demo_persistence_gate,
)
from src.fc27_demo_api import create_demo_app
from src.fc27_provider import (
    FC27AuthenticationError,
    FC27InvalidHostError,
    FC27LiveAccessBlockedError,
    FC27PayloadTooLargeError,
    FC27RateLimitError,
    FC27TransportError,
)
from src.fc27_repository import FC27ReplayConflictError
from src.fc27_user_export import FC27SensitiveFieldDetectedError

_ORIG_GETADDRINFO = socket.getaddrinfo
_ORIG_CREATE_CONNECTION = socket.create_connection


@pytest.fixture(autouse=True)
def _loopback_only_network_guard(monkeypatch: pytest.MonkeyPatch):
    """Blocks all non-loopback network connections and all EA hostname DNS resolutions."""
    ea_dns_calls: List[str] = []
    non_loopback_connects: List[str] = []

    def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any):
        host_str = str(host or "").strip().lower()
        if "ea.com" in host_str or "proclubs" in host_str:
            ea_dns_calls.append(host_str)
            raise AssertionError(f"Prohibited EA DNS resolution attempted for '{host_str}'")
        if host_str not in ("127.0.0.1", "localhost", "::1", ""):
            non_loopback_connects.append(host_str)
            raise AssertionError(f"Non-loopback DNS resolution attempted for '{host_str}'")
        return _ORIG_GETADDRINFO(host, port, *args, **kwargs)

    def _guarded_create_connection(address: Tuple[str, int], *args: Any, **kwargs: Any):
        host_str = str(address[0] or "").strip().lower()
        if host_str != "127.0.0.1":
            non_loopback_connects.append(host_str)
            raise AssertionError(f"Non-loopback socket connection attempted to '{host_str}'")
        return _ORIG_CREATE_CONNECTION(address, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    yield {"ea_dns_calls": ea_dns_calls, "non_loopback_connects": non_loopback_connects}
    assert ea_dns_calls == []
    assert non_loopback_connects == []


# ==============================================================================
# PART M: Tests 1 - 17 (Configuration, Bind, Scope, Operations, Allowlists)
# ==============================================================================


def test_01_demo_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """1. Demo disabled by default (`FC27_DEMO_MODE=false`)."""
    monkeypatch.delenv("FC27_DEMO_MODE", raising=False)
    assert settings.is_fc27_demo_mode_enabled() is False
    monkeypatch.setenv("FC27_DEMO_MODE", "malformed_boolean")
    assert settings.is_fc27_demo_mode_enabled() is False

    with pytest.raises(FC27DemoDisabledError):
        run_fc27_demo_ingestion(demo_mode_enabled=None)


def test_02_fixture_source_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """2. Fixture source default (`FC27_DEMO_SOURCE=fixture`)."""
    monkeypatch.delenv("FC27_DEMO_SOURCE", raising=False)
    assert settings.get_fc27_demo_source() == "fixture"
    monkeypatch.setenv("FC27_DEMO_SOURCE", "malformed_source")
    assert settings.get_fc27_demo_source() == "fixture"


def test_03_loopback_only_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """3. Loopback-only bind (`127.0.0.1`)."""
    monkeypatch.delenv("FC27_DEMO_BIND_HOST", raising=False)
    assert settings.get_fc27_demo_bind_host() == "127.0.0.1"
    assert validate_loopback_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_demo_host_strict("127.0.0.1") == "127.0.0.1"


def test_04_ephemeral_port() -> None:
    """4. Ephemeral port allocation (`port=0` binds to an OS-assigned loopback port > 0)."""
    with EphemeralFixtureServerContext(bind_host="127.0.0.1", port=0, scenario="valid") as srv:
        assert srv.bound_host == "127.0.0.1"
        assert srv.bound_port > 0


def test_05_non_loopback_host_rejected() -> None:
    """5. Non-loopback host rejected (LAN, external domain, IPv6, localhost)."""
    for bad_host in ("192.168.1.10", "10.0.0.1", "proclubs.ea.com", "::1", "[::1]", "localhost"):
        with pytest.raises((ValueError, FC27InvalidHostError)):
            validate_demo_host_strict(bad_host)
        with pytest.raises(ValueError):
            FC27DemoFixtureHTTPServer(bind_host=bad_host, port=0)


def test_06_wildcard_host_rejected() -> None:
    """6. Wildcard host rejected (`0.0.0.0`, `::`, `*`)."""
    for wildcard in ("0.0.0.0", "::", "[::]", "*"):
        with pytest.raises(FC27InvalidHostError):
            validate_demo_host_strict(wildcard)
        with pytest.raises(ValueError):
            FC27DemoFixtureHTTPServer(bind_host=wildcard, port=0)
        with pytest.raises(FC27InvalidHostError):
            create_demo_app(bind_host=wildcard, demo_mode_override=True)


def test_07_valid_club_identity() -> None:
    """7. Valid club identity (`330323`, `East Coasters`, `common-gen5`, `FC27`)."""
    result = run_fc27_demo_ingestion(
        source="fixture",
        operation="club-identity",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert result.validation_status == "PASS"
    assert result.identity["club_id"] == "330323"
    assert result.identity["club_name"] == "East Coasters"
    assert result.identity["platform"] == "common-gen5"
    assert result.identity["game_version"] == "FC27"


def test_08_wrong_club_rejected() -> None:
    """8. Wrong club rejected both at request construction and response normalization."""
    with pytest.raises(FC27ContractError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="club-identity",
            club_id="999999",
            demo_mode_enabled=True,
        )
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27SchemaValidationError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="club-identity",
            scenario="wrong_club",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN


def test_09_wrong_platform_rejected() -> None:
    """9. Wrong platform rejected both at request construction and response normalization."""
    with pytest.raises(FC27ContractError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="club-identity",
            platform="ps4",
            demo_mode_enabled=True,
        )
    with pytest.raises(FC27SchemaValidationError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="club-identity",
            scenario="wrong_platform",
            demo_mode_enabled=True,
        )


def test_10_wrong_game_version_rejected() -> None:
    """10. Wrong game version rejected."""
    with pytest.raises(FC27ContractError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="club-identity",
            game_version="FC26",
            demo_mode_enabled=True,
        )


def test_11_full_fixture_ingestion() -> None:
    """11. Full fixture ingestion runs end-to-end across all 25 pipeline steps."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert artifacts.result.validation_status == "PASS"
    assert artifacts.result.accepted_count == 6
    assert len(artifacts.matches) == 6
    assert artifacts.result.normalized_counts["matches"] == 6
    assert artifacts.result.normalized_counts["player_performances"] == 24


def test_12_club_identity_operation() -> None:
    """12. Club identity operation (`--operation club-identity`)."""
    res = run_fc27_demo_ingestion(
        source="fixture",
        operation="club-identity",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert res.request_count == 1
    assert res.normalized_counts["club_identity_records"] == 1
    assert res.normalized_counts["matches"] == 0


def test_13_recent_matches_operation() -> None:
    """13. Recent matches operation (`--operation recent-matches`)."""
    res = run_fc27_demo_ingestion(
        source="fixture",
        operation="recent-matches",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert res.request_count == 1
    assert res.normalized_counts["matches"] == 6
    assert res.normalized_counts["player_performances"] == 0


def test_14_full_demo_operation() -> None:
    """14. Full-demo operation (`--operation full-demo`) populates matches and player stats in 1 request."""
    res = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert res.request_count == 1
    assert res.normalized_counts["club_identity_records"] == 1
    assert res.normalized_counts["matches"] == 6
    assert res.normalized_counts["player_performances"] == 24


def test_15_strict_endpoint_allowlist() -> None:
    """15. Strict endpoint allowlist rejects unapproved routes on the fixture server."""
    with EphemeralFixtureServerContext(scenario="valid") as srv:
        policy = FC27DemoLoopbackTransportPolicy(port=srv.bound_port)
        breaker = FC27AdapterCircuitBreaker(
            approved_provider_selected=True, is_ea_provider=False, enabled=True
        )
        limiter = FC27ProviderTokenBucketLimiter(enabled=True, capacity=2, refill_tokens=2)
        budget = FC27RequestBudget(
            provider_id=DEMO_PROVIDER_ID,
            operation="CLUB_IDENTITY",
            authorization_reference=DEMO_AUTH_VERSION,
        )
        transport = FC27DemoLoopbackTransport(
            policy=policy,
            circuit_breaker=breaker,
            rate_limiter=limiter,
            request_budget=budget,
        )
        bound_req = build_east_coasters_endpoint_request(FC27ClubsEndpointOperation.CLUB_IDENTITY)
        with pytest.raises(FC27ContractError):
            transport.execute(
                bound_req,
                demo_mode_enabled=True,
                override_path="/api/fc/clubs/unapproved",
            )


def test_16_strict_path_allowlist() -> None:
    """16. Strict path allowlist rejects arbitrary path in `FC27DemoLoopbackTransportPolicy`."""
    with pytest.raises(FC27ContractError):
        FC27DemoLoopbackTransportPolicy(
            port=8080,
            allowed_paths=("/api/fc/clubs/info", "/arbitrary/proxy/path"),
        )


def test_17_strict_operation_allowlist() -> None:
    """17. Strict operation allowlist rejects unknown operation names."""
    with pytest.raises(FC27ContractError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="arbitrary-operation",
            demo_mode_enabled=True,
        )


# ==============================================================================
# PART M: Tests 18 - 30 (JSON, Drift, Sensitive Fields, Transport Resilience)
# ==============================================================================


def test_18_valid_json() -> None:
    """18. Valid JSON is parsed strictly and produces a SHA-256 payload digest."""
    res = run_fc27_demo_ingestion(
        source="fixture",
        operation="club-statistics",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert res.validation_status == "PASS"
    assert res.normalized_counts["club_statistics_records"] == 1


def test_19_malformed_json_rejected() -> None:
    """19. Malformed JSON rejected and opens circuit breaker."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27SchemaValidationError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="malformed_json",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN


def test_20_duplicate_json_key_rejected() -> None:
    """20. Duplicate JSON key rejected (`duplicate_json_keys` scenario)."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27SchemaValidationError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="duplicate_json_keys",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN


def test_21_schema_drift_rejected() -> None:
    """21. Schema drift rejected (`schema_drift` scenario)."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27SchemaValidationError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="schema_drift",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN


def test_22_sensitive_field_rejected() -> None:
    """22. Sensitive field rejected (`sensitive_field` scenario containing `personaId` / `gamerTag`)."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27SensitiveFieldDetectedError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="sensitive_field",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN
    assert breaker.open_reason == "SENSITIVE_FIELD_VIOLATION"


def test_23_oversized_response_rejected() -> None:
    """23. Oversized response rejected (`oversized_response` scenario)."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27PayloadTooLargeError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="oversized_response",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN


def test_24_timeout_bounded() -> None:
    """24. Timeout bounded (`timeout` scenario fails within bounded socket timeout)."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True,
        is_ea_provider=False,
        timeout_threshold=1,
        enabled=True,
    )
    with pytest.raises(FC27TransportError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="timeout",
            timeout_seconds=0.1,
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN


def test_25_redirect_rejected() -> None:
    """25. Redirect rejected (`redirect` scenario HTTP 302 is never followed and opens circuit)."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27TransportError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="redirect",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN
    assert breaker.open_reason == "REDIRECT_REJECTED"


def test_26_html_challenge_rejected() -> None:
    """26. HTML challenge rejected (`html_challenge` scenario opens circuit)."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27TransportError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="html_challenge",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN
    assert breaker.open_reason == "HTML_CHALLENGE"


def test_27_http_401_opens_circuit() -> None:
    """27. HTTP 401 opens circuit breaker immediately."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27AuthenticationError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="http_401",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN
    assert breaker.open_reason == "HTTP_401"


def test_28_http_403_opens_circuit() -> None:
    """28. HTTP 403 opens circuit breaker immediately."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27AuthenticationError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="http_403",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN
    assert breaker.open_reason == "HTTP_403"


def test_29_unknown_match_type_handled_safely() -> None:
    """29. Unknown match type is normalized to `UNKNOWN` and handled safely by Search."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="unknown_match_type",
        demo_mode_enabled=True,
    )
    assert artifacts.matches[0].match_type == "UNKNOWN"
    assert artifacts.search_context.baseline_matches[0].match_type == "UNKNOWN"
    assert artifacts.result.validation_status == "PASS"


def test_30_missing_optional_data_remains_unavailable() -> None:
    """30. Missing optional metrics remain `None` (never fabricated as `0`)."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="missing_optional_metrics",
        demo_mode_enabled=True,
    )
    base_agg = artifacts.search_context.baseline_team_aggregates
    assert base_agg.possession is None
    assert base_agg.shots is None
    assert base_agg.passes_attempted is None
    assert base_agg.tackles_attempted is None


# ==============================================================================
# PART M: Tests 31 - 44 (Privacy, Cache, Rate Limit, Budget, Circuit, Idempotency)
# ==============================================================================


def test_31_player_pseudonymization() -> None:
    """31. Player pseudonymization converts `fixture_player_*` into `plr_ecf_<12-hex>`."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    pattern = re.compile(r"^plr_ecf_[0-9a-f]{12}$")
    for m in artifacts.matches:
        assert len(m.pseudonymous_player_refs) == 4
        for p_ref in m.pseudonymous_player_refs:
            assert pattern.match(p_ref), f"Unexpected player ref format: {p_ref}"


def test_32_gamer_tag_absent() -> None:
    """32. Gamer tag absent across all normalized artifacts and serialized outputs."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    dumped = json.dumps(artifacts.model_dump(mode="json")).lower()
    assert "gamertag" not in dumped
    assert "gamer_tag" not in dumped
    assert "blazeid" not in dumped


def test_33_display_alias_absent() -> None:
    """33. Display alias absent across all normalized artifacts."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    dumped = json.dumps(artifacts.model_dump(mode="json"))
    assert "display_alias" not in dumped
    assert "proName" not in dumped


def test_34_external_player_id_absent() -> None:
    """34. Pre-pseudonymization `fixture_player_*` and external player IDs are absent."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    dumped = json.dumps(artifacts.model_dump(mode="json"))
    assert "fixture_player_" not in dumped
    assert "personaId" not in dumped
    assert "nucleusId" not in dumped
    assert "eaAccountId" not in dumped


def test_35_raw_response_not_retained() -> None:
    """35. Raw response bytes are zeroed and never retained on `FC27DemoResult`."""
    res = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    res_dict = res.model_dump(mode="json")
    assert "raw_response" not in res_dict
    assert "raw_body" not in res_dict
    assert "parsed_data" not in res_dict

    with pytest.raises(FC27ContractError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            raw_response_retention=True,
            demo_mode_enabled=True,
        )


def test_36_raw_response_not_cached() -> None:
    """36. Bounded normalized cache rejects raw bytes/strings and stores only normalized models."""
    cache = FC27BoundedNormalizedCache(enabled=True)
    _, spec = build_fc27_cache_key(
        provider_id=DEMO_PROVIDER_ID,
        provider_authorization_version=DEMO_AUTH_VERSION,
        operation="MATCH_PLAYER_STATISTICS",
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        parameters={},
        response_schema_version="1.0.0",
        normalization_version="1.0.0",
    )
    with pytest.raises(Exception):
        cache.put(spec, b'{"raw": "bytes"}')


def test_37_cache_miss_then_hit() -> None:
    """37. Cache miss on first execution (`request_count=1`) then hit on second (`request_count=0`)."""
    shared_cache = FC27BoundedNormalizedCache(enabled=True)
    first = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        cache=shared_cache,
        demo_mode_enabled=True,
    )
    assert first.cache_misses == 1
    assert first.cache_hits == 0
    assert first.request_count == 1

    second = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        cache=shared_cache,
        demo_mode_enabled=True,
    )
    assert second.cache_hits == 1
    assert second.cache_misses == 0
    assert second.request_count == 0
    assert second.execution_hash == first.execution_hash


def test_38_rate_limiter_enforced() -> None:
    """38. Token-bucket rate limiter blocks when bucket capacity is exhausted."""
    limiter = FC27ProviderTokenBucketLimiter(
        enabled=True,
        capacity=1,
        refill_tokens=1,
        refill_interval_seconds=3600.0,
    )
    first = run_fc27_demo_ingestion(
        source="fixture",
        operation="club-identity",
        scenario="valid",
        rate_limiter=limiter,
        demo_mode_enabled=True,
    )
    assert first.request_count == 1

    with pytest.raises(FC27RateLimitError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="recent-matches",
            scenario="valid",
            rate_limiter=limiter,
            demo_mode_enabled=True,
        )


def test_39_request_budget_enforced() -> None:
    """39. Per-run request budget blocks second transport call when `max_attempts=1`."""
    with EphemeralFixtureServerContext(scenario="valid") as srv:
        policy = FC27DemoLoopbackTransportPolicy(port=srv.bound_port, max_requests=1)
        breaker = FC27AdapterCircuitBreaker(
            approved_provider_selected=True, is_ea_provider=False, enabled=True
        )
        limiter = FC27ProviderTokenBucketLimiter(enabled=True, capacity=4, refill_tokens=4)
        budget = FC27RequestBudget(
            provider_id=DEMO_PROVIDER_ID,
            operation="CLUB_IDENTITY",
            authorization_reference=DEMO_AUTH_VERSION,
            max_attempts=1,
            max_retries=0,
        )
        transport = FC27DemoLoopbackTransport(
            policy=policy,
            circuit_breaker=breaker,
            rate_limiter=limiter,
            request_budget=budget,
        )
        req = build_east_coasters_endpoint_request(FC27ClubsEndpointOperation.CLUB_IDENTITY)
        resp = transport.execute(req, demo_mode_enabled=True)
        assert resp.http_status == 200

        with pytest.raises(FC27RequestBudgetExceededError):
            transport.execute(req, demo_mode_enabled=True)


def test_40_maximum_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """40. Default `FC27_DEMO_MAX_REQUESTS` is 1 and `full-demo` uses at most 1 HTTP request."""
    monkeypatch.delenv("FC27_DEMO_MAX_REQUESTS", raising=False)
    assert settings.get_fc27_demo_max_requests() == 1
    res = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        max_requests=1,
        demo_mode_enabled=True,
    )
    assert res.request_count == 1


def test_41_zero_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """41. Default `FC27_DEMO_MAX_RETRIES` is 0 and non-zero `max_retries` is rejected."""
    monkeypatch.delenv("FC27_DEMO_MAX_RETRIES", raising=False)
    assert settings.get_fc27_demo_max_retries() == 0
    with pytest.raises(FC27ContractError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            max_retries=1,
            demo_mode_enabled=True,
        )


def test_42_circuit_breaker_blocks_subsequent_request() -> None:
    """42. Circuit breaker opened by a failed call blocks subsequent requests before transport."""
    breaker = FC27AdapterCircuitBreaker(
        approved_provider_selected=True, is_ea_provider=False, enabled=True
    )
    with pytest.raises(FC27AuthenticationError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="http_401",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )
    assert breaker.state == FC27CircuitState.OPEN

    with pytest.raises(FC27TransportError, match="Circuit breaker is OPEN"):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="valid",
            circuit_breaker=breaker,
            demo_mode_enabled=True,
        )


def test_43_duplicate_match_idempotent() -> None:
    """43. Duplicate match in payload (`duplicates` scenario) is deduplicated idempotently."""
    res = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="duplicates",
        demo_mode_enabled=True,
    )
    assert res.validation_status == "PASS"
    assert res.accepted_count == 6
    assert res.duplicate_count == 1
    assert res.conflict_count == 0


def test_44_changed_replay_conflict_rejected() -> None:
    """44. Changed replay conflict (`changed_replay_conflict` scenario) is rejected fail-closed."""
    with pytest.raises(FC27ReplayConflictError):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            scenario="changed_replay_conflict",
            demo_mode_enabled=True,
        )


# ==============================================================================
# PART M: Tests 45 - 52 (Search, Scout, Analyst, Hashes, Determinism)
# ==============================================================================


def test_45_search_executes() -> None:
    """45. Search context assembly executes and populates team aggregates."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert artifacts.search_context.eligible_match_count == 6
    assert artifacts.search_context.baseline_team_aggregates.matches_played == 6
    assert artifacts.search_context.baseline_team_aggregates.wins == 4
    assert artifacts.search_context.baseline_team_aggregates.draws == 1
    assert artifacts.search_context.baseline_team_aggregates.losses == 1


def test_46_scout_executes() -> None:
    """46. Scout report synthesis executes and populates formation, role, and player evidence."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert artifacts.scout_report.eligible_match_count == 6
    assert len(artifacts.scout_report.formation_evidence) == 2
    assert len(artifacts.scout_report.player_evidence) == 4


def test_47_analyst_executes() -> None:
    """47. Analyst evidence synthesis executes and produces observational findings & confounders."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    assert artifacts.analyst_report.overall_confidence == "LOW_CONFIDENCE"
    assert len(artifacts.analyst_report.findings) > 0
    assert len(artifacts.analyst_report.confounders) > 0
    assert len(artifacts.analyst_report.data_gaps) > 0
    for finding in artifacts.analyst_report.findings:
        assert finding.recommendation is False
        assert finding.approvable is False
        assert finding.dispatchable is False
        assert finding.causal_claim is False


def test_48_search_hash_populated() -> None:
    """48. Search `context_hash` is a valid 64-char lowercase hex SHA-256."""
    res = run_fc27_demo_ingestion(
        source="fixture", operation="full-demo", scenario="valid", demo_mode_enabled=True
    )
    assert re.match(r"^[0-9a-f]{64}$", res.search_context_hash)


def test_49_scout_hash_populated() -> None:
    """49. Scout `report_hash` is a valid 64-char lowercase hex SHA-256."""
    res = run_fc27_demo_ingestion(
        source="fixture", operation="full-demo", scenario="valid", demo_mode_enabled=True
    )
    assert re.match(r"^[0-9a-f]{64}$", res.scout_report_hash)


def test_50_analyst_hash_populated() -> None:
    """50. Analyst `report_hash` is a valid 64-char lowercase hex SHA-256."""
    res = run_fc27_demo_ingestion(
        source="fixture", operation="full-demo", scenario="valid", demo_mode_enabled=True
    )
    assert re.match(r"^[0-9a-f]{64}$", res.analyst_report_hash)


def test_51_execution_hash_deterministic() -> None:
    """51. `execution_hash` is a valid 64-char lowercase hex SHA-256."""
    res = run_fc27_demo_ingestion(
        source="fixture", operation="full-demo", scenario="valid", demo_mode_enabled=True
    )
    assert re.match(r"^[0-9a-f]{64}$", res.execution_hash)


def test_52_identical_fixture_run_deterministic() -> None:
    """52. Two independent fixture runs produce identical Search, Scout, Analyst, and Execution hashes."""
    run_a = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        analysis_generated_at=DEMO_DEFAULT_GENERATED_AT,
        demo_mode_enabled=True,
    )
    run_b = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        analysis_generated_at=DEMO_DEFAULT_GENERATED_AT,
        demo_mode_enabled=True,
    )
    assert run_a.search_context_hash == run_b.search_context_hash
    assert run_a.scout_report_hash == run_b.scout_report_hash
    assert run_a.analyst_report_hash == run_b.analyst_report_hash
    assert run_a.execution_hash == run_b.execution_hash


# ==============================================================================
# PART M: Tests 53 - 59 & 80 (Demo API Endpoints & Minimal HTML Page)
# ==============================================================================


def test_53_demo_status_endpoint() -> None:
    """53. `GET /demo/status` returns 403 when disabled and full metadata when enabled."""
    disabled_app = create_demo_app(demo_mode_override=False)
    with TestClient(disabled_app) as client_off:
        assert client_off.get("/demo/status").status_code == 403

    store = FC27DemoStateStore()
    enabled_app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(enabled_app) as client_on:
        resp = client_on.get("/demo/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["demo_enabled"] is True
        assert body["source_mode"] == DEMO_FIXTURE_LOOPBACK
        assert body["club_identity"]["club_id"] == "330323"
        assert body["validation_status"] == "PASS"
        assert body["recommendations_enabled"] is False
        assert body["dispatch_enabled"] is False
        assert body["raw_response_retained"] is False
        assert re.match(r"^[0-9a-f]{64}$", body["execution_hash"])


def test_54_demo_matches_endpoint() -> None:
    """54. `GET /demo/matches` returns sanitized normalized match summaries with pseudonymous refs."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        resp = client.get("/demo/matches")
        assert resp.status_code == 200
        body = resp.json()
        assert body["match_count"] == 6
        assert body["raw_provider_ids_exposed"] is False
        assert body["player_aliases_exposed"] is False
        first_match = body["matches"][0]
        assert re.match(r"^mref_ecf_[0-9a-f]{12}$", first_match["pseudonymous_match_ref"])
        for pref in first_match["pseudonymous_player_refs"]:
            assert re.match(r"^plr_ecf_[0-9a-f]{12}$", pref)
        raw_json = resp.text
        assert "fixture_player_" not in raw_json
        assert "ecf_demo_match_" not in raw_json


def test_55_demo_analysis_endpoint() -> None:
    """55. `GET /demo/analysis` returns Search, Scout, Analyst findings, confounders, and hashes."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        resp = client.get("/demo/analysis")
        assert resp.status_code == 200
        body = resp.json()
        assert body["confidence"] == "LOW_CONFIDENCE"
        assert re.match(r"^[0-9a-f]{64}$", body["search_hash"])
        assert re.match(r"^[0-9a-f]{64}$", body["scout_hash"])
        assert re.match(r"^[0-9a-f]{64}$", body["analyst_hash"])
        assert "search_team_summary" in body
        assert "recent_versus_baseline_summary" in body
        assert "scout_team_and_formation_evidence" in body
        assert len(body["analyst_findings"]) > 0
        assert len(body["confounders"]) > 0
        assert len(body["data_gaps"]) > 0
        assert body["recommendations_enabled"] is False


def test_56_fixture_ingest_endpoint() -> None:
    """56. `POST /demo/ingest-fixture` accepts only `valid`, `empty_history`, `duplicates`."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        for allowed_scenario, expected_accepted in (
            ("valid", 6),
            ("empty_history", 0),
            ("duplicates", 6),
        ):
            r = client.post("/demo/ingest-fixture", json={"scenario": allowed_scenario})
            assert r.status_code == 200
            assert r.json()["scenario"] == allowed_scenario
            assert r.json()["accepted_count"] == expected_accepted

        # Adversarial / non-UI scenarios rejected with 422
        bad = client.post("/demo/ingest-fixture", json={"scenario": "malformed_json"})
        assert bad.status_code == 422


def test_57_no_live_endpoint() -> None:
    """57. No live ingest endpoint exists and `source=live` is rejected on `/demo/ingest-fixture`."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        assert client.post("/demo/ingest-live").status_code == 404
        assert client.get("/demo/live").status_code == 404
        assert client.post("/demo/ingest-fixture", json={"source": "live"}).status_code == 422
        assert client.post("/demo/ingest-fixture?source=live").status_code == 400


def test_58_no_arbitrary_url_parameter() -> None:
    """58. Arbitrary URL / host / operation parameters in query or JSON body are rejected."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        assert client.get("/demo/status?url=https://proclubs.ea.com").status_code == 400
        assert client.get("/demo/matches?provider_url=http://127.0.0.1:9999").status_code == 400
        assert client.get("/demo/analysis?operation=arbitrary").status_code == 400
        assert (
            client.post(
                "/demo/ingest-fixture",
                json={"scenario": "valid", "url": "https://proclubs.ea.com"},
            ).status_code
            == 422
        )


def test_59_no_raw_response_endpoint() -> None:
    """59. No raw-response, alias, or external-ID endpoint exists."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        for path in ("/demo/raw", "/demo/raw-response", "/demo/aliases", "/demo/players/raw"):
            assert client.get(path).status_code == 404


# ==============================================================================
# PART M: Tests 60 - 68 (Optional PostgreSQL 16 Persistence & Isolation)
# ==============================================================================


def test_60_persistence_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """60. Persistence is disabled by default (`persist=False`, `FC27_PERSISTENCE_ENABLED=false`)."""
    monkeypatch.delenv("FC27_PERSISTENCE_ENABLED", raising=False)
    assert settings.is_fc27_persistence_enabled() is False
    assert validate_demo_persistence_gate(persist_requested=False) is False
    res = run_fc27_demo_ingestion(
        source="fixture", operation="full-demo", scenario="valid", demo_mode_enabled=True
    )
    assert res.persistence_status == "DISABLED"


def test_61_persist_flag_alone_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """61. `--persist` alone is rejected fail-closed without connecting to PostgreSQL."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "false")
    with pytest.raises(FC27PersistenceGateError, match="requires FC27_PERSISTENCE_ENABLED=true"):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            persist=True,
            postgres_conn_or_uri="postgresql://unreachable-should-not-connect:5432/db",
            demo_mode_enabled=True,
        )


def test_62_persistence_env_flag_alone_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """62. `FC27_PERSISTENCE_ENABLED=true` alone is rejected fail-closed without `--persist`."""
    monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
    with pytest.raises(FC27PersistenceGateError, match="cannot persist without --persist"):
        run_fc27_demo_ingestion(
            source="fixture",
            operation="full-demo",
            persist=False,
            postgres_conn_or_uri="postgresql://unreachable-should-not-connect:5432/db",
            demo_mode_enabled=True,
        )


def test_63_persistence_with_both_gates(pg_db_migrated) -> None:
    """63. Persistence succeeds in one transaction when both `--persist` and `FC27_PERSISTENCE_ENABLED=true` are present."""
    res = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        persist=True,
        persistence_env_enabled=True,
        postgres_conn_or_uri=pg_db_migrated.app,
        export_id="exp-demo-persist-063",
        demo_mode_enabled=True,
    )
    assert res.persistence_status == "APPLIED"
    assert res.accepted_count == 6

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 6
            cur.execute(
                "SELECT COUNT(*) FROM public.fc27_player_match_stats WHERE display_alias IS NOT NULL"
            )
            assert cur.fetchone()[0] == 0


def test_64_duplicate_persistence_idempotent(pg_db_migrated) -> None:
    """64. Duplicate persistence replay is idempotent (`DUPLICATE_IGNORED`) without duplicating rows."""
    first = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        persist=True,
        persistence_env_enabled=True,
        postgres_conn_or_uri=pg_db_migrated.app,
        export_id="exp-demo-persist-064",
        demo_mode_enabled=True,
    )
    assert first.persistence_status == "APPLIED"

    second = run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        persist=True,
        persistence_env_enabled=True,
        postgres_conn_or_uri=pg_db_migrated.app,
        export_id="exp-demo-persist-064",
        demo_mode_enabled=True,
    )
    assert second.persistence_status == "DUPLICATE_IGNORED"

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = '330323'")
            assert cur.fetchone()[0] == 6


def test_65_conflicting_persistence_rolls_back(pg_db_migrated) -> None:
    """65. Conflicting replay raises `FC27ReplayConflictError` and rolls back without mutating stored matches."""
    run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        persist=True,
        persistence_env_enabled=True,
        postgres_conn_or_uri=pg_db_migrated.app,
        export_id="exp-demo-persist-065-a",
        demo_mode_enabled=True,
    )

    artifacts = run_fc27_demo_ingestion_with_artifacts(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        demo_mode_enabled=True,
    )
    # Mutate goals_for on an existing match_id to trigger a replay conflict against PostgreSQL
    from src.fc27_clubs_api_contracts import normalize_matches_payload

    raw_payload = build_synthetic_matches_payload()
    raw_payload["matches"][0]["goalsFor"] = 9
    _, modified_supplied, _ = normalize_matches_payload(
        raw_payload, include_player_performances=True
    )
    conflicting_env = _build_export_envelope_for_persistence(
        modified_supplied,
        export_id="exp-demo-persist-065-b",
        generated_at=datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    from src.fc27_repository import apply_export_plan

    apply_res = apply_export_plan(
        pg_db_migrated.app,
        envelope=conflicting_env,
        actor="fc27_demo_test",
        enforce_posture=False,
        quarantine_conflicts=False,
    )
    assert apply_res.status == "REJECTED"
    assert apply_res.replay_conflict is True

    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT goals_for FROM public.fc27_matches WHERE external_match_id = 'match_ecf_demo_001'"
            )
            assert cur.fetchone()[0] == 3
            cur.execute("SELECT COUNT(*) FROM public.fc27_quarantine_records")
            assert cur.fetchone()[0] == 0


def test_66_postgressaver_unchanged(pg_db_migrated) -> None:
    """66. PostgresSaver checkpoint tables remain untouched by demo persistence."""
    run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        persist=True,
        persistence_env_enabled=True,
        postgres_conn_or_uri=pg_db_migrated.app,
        export_id="exp-demo-persist-066",
        demo_mode_enabled=True,
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.checkpoints")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.checkpoint_blobs")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.checkpoint_writes")
            assert cur.fetchone()[0] == 0


def test_67_approval_tables_unchanged(pg_db_migrated) -> None:
    """67. Approval tables (`approval_requests`, `approval_audit`) have zero rows written by demo persistence."""
    run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        persist=True,
        persistence_env_enabled=True,
        postgres_conn_or_uri=pg_db_migrated.app,
        export_id="exp-demo-persist-067",
        demo_mode_enabled=True,
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.approval_requests")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.approval_audit")
            assert cur.fetchone()[0] == 0


def test_68_dispatch_tables_unchanged(pg_db_migrated) -> None:
    """68. Dispatch tables (`dispatches`, `dispatch_delivery_attempts`) have zero rows written."""
    run_fc27_demo_ingestion(
        source="fixture",
        operation="full-demo",
        scenario="valid",
        persist=True,
        persistence_env_enabled=True,
        postgres_conn_or_uri=pg_db_migrated.app,
        export_id="exp-demo-persist-068",
        demo_mode_enabled=True,
    )
    with psycopg.connect(pg_db_migrated.admin) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.dispatches")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM public.dispatch_delivery_attempts")
            assert cur.fetchone()[0] == 0


# ==============================================================================
# PART M: Tests 69 - 80 (Feature Gates, Network Guard, Live Boundary, CLI, HTML)
# ==============================================================================


def test_69_recommendations_remain_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """69. Enabling `FC27_DEMO_MODE=true` does not enable recommendations."""
    monkeypatch.setenv("FC27_DEMO_MODE", "true")
    assert settings.is_fc27_recommendations_enabled() is False
    with pytest.raises(FC27ContractError):
        run_fc27_demo_ingestion(recommendations=True, demo_mode_enabled=True)


def test_70_dispatch_remains_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """70. Enabling `FC27_DEMO_MODE=true` does not enable dispatch or dispatch worker."""
    monkeypatch.setenv("FC27_DEMO_MODE", "true")
    assert settings.is_dispatch_enabled() is False
    assert settings.is_dispatch_worker_enabled() is False
    with pytest.raises(FC27ContractError):
        run_fc27_demo_ingestion(dispatch=True, demo_mode_enabled=True)


def test_71_continuous_polling_remains_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """71. Enabling `FC27_DEMO_MODE=true` does not enable live ingestion or continuous polling."""
    monkeypatch.setenv("FC27_DEMO_MODE", "true")
    assert settings.is_fc27_live_ingestion_enabled() is False
    assert settings.is_fc27_provider_enabled() is False


def test_72_no_external_network(_loopback_only_network_guard: dict[str, Any]) -> None:
    """72. Full demo run contacts `127.0.0.1` loopback only and makes zero external connections."""
    res = run_fc27_demo_ingestion(
        source="fixture", operation="full-demo", scenario="valid", demo_mode_enabled=True
    )
    assert res.validation_status == "PASS"
    assert _loopback_only_network_guard["non_loopback_connects"] == []


def test_73_no_ea_dns_resolution(_loopback_only_network_guard: dict[str, Any]) -> None:
    """73. Zero EA DNS resolutions occur during fixture ingestion or API calls."""
    res = run_fc27_demo_ingestion(
        source="fixture", operation="full-demo", scenario="valid", demo_mode_enabled=True
    )
    assert res.validation_status == "PASS"
    assert _loopback_only_network_guard["ea_dns_calls"] == []


def test_74_live_source_requires_live_confirm() -> None:
    """74. `--source live` without `--live-confirm` is rejected immediately."""
    with pytest.raises(FC27LiveAccessBlockedError, match="--live-confirm"):
        FC27LiveProbeConfig(source="live", live_confirm=False)
    with pytest.raises(FC27LiveAccessBlockedError, match="--live-confirm"):
        run_fc27_demo_ingestion(
            source="live",
            operation="club-identity",
            live_confirm=False,
            demo_mode_enabled=True,
        )


def test_75_live_operation_limited_to_club_identity() -> None:
    """75. Live probe operation is restricted strictly to `club-identity`."""
    cfg = FC27LiveProbeConfig(source="live", live_confirm=True, operation="club-identity")
    assert cfg.operation == "club-identity"
    with pytest.raises(FC27ContractError, match="restricted exclusively to 'club-identity'"):
        FC27LiveProbeConfig(source="live", live_confirm=True, operation="full-demo")


def test_76_live_source_one_request() -> None:
    """76. Live probe boundary enforces `max_requests == 1`."""
    with pytest.raises(FC27ContractError, match="max_requests == 1"):
        FC27LiveProbeConfig(
            source="live",
            live_confirm=True,
            operation="club-identity",
            max_requests=2,
        )


def test_77_live_source_zero_retries() -> None:
    """77. Live probe boundary enforces `max_retries == 0`."""
    with pytest.raises(FC27ContractError, match="max_retries == 0"):
        FC27LiveProbeConfig(
            source="live",
            live_confirm=True,
            operation="club-identity",
            max_retries=1,
        )


def test_78_live_probe_not_executed_in_tests() -> None:
    """78. `LIVE_PROBE_EXECUTED` is `False` and even `--source live --live-confirm` blocks network execution in this phase."""
    assert LIVE_PROBE_EXECUTED is False
    with pytest.raises(FC27LiveAccessBlockedError, match="LIVE_PROBE_EXECUTED=NO"):
        run_fc27_demo_ingestion(
            source="live",
            operation="club-identity",
            live_confirm=True,
            demo_mode_enabled=True,
        )


def test_79_metadata_only_logs(capsys: pytest.CaptureFixture[str]) -> None:
    """79. One-shot CLI output and transport logs are strictly metadata-only."""
    exit_code = demo_cli_main(["--source", "fixture", "--operation", "full-demo"])
    captured = capsys.readouterr().out
    assert exit_code == 0
    assert "VALIDATION_STATUS=PASS" in captured
    assert "RAW_RESPONSE_RETAINED=NO" in captured
    assert "SEARCH_HASH=" in captured
    assert "SCOUT_HASH=" in captured
    assert "ANALYST_HASH=" in captured
    assert "fixture_player_" not in captured
    assert "ecf_demo_match_" not in captured
    assert "gamertag" not in captured.lower()


def test_80_fixture_banner_on_html_page() -> None:
    """80. `GET /demo` renders self-contained HTML with visible 'Synthetic Fixture Data' and 'Recommendations Disabled' banners."""
    store = FC27DemoStateStore()
    app = create_demo_app(state_store=store, demo_mode_override=True)
    with TestClient(app) as client:
        resp = client.get("/demo")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        html_text = resp.text
        assert "Synthetic Fixture Data" in html_text
        assert "Recommendations Disabled" in html_text
        assert DEMO_FIXTURE_LOOPBACK in html_text
        assert "<script src=" not in html_text.lower()
        assert '<link rel="stylesheet"' not in html_text.lower()
        assert "localstorage" not in html_text.lower()
        assert "document.cookie" not in html_text.lower()
        assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}


def test_81_canonical_demo_asgi_factory_and_documentation_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """81. Verifies `src.fc27_demo_api:create_demo_app` is the canonical ASGI factory and no current doc uses `src.fc27_demo_api:create_app`."""
    import importlib
    from pathlib import Path
    import re
    from fastapi import FastAPI
    import src.fc27_demo_api as demo_api_mod

    # 1. Canonical factory exists, is callable, and returns a FastAPI application
    assert hasattr(demo_api_mod, "create_demo_app")
    assert callable(demo_api_mod.create_demo_app)
    assert not hasattr(demo_api_mod, "create_app")

    app_instance = demo_api_mod.create_demo_app()
    assert isinstance(app_instance, FastAPI)

    # 2. Documented Uvicorn module:factory in docs/FC27_DEMO.md resolves cleanly
    demo_doc_path = Path(__file__).resolve().parent.parent / "docs" / "FC27_DEMO.md"
    demo_doc_text = demo_doc_path.read_text(encoding="utf-8")
    matches = re.findall(
        r"uvicorn(?:\.exe)?\s+(src\.[A-Za-z0-9_.]+):([A-Za-z0-9_]+)",
        demo_doc_text,
    )
    assert matches, "Expected at least one uvicorn module:factory command in docs/FC27_DEMO.md"
    for mod_name, factory_name in matches:
        assert mod_name == "src.fc27_demo_api"
        assert factory_name == "create_demo_app"
        resolved_mod = importlib.import_module(mod_name)
        resolved_factory = getattr(resolved_mod, factory_name)
        assert callable(resolved_factory)
        assert isinstance(resolved_factory(), FastAPI)

    # 3. No current document references stale `src.fc27_demo_api:create_app`
    repo_root = Path(__file__).resolve().parent.parent
    current_docs = [
        repo_root / "README.md",
        repo_root / "MIGRATION_GUIDE.md",
        repo_root / "TEST_RESULTS.md",
        repo_root / "SECURITY.md",
        repo_root / "THIRD_PARTY_NOTICES.md",
        *sorted((repo_root / "docs").rglob("*.md")),
    ]
    for doc_file in current_docs:
        rel_posix = doc_file.relative_to(repo_root).as_posix()
        if rel_posix.startswith("docs/archive/"):
            continue
        doc_content = doc_file.read_text(encoding="utf-8", errors="ignore")
        assert "src.fc27_demo_api:create_app" not in doc_content, (
            f"Stale factory reference found in {rel_posix}"
        )

    # 4. Demo remains disabled when FC27_DEMO_MODE=false and starts when FC27_DEMO_MODE=true
    monkeypatch.setenv("FC27_DEMO_MODE", "false")
    with TestClient(demo_api_mod.create_demo_app()) as disabled_client:
        assert disabled_client.get("/demo").status_code == 403
        assert disabled_client.get("/demo/status").status_code == 403

    monkeypatch.setenv("FC27_DEMO_MODE", "true")
    store = FC27DemoStateStore()
    with TestClient(demo_api_mod.create_demo_app(state_store=store)) as enabled_client:
        assert enabled_client.get("/demo").status_code == 200
        assert enabled_client.get("/demo/status").status_code == 200
        assert enabled_client.get("/demo/matches").status_code == 200
        assert enabled_client.get("/demo/analysis").status_code == 200
