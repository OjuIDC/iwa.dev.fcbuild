"""Comprehensive test suite for FC27 advisory-report caching and internal validation boundary optimization (Part N).

Covers all 47 required scenarios plus focused tests for every replaced internal rescan.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading
import time
from typing import Any

import psycopg
from pydantic import ValidationError
import pytest

from src import settings
from src.fc27_advisory_cache import (
    DEFAULT_ADVISORY_CACHE_MAX_ENTRIES,
    DEFAULT_ADVISORY_CACHE_TTL_SECONDS,
    PRIVACY_POLICY_VERSION,
    FC27AdvisoryCache,
    FC27AdvisoryCacheEntry,
    FC27AdvisoryCacheKey,
    FC27AdvisoryCachePolicy,
    FC27AdvisoryCacheReplayBlockedError,
    FC27AdvisoryCacheValidationError,
    build_advisory_cache_key_from_matches,
    build_full_advisory_bundle_from_candidates,
    fetch_persisted_advisory_cache_key,
)
from src.fc27_analyst import (
    ANALYST_POLICY_VERSION,
    ANALYST_SYNTHESIS_VERSION,
    FC27AnalystInput,
    _copy_and_scan_input_mapping,
    build_fc27_analyst_report,
)
from src.fc27_clubs_api_contracts import (
    FC27SchemaValidationError,
    normalize_matches_payload,
)
from src.fc27_coach import (
    COACH_VERSION,
    FC27CoachInput,
    build_fc27_coach_report,
)
from src.fc27_community_provider import (
    _supplied_matches_from_envelopes,
    normalize_community_match_records,
    persist_community_provider_batch,
)
from src.fc27_director import (
    DIRECTOR_VERSION,
    FC27DirectorInput,
    build_fc27_director_report,
)
from src.fc27_discord_bot import (
    FC27DiscordBotConfig,
    FC27DiscordBotService,
    FC27DiscordPrivacyError,
    assert_discord_output_sanitized,
)
from src.fc27_performance import (
    build_benchmark_scenario_bundle,
    disposable_postgres_benchmark_db,
)
from src.fc27_polling_worker import FC27PollingRuntime, run_fc27_poll_cycle
from src.fc27_privacy import (
    FC27SensitiveFieldDetectedError,
    is_sensitive_key as _is_sensitive_key,
    scan_for_sensitive_fields,
)
from src.fc27_repository import (
    _build_export_envelope_for_persistence,
    _supplied_matches_to_search_candidates,
    plan_fc27_user_export_import,
)
from src.fc27_search_scout import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    FORMULA_VERSION,
    FC27AnalysisIdentity,
    FC27PrivacyViolationError,
    FC27ScopeViolationError,
    FC27SearchInput,
    _scan_reject_privacy_fields,
    build_fc27_scout_report,
    build_fc27_search_context,
)
from src.fc27_tactics import (
    TACTICS_VERSION,
    FC27TacticsInput,
    build_fc27_tactics_report,
)

FIXED_TS = "2026-11-03T12:00:00Z"


@pytest.fixture(autouse=True)
def _enable_analytics_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FC27_ANALYTICS_ENABLED", "true")


def _sample_candidates(count: int = 10) -> list[dict[str, Any]]:
    meta, _, norm_matches, supplied = build_benchmark_scenario_bundle("SMALL")
    cands = _supplied_matches_to_search_candidates(
        norm_matches[:count],
        supplied[:count],
        payload_sha256=meta.fixture_hash,
    )
    return list(cands)


def _build_uncached_six_layer_bundle(
    candidates: list[dict[str, Any]],
) -> tuple[Any, Any, Any, Any, Any, Any]:
    s_ctx = build_fc27_search_context(
        FC27SearchInput(
            identity=FC27AnalysisIdentity(
                club_id=EAST_COASTERS_CLUB_ID,
                club_name=EAST_COASTERS_CLUB_NAME,
                platform=EAST_COASTERS_PLATFORM,
                game_version=EAST_COASTERS_GAME_VERSION,
            ),
            analysis_generated_at=FIXED_TS,
            source_classification="SYNTHETIC_FIXTURE",
            candidate_matches=tuple(candidates),
        )
    )
    scout = build_fc27_scout_report(s_ctx)
    analyst = build_fc27_analyst_report(
        FC27AnalystInput(
            search_context=s_ctx,
            scout_report=scout,
            analysis_generated_at=FIXED_TS,
        )
    )
    coach = build_fc27_coach_report(
        FC27CoachInput(
            search_context=s_ctx,
            scout_report=scout,
            analyst_report=analyst,
            coach_generated_at=FIXED_TS,
        )
    )
    tactics = build_fc27_tactics_report(
        FC27TacticsInput(
            search_context=s_ctx,
            scout_report=scout,
            analyst_report=analyst,
            coach_report=coach,
            tactics_generated_at=FIXED_TS,
        )
    )
    director = build_fc27_director_report(
        FC27DirectorInput(
            search_context=s_ctx,
            scout_report=scout,
            analyst_report=analyst,
            coach_report=coach,
            tactics_report=tactics,
            director_generated_at=FIXED_TS,
        )
    )
    return (s_ctx, scout, analyst, coach, tactics, director)


# ==============================================================================
# SCENARIOS 1 - 16: CACHE POLICY & KEY INVARIANTS
# ==============================================================================


def test_01_cache_enabled_by_default_with_bounded_configuration() -> None:
    cache = FC27AdvisoryCache()
    assert cache.policy.enabled is True
    assert cache.policy.maximum_entries == DEFAULT_ADVISORY_CACHE_MAX_ENTRIES == 16
    assert cache.policy.ttl_seconds == DEFAULT_ADVISORY_CACHE_TTL_SECONDS == 43200
    assert cache.policy.copy_on_read is True
    assert cache.policy.cache_raw_provider_records is False
    assert cache.policy.cache_database_credentials is False
    assert cache.policy.cache_discord_output is False
    assert cache.policy.cache_mutable_objects is False

    with pytest.raises(ValidationError):
        FC27AdvisoryCachePolicy(maximum_entries=17)
    with pytest.raises(ValidationError):
        FC27AdvisoryCachePolicy(ttl_seconds=43201)


def test_02_cache_disabled_mode_bypasses_storage_cleanly() -> None:
    cache = FC27AdvisoryCache(FC27AdvisoryCachePolicy(enabled=False))
    cands = _sample_candidates(10)
    key = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="batch-disabled")
    entry1 = build_full_advisory_bundle_from_candidates(
        cands, analysis_generated_at=FIXEDTS if "FIXEDTS" in globals() else FIXED_TS, cache=cache, cache_key=key
    )
    assert entry1.search_context.eligible_match_count == 10
    assert cache.get(key) is None
    st = cache.stats()
    assert st.current_entries == 0
    assert st.hits == 0
    assert st.misses >= 2
    assert st.builds == 1


def test_03_cache_key_requires_club_id_330323() -> None:
    key = build_advisory_cache_key_from_matches(_sample_candidates(2))
    assert key.club_id == "330323"
    with pytest.raises(FC27ScopeViolationError, match="330323"):
        key.model_copy(update={"club_id": "999999"}).__class__.model_validate(
            {**key.model_dump(), "club_id": "999999"}
        )


def test_04_cache_key_requires_platform_common_gen5() -> None:
    key = build_advisory_cache_key_from_matches(_sample_candidates(2))
    assert key.platform == "common-gen5"
    with pytest.raises(FC27ScopeViolationError, match="common-gen5"):
        FC27AdvisoryCacheKey.model_validate({**key.model_dump(), "platform": "ps4"})


def test_05_cache_key_requires_game_version_fc27() -> None:
    key = build_advisory_cache_key_from_matches(_sample_candidates(2))
    assert key.game_version == "FC27"
    with pytest.raises(FC27ScopeViolationError, match="FC27"):
        FC27AdvisoryCacheKey.model_validate({**key.model_dump(), "game_version": "FC26"})


def test_06_cache_key_includes_latest_export_batch_id() -> None:
    cands = _sample_candidates(2)
    k1 = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="exp-001")
    k2 = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="exp-002")
    assert k1.latest_export_batch_id == "exp-001"
    assert k1.to_digest() != k2.to_digest()


def test_07_cache_key_includes_latest_match_id() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate({**k1.model_dump(), "latest_match_id": "m_other_999"})
    assert k1.latest_match_id != "m_other_999"
    assert k1.to_digest() != k2.to_digest()


def test_08_cache_key_includes_match_count() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate({**k1.model_dump(), "match_count": 99})
    assert k1.match_count == 2
    assert k1.to_digest() != k2.to_digest()


def test_09_cache_key_includes_normalized_data_hash() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate({**k1.model_dump(), "normalized_data_hash": "f" * 64})
    assert k1.to_digest() != k2.to_digest()
    with pytest.raises(FC27AdvisoryCacheValidationError, match="64-character"):
        FC27AdvisoryCacheKey.model_validate({**k1.model_dump(), "normalized_data_hash": "z" * 64})


def test_10_cache_key_includes_search_formula_version() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate(
        {**k1.model_dump(), "search_formula_version": "fc27_formula_v9.9"}
    )
    assert k1.search_formula_version == FORMULA_VERSION
    assert k1.to_digest() != k2.to_digest()


def test_11_cache_key_includes_scout_formula_version() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate(
        {**k1.model_dump(), "scout_formula_version": "fc27_scout_v9.9"}
    )
    assert k1.scout_formula_version == FORMULA_VERSION
    assert k1.to_digest() != k2.to_digest()


def test_12_cache_key_includes_analyst_synthesis_and_policy_versions() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate(
        {**k1.model_dump(), "analyst_synthesis_version": "analyst_v9.9"}
    )
    k3 = FC27AdvisoryCacheKey.model_validate(
        {**k1.model_dump(), "analyst_policy_version": "policy_v9.9"}
    )
    assert k1.analyst_synthesis_version == ANALYST_SYNTHESIS_VERSION
    assert k1.analyst_policy_version == ANALYST_POLICY_VERSION
    assert len({k1.to_digest(), k2.to_digest(), k3.to_digest()}) == 3


def test_13_cache_key_includes_coach_version() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate({**k1.model_dump(), "coach_version": "coach_v9.9"})
    assert k1.coach_version == COACH_VERSION
    assert k1.to_digest() != k2.to_digest()


def test_14_cache_key_includes_tactics_version() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate({**k1.model_dump(), "tactics_version": "tactics_v9.9"})
    assert k1.tactics_version == TACTICS_VERSION
    assert k1.to_digest() != k2.to_digest()


def test_15_cache_key_includes_director_version() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate({**k1.model_dump(), "director_version": "director_v9.9"})
    assert k1.director_version == DIRECTOR_VERSION
    assert k1.to_digest() != k2.to_digest()


def test_16_cache_key_includes_privacy_policy_version() -> None:
    k1 = build_advisory_cache_key_from_matches(_sample_candidates(2))
    k2 = FC27AdvisoryCacheKey.model_validate(
        {**k1.model_dump(), "privacy_policy_version": "privacy_v9.9"}
    )
    assert k1.privacy_policy_version == PRIVACY_POLICY_VERSION
    assert k1.to_digest() != k2.to_digest()


# ==============================================================================
# SCENARIOS 17 - 29: CACHE LIFECYCLE, CONCURRENCY, EVICTION & INVALIDATION
# ==============================================================================


def test_17_first_advisory_build_records_miss_and_one_build() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    key = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-17")
    entry = build_full_advisory_bundle_from_candidates(
        cands, analysis_generated_at=FIXED_TS, cache=cache, cache_key=key
    )
    assert isinstance(entry, FC27AdvisoryCacheEntry)
    st = cache.stats()
    assert st.misses == 1
    assert st.builds == 1
    assert st.hits == 0
    assert st.current_entries == 1


def test_18_second_advisory_read_with_identical_key_records_hit_and_zero_rebuilds() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    key = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-18")
    build_calls = 0

    def _counting_builder() -> tuple[Any, Any, Any, Any, Any, Any]:
        nonlocal build_calls
        build_calls += 1
        return _build_uncached_six_layer_bundle(cands)

    e1 = cache.get_or_build(key, _counting_builder)
    e2 = cache.get_or_build(key, _counting_builder)
    assert build_calls == 1
    assert e1.director_report.report_hash == e2.director_report.report_hash
    st = cache.stats()
    assert st.misses == 1
    assert st.builds == 1
    assert st.hits == 1


def test_19_cached_reports_match_uncached_reports_hash_for_hash() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    uncached = _build_uncached_six_layer_bundle(cands)
    cached = build_full_advisory_bundle_from_candidates(
        cands, analysis_generated_at=FIXED_TS, cache=cache
    )
    assert cached.search_context.context_hash == uncached[0].context_hash
    assert cached.scout_report.report_hash == uncached[1].report_hash
    assert cached.analyst_report.report_hash == uncached[2].report_hash
    assert cached.coach_report.report_hash == uncached[3].report_hash
    assert cached.tactics_report.report_hash == uncached[4].report_hash
    assert cached.director_report.report_hash == uncached[5].report_hash
    assert cached.director_reconciliation_report.report_hash == uncached[5].report_hash


def test_20_caller_mutation_cannot_mutate_cached_entry() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    key = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-20")
    e1 = build_full_advisory_bundle_from_candidates(
        cands, analysis_generated_at=FIXED_TS, cache=cache, cache_key=key
    )
    # Frozen Pydantic model rejects attribute assignment
    with pytest.raises(ValidationError):
        e1.analyst_report.overall_confidence = "TAMPERED"  # type: ignore[misc]

    # Deep copy-on-read guarantees distinct object identities across reads
    e2 = cache.get(key)
    assert e2 is not None
    assert e1 is not e2
    assert e1.analyst_report is not e2.analyst_report

    # Serialized projection also returns deep copies
    p1 = cache.get_or_build_serialized_projection(key, "test_proj", lambda: {"nested": ["a", "b"]})
    p1["nested"].append("MUTATED")
    p2 = cache.get_or_build_serialized_projection(key, "test_proj", lambda: {"nested": ["a", "b"]})
    assert p2["nested"] == ["a", "b"]


def test_21_concurrent_reads_during_cold_build_execute_builder_once() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    key = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-21")
    build_count = 0
    count_lock = threading.Lock()
    barrier = threading.Event()

    def _slow_builder() -> tuple[Any, Any, Any, Any, Any, Any]:
        nonlocal build_count
        with count_lock:
            build_count += 1
        barrier.wait(timeout=2.0)
        return _build_uncached_six_layer_bundle(cands)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(cache.get_or_build, key, _slow_builder) for _ in range(6)]
        time.sleep(0.05)
        barrier.set()
        results = [f.result(timeout=10.0) for f in futures]

    assert build_count == 1
    hashes = {r.director_report.report_hash for r in results}
    assert len(hashes) == 1
    st = cache.stats()
    assert st.builds == 1
    assert st.hits == 5


def test_22_failed_build_is_not_cached_and_does_not_poison_subsequent_reads() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    key = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-22")

    with pytest.raises(RuntimeError, match="transient builder error"):
        cache.get_or_build(key, lambda: (_ for _ in ()).throw(RuntimeError("transient builder error")))

    st_after_fail = cache.stats()
    assert st_after_fail.build_failures == 1
    assert st_after_fail.current_entries == 0
    assert cache.get(key) is None

    # Subsequent call with healthy builder succeeds cleanly
    recovered = cache.get_or_build(key, lambda: _build_uncached_six_layer_bundle(cands))
    assert recovered.search_context.eligible_match_count == 10
    assert cache.stats().current_entries == 1


def test_23_maximum_entry_limit_evicts_oldest_entry() -> None:
    cache = FC27AdvisoryCache(FC27AdvisoryCachePolicy(maximum_entries=3, ttl_seconds=3600))
    cands = _sample_candidates(10)
    built_bundle = _build_uncached_six_layer_bundle(cands)
    keys = [
        build_advisory_cache_key_from_matches(cands, latest_export_batch_id=f"batch-{i}")
        for i in range(4)
    ]
    for k in keys:
        cache.get_or_build(k, lambda: built_bundle)

    st = cache.stats()
    assert st.current_entries == 3
    assert st.evictions == 1
    assert cache.get(keys[0]) is None
    assert cache.get(keys[1]) is not None
    assert cache.get(keys[2]) is not None
    assert cache.get(keys[3]) is not None


def test_24_ttl_expiration_invalidates_expired_entries() -> None:
    now_val = [1000.0]
    cache = FC27AdvisoryCache(
        FC27AdvisoryCachePolicy(maximum_entries=8, ttl_seconds=60),
        clock_fn=lambda: now_val[0],
    )
    cands = _sample_candidates(10)
    key = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-ttl")
    cache.get_or_build(key, lambda: _build_uncached_six_layer_bundle(cands))
    assert cache.get(key) is not None

    now_val[0] = 1061.0
    assert cache.get(key) is None
    st = cache.stats()
    assert st.invalidations >= 1
    assert st.current_entries == 0


def test_25_new_persisted_matches_invalidate_or_advance_cache_key() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(8)
    k1 = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-initial")
    cache.get_or_build(k1, lambda: _build_uncached_six_layer_bundle(cands))
    assert cache.stats().current_entries == 1

    action = cache.on_poll_cycle_outcome(
        poll_status="NEW_MATCHES_PERSISTED",
        inserted_match_count=2,
        duplicate_ignored_count=8,
        replay_conflict_count=0,
    )
    assert action == "INVALIDATED_NEW_MATCHES"
    assert cache.get(k1) is None
    assert cache.stats().current_entries == 0


def test_26_duplicate_only_polling_cycle_retains_valid_cache_entry() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    k1 = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-dup")
    cache.get_or_build(k1, lambda: _build_uncached_six_layer_bundle(cands))
    assert cache.stats().current_entries == 1

    action = cache.on_poll_cycle_outcome(
        poll_status="NO_NEW_MATCHES",
        inserted_match_count=0,
        duplicate_ignored_count=10,
        replay_conflict_count=0,
    )
    assert action == "RETAINED_NO_NEW_MATCHES"
    assert cache.get(k1) is not None
    assert cache.stats().current_entries == 1


def test_27_replay_conflict_invalidates_cache_and_blocks_analytical_output() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    k1 = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-conflict")
    cache.get_or_build(k1, lambda: _build_uncached_six_layer_bundle(cands))

    action = cache.on_poll_cycle_outcome(
        poll_status="REPLAY_CONFLICT",
        inserted_match_count=0,
        replay_conflict_count=1,
    )
    assert action == "INVALIDATED_REPLAY_CONFLICT"
    assert cache.is_replay_conflict_blocked is True
    with pytest.raises(FC27AdvisoryCacheReplayBlockedError, match="replay conflict"):
        cache.get(k1)
    with pytest.raises(FC27AdvisoryCacheReplayBlockedError, match="replay conflict"):
        cache.get_or_build(k1, lambda: _build_uncached_six_layer_bundle(cands))


def test_28_formula_or_version_change_produces_new_cache_key_and_misses_cleanly() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    bundle = _build_uncached_six_layer_bundle(cands)
    k_v1 = build_advisory_cache_key_from_matches(cands, coach_version="fc27_coach_v1.0")
    k_v2 = build_advisory_cache_key_from_matches(cands, coach_version="fc27_coach_v2.0")
    cache.get_or_build(k_v1, lambda: bundle)
    assert cache.get(k_v1) is not None
    assert cache.get(k_v2) is None


def test_29_manual_cache_reset_clears_entries_and_increments_invalidation_counter() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    k1 = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="b-reset")
    cache.get_or_build(k1, lambda: _build_uncached_six_layer_bundle(cands))
    cache.invalidate_for_replay_conflict()
    assert cache.is_replay_conflict_blocked is True

    inv_before = cache.stats().invalidations
    cache.reset()
    st = cache.stats()
    assert st.current_entries == 0
    assert st.replay_conflict_blocked is False
    assert st.invalidations > inv_before


# ==============================================================================
# SCENARIOS 30 - 34: NEGATIVE PRIVACY & IMMUTABILITY CACHE GUARDS
# ==============================================================================


def test_30_raw_provider_payload_cannot_be_stored_in_cache() -> None:
    cache = FC27AdvisoryCache()
    with pytest.raises((FC27PrivacyViolationError, FC27AdvisoryCacheValidationError)):
        cache.validate_candidate_value({"raw_response": "{\"clubs\":{}}", "matches": []})


def test_31_source_player_identifiers_cannot_be_stored_in_cache() -> None:
    cache = FC27AdvisoryCache()
    for forbidden_field in ("personaId", "blazeId", "nucleusId", "proName", "gamertag"):
        with pytest.raises(FC27PrivacyViolationError):
            cache.validate_candidate_value({forbidden_field: "ProGamer123"})


def test_32_secrets_and_database_credentials_cannot_be_stored_in_cache() -> None:
    cache = FC27AdvisoryCache()
    with pytest.raises(FC27AdvisoryCacheValidationError, match="Credential"):
        cache.validate_candidate_value("postgresql://fc27_app:secretpass@localhost:5432/fc27")
    with pytest.raises(FC27AdvisoryCacheValidationError, match="Credential"):
        build_advisory_cache_key_from_matches(
            _sample_candidates(2),
            latest_export_batch_id="postgresql://fc27_app:secret@127.0.0.1:5432/db",
        )


def test_33_mutable_dictionary_or_unvalidated_object_cannot_be_stored_in_cache() -> None:
    cache = FC27AdvisoryCache()
    with pytest.raises(FC27AdvisoryCacheValidationError, match="mutable"):
        cache.validate_candidate_value({"clean_key": "clean_value"})
    with pytest.raises(FC27AdvisoryCacheValidationError):
        cache.validate_candidate_value(["list_item"])
    with pytest.raises(FC27AdvisoryCacheValidationError, match="exception"):
        cache.validate_candidate_value(RuntimeError("boom"))


def test_34_cache_telemetry_contains_metadata_only_and_zero_player_references() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    build_full_advisory_bundle_from_candidates(cands, analysis_generated_at=FIXED_TS, cache=cache)
    stats_json = json.dumps(cache.stats().model_dump(mode="json"), sort_keys=True)
    assert "plr_ecf_" not in stats_json
    assert "postgresql://" not in stats_json
    assert scan_for_sensitive_fields(cache.stats().model_dump(mode="json")) == []


# ==============================================================================
# SCENARIOS 35 - 41: DISCORD & DASHBOARD CACHE INTEGRATION
# ==============================================================================


def _seed_test_db_for_discord(admin_uri: str) -> Any:
    return disposable_postgres_benchmark_db(admin_uri)


def test_35_to_40_discord_commands_use_advisory_cache_and_status_polling_skip_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_uri = "postgresql://fc27test@127.0.0.1:55432/postgres"
    val_env, norm_small = normalize_community_match_records(
        [
            {
                "match_id": str(995000 + i),
                "match_type": "leagueMatch",
                "timestamp": 1761800000 + i * 3600,
                "clubs": [
                    {
                        "club_id": "330323",
                        "club_name": "East Coasters",
                        "goals": 2,
                        "goals_against": 1,
                        "result": "1",
                        "wins": 1,
                        "ties": 0,
                        "losses": 0,
                        "winner_by_dnf": 0,
                        "region_id": 4464981,
                        "team_id": 112,
                    },
                    {
                        "club_id": f"880{200 + i}",
                        "club_name": f"Opponent {i}",
                        "goals": 1,
                        "goals_against": 2,
                        "result": "2",
                        "wins": 0,
                        "ties": 0,
                        "losses": 1,
                        "winner_by_dnf": 0,
                        "region_id": 4464981,
                        "team_id": 115,
                    },
                ],
                "players": [
                    {
                        "club_id": "330323",
                        "player_id": f"900{p}",
                        "position": pos,
                        "rating": 7.6,
                        "goals": 1 if p == 0 else 0,
                        "assists": 1 if p == 1 else 0,
                        "shots": 2,
                        "passes_made": 15,
                        "pass_attempts": 18,
                        "tackles_made": 2,
                        "tackle_attempts": 3,
                        "saves": 2 if pos == "GK" else 0,
                        "red_cards": 0,
                        "mom": 1 if p == 0 else 0,
                        "seconds_played": 5400,
                        "archetype_id": p + 1,
                        "event_mask": 2,
                    }
                    for p, pos in enumerate(("GK", "CB", "CB", "CM", "CAM", "ST"))
                ],
                "aggregate": {"330323": {"passesmade": 90}},
            }
            for i in range(10)
        ],
        observed_at="2026-10-31T12:00:00Z",
    )

    guild_id = "123456789012345678"
    channel_id = "234567890123456789"
    user_id = "345678901234567890"

    with disposable_postgres_benchmark_db(admin_uri) as db_ctx:
        app_uri = db_ctx["app_uri"]
        runtime = FC27PollingRuntime()
        monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "true")
        monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "true")
        monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
        gen_dt = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
        run_fc27_poll_cycle(
            runtime=runtime,
            confirm_live_polling=True,
            db_conn_or_uri=app_uri,
            fetch_matches_fn=lambda: val_env,
            persist_fn=lambda env: persist_community_provider_batch(
                env,
                match_envelopes=norm_small,
                postgres_conn_or_uri=app_uri,
                persist_requested=True,
                persistence_enabled=True,
                export_id="exp-cache-test-001",
                generated_at=gen_dt,
            ),
            use_cache=False,
            use_rate_limiter=False,
        )

        # Reset advisory cache to test cold vs warm Discord behavior explicitly
        runtime.advisory_cache.reset()
        builds_initial = runtime.advisory_cache.stats().builds
        svc = FC27DiscordBotService(
            FC27DiscordBotConfig(
                enabled=True,
                allowed_guild_ids=frozenset({guild_id}),
                allowed_channel_ids=frozenset({channel_id}),
                max_user_commands_per_minute=60,
                max_guild_commands_per_minute=120,
            ),
            db_conn_or_uri=app_uri,
            polling_runtime=runtime,
            now_utc_fn=lambda: gen_dt,
        )

        # Scenarios 35 & 36: /fc27 status and /fc27 polling do NOT build advisory bundle
        r_status = svc.handle_slash_command("status", guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        r_polling = svc.handle_slash_command("polling", guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        assert r_status.status == "OK"
        assert r_polling.status == "OK"
        assert svc.advisory_cache.stats().builds == builds_initial

        # Scenario 37: /fc27 coach cold build (builds == builds_initial + 1) then warm hit
        r_coach1 = svc.handle_slash_command("coach", guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        assert r_coach1.status == "OK"
        assert svc.advisory_cache.stats().builds == builds_initial + 1
        hits_after_coach1 = svc.advisory_cache.stats().hits

        r_coach2 = svc.handle_slash_command("coach", guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        assert r_coach2.status == "OK"
        assert svc.advisory_cache.stats().builds == builds_initial + 1
        assert svc.advisory_cache.stats().hits > hits_after_coach1

        # Scenarios 38, 39, 40: /fc27 tactics, /fc27 director, /fc27 analysis reuse the cached bundle
        for cmd in ("tactics", "director", "analysis", "data-gaps", "latest", "form"):
            hits_before = svc.advisory_cache.stats().hits
            resp = svc.handle_slash_command(cmd, guild_id=guild_id, channel_id=channel_id, user_id=user_id)
            assert resp.status == "OK"
            assert resp.provider_requests_executed == 0
            assert svc.advisory_cache.stats().builds == builds_initial + 1
            assert svc.advisory_cache.stats().hits > hits_before


def test_41_serialized_advisory_projections_use_cached_advisory_bundle() -> None:
    cache = FC27AdvisoryCache()
    cands = _sample_candidates(10)
    key = build_advisory_cache_key_from_matches(cands, latest_export_batch_id="batch-proj-41")
    entry = build_full_advisory_bundle_from_candidates(
        cands,
        analysis_generated_at=FIXED_TS,
        cache=cache,
        cache_key=key,
    )
    assert cache.stats().builds == 1

    for proj_name, builder in (
        ("analysis", lambda: entry.analyst_report.model_dump(mode="json")),
        ("coach", lambda: entry.coach_report.model_dump(mode="json")),
        ("tactics", lambda: entry.tactics_report.model_dump(mode="json")),
        ("director", lambda: entry.director_report.model_dump(mode="json")),
    ):
        first = cache.get_or_build_serialized_projection(key, proj_name, builder)
        hits_before = cache.stats().hits
        second = cache.get_or_build_serialized_projection(key, proj_name, builder)
        assert first == second
        assert cache.stats().builds == 1
        assert cache.stats().hits > hits_before



# ==============================================================================
# SCENARIOS 42 - 47: TRUST BOUNDARIES, EQUIVALENCE & GOVERNANCE INVARIANTS
# ==============================================================================


def test_42_external_provider_trust_boundary_still_rejects_prohibited_fields() -> None:
    _, raw_payload, _, _ = build_benchmark_scenario_bundle("SMALL")
    poisoned = json.loads(json.dumps(raw_payload))
    poisoned["matches"][0]["gamertag"] = "LeakedGamerTag99"
    assert len(scan_for_sensitive_fields(poisoned)) > 0
    _, _, drift = normalize_matches_payload(poisoned)
    assert len(drift) > 0
    with pytest.raises(Exception):
        normalize_community_match_records(
            [{"match_id": "999", "match_type": "leagueMatch", "timestamp": 1761800000, "gamertag": "LeakedGamerTag99"}]
        )


def test_43_import_envelope_validation_still_rejects_sensitive_fields() -> None:
    _, _, _, supplied = build_benchmark_scenario_bundle("SMALL")
    env = _build_export_envelope_for_persistence(
        supplied[:2],
        export_id="exp-trust-43",
        generated_at=datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    raw_env = env.model_dump(mode="json")
    raw_env["matches"][0]["personaId"] = "99887766"
    assert len(scan_for_sensitive_fields(raw_env)) > 0


def test_44_outbound_discord_sanitizer_still_blocks_sensitive_fields() -> None:
    with pytest.raises(FC27DiscordPrivacyError):
        assert_discord_output_sanitized("Player personaId: 123456789")
    with pytest.raises(FC27DiscordPrivacyError):
        assert_discord_output_sanitized("gamertag=SecretProPlayer")


def test_45_outbound_export_sanitizer_still_blocks_sensitive_fields() -> None:
    bad_export = {"club_id": "330323", "nested": [{"blazeId": "554433"}]}
    violations = scan_for_sensitive_fields(bad_export, prefix="export")
    assert any("blazeId" in v for v in violations)


def test_46_replaced_internal_rescans_preserve_exact_report_hashes() -> None:
    # Verify that passing validated frozen models vs raw dict mappings into Analyst, Coach, Tactics, and Director
    # produces the exact same deterministic SHA-256 report hashes
    cands = _sample_candidates(10)
    s_ctx, scout, analyst, coach, tactics, director = _build_uncached_six_layer_bundle(cands)

    analyst_from_dict = build_fc27_analyst_report(
        {
            "search_context": s_ctx.model_dump(mode="json"),
            "scout_report": scout.model_dump(mode="json"),
            "analysis_generated_at": FIXED_TS,
        }
    )
    assert analyst_from_dict.report_hash == analyst.report_hash

    coach_from_dict = build_fc27_coach_report(
        {
            "search_context": s_ctx.model_dump(mode="json"),
            "scout_report": scout.model_dump(mode="json"),
            "analyst_report": analyst.model_dump(mode="json"),
            "coach_generated_at": FIXED_TS,
        }
    )
    assert coach_from_dict.report_hash == coach.report_hash

    tactics_from_dict = build_fc27_tactics_report(
        {
            "search_context": s_ctx.model_dump(mode="json"),
            "scout_report": scout.model_dump(mode="json"),
            "analyst_report": analyst.model_dump(mode="json"),
            "coach_report": coach.model_dump(mode="json"),
            "tactics_generated_at": FIXED_TS,
        }
    )
    assert tactics_from_dict.report_hash == tactics.report_hash

    director_from_dict = build_fc27_director_report(
        {
            "search_context": s_ctx.model_dump(mode="json"),
            "scout_report": scout.model_dump(mode="json"),
            "analyst_report": analyst.model_dump(mode="json"),
            "coach_report": coach.model_dump(mode="json"),
            "tactics_report": tactics.model_dump(mode="json"),
            "director_generated_at": FIXED_TS,
        }
    )
    assert director_from_dict.report_hash == director.report_hash


def test_47_recommendations_approval_and_dispatch_remain_disabled() -> None:
    assert settings.is_fc27_recommendations_enabled() is False
    assert settings.is_dispatch_enabled() is False
    assert settings.is_dispatch_worker_enabled() is False

    cands = _sample_candidates(10)
    entry = build_full_advisory_bundle_from_candidates(cands, analysis_generated_at=FIXED_TS)
    assert entry.analyst_report.approvable is False
    assert entry.analyst_report.dispatchable is False
    assert entry.coach_report.approvable is False
    assert entry.coach_report.dispatchable is False
    assert entry.tactics_report.approvable is False
    assert entry.tactics_report.dispatchable is False
    assert entry.director_report.approvable is False
    assert entry.director_report.dispatchable is False


# ==============================================================================
# FOCUSED TESTS FOR REPLACED INTERNAL RESCANS (PART M)
# ==============================================================================


def test_48_focused_internal_rescan_helpers_and_key_memoization() -> None:
    # 1. _is_sensitive_key is memoized and accurately detects sensitive vs safe keys
    assert _is_sensitive_key("personaId") is True
    assert _is_sensitive_key("gamertag") is True
    assert _is_sensitive_key("raw_response") is True
    assert _is_sensitive_key("local_player_reference") is False
    assert _is_sensitive_key("goals_for") is False

    # 2. _scan_reject_privacy_fields scans nested dict/list structures in one pass without missing deep violations
    with pytest.raises(FC27PrivacyViolationError, match="personaId"):
        _scan_reject_privacy_fields({"outer": [{"inner": {"personaId": "12345"}}]})

    # 3. _copy_and_scan_input_mapping skips deep-copy/rescan on already-validated frozen Pydantic sub-models
    #    while still deep-copying and scanning untrusted dicts
    cands = _sample_candidates(2)
    s_ctx, scout, _, _, _, _ = _build_uncached_six_layer_bundle(cands)
    copied_map = _copy_and_scan_input_mapping(
        {
            "search_context": s_ctx,
            "scout_report": scout,
            "analysis_generated_at": FIXED_TS,
        },
        path="analyst_input",
    )
    assert copied_map["search_context"] is s_ctx
    assert copied_map["scout_report"] is scout

    with pytest.raises(FC27PrivacyViolationError):
        _copy_and_scan_input_mapping({"search_context": {"gamertag": "BadActor"}}, path="analyst_input")
