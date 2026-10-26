"""Sanitized FC 27 Provider Fixtures with Explicit Provenance and Classification.

All fixtures are secret-free, sanitized, and classified as either:
- SYNTHETIC: Project-constructed simulation data.
- COMMUNITY_RESEARCH_DERIVED: Reconstructed from public research observations (clean room).
- AUTHORIZED_CAPTURE: Explicitly authorized captures (none in this phase).
"""
from __future__ import annotations

from typing import Any
from src.schemas import FixtureClassification

EAST_COASTERS_CLUB_ID = "330323"
EAST_COASTERS_CLUB_NAME = "East Coasters"
COMMON_GEN5_PLATFORM = "common-gen5"


# 1. Valid Club Info Fixtures
COMMUNITY_RESEARCH_CLUB_INFO_PAYLOAD: dict[str, Any] = {
    "classification": FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value,
    "club_id": EAST_COASTERS_CLUB_ID,
    "club_name": EAST_COASTERS_CLUB_NAME,
    "platform": COMMON_GEN5_PLATFORM,
    "status": "ACTIVE",
    "region": "NA_EAST",
    "reputation_tier": 2,
    "division": 1,
}

SYNTHETIC_CLUB_INFO_PAYLOAD: dict[str, Any] = {
    "classification": FixtureClassification.SYNTHETIC.value,
    "club_id": EAST_COASTERS_CLUB_ID,
    "club_name": EAST_COASTERS_CLUB_NAME,
    "platform": COMMON_GEN5_PLATFORM,
    "is_synthetic": True,
    "status": "ACTIVE",
}

CONFLICTING_CLUB_INFO_PAYLOAD: dict[str, Any] = {
    "classification": FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value,
    "club_id": EAST_COASTERS_CLUB_ID,
    "club_name": "Different Name FC",  # Name conflict for same ID
    "platform": COMMON_GEN5_PLATFORM,
    "status": "ACTIVE",
}

RETIRED_CLUB_INFO_PAYLOAD: dict[str, Any] = {
    "classification": FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value,
    "club_id": EAST_COASTERS_CLUB_ID,
    "club_name": EAST_COASTERS_CLUB_NAME,
    "platform": COMMON_GEN5_PLATFORM,
    "status": "RETIRED",
}


# 2. Roster / Member Fixture
COMMUNITY_RESEARCH_ROSTER_PAYLOAD: dict[str, Any] = {
    "classification": FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value,
    "clubId": EAST_COASTERS_CLUB_ID,
    "members": [
        {"name": "ApexStriker", "gamesPlayed": 45, "goals": 38, "assists": 12, "favoritePosition": "ST"},
        {"name": "batmanL8", "gamesPlayed": 42, "goals": 14, "assists": 28, "favoritePosition": "CAM"},
        {"name": "MidGeneral", "gamesPlayed": 40, "goals": 6, "assists": 18, "favoritePosition": "CM"},
        {"name": "IronAnchor", "gamesPlayed": 38, "goals": 2, "assists": 5, "favoritePosition": "CDM"},
        {"name": "RockDefender", "gamesPlayed": 44, "goals": 3, "assists": 2, "favoritePosition": "CB"},
    ],
}


# 3. Match Fixtures
COMMUNITY_RESEARCH_MATCH_PAYLOAD: dict[str, Any] = {
    "classification": FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value,
    "match_id": "match_cr_0001",
    "club_id": EAST_COASTERS_CLUB_ID,
    "opponent_club_id": "opp_99182",
    "opponent_club_name": "Empire City FC",
    "match_timestamp": "2026-10-20T19:30:00Z",
    "goals_for": 2,
    "goals_against": 1,
    "result": "WIN",
    "formation": "4-3-3(2)",
    "lineup": ["p_batmanL8", "p_striker99"],
    "player_performances": [
        {
            "player_id": "p_batmanL8",
            "club_id": EAST_COASTERS_CLUB_ID,
            "position": "CAM",
            "role": "Playmaker",
            "minutes_played": 90,
            "match_rating": 8.4,
            "goals": 1,
            "assists": 1,
            "shots": 3,
            "shots_on_target": 2,
            "passes_attempted": 34,
            "passes_completed": 30,
            "tackles_attempted": 2,
            "tackles_won": 2,
            "interceptions": 1,
            "possession_won": 4,
            "possession_lost": 2,
            "aerials_won": 0,
            "aerials_lost": 1,
            "distance_covered_km": 10.1,
            "confidence_metadata": {"events_parsed": "215:30,216:4,214:1,11:1"},
        },
        {
            "player_id": "p_striker99",
            "club_id": EAST_COASTERS_CLUB_ID,
            "position": "ST",
            "role": "Advanced Forward",
            "minutes_played": 90,
            "match_rating": 8.1,
            "goals": 1,
            "assists": 0,
            "shots": 4,
            "shots_on_target": 3,
            "passes_attempted": 14,
            "passes_completed": 11,
            "tackles_attempted": 1,
            "tackles_won": 1,
            "interceptions": 0,
            "possession_won": 2,
            "possession_lost": 3,
            "aerials_won": 2,
            "aerials_lost": 1,
            "distance_covered_km": 9.4,
            "confidence_metadata": {"events_parsed": "215:11,216:3,214:1,217:3"},
        },
    ],
    "team_stats": {
        "possession_pct": 54.0,
        "pass_accuracy_pct": 85.4,
        "shots": 7,
        "shots_on_target": 5,
    },
}


# 4. Partial and Abandoned Matches
PARTIAL_MATCH_PAYLOAD: dict[str, Any] = {
    "classification": FixtureClassification.SYNTHETIC.value,
    "match_id": "match_partial_001",
    "club_id": EAST_COASTERS_CLUB_ID,
    "opponent_club_id": "opp_partial",
    "opponent_club_name": "Quitter City",
    "match_timestamp": "2026-10-21T14:00:00Z",
    "goals_for": 1,
    "goals_against": 0,
    "result": "WIN",
    "formation": "4-3-3(2)",
    "lineup": ["p_striker99"],
    "player_performances": [
        {
            "player_id": "p_striker99",
            "club_id": EAST_COASTERS_CLUB_ID,
            "position": "ST",
            "role": "Advanced Forward",
            "minutes_played": 22,  # Disconnect after 22 minutes
            "match_rating": 7.2,
            "goals": 1,
            "assists": 0,
            "shots": 1,
            "shots_on_target": 1,
            "passes_attempted": 4,
            "passes_completed": 3,
            "tackles_attempted": 0,
            "tackles_won": 0,
            "interceptions": 0,
            "possession_won": 1,
            "possession_lost": 0,
            "aerials_won": 0,
            "aerials_lost": 0,
            "distance_covered_km": 2.5,
        }
    ],
    "team_stats": {"status": "ABANDONED_OPPONENT_QUIT"},
}


# 5. Error Responses
COMMUNITY_RESEARCH_404_NOT_FOUND: dict[str, Any] = {
    "classification": FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value,
    "status": 404,
    "error": "ClubNotFound",
    "message": "Club with ID 99999999 not found",
}

COMMUNITY_RESEARCH_401_UNAUTHORIZED: dict[str, Any] = {
    "classification": FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value,
    "status": 401,
    "error": "Unauthorized",
    "message": "Authentication required or session expired",
}

COMMUNITY_RESEARCH_429_RATE_LIMITED: dict[str, Any] = {
    "classification": FixtureClassification.COMMUNITY_RESEARCH_DERIVED.value,
    "status": 429,
    "error": "TooManyRequests",
    "message": "Rate limit exceeded. Try again in 60 seconds.",
    "retry_after": 60,
}
