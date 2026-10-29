"""Tests for FC 27 Clubs API endpoint contracts, schemas, normalization, and match event decoding."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
import urllib.request

import pytest
from pydantic import ValidationError

from src.fc27_authorized_transport import parse_json_strict_no_duplicates
from src.fc27_clubs_api_contracts import (
    ALL_CLUBS_ENDPOINT_OPERATIONS,
    ALL_RESPONSE_SCHEMA_MODELS,
    ENDPOINT_CONTRACT_VERSION,
    EVENT_MAPPING_VERSION,
    FC27_ENDPOINT_CONTRACTS,
    FC27_KNOWN_EVENT_BIT_MAP,
    NORMALIZATION_VERSION,
    RESPONSE_SCHEMA_VERSION,
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
    FC27AggregateMatchStatisticsSchema,
    FC27AssetAndCrestMetadataSchema,
    FC27CareerMemberStatisticsSchema,
    FC27ClubAggregateStatisticsSchema,
    FC27ClubIdentitySchema,
    FC27ClubSearchResultSchema,
    FC27ClubSideMatchRecordSchema,
    FC27ClubsEndpointOperation,
    FC27ContractError,
    FC27CurrentMemberStatisticsSchema,
    FC27LeaderboardEntrySchema,
    FC27MatchEnvelopeSchema,
    FC27MatchEventBitmapSchema,
    FC27PlayerMatchStatisticsSchema,
    FC27PlayoffAchievementSchema,
    FC27PseudonymCollisionDetector,
    FC27PseudonymCollisionError,
    FC27SchemaValidationError,
    build_east_coasters_endpoint_request,
    decode_fc27_match_events,
    enumerate_event_bits,
    normalize_asset_and_crest_metadata,
    normalize_club_identity_payload,
    normalize_club_statistics_payload,
    normalize_fc27_match_type,
    normalize_matches_payload,
    pseudonymize_east_coasters_identifier,
    validate_aggregate_event_counts,
    validate_event_bitmap,
    verify_and_normalize_club_search_payload,
)
from src.fc27_provider import FC27TransportError


# ============================================================================
# 1. Endpoint Contract Tests (11 fixed operations & parameter validation)
# ============================================================================


def test_01_all_eleven_fixed_operations_defined() -> None:
    assert len(ALL_CLUBS_ENDPOINT_OPERATIONS) == 11
    assert len(FC27_ENDPOINT_CONTRACTS) == 11
    expected_ops = {
        "CLUB_SEARCH",
        "CLUB_IDENTITY",
        "CLUB_STATISTICS",
        "RECENT_LEAGUE_MATCHES",
        "RECENT_PLAYOFF_MATCHES",
        "MATCH_PLAYER_STATISTICS",
        "CURRENT_MEMBER_STATISTICS",
        "MEMBER_CAREER_STATISTICS",
        "PLAYOFF_ACHIEVEMENTS",
        "CURRENT_SEASON_LEADERBOARD",
        "ALL_TIME_LEADERBOARD",
    }
    assert set(ALL_CLUBS_ENDPOINT_OPERATIONS) == expected_ops


def test_02_each_operation_maps_to_exact_path_and_query_allowlist() -> None:
    c_search = FC27_ENDPOINT_CONTRACTS[FC27ClubsEndpointOperation.CLUB_SEARCH]
    assert c_search.path_template == "/api/fc/allTimeLeaderboard/search"
    assert c_search.required_query_parameters == ("platform", "clubName")

    c_info = FC27_ENDPOINT_CONTRACTS[FC27ClubsEndpointOperation.CLUB_IDENTITY]
    assert c_info.path_template == "/api/fc/clubs/info"
    assert c_info.required_query_parameters == ("platform", "clubIds")

    c_stats = FC27_ENDPOINT_CONTRACTS[FC27ClubsEndpointOperation.CLUB_STATISTICS]
    assert c_stats.path_template == "/api/fc/clubs/overallStats"
    assert c_stats.required_query_parameters == ("platform", "clubIds")

    c_league = FC27_ENDPOINT_CONTRACTS[FC27ClubsEndpointOperation.RECENT_LEAGUE_MATCHES]
    assert c_league.path_template == "/api/fc/clubs/matches"
    assert c_league.fixed_query_parameters == (("matchType", "leagueMatch"),)

    c_playoff = FC27_ENDPOINT_CONTRACTS[FC27ClubsEndpointOperation.RECENT_PLAYOFF_MATCHES]
    assert c_playoff.path_template == "/api/fc/clubs/matches"
    assert c_playoff.fixed_query_parameters == (("matchType", "playoffMatch"),)

    c_mem = FC27_ENDPOINT_CONTRACTS[FC27ClubsEndpointOperation.CURRENT_MEMBER_STATISTICS]
    assert c_mem.path_template == "/api/fc/members/stats"
    assert c_mem.personal_data_classification == "CONTAINS_PSEUDONYMIZED_MEMBER_DATA"

    c_career = FC27_ENDPOINT_CONTRACTS[FC27ClubsEndpointOperation.MEMBER_CAREER_STATISTICS]
    assert c_career.path_template == "/api/fc/members/career/stats"
    assert c_career.personal_data_classification == "CONTAINS_PSEUDONYMIZED_MEMBER_DATA"

    c_po = FC27_ENDPOINT_CONTRACTS[FC27ClubsEndpointOperation.PLAYOFF_ACHIEVEMENTS]
    assert c_po.path_template == "/api/fc/club/playoffAchievements"


def test_03_caller_supplied_arbitrary_url_rejected() -> None:
    with pytest.raises(FC27ContractError, match="prohibited"):
        build_east_coasters_endpoint_request(
            FC27ClubsEndpointOperation.CLUB_IDENTITY,
            url="https://proclubs.ea.com/api/fc/clubs/info",
        )


def test_04_caller_supplied_host_rejected() -> None:
    with pytest.raises(FC27ContractError, match="prohibited"):
        build_east_coasters_endpoint_request(
            FC27ClubsEndpointOperation.CLUB_IDENTITY,
            host="proclubs.ea.com",
        )


def test_05_caller_supplied_path_override_rejected() -> None:
    with pytest.raises(FC27ContractError, match="prohibited"):
        build_east_coasters_endpoint_request(
            FC27ClubsEndpointOperation.CLUB_IDENTITY,
            path="/api/fc/admin",
        )


def test_06_unknown_operation_rejected() -> None:
    with pytest.raises(FC27ContractError, match="Unknown or unallowlisted operation"):
        build_east_coasters_endpoint_request("ARBITRARY_CUSTOM_ENDPOINT")


def test_07_non_east_coasters_club_id_rejected() -> None:
    with pytest.raises(FC27ContractError, match="club_id='330323'"):
        build_east_coasters_endpoint_request(
            FC27ClubsEndpointOperation.CLUB_IDENTITY,
            club_id="999999",
        )


def test_08_non_common_gen5_platform_rejected() -> None:
    with pytest.raises(FC27ContractError, match="platform='common-gen5'"):
        build_east_coasters_endpoint_request(
            FC27ClubsEndpointOperation.CLUB_IDENTITY,
            platform="common-gen4",
        )


def test_09_non_fc27_game_version_rejected() -> None:
    with pytest.raises(FC27ContractError, match="game_version='FC27'"):
        build_east_coasters_endpoint_request(
            FC27ClubsEndpointOperation.CLUB_IDENTITY,
            game_version="FC26",
        )


def test_10_club_search_mismatch_rejected() -> None:
    payload = [
        {"clubId": "111111", "clubName": "East Coasters", "platform": "common-gen5"},
        {"clubId": "222222", "clubName": "Other FC", "platform": "common-gen5"},
    ]
    res = verify_and_normalize_club_search_payload(payload)
    assert res.status == "IDENTITY_CONFLICT"
    assert res.can_continue is False


def test_11_exact_east_coasters_match_accepted() -> None:
    req = build_east_coasters_endpoint_request(
        FC27ClubsEndpointOperation.RECENT_LEAGUE_MATCHES,
        query_overrides={"maxResultCount": "5"},
    )
    assert req.exact_path == "/api/fc/clubs/matches"
    assert dict(req.query_parameters) == {
        "platform": "common-gen5",
        "clubIds": "330323",
        "matchType": "leagueMatch",
        "maxResultCount": "5",
    }

    search_payload = [
        {"clubId": "111111", "clubName": "Other Club", "platform": "common-gen5"},
        {
            "clubId": "330323",
            "clubName": "East Coasters",
            "platform": "common-gen5",
            "wins": 42,
            "losses": 10,
            "ties": 5,
            "gamesPlayed": 57,
        },
    ]
    res = verify_and_normalize_club_search_payload(search_payload)
    assert res.status == "VERIFIED_PROVISIONAL"
    assert res.can_continue is True
    assert res.matched_results[0].club_id == TARGET_CLUB_ID
    assert res.matched_results[0].club_name == TARGET_CLUB_NAME


# ============================================================================
# 2. Schema and Normalization Tests (13 response models & strict validation)
# ============================================================================


def test_12_valid_club_identity_payload_normalizes_cleanly() -> None:
    raw = {
        "330323": {
            "clubId": "330323",
            "name": "East Coasters",
            "platform": "common-gen5",
            "regionId": 4545109,
            "customKit": {"stadName": "East Coast Park", "crestAssetId": 12345},
        }
    }
    schema, domain = normalize_club_identity_payload(
        raw,
        provider_id="ea-authorized-clubs-partner",
        authorization_reference="EA-AUTH-2026-01",
        payload_sha256="a" * 64,
    )
    assert schema.club_id == "330323"
    assert schema.club_name == "East Coasters"
    assert schema.asset_metadata.stadium_name == "East Coast Park"
    assert domain.club_id == "330323"


def test_13_valid_club_statistics_payload_normalizes_cleanly() -> None:
    raw = {
        "330323": {
            "gamesPlayed": 20,
            "wins": 12,
            "ties": 4,
            "losses": 4,
            "goals": 45,
            "goalsAgainst": 21,
            "skillRating": 1850,
            "bestDivision": 2,
        }
    }
    schema, supplied = normalize_club_statistics_payload(raw)
    assert schema.games_played == 20
    assert supplied.matches_played == 20
    assert supplied.wins == 12
    assert supplied.draws == 4
    assert supplied.losses == 4
    assert supplied.goals_for == 45
    assert supplied.goals_against == 21


def test_14_valid_match_list_payload_normalizes_cleanly() -> None:
    raw = [
        {
            "matchId": "9001001",
            "timestamp": 1761500000,
            "matchType": "gameType9",
            "clubs": {
                "330323": {
                    "details": {"clubId": 330323, "name": "East Coasters"},
                    "goals": "3",
                    "goalsAgainst": "1",
                    "wins": "1",
                    "ties": "0",
                    "losses": "0",
                    "winnerByDnf": "0",
                },
                "888888": {
                    "details": {"clubId": 888888, "name": "Harbor City"},
                    "goals": "1",
                    "goalsAgainst": "3",
                    "wins": "0",
                    "ties": "0",
                    "losses": "1",
                },
            },
            "players": {
                "330323": {
                    "player_alpha": {
                        "name": "SecretGamerTag1",
                        "pos": "midfielder",
                        "rating": "8.4",
                        "goals": "2",
                        "assists": "1",
                        "shots": "4",
                        "passesCompleted": "22",
                        "passesAttempted": "25",
                        "tacklesWon": "3",
                        "tacklesAttempted": "4",
                        "saves": "0",
                        "minutesPlayed": "90",
                        "eventBitmap": (1 << 0) | (1 << 1) | (1 << 6),
                    }
                }
            },
        }
    ]
    envelopes, supplied_matches, drift = normalize_matches_payload(
        raw,
        default_match_type="gameType9",
        include_player_performances=True,
    )
    assert len(envelopes) == 1
    assert envelopes[0].match_id == "9001001"
    assert envelopes[0].normalized_match_type == "LEAGUE"
    assert envelopes[0].result == "WIN"
    assert len(envelopes[0].player_statistics) == 1
    p_stat = envelopes[0].player_statistics[0]
    assert p_stat.local_player_reference.startswith("plr_ecf_")
    assert p_stat.event_summary is not None
    assert "GOAL_SCORED" in p_stat.event_summary.known_event_codes
    assert len(supplied_matches) == 1
    assert drift == ()


def test_15_valid_member_career_statistics_payload_normalizes_cleanly() -> None:
    item = FC27CareerMemberStatisticsSchema(
        local_player_reference="plr_ecf_0123456789ab",
        favorite_position="ST",
        games_played=50,
        goals=35,
        assists=18,
        man_of_the_match=12,
        average_rating=8.2,
    )
    assert item.games_played == 50
    assert item.goals == 35


def test_16_valid_member_match_statistics_payload_normalizes_cleanly() -> None:
    item = FC27CurrentMemberStatisticsSchema(
        local_player_reference="plr_ecf_0123456789ab",
        favorite_position="CM",
        games_played=25,
        goals=19,
        assists=11,
        pass_success_pct=88.5,
        tackle_success_pct=75.0,
    )
    assert item.games_played == 25
    assert item.pass_success_pct == 88.5


def test_17_valid_playoff_achievements_payload_normalizes_cleanly() -> None:
    item = FC27PlayoffAchievementSchema(
        season_id="10",
        season_name="Season 10",
        best_division=1,
        playoff_wins=10,
        playoff_losses=2,
        finishing_position=3,
    )
    assert item.season_id == "10"
    assert item.best_division == 1


def test_18_valid_club_overall_stats_payload_normalizes_cleanly() -> None:
    item = FC27ClubAggregateStatisticsSchema(
        club_id="330323",
        games_played=100,
        wins=65,
        draws=15,
        losses=20,
        goals_for=210,
        goals_against=95,
        best_division=1,
    )
    assert item.club_id == "330323"
    assert item.wins == 65


def test_19_valid_leaderboard_payload_normalizes_cleanly() -> None:
    entry = FC27LeaderboardEntrySchema(
        rank=12,
        club_id="330323",
        club_name="East Coasters",
        platform="common-gen5",
        skill_rating=2150,
        wins=80,
        losses=15,
        draws=10,
    )
    assert entry.rank == 12


def test_20_missing_required_field_fails_closed() -> None:
    with pytest.raises(FC27SchemaValidationError, match="Missing required fields"):
        normalize_club_statistics_payload({"330323": {"gamesPlayed": 10, "wins": 5}})


def test_21_unexpected_field_fails_closed_under_strict_and_records_drift() -> None:
    with pytest.raises(ValidationError):
        FC27ClubIdentitySchema(
            club_id="330323",
            club_name="East Coasters",
            platform="common-gen5",
            unexpectedExtraField="forbidden",  # type: ignore[call-arg]
        )

    raw_with_drift = {
        "330323": {
            "clubId": "330323",
            "name": "East Coasters",
            "platform": "common-gen5",
            "newUpstreamField": "some_val",
        }
    }
    schema, _ = normalize_club_identity_payload(
        raw_with_drift,
        provider_id="ea-authorized-clubs-partner",
        authorization_reference="EA-AUTH-01",
        payload_sha256="b" * 64,
    )
    assert "newUpstreamField" in schema.extra_fields


def test_22_duplicate_json_key_fails_closed() -> None:
    raw_json_bytes = b'{"clubId": "330323", "clubId": "999999", "name": "East Coasters"}'
    with pytest.raises(FC27TransportError, match="Duplicate JSON key"):
        parse_json_strict_no_duplicates(raw_json_bytes)


def test_23_nan_and_infinity_fail_closed() -> None:
    with pytest.raises(ValidationError):
        FC27PlayerMatchStatisticsSchema(
            local_player_reference="plr_ecf_0123456789ab",
            position="CM",
            rating=float("nan"),
        )
    with pytest.raises(ValidationError):
        FC27PlayerMatchStatisticsSchema(
            local_player_reference="plr_ecf_0123456789ab",
            position="CM",
            rating=float("inf"),
        )
    with pytest.raises(FC27TransportError):
        parse_json_strict_no_duplicates(b'{"rating": NaN}')


def test_24_negative_count_or_duration_fails_closed() -> None:
    with pytest.raises(ValidationError):
        FC27PlayerMatchStatisticsSchema(
            local_player_reference="plr_ecf_0123456789ab",
            position="CM",
            goals=-1,
        )
    with pytest.raises(ValidationError):
        FC27PlayerMatchStatisticsSchema(
            local_player_reference="plr_ecf_0123456789ab",
            position="CM",
            minutes_played=-5,
        )


def test_25_score_result_inconsistency_fails_closed() -> None:
    raw = [
        {
            "matchId": "9001002",
            "timestamp": 1761500000,
            "result": "WIN",
            "goalsFor": 1,
            "goalsAgainst": 3,
            "opponentName": "Other FC",
        }
    ]
    with pytest.raises(FC27SchemaValidationError, match="inconsistent with declared result"):
        normalize_matches_payload(raw)


def test_26_completed_exceeds_attempted_statistic_fails_closed() -> None:
    with pytest.raises(FC27SchemaValidationError, match="passes_completed cannot exceed passes_attempted"):
        FC27PlayerMatchStatisticsSchema(
            local_player_reference="plr_ecf_0123456789ab",
            position="CM",
            passes_completed=30,
            passes_attempted=20,
        )
    with pytest.raises(FC27SchemaValidationError, match="tackles_won cannot exceed tackles_attempted"):
        FC27PlayerMatchStatisticsSchema(
            local_player_reference="plr_ecf_0123456789ab",
            position="CM",
            tackles_won=10,
            tackles_attempted=5,
        )
    with pytest.raises(FC27SchemaValidationError, match="shots_on_target cannot exceed shots"):
        FC27AggregateMatchStatisticsSchema(shots=5, shots_on_target=8)


def test_27_gametype9_normalizes_to_league() -> None:
    mt, review_req = normalize_fc27_match_type("gameType9")
    assert mt == "LEAGUE"
    assert review_req is False


def test_28_gametype13_normalizes_to_playoff() -> None:
    mt, review_req = normalize_fc27_match_type("gameType13")
    assert mt == "PLAYOFF"
    assert review_req is False


def test_29_unknown_match_type_normalizes_to_unknown_with_review_required() -> None:
    mt, review_req = normalize_fc27_match_type("gameType999")
    assert mt == "UNKNOWN"
    assert review_req is True


def test_30_all_thirteen_response_schema_models_forbid_extra_fields() -> None:
    assert len(ALL_RESPONSE_SCHEMA_MODELS) == 13
    for model_cls in ALL_RESPONSE_SCHEMA_MODELS:
        assert model_cls.model_config.get("extra") == "forbid"


# ============================================================================
# 3. Match Event Decoding Tests
# ============================================================================


def test_31_known_event_bits_decode_deterministically() -> None:
    bitmap = (1 << 0) | (1 << 1) | (1 << 5)
    decoded = decode_fc27_match_events(bitmap)
    assert decoded.event_bitmap == bitmap
    assert decoded.known_event_codes == ("GOAL_SCORED", "ASSIST_RECORDED", "CLEAN_SHEET_AWARDED")
    assert decoded.unknown_bit_positions == ()
    assert decoded.review_required is False
    assert enumerate_event_bits(bitmap) == (0, 1, 5)


def test_32_unknown_bits_preserved_in_unknown_bit_positions() -> None:
    bitmap = (1 << 0) | (1 << 20) | (1 << 31)
    decoded = decode_fc27_match_events(bitmap)
    assert decoded.known_event_codes == ("GOAL_SCORED",)
    assert decoded.unknown_bit_positions == (20, 31)
    assert decoded.review_required is True


def test_33_negative_or_oversized_event_bitmap_fails_closed() -> None:
    with pytest.raises(FC27SchemaValidationError, match="out of 64-bit unsigned range"):
        validate_event_bitmap(-1)
    with pytest.raises(FC27SchemaValidationError, match="out of 64-bit unsigned range"):
        validate_event_bitmap(1 << 64)
    with pytest.raises(FC27SchemaValidationError, match="must be a non-negative integer"):
        validate_event_bitmap(True)


def test_34_event_decoder_never_emits_cheating_conduct_or_discipline_labels() -> None:
    prohibited_words = {"CHEAT", "HACK", "EXPLOIT", "CONDUCT", "TOXIC", "BAN", "DISCIPLINE", "SUSPENSION"}
    for bit_name in FC27_KNOWN_EVENT_BIT_MAP.values():
        for bad in prohibited_words:
            assert bad not in bit_name.upper()


def test_35_event_dictionary_version_attached_to_normalized_output() -> None:
    decoded = decode_fc27_match_events(1)
    assert decoded.mapping_version == EVENT_MAPPING_VERSION
    assert validate_aggregate_event_counts(decoded, goals=1, assists=0) is True
    assert validate_aggregate_event_counts(decoded, goals=0, assists=0) is False


# ============================================================================
# 4. Privacy, Pseudonymization, and Loopback Fixture Tests
# ============================================================================


def test_36_gamer_tags_not_retained_in_normalized_outputs() -> None:
    raw = [
        {
            "matchId": "9002001",
            "timestamp": 1761500000,
            "result": "WIN",
            "goalsFor": 2,
            "goalsAgainst": 0,
            "opponentName": "Rival FC",
            "players": [
                {
                    "playerKey": "raw_id_77",
                    "name": "RealGamerTagShouldNotAppear",
                    "position": "ST",
                    "goals": 2,
                }
            ],
        }
    ]
    envelopes, supplied, _ = normalize_matches_payload(raw, include_player_performances=True)
    serialized = json.dumps([e.model_dump(mode="json") for e in envelopes]) + json.dumps(
        [s.model_dump(mode="json") for s in supplied]
    )
    assert "RealGamerTagShouldNotAppear" not in serialized


def test_37_display_aliases_not_retained_in_normalized_outputs() -> None:
    raw = [
        {
            "matchId": "9002002",
            "timestamp": 1761500000,
            "result": "DRAW",
            "goalsFor": 1,
            "goalsAgainst": 1,
            "opponentName": "Rival FC",
            "players": [
                {
                    "playerKey": "raw_id_88",
                    "name": "SecretDisplayAlias99",
                    "position": "CM",
                    "goals": 1,
                }
            ],
        }
    ]
    _, supplied, _ = normalize_matches_payload(raw, include_player_performances=True)
    assert supplied[0].player_performances[0].display_alias is None
    serialized = json.dumps([s.model_dump(mode="json") for s in supplied])
    assert "SecretDisplayAlias99" not in serialized


def test_38_account_persona_nucleus_and_external_player_ids_not_retained() -> None:
    raw = [
        {
            "matchId": "9002003",
            "timestamp": 1761500000,
            "result": "WIN",
            "goalsFor": 1,
            "goalsAgainst": 0,
            "opponentName": "Rival FC",
            "players": [
                {
                    "playerId": "external_raw_player_998877",
                    "position": "CB",
                    "goals": 0,
                }
            ],
        }
    ]
    envelopes, supplied, _ = normalize_matches_payload(raw, include_player_performances=True)
    serialized = json.dumps([e.model_dump(mode="json") for e in envelopes]) + json.dumps(
        [s.model_dump(mode="json") for s in supplied]
    )
    assert "external_raw_player_998877" not in serialized


def test_39_deterministic_east_coasters_pseudonyms_stable_across_runs() -> None:
    p1 = pseudonymize_east_coasters_identifier("player_12345")
    p2 = pseudonymize_east_coasters_identifier("player_12345")
    assert p1 == p2
    assert p1.startswith("plr_ecf_")
    assert len(p1) == len("plr_ecf_") + 12


def test_40_pseudonym_collision_fails_closed() -> None:
    p_a = pseudonymize_east_coasters_identifier("player_a")
    # Pre-seed the detector with a different SHA-256 for the same pseudonym to simulate a hash collision
    detector = FC27PseudonymCollisionDetector(preseeded_reverse_digests={p_a: "0" * 64})
    with pytest.raises(FC27PseudonymCollisionError, match="PSEUDONYM_COLLISION"):
        detector.resolve("player_a")


def test_41_loopback_http_fixture_server_validates_contracts_offline() -> None:
    fixture_body = json.dumps(
        {
            "330323": {
                "clubId": "330323",
                "name": "East Coasters",
                "platform": "common-gen5",
                "regionId": 4545109,
            }
        }
    ).encode("utf-8")

    class _FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(fixture_body)))
            self.end_headers()
            self.wfile.write(fixture_body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _FixtureHandler)
    port = server.server_port
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    try:
        req_spec = build_east_coasters_endpoint_request(FC27ClubsEndpointOperation.CLUB_IDENTITY)
        url = f"http://127.0.0.1:{port}{req_spec.relative_url}"
        with urllib.request.urlopen(url, timeout=3.0) as resp:  # noqa: S310
            raw_bytes = resp.read()
        parsed = parse_json_strict_no_duplicates(raw_bytes)
        schema, domain = normalize_club_identity_payload(
            parsed,
            provider_id="ea-authorized-clubs-partner",
            authorization_reference="EA-AUTH-LOOPBACK",
            payload_sha256="c" * 64,
        )
        assert schema.club_id == "330323"
        assert domain.club_name == "East Coasters"
    finally:
        server.server_close()
