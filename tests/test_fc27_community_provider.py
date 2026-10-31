"""Offline Unit and Integration Tests for `src/fc27_community_provider.py` (`PART K`, 58 tests).

Performs zero live network requests (`NETWORK_REQUESTS_EXECUTED=0`) and zero EA DNS
resolutions (`EA_DNS_RESOLUTIONS_EXECUTED=0`).
"""
from __future__ import annotations

import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.test_fc27_pandas_view import _make_pandas_shim

from src.fc27_clubs_api_contracts import (
    FC27ContractError,
    FC27PseudonymCollisionDetector,
    FC27SchemaValidationError,
)
from src.fc27_community_provider import (
    COMMUNITY_PROVIDER_ID,
    DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
    LIVE_EA_CLUBS_COMMUNITY_CLIENT,
    PLAYER_MATCH_LIVE_REQUEST_EXECUTED,
    UPSTREAM_AUDIT_RECORD,
    UPSTREAM_COMMIT_SHA,
    UPSTREAM_LICENSE,
    FC27CommunityProviderAdapter,
    FC27CommunityProviderDeclaration,
    FC27CommunityProviderEnvelope,
    apply_community_event_policy,
    build_demo_session_artifacts_from_community_envelope,
    classify_community_event_id,
    ingest_community_provider_envelope,
    normalize_community_identity_records,
    normalize_community_match_records,
    validate_community_player_match_records,
)
from src.fc27_demo import FC27DemoStateStore
from src.fc27_demo_api import create_demo_app
from src.fc27_pandas_view import FC27PandasView
from src.fc27_repository import FC27ReplayConflictError
from src.fc27_search_scout import FC27PrivacyViolationError

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _block_external_network_and_ea_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocks external network sockets and EA DNS resolution during pytest."""
    orig_getaddrinfo = socket.getaddrinfo

    def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        h_str = str(host or "").strip().lower()
        if h_str not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"Blocked external DNS resolution in pytest: {host!r}")
        return orig_getaddrinfo(host, *args, **kwargs)

    def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else ""
        if str(host).strip().lower() not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"Blocked external socket connection in pytest: {address!r}")
        raise AssertionError("No socket connections expected in community provider unit tests")

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    try:
        import pandas  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "pandas", _make_pandas_shim())


def _sample_community_identity_records() -> List[Dict[str, Any]]:
    return [
        {
            "club_id": "330323",
            "club_name": "East Coasters",
            "region_id": 4464981,
            "team_id": 112,
            "custom_kit": {"stadName": "East Coast Park"},
            "raw": {"clubId": 330323, "name": "East Coasters"},
        }
    ]


def _sample_community_match_records(count: int = 3) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for i in range(count):
        records.append(
            {
                "match_id": f"99000{i + 1}",
                "match_type": "leagueMatch",
                "timestamp": 1761800000 + i * 3600,
                "time_ago": {"number": i + 1, "unit": "hours"},
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
                        "club_id": "880100",
                        "club_name": "Harbor Athletic",
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
                        "player_id": "70001",
                        "player_name": "ShouldBeStrippedTag1",
                        "position": "ST",
                        "rating": 8.4,
                        "goals": 2,
                        "assists": 0,
                        "shots": 4,
                        "passes_made": 18,
                        "pass_attempts": 22,
                        "tackles_made": 1,
                        "tackle_attempts": 2,
                        "saves": 0,
                        "red_cards": 0,
                        "mom": 1,
                        "seconds_played": 5400,
                        "archetype_id": 3,
                        "event_mask": (1 << 1) | (1 << 4) | (1 << 16),
                        "raw": {"playername": "ShouldBeStrippedTag1"},
                    },
                    {
                        "club_id": "330323",
                        "player_id": "70002",
                        "player_name": "ShouldBeStrippedTag2",
                        "position": "CAM",
                        "rating": 7.9,
                        "goals": 0,
                        "assists": 2,
                        "shots": 1,
                        "passes_made": 25,
                        "pass_attempts": 28,
                        "tackles_made": 2,
                        "tackle_attempts": 3,
                        "saves": 0,
                        "red_cards": 0,
                        "mom": 0,
                        "seconds_played": 5400,
                        "archetype_id": 5,
                        "event_mask": (1 << 1) | (1 << 17),
                        "raw": {"playername": "ShouldBeStrippedTag2"},
                    },
                ],
                "aggregate": {"330323": {"passesmade": 43}, "880100": {"passesmade": 31}},
                "raw": {"matchId": f"99000{i + 1}"},
            }
        )
    return records


def _sample_player_match_records() -> List[Dict[str, Any]]:
    return [
        {
            "match_id": "990001",
            "match_type": "leagueMatch",
            "timestamp": 1761800000,
            "club_id": "330323",
            "club_name": "East Coasters",
            "player_id": "70001",
            "player_name": "PrivateGamerTagAlpha",
            "position": "ST",
            "rating": 8.4,
            "goals": 2,
            "assists": 1,
            "shots": 4,
            "passes_made": 18,
            "pass_attempts": 22,
            "tackles_made": 1,
            "tackle_attempts": 2,
            "saves": 0,
            "red_cards": 0,
            "mom": 1,
            "seconds_played": 5400,
            "archetype_id": 3,
            "event_mask": (1 << 1) | (1 << 4) | (1 << 16) | (1 << 17),
            "events": [
                {"event_id": 1, "value": 22},
                {"event_id": 4, "value": 4},
                {"event_id": 16, "value": 2},
                {"event_id": 17, "value": 1},
            ],
            "raw": {"playername": "PrivateGamerTagAlpha"},
        }
    ]


# 1. FC27CommunityProviderDeclaration immutability
def test_comm_01_declaration_immutability() -> None:
    decl = DEFAULT_COMMUNITY_PROVIDER_DECLARATION
    with pytest.raises(ValidationError):
        decl.club_id = "999999"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        FC27CommunityProviderDeclaration(extra_unknown="bad")  # type: ignore[call-arg]


# 2. upstream_commit required and non-empty
def test_comm_02_upstream_commit_required_and_pinned() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.upstream_commit == UPSTREAM_COMMIT_SHA
    assert len(DEFAULT_COMMUNITY_PROVIDER_DECLARATION.upstream_commit) == 40
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(upstream_commit="")
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(upstream_commit="deadbeef")


# 3. upstream_license == "MIT"
def test_comm_03_upstream_license_mit() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.upstream_license == "MIT"
    assert UPSTREAM_AUDIT_RECORD["UPSTREAM_LICENSE"] == UPSTREAM_LICENSE
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(upstream_license="GPL-3.0")


# 4. source_classification == "LIVE_EA_CLUBS_COMMUNITY_CLIENT"
def test_comm_04_source_classification() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.source_classification == LIVE_EA_CLUBS_COMMUNITY_CLIENT
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(source_classification="SYNTHETIC_FIXTURE")


# 5. club_id == "330323"
def test_comm_05_club_id_locked() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.club_id == "330323"
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(club_id="123456")


# 6. club_name == "East Coasters"
def test_comm_06_club_name_locked() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.club_name == "East Coasters"
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(club_name="Other Club")


# 7. platform == "common-gen5"
def test_comm_07_platform_locked() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.platform == "common-gen5"
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(platform="common-gen4")


# 8. game_version == "FC27"
def test_comm_08_game_version_locked() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.game_version == "FC27"
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(game_version="FC26")


# 9. output_format == "records"
def test_comm_09_output_format_records() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.output_format == "records"


# 10. raw_response_retention == False
def test_comm_10_raw_response_retention_false() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.raw_response_retention is False
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(raw_response_retention=True)


# 11. maximum_identity_requests == 1
def test_comm_11_maximum_identity_requests_one() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.maximum_identity_requests == 1
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(maximum_identity_requests=2)


# 12. maximum_match_requests == 1
def test_comm_12_maximum_match_requests_one() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.maximum_match_requests == 1
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(maximum_match_requests=2)


# 13. maximum_matches <= 10
def test_comm_13_maximum_matches_bound() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.maximum_matches == 10
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(maximum_matches=11)


# 14. maximum_retries == 0
def test_comm_14_maximum_retries_zero() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.maximum_retries == 0
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(maximum_retries=1)


# 15. follow_redirects == False
def test_comm_15_follow_redirects_false() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.follow_redirects is False
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(follow_redirects=True)


# 16. persistence_enabled == False
def test_comm_16_persistence_enabled_false() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.persistence_enabled is False
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(persistence_enabled=True)


# 17. polling_enabled == False
def test_comm_17_polling_enabled_false() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.polling_enabled is False
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(polling_enabled=True)


# 18. recommendations_enabled == False
def test_comm_18_recommendations_enabled_false() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.recommendations_enabled is False
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(recommendations_enabled=True)


# 19. dispatch_enabled == False
def test_comm_19_dispatch_enabled_false() -> None:
    assert DEFAULT_COMMUNITY_PROVIDER_DECLARATION.dispatch_enabled is False
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(dispatch_enabled=True)


# 20. get_json is not exposed by the repository wrapper
def test_comm_20_get_json_not_exposed() -> None:
    adapter = FC27CommunityProviderAdapter(provider_enabled=True, confirm=True)
    assert not hasattr(adapter, "get_json")


# 21. leaderboard search is not exposed by the one-shot wrapper
def test_comm_21_leaderboard_search_not_exposed() -> None:
    adapter = FC27CommunityProviderAdapter(provider_enabled=True, confirm=True)
    assert not hasattr(adapter, "all_time_leaderboard")
    assert not hasattr(adapter, "current_season_leaderboard")


# 22. member stats are not exposed by the one-shot wrapper
def test_comm_22_member_stats_not_exposed() -> None:
    adapter = FC27CommunityProviderAdapter(provider_enabled=True, confirm=True)
    assert not hasattr(adapter, "member_stats")
    assert not hasattr(adapter, "member_career_stats")


# 23. player_matches live execution is not exposed by the one-shot wrapper
def test_comm_23_player_matches_live_not_exposed() -> None:
    adapter = FC27CommunityProviderAdapter(provider_enabled=True, confirm=True)
    assert not hasattr(adapter, "player_matches")
    assert PLAYER_MATCH_LIVE_REQUEST_EXECUTED is False


# 24. wrong club_id rejected before call
def test_comm_24_wrong_club_id_rejected_before_call() -> None:
    called = False

    def _mock_id() -> List[Dict[str, Any]]:
        nonlocal called
        called = True
        return _sample_community_identity_records()

    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_mock_id,
    )
    with pytest.raises(FC27ContractError):
        adapter.get_east_coasters_identity_once(club_id="999999")
    assert called is False
    assert adapter.identity_requests_executed == 0


# 25. wrong platform rejected before call
def test_comm_25_wrong_platform_rejected_before_call() -> None:
    called = False

    def _mock_id() -> List[Dict[str, Any]]:
        nonlocal called
        called = True
        return _sample_community_identity_records()

    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_mock_id,
    )
    with pytest.raises(FC27ContractError):
        adapter.get_east_coasters_identity_once(platform="common-gen4")
    assert called is False


# 26. invalid match_type rejected before call
def test_comm_26_invalid_match_type_rejected_before_call() -> None:
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_sample_community_identity_records,
        mock_matches_callable=lambda mt, c: _sample_community_match_records(c),
    )
    id_env = adapter.get_east_coasters_identity_once()
    with pytest.raises(FC27ContractError):
        adapter.get_east_coasters_recent_matches_once(
            match_type="invalidMatchType",
            identity_envelope=id_env,
        )
    assert adapter.match_requests_executed == 0


# 27. count < 1 rejected before call
def test_comm_27_count_less_than_one_rejected() -> None:
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_sample_community_identity_records,
    )
    id_env = adapter.get_east_coasters_identity_once()
    with pytest.raises(FC27ContractError):
        adapter.get_east_coasters_recent_matches_once(count=0, identity_envelope=id_env)
    assert adapter.match_requests_executed == 0


# 28. count > 10 rejected before call
def test_comm_28_count_greater_than_ten_rejected() -> None:
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_sample_community_identity_records,
    )
    id_env = adapter.get_east_coasters_identity_once()
    with pytest.raises(FC27ContractError):
        adapter.get_east_coasters_recent_matches_once(count=11, identity_envelope=id_env)
    assert adapter.match_requests_executed == 0


# 29. output="raw" rejected for ingestion
def test_comm_29_output_raw_rejected() -> None:
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(output_format="raw")
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_sample_community_identity_records,
    )
    with pytest.raises(FC27ContractError):
        adapter.get_east_coasters_identity_once(output="raw")


# 30. output="dataframe" rejected for core ingestion
def test_comm_30_output_dataframe_rejected() -> None:
    with pytest.raises(FC27ContractError):
        FC27CommunityProviderDeclaration(output_format="dataframe")
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_sample_community_identity_records,
    )
    with pytest.raises(FC27ContractError):
        adapter.get_east_coasters_identity_once(output="dataframe")


# 31. mock clubs_info valid East Coasters record accepted
def test_comm_31_mock_clubs_info_valid_accepted() -> None:
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_sample_community_identity_records,
    )
    env = adapter.get_east_coasters_identity_once()
    assert env.schema_status == "PASS"
    assert env.normalization_status == "PASS"
    assert env.privacy_status == "PASS"
    assert env.normalized_records[0]["club_id"] == "330323"
    assert env.normalized_records[0]["club_name"] == "East Coasters"
    assert "raw" not in env.normalized_records[0]


# 32. mock clubs_info empty record list rejected
def test_comm_32_mock_clubs_info_empty_rejected() -> None:
    with pytest.raises(FC27SchemaValidationError):
        normalize_community_identity_records([])


# 33. mock clubs_info wrong club_id rejected
def test_comm_33_mock_clubs_info_wrong_club_id_rejected() -> None:
    with pytest.raises(FC27SchemaValidationError):
        normalize_community_identity_records(
            [{"club_id": "999999", "club_name": "East Coasters"}]
        )


# 34. mock clubs_info wrong club_name rejected
def test_comm_34_mock_clubs_info_wrong_club_name_rejected() -> None:
    with pytest.raises(FC27SchemaValidationError):
        normalize_community_identity_records(
            [{"club_id": "330323", "club_name": "West Coasters"}]
        )


# 35. mock clubs_matches valid East Coasters records accepted
def test_comm_35_mock_clubs_matches_valid_accepted() -> None:
    adapter = FC27CommunityProviderAdapter(
        provider_enabled=True,
        confirm=True,
        mock_identity_callable=_sample_community_identity_records,
        mock_matches_callable=lambda mt, c: _sample_community_match_records(3),
    )
    id_env = adapter.get_east_coasters_identity_once()
    m_env = adapter.get_east_coasters_recent_matches_once(identity_envelope=id_env)
    assert m_env.schema_status == "PASS"
    assert m_env.accepted_record_count == 3
    assert len(m_env.record_hashes) == 3


# 36. mock clubs_matches missing East Coasters rejected
def test_comm_36_mock_clubs_matches_missing_east_coasters_rejected() -> None:
    bad_records = _sample_community_match_records(1)
    bad_records[0]["clubs"][0]["club_id"] = "777777"
    with pytest.raises(FC27SchemaValidationError):
        normalize_community_match_records(bad_records)


# 37. mock clubs_matches duplicate match deduplicated
def test_comm_37_mock_clubs_matches_duplicate_deduplicated() -> None:
    recs = _sample_community_match_records(2)
    dup_recs = [recs[0], recs[0], recs[1]]
    env, _ = normalize_community_match_records(dup_recs)
    assert env.source_record_count == 3
    assert env.accepted_record_count == 2
    assert env.rejected_record_count == 1


# 38. mock clubs_matches conflicting duplicate match rejected
def test_comm_38_mock_clubs_matches_conflicting_duplicate_rejected() -> None:
    recs = _sample_community_match_records(1)
    conflict = _sample_community_match_records(1)[0]
    conflict["clubs"][0]["goals"] = 9
    with pytest.raises(FC27ReplayConflictError):
        normalize_community_match_records([recs[0], conflict])


# 39. Unix timestamp converted to UTC ISO-8601
def test_comm_39_unix_timestamp_converted_to_utc_iso8601() -> None:
    recs = _sample_community_match_records(1)
    recs[0]["timestamp"] = 1761800000
    env, _ = normalize_community_match_records(recs)
    expected_iso = datetime.fromtimestamp(1761800000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert env.normalized_records[0]["played_at"] == expected_iso


# 40. invalid timestamp rejected
def test_comm_40_invalid_timestamp_rejected() -> None:
    recs = _sample_community_match_records(1)
    recs[0]["timestamp"] = "not-a-timestamp"
    with pytest.raises(FC27SchemaValidationError):
        normalize_community_match_records(recs)


# 41. player display names stripped or pseudonymized
def test_comm_41_player_display_names_stripped() -> None:
    env, _ = normalize_community_match_records(_sample_community_match_records(1))
    dumped = env.model_dump_json()
    assert "ShouldBeStrippedTag1" not in dumped
    assert "ShouldBeStrippedTag2" not in dumped
    assert "player_name" not in dumped
    assert "playername" not in dumped


# 42. player IDs mapped to deterministic plr_ecf_<12-hex>
def test_comm_42_player_ids_mapped_to_deterministic_pseudonyms() -> None:
    env1, _ = normalize_community_match_records(_sample_community_match_records(1))
    env2, _ = normalize_community_match_records(_sample_community_match_records(1))
    p1 = env1.normalized_records[0]["player_statistics"][0]["player_ref"]
    p2 = env2.normalized_records[0]["player_statistics"][0]["player_ref"]
    assert p1 == p2
    assert p1.startswith("plr_ecf_")
    assert len(p1) == 20


# 43. pseudonym collision detection preserved
def test_comm_43_pseudonym_collision_detection_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.fc27_clubs_api_contracts as contracts_mod

    monkeypatch.setattr(
        contracts_mod,
        "pseudonymize_east_coasters_identifier",
        lambda raw_id, club_id="330323": "plr_ecf_000000000000",
    )
    detector = FC27PseudonymCollisionDetector()
    detector.resolve("70001")
    with pytest.raises(contracts_mod.FC27PseudonymCollisionError):
        detector.resolve("70002")


# 44. named player totals remain authoritative
def test_comm_44_named_player_totals_authoritative() -> None:
    res = apply_community_event_policy(
        named_totals={"goals": 2, "assists": 1, "shots": 4, "pass_attempts": 20},
        ev_mask=(1 << 16),
        upstream_events=[{"event_id": 16, "value": 99}],
    )
    assert res["primary_authority"] == "NAMED_PLAYER_TOTALS"
    assert res["authoritative_totals"]["goals"] == 2
    assert res["community_high_confidence_pending_review"]["total_goals"] == 99
    assert res["event_reconciliation_warning"] is True
    assert "goals" in res["discrepant_fields"]


# 45. community partial event mappings do not override authoritative totals
def test_comm_45_partial_event_mappings_not_enabled() -> None:
    assert classify_community_event_id(0) == "COMMUNITY_PARTIAL_NOT_ENABLED"
    assert classify_community_event_id(14) == "COMMUNITY_PARTIAL_NOT_ENABLED"
    res = apply_community_event_policy(
        named_totals={"goals": 1, "saves": 3},
        ev_mask=(1 << 0) | (1 << 14),
        upstream_events=[{"event_id": 0, "value": 10}, {"event_id": 14, "value": 10}],
    )
    assert res["authoritative_totals"]["goals"] == 1
    assert res["authoritative_totals"]["saves"] == 3
    assert res["community_partial_not_enabled_ids"] == [0, 14]


# 46. community exploratory event mappings do not override authoritative totals
def test_comm_46_exploratory_event_mappings_not_enabled() -> None:
    assert classify_community_event_id(6) == "COMMUNITY_EXPLORATORY_NOT_ENABLED"
    assert classify_community_event_id(22) == "COMMUNITY_EXPLORATORY_NOT_ENABLED"
    res = apply_community_event_policy(
        named_totals={"goals": 1},
        ev_mask=(1 << 6) | (1 << 22),
        upstream_events=[{"event_id": 6, "value": 5}, {"event_id": 22, "value": 8}],
    )
    assert res["authoritative_totals"]["goals"] == 1
    assert res["community_exploratory_not_enabled_ids"] == [6, 22]


# 47. unknown event bits preserved as unknown
def test_comm_47_unknown_event_bits_preserved() -> None:
    assert classify_community_event_id(24) == "UNKNOWN"
    res = apply_community_event_policy(
        named_totals={"goals": 1},
        ev_mask=(1 << 24) | (1 << 26),
    )
    assert res["unknown_event_bits_mask"] == ((1 << 24) | (1 << 26))
    assert 24 in res["unknown_event_ids"]
    assert 26 in res["unknown_event_ids"]


# 48. offline player_matches record validator accepts valid mock rows
def test_comm_48_offline_player_matches_validator_accepts_valid() -> None:
    env = validate_community_player_match_records(_sample_player_match_records())
    assert env.operation == "PLAYER_MATCH_RECORDS_OFFLINE"
    assert env.schema_status == "PASS"
    assert env.accepted_record_count == 1
    assert env.normalized_records[0]["player_ref"].startswith("plr_ecf_")


# 49. offline player_matches record validator strips player_name
def test_comm_49_offline_player_matches_validator_strips_player_name() -> None:
    env = validate_community_player_match_records(_sample_player_match_records())
    dumped = env.model_dump_json()
    assert "PrivateGamerTagAlpha" not in dumped
    assert "player_name" not in dumped
    assert "player_id" not in dumped


# 50. offline player_matches record validator rejects wrong club_id
def test_comm_50_offline_player_matches_validator_rejects_wrong_club_id() -> None:
    bad = _sample_player_match_records()
    bad[0]["club_id"] = "888888"
    with pytest.raises(FC27SchemaValidationError):
        validate_community_player_match_records(bad)


# 51. FC27PandasView works from normalized records
def test_comm_51_pandas_view_works_from_normalized_records() -> None:
    env, _ = normalize_community_match_records(_sample_community_match_records(2))
    view = FC27PandasView()
    df_matches = view.recent_matches_dataframe(env.normalized_records)
    df_players = view.player_contributions_dataframe(env.normalized_records)
    assert len(df_matches) == 2
    assert len(df_players) == 4


# 52. FC27PandasView is not required by core ingestion
def test_comm_52_pandas_view_not_required_by_core_ingestion() -> None:
    src_text = (REPO_ROOT / "src" / "fc27_community_provider.py").read_text(encoding="utf-8")
    assert "import pandas" not in src_text
    assert "fc27_pandas_view" not in src_text


# 53. sanitized CSV export escapes formula-injection prefixes
def test_comm_53_sanitized_csv_escapes_formula_prefixes() -> None:
    env, _ = normalize_community_match_records(_sample_community_match_records(1))
    rec = dict(env.normalized_records[0])
    rec["opponent_name"] = "=CMD|' /C calc'!A0"
    view = FC27PandasView()
    csv_out = view.to_csv([rec], view="recent_matches")
    assert "'=CMD" in csv_out


# 54. sanitized Excel export works without player names or raw responses
def test_comm_54_sanitized_excel_export_clean() -> None:
    env, _ = normalize_community_match_records(_sample_community_match_records(1))
    view = FC27PandasView()
    xls_bytes = view.to_excel(env.normalized_records, view="player_contributions")
    assert xls_bytes.startswith((b"<?xml", b"PK\x03\x04"))
    assert b"ShouldBeStrippedTag1" not in xls_bytes


# 55. Search -> Scout -> Analyst works from normalized community records
def test_comm_55_search_scout_analyst_from_normalized_community_records() -> None:
    env, match_envs = normalize_community_match_records(
        _sample_community_match_records(3),
        observed_at="2026-10-31T12:00:00Z",
    )
    summary, s_ctx, scout_rep, analyst_rep = ingest_community_provider_envelope(
        env,
        match_envelopes=match_envs,
        persist=False,
        analysis_generated_at=datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert summary.search_executed == "YES"
    assert summary.scout_executed == "YES"
    assert summary.analyst_executed == "YES"
    assert s_ctx.eligible_match_count == 3
    assert len(scout_rep.report_hash) == 64
    assert len(analyst_rep.report_hash) == 64


# 56. Coach/Tactics/Director/recommendations/dispatch are not called
def test_comm_56_downstream_coaching_and_dispatch_not_called() -> None:
    env, match_envs = normalize_community_match_records(_sample_community_match_records(3))
    summary, _, _, _ = ingest_community_provider_envelope(
        env,
        match_envelopes=match_envs,
        persist=False,
    )
    assert summary.coach_executed == "NO"
    assert summary.tactics_executed == "NO"
    assert summary.director_executed == "NO"
    assert summary.recommendations_enabled == "NO"
    assert summary.dispatch_enabled == "NO"

    store = FC27DemoStateStore()
    artifacts = build_demo_session_artifacts_from_community_envelope(env)
    store.set_artifacts(artifacts)
    app = create_demo_app(state_store=store, demo_mode_override=True)
    client = TestClient(app)
    resp = client.get("/demo/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_mode"] == LIVE_EA_CLUBS_COMMUNITY_CLIENT
    assert body["synthetic_data_classification"] == LIVE_EA_CLUBS_COMMUNITY_CLIENT
    assert body["recommendations_enabled"] is False
    assert body["dispatch_enabled"] is False


# 57. container compose configuration mounts no secrets or home directories
def test_comm_57_container_compose_isolation() -> None:
    compose_path = REPO_ROOT / "docker-compose.community-eval.yml"
    dockerfile_path = REPO_ROOT / "Dockerfile.community-eval"
    assert compose_path.is_file()
    assert dockerfile_path.is_file()
    df_text = dockerfile_path.read_text(encoding="utf-8")
    assert "@sha256:" in df_text
    assert UPSTREAM_COMMIT_SHA in df_text
    assert "USER 10001:65534" in df_text

    cfg = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    svc = cfg["services"]["fc27-community-client-evaluation"]
    assert svc["read_only"] is True
    assert svc["restart"] == "no"
    assert "no-new-privileges:true" in svc["security_opt"]
    assert "ALL" in svc["cap_drop"]
    assert "secrets" not in svc
    assert "volumes" not in svc
    assert "ports" not in svc
    env = svc.get("environment") or {}
    for forbidden in (
        "POSTGRES_URI",
        "POSTGRES_URI_FILE",
        "APPROVAL_SIGNING_SECRET",
        "GRAPH_SERVICE_TOKEN",
        "GRAPH_COMMISSIONER_TOKEN_SHA256",
        "MCP_API_TOKEN",
        "WS_AUTH_TOKEN",
        "LANGFUSE_SECRET_KEY",
    ):
        assert forbidden not in env


# 58. pytest performs zero live calls and zero EA DNS lookups
def test_comm_58_pytest_zero_live_calls_and_zero_ea_dns() -> None:
    adapter = FC27CommunityProviderAdapter(provider_enabled=True, confirm=True)
    with pytest.raises(Exception):
        adapter.get_east_coasters_identity_once()
    with pytest.raises(AssertionError, match="Blocked external DNS resolution"):
        socket.getaddrinfo("proclubs.ea.com", 443)
    with pytest.raises(FC27PrivacyViolationError):
        FC27CommunityProviderEnvelope(
            identity={
                "club_id": "330323",
                "club_name": "East Coasters",
                "platform": "common-gen5",
                "game_version": "FC27",
            },
            operation="CLUB_IDENTITY",
            normalized_records=({"club_id": "330323", "player_name": "Leaked"},),
            record_hashes=("a" * 64,),
            payload_hash="a" * 64,
            provenance={
                "provider_id": COMMUNITY_PROVIDER_ID,
                "source_classification": LIVE_EA_CLUBS_COMMUNITY_CLIENT,
                "raw_response_retained": "NO",
            },
            observed_at="2026-10-31T12:00:00Z",
            source_record_count=1,
            accepted_record_count=1,
            rejected_record_count=0,
            schema_status="PASS",
            normalization_status="PASS",
            privacy_status="PASS",
        )
