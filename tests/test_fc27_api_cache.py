"""Tests for bounded, TTL-controlled, normalized-only FC 27 API response cache."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, ConfigDict
import pytest

from src.fc27_api_cache import (
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_MAX_VALUE_BYTES,
    OPERATION_DEFAULT_TTL_SECONDS,
    FC27BoundedNormalizedCache,
    FC27CacheError,
    FC27CacheKeySpec,
    build_fc27_cache_key,
)
from src.fc27_provider import create_provisional_club_identity
from src.settings import is_fc27_api_cache_enabled


class _SampleMetadataTransportLog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    operation: str
    request_host: str
    request_path: str
    http_status: int
    content_type: str
    response_size_bytes: int
    payload_sha256: str
    outcome_status: str
    circuit_breaker_state: str


class _SampleSourceProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    provider_declaration_version: str
    observed_timestamp: datetime
    source_operation: str


class _SampleNormalizedOperationEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    operation: str
    payload_sha256: str
    club_identity: Any
    transport_log: _SampleMetadataTransportLog
    source_provenance: _SampleSourceProvenance
    raw_response_retained: bool = False
    store_display_aliases: bool = False
    store_gamer_tags: bool = False
    store_external_player_identifiers: bool = False
    review_required: bool = True


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_sample_envelope(operation: str = "CLUB_IDENTITY", sha_char: str = "a") -> _SampleNormalizedOperationEnvelope:
    identity = create_provisional_club_identity(
        club_id="330323",
        club_name="East Coasters",
        platform="common-gen5",
    )
    log = _SampleMetadataTransportLog(
        provider_id="fc-clubs-api-community-v1",
        operation=operation,
        request_host="proclubs.ea.com",
        request_path="/api/fc/clubs/info",
        http_status=200,
        content_type="application/json",
        response_size_bytes=128,
        payload_sha256=sha_char * 64,
        outcome_status="OK",
        circuit_breaker_state="CLOSED",
    )
    prov = _SampleSourceProvenance(
        provider_id="fc-clubs-api-community-v1",
        provider_declaration_version="fc-clubs-api-community-v1",
        observed_timestamp=datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc),
        source_operation=operation,
    )
    return _SampleNormalizedOperationEnvelope(
        provider_id="fc-clubs-api-community-v1",
        operation=operation,
        payload_sha256=sha_char * 64,
        club_identity=identity,
        transport_log=log,
        source_provenance=prov,
        raw_response_retained=False,
        store_display_aliases=False,
        store_gamer_tags=False,
        store_external_player_identifiers=False,
        review_required=True,
    )


def test_01_cache_disabled_by_default() -> None:
    assert is_fc27_api_cache_enabled() is False
    assert DEFAULT_CACHE_MAX_ENTRIES == 128
    assert DEFAULT_CACHE_MAX_VALUE_BYTES == 262_144
    cache = FC27BoundedNormalizedCache()
    assert cache.enabled is False
    _, spec = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="EA-CLUBS-AUTH-REF-2026-001",
        operation="CLUB_IDENTITY",
    )
    env = _make_sample_envelope()
    assert cache.put(spec, env) is False
    assert cache.get(spec) is None
    assert cache.metrics().hits == 0
    assert cache.metrics().misses == 1


def test_02_cache_stores_and_retrieves_normalized_envelope_when_enabled() -> None:
    clock = _FakeClock()
    cache = FC27BoundedNormalizedCache(enabled=True, clock=clock)
    _, spec = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="EA-CLUBS-AUTH-REF-2026-001",
        operation="CLUB_IDENTITY",
    )
    env = _make_sample_envelope()
    assert cache.put(spec, env) is True
    assert cache.metrics().entry_count == 1
    hit = cache.get(spec)
    assert hit is not None
    assert hit.payload_sha256 == "a" * 64
    assert cache.metrics().hits == 1
    assert cache.metrics().puts == 1


def test_03_cache_ttl_defaults_and_expiration_with_injected_clock() -> None:
    assert OPERATION_DEFAULT_TTL_SECONDS["CLUB_IDENTITY"] == 21600
    assert OPERATION_DEFAULT_TTL_SECONDS["CLUB_STATISTICS"] == 900
    assert OPERATION_DEFAULT_TTL_SECONDS["RECENT_MATCHES"] == 300
    assert OPERATION_DEFAULT_TTL_SECONDS["PLAYOFF_ACHIEVEMENTS"] == 3600

    clock = _FakeClock(initial=5000.0)
    cache = FC27BoundedNormalizedCache(enabled=True, clock=clock)
    _, spec = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="EA-CLUBS-AUTH-REF-2026-001",
        operation="RECENT_MATCHES",
    )
    env = _make_sample_envelope(operation="RECENT_MATCHES")
    cache.put(spec, env)  # uses default 300s TTL for RECENT_MATCHES
    clock.advance(299.0)
    assert cache.get(spec) is not None
    clock.advance(2.0)  # total 301s > 300s
    assert cache.get(spec) is None
    assert cache.metrics().expirations == 1


def test_04_cache_lru_eviction_when_full() -> None:
    clock = _FakeClock()
    cache = FC27BoundedNormalizedCache(enabled=True, max_entries=2, clock=clock)
    _, spec1 = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="v1",
        operation="CLUB_IDENTITY",
        parameters={"maxResultCount": "1"},
    )
    _, spec2 = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="v1",
        operation="CLUB_IDENTITY",
        parameters={"maxResultCount": "2"},
    )
    _, spec3 = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="v1",
        operation="CLUB_IDENTITY",
        parameters={"maxResultCount": "3"},
    )
    cache.put(spec1, _make_sample_envelope(sha_char="1"))
    cache.put(spec2, _make_sample_envelope(sha_char="2"))
    # Access spec1 so spec2 becomes least-recently-used
    assert cache.get(spec1) is not None
    # Insert spec3 -> should evict spec2
    cache.put(spec3, _make_sample_envelope(sha_char="3"))
    assert cache.metrics().entry_count == 2
    assert cache.get(spec1) is not None
    assert cache.get(spec2) is None
    assert cache.get(spec3) is not None
    assert cache.metrics().evictions == 1


def test_05_cache_key_deterministic_and_version_aware() -> None:
    k1, s1 = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="v1",
        operation="RECENT_MATCHES",
        parameters={"b": 2, "a": 1},
        response_schema_version="1.0.0",
        normalization_version="1.0.0",
    )
    k2, s2 = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="v1",
        operation="RECENT_MATCHES",
        parameters={"a": 1, "b": 2},
        response_schema_version="1.0.0",
        normalization_version="1.0.0",
    )
    assert k1 == k2
    assert s1.bounded_parameters == s2.bounded_parameters

    k_diff_schema, _ = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="v1",
        operation="RECENT_MATCHES",
        parameters={"a": 1, "b": 2},
        response_schema_version="2.0.0",
        normalization_version="1.0.0",
    )
    assert k1 != k_diff_schema


def test_06_cache_key_rejects_secrets_tokens_cookies_and_gamer_tags() -> None:
    for bad_param in ("api_key", "token", "cookie", "session", "gamertag", "persona_id", "nucleus_id"):
        with pytest.raises(FC27CacheError, match="Prohibited sensitive parameter"):
            build_fc27_cache_key(
                provider_id="ea-authorized-clubs-partner",
                provider_authorization_version="v1",
                operation="CLUB_IDENTITY",
                parameters={bad_param: "secret_value"},
            )


def test_07_cache_rejects_poisoned_or_non_east_coasters_entries() -> None:
    with pytest.raises(FC27CacheError, match="club_id must be '330323'"):
        build_fc27_cache_key(
            provider_id="ea-authorized-clubs-partner",
            provider_authorization_version="v1",
            operation="CLUB_IDENTITY",
            club_id="999999",
        )

    cache = FC27BoundedNormalizedCache(enabled=True)
    _, spec = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="v1",
        operation="CLUB_IDENTITY",
    )
    with pytest.raises(FC27CacheError, match="Cache poisoning rejected"):
        cache.put(
            spec,
            {
                "club_id": "330323",
                "display_alias": "PlaintextAliasNotAllowed",
            },
        )
    assert cache.metrics().poisoning_rejections == 1


def test_08_cache_never_stores_raw_responses_or_oversized_values() -> None:
    cache = FC27BoundedNormalizedCache(enabled=True, max_value_bytes=512)
    _, spec = build_fc27_cache_key(
        provider_id="ea-authorized-clubs-partner",
        provider_authorization_version="v1",
        operation="CLUB_IDENTITY",
    )
    with pytest.raises(FC27CacheError, match="prohibited field"):
        cache.put(
            spec,
            {
                "club_id": "330323",
                "raw_body": "{\"raw\": true}",
            },
        )

    with pytest.raises(FC27CacheError, match="exceeds cache maximum"):
        cache.put(
            spec,
            {
                "club_id": "330323",
                "padding": "x" * 1024,
            },
        )


def test_09_cache_explicit_invalidation_and_clear() -> None:
    cache = FC27BoundedNormalizedCache(enabled=True)
    _, spec1 = build_fc27_cache_key(
        provider_id="provider-a",
        provider_authorization_version="v1",
        operation="CLUB_IDENTITY",
    )
    _, spec2 = build_fc27_cache_key(
        provider_id="provider-b",
        provider_authorization_version="v1",
        operation="CLUB_IDENTITY",
    )
    cache.put(spec1, _make_sample_envelope(sha_char="a"))
    cache.put(spec2, _make_sample_envelope(sha_char="b"))
    assert cache.metrics().entry_count == 2

    removed = cache.invalidate_provider_authorization("provider-a")
    assert removed == 1
    assert cache.get(spec1) is None
    assert cache.get(spec2) is not None

    assert cache.invalidate_key(spec2) is True
    assert cache.metrics().entry_count == 0

    cache.put(spec1, _make_sample_envelope(sha_char="c"))
    assert cache.clear() == 1
    assert cache.metrics().entry_count == 0
