"""FC 27 Provider Adapter and Ingestion Layer for East Coasters.

Provides deterministic fixture-backed normalization, payload verification,
and strict envelope validation without runtime dependencies on external providers.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from src.schemas import (
    ClubMatch,
    Lineup,
    PlayerBuild,
    PlayerMatch,
    ProviderEnvelope,
    VALID_FC27_FORMATIONS,
    VALID_FC27_POSITIONS,
    VALID_FC27_ROLES,
)

EAST_COASTERS_CLUB_ID = "330323"
EAST_COASTERS_CLUB_NAME = "East Coasters"


class FC27ProviderError(Exception):
    """Raised when ingestion or payload normalization fails contract invariants."""


def compute_payload_hash(payload: dict[str, Any] | str) -> str:
    """Computes a deterministic SHA-256 hash of a payload dictionary or string."""
    if isinstance(payload, dict):
        canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_envelope(
    *,
    provider: str,
    provider_version: str,
    raw_payload: dict[str, Any],
    source_reference: str,
    data_quality_status: str = "VERIFIED",
    captured_at: str | None = None,
) -> ProviderEnvelope:
    """Wraps a raw match or telemetry dictionary into a verified ProviderEnvelope."""
    ts = captured_at or datetime.now(timezone.utc).isoformat()
    p_hash = compute_payload_hash(raw_payload)

    return ProviderEnvelope(
        provider=provider,
        provider_version=provider_version,
        game_version="FC27",
        schema_version="1.0.0",
        captured_at=ts,
        source_reference=source_reference,
        payload_hash=p_hash,
        raw_payload_reference=None,
        data_quality_status=data_quality_status,  # type: ignore[arg-type]
        payload=raw_payload,
    )


def normalize_match_envelope(envelope: ProviderEnvelope) -> ClubMatch:
    """Validates and parses a ProviderEnvelope into a strongly typed ClubMatch domain object.

    Fails closed if the envelope payload contains unknown positions, formations, or roles.
    """
    # Enforce envelope integrity
    computed_hash = compute_payload_hash(envelope.payload)
    if computed_hash != envelope.payload_hash:
        raise FC27ProviderError(
            f"payload hash mismatch: envelope says {envelope.payload_hash}, computed {computed_hash}"
        )

    if envelope.data_quality_status == "REJECTED":
        raise FC27ProviderError("cannot normalize rejected envelope")

    data = envelope.payload
    try:
        performances: list[PlayerMatch] = []
        for p_data in data.get("player_performances", []):
            performances.append(
                PlayerMatch(
                    match_id=data["match_id"],
                    player_id=p_data["player_id"],
                    club_id=p_data.get("club_id", data.get("club_id", EAST_COASTERS_CLUB_ID)),
                    position=p_data["position"],
                    role=p_data["role"],
                    minutes_played=p_data["minutes_played"],
                    match_rating=p_data["match_rating"],
                    goals=p_data.get("goals", 0),
                    assists=p_data.get("assists", 0),
                    shots=p_data.get("shots", 0),
                    shots_on_target=p_data.get("shots_on_target", 0),
                    passes_attempted=p_data.get("passes_attempted", 0),
                    passes_completed=p_data.get("passes_completed", 0),
                    tackles_attempted=p_data.get("tackles_attempted", 0),
                    tackles_won=p_data.get("tackles_won", 0),
                    interceptions=p_data.get("interceptions", 0),
                    possession_won=p_data.get("possession_won", 0),
                    possession_lost=p_data.get("possession_lost", 0),
                    aerials_won=p_data.get("aerials_won", 0),
                    aerials_lost=p_data.get("aerials_lost", 0),
                    distance_covered_km=p_data.get("distance_covered_km", 0.0),
                    confidence_metadata=p_data.get("confidence_metadata", {}),
                )
            )

        match = ClubMatch(
            match_id=data["match_id"],
            club_id=data.get("club_id", EAST_COASTERS_CLUB_ID),
            opponent_club_id=data["opponent_club_id"],
            opponent_club_name=data["opponent_club_name"],
            match_timestamp=data.get("match_timestamp", envelope.captured_at),
            goals_for=data["goals_for"],
            goals_against=data["goals_against"],
            result=data["result"],
            formation=data["formation"],
            lineup=data["lineup"],
            player_performances=performances,
            team_stats=data.get("team_stats", {}),
            provider_provenance=envelope,
        )
        return match
    except (KeyError, ValueError, ValidationError) as exc:
        raise FC27ProviderError(f"contract validation error in match payload: {exc}") from exc


class FC27SyntheticProvider:
    """Fixture-backed synthetic provider generating deterministic FC 27 Pro Clubs matches.

    Used for testing, benchmarking, coaching analysis, and offline operation.
    """

    def __init__(self, club_id: str = EAST_COASTERS_CLUB_ID):
        self.club_id = club_id

    @staticmethod
    def get_canonical_roster() -> list[dict[str, str]]:
        return [
            {"player_id": "p_batmanL8", "gamertag": "batmanL8", "fav_pos": "CAM", "fav_role": "Playmaker"},
            {"player_id": "p_striker99", "gamertag": "ApexStriker", "fav_pos": "ST", "fav_role": "Advanced Forward"},
            {"player_id": "p_midgeneral", "gamertag": "MidGeneral", "fav_pos": "CM", "fav_role": "Box-to-Box"},
            {"player_id": "p_anchordef", "gamertag": "IronAnchor", "fav_pos": "CDM", "fav_role": "Anchor"},
            {"player_id": "p_wingflash", "gamertag": "WingFlash", "fav_pos": "LW", "fav_role": "Inside Forward"},
            {"player_id": "p_cb_rock", "gamertag": "RockDefender", "fav_pos": "CB", "fav_role": "Ball Playing Defender"},
            {"player_id": "p_cb_wall", "gamertag": "WallStopper", "fav_pos": "CB", "fav_role": "Stopper"},
            {"player_id": "p_lb_speed", "gamertag": "LeftWingback", "fav_pos": "LB", "fav_role": "Attacking Fullback"},
            {"player_id": "p_rb_solid", "gamertag": "RightGuard", "fav_pos": "RB", "fav_role": "Fullback"},
            {"player_id": "p_keeper1", "gamertag": "ShieldGK", "fav_pos": "GK", "fav_role": "Sweeper Keeper"},
        ]

    def generate_match_fixture(
        self,
        match_index: int,
        formation: str = "4-3-3(2)",
        outcome: str = "WIN",
    ) -> ProviderEnvelope:
        """Generates a realistic, deterministic FC 27 match envelope for the East Coasters."""
        roster = self.get_canonical_roster()
        match_id = f"fc27_ec_match_{match_index:04d}"

        if outcome == "WIN":
            goals_for = 3
            goals_against = 1
            result = "WIN"
            rating_boost = 0.8
        elif outcome == "LOSS":
            goals_for = 1
            goals_against = 2
            result = "LOSS"
            rating_boost = -0.5
        else:
            goals_for = 2
            goals_against = 2
            result = "DRAW"
            rating_boost = 0.0

        player_perfs: list[dict[str, Any]] = []
        for p in roster:
            base_rating = 7.0 + rating_boost + ((hash(p["player_id"] + str(match_index)) % 15) / 10.0)
            base_rating = max(4.0, min(10.0, round(base_rating, 1)))

            p_perf = {
                "player_id": p["player_id"],
                "club_id": self.club_id,
                "position": p["fav_pos"],
                "role": p["fav_role"],
                "minutes_played": 90,
                "match_rating": base_rating,
                "goals": 1 if p["fav_pos"] in ("ST", "CAM", "LW") and outcome == "WIN" else 0,
                "assists": 1 if p["fav_pos"] in ("CAM", "CM", "LW") and goals_for > 1 else 0,
                "shots": 4 if p["fav_pos"] in ("ST", "CAM") else 1,
                "shots_on_target": 3 if p["fav_pos"] in ("ST", "CAM") else 1,
                "passes_attempted": 35 if "M" in p["fav_pos"] else 20,
                "passes_completed": 31 if "M" in p["fav_pos"] else 17,
                "tackles_attempted": 6 if "B" in p["fav_pos"] or "CDM" in p["fav_pos"] else 2,
                "tackles_won": 5 if "B" in p["fav_pos"] or "CDM" in p["fav_pos"] else 1,
                "interceptions": 4 if "B" in p["fav_pos"] or "CDM" in p["fav_pos"] else 1,
                "possession_won": 6,
                "possession_lost": 4,
                "aerials_won": 3 if p["fav_pos"] in ("CB", "ST") else 1,
                "aerials_lost": 1,
                "distance_covered_km": 10.2,
                "confidence_metadata": {"telemetry_source": "synthetic_fc27_v1"},
            }
            player_perfs.append(p_perf)

        raw_match = {
            "match_id": match_id,
            "club_id": self.club_id,
            "opponent_club_id": f"opp_club_{match_index}",
            "opponent_club_name": f"Rival FC {match_index}",
            "match_timestamp": f"2026-10-{10 + (match_index % 14):02d}T20:00:00Z",
            "goals_for": goals_for,
            "goals_against": goals_against,
            "result": result,
            "formation": formation,
            "lineup": [p["player_id"] for p in roster],
            "player_performances": player_perfs,
            "team_stats": {
                "possession_pct": 56.5 if outcome == "WIN" else 48.0,
                "pass_accuracy_pct": 87.2 if outcome == "WIN" else 81.0,
                "shots": 12 if outcome == "WIN" else 7,
                "shots_on_target": 8 if outcome == "WIN" else 4,
                "tackles": 22,
                "tackles_won": 18 if outcome == "WIN" else 12,
                "interceptions": 15,
                "fouls": 4,
                "corners": 6,
            },
        }

        return create_envelope(
            provider="synthetic_fc27",
            provider_version="1.0.0",
            raw_payload=raw_match,
            source_reference=f"fixtures://fc27/match_{match_index:04d}",
            data_quality_status="VERIFIED",
            captured_at=raw_match["match_timestamp"],
        )

    def get_season_fixtures(self, count: int = 15) -> list[ClubMatch]:
        """Returns a series of normalized ClubMatch fixtures simulating recent match history."""
        matches: list[ClubMatch] = []
        formations = ["4-3-3(2)", "4-3-3(2)", "4-2-3-1", "4-3-3(2)", "4-1-2-1-2(2)"]
        outcomes = ["WIN", "WIN", "DRAW", "LOSS", "WIN", "WIN", "DRAW", "WIN"]

        for i in range(1, count + 1):
            form = formations[(i - 1) % len(formations)]
            outc = outcomes[(i - 1) % len(outcomes)]
            env = self.generate_match_fixture(match_index=i, formation=form, outcome=outc)
            match = normalize_match_envelope(env)
            matches.append(match)

        return matches
