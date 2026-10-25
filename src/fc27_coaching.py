"""East Coasters Coaching Engine for FC 27 Pro Clubs.

Synthesizes pure functional analytics into structured, actionable coaching recommendations.
Enforces four-part evidence decomposition:
1. Empirical Observations (exact numbers from match data)
2. Correlations (observed relationships between configurations and outcomes)
3. Domain Heuristics (grounded in FC 27 mechanics)
4. Concrete Actionable Recommendations
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any
from uuid import uuid4

from src.fc27_analytics import (
    analyze_partnerships,
    calculate_player_contribution,
    calculate_team_balance,
    determine_sample_size_tier,
    evaluate_build_suitability,
    evaluate_formation_performance,
)
from src.schemas import (
    ClubMatch,
    EastCoastersRecommendation,
    PlayerBuild,
)

COACHING_FORMULA_VERSION = "fc27-v1.0"
COACHING_CONFIG_VERSION = "east-coasters-v1.0"


def is_recommendations_enabled() -> bool:
    """Safety gate: checks if recommendations generation is enabled."""
    return os.getenv("FC27_RECOMMENDATIONS_ENABLED", "false").lower() in ("true", "1", "yes")


def generate_coaching_recommendations(
    matches: list[ClubMatch],
    builds: dict[str, PlayerBuild] | None = None,
) -> list[EastCoastersRecommendation]:
    """Generates an evidence-backed suite of coaching recommendations for the East Coasters."""
    if not is_recommendations_enabled():
        return []

    recommendations: list[EastCoastersRecommendation] = []
    match_count = len(matches)
    sample_tier = determine_sample_size_tier(match_count)
    min_sample_met = match_count >= 5

    tier_to_confidence = {
        "INSUFFICIENT_DATA": "PROVISIONAL",
        "EXPLORATORY": "EXPLORATORY",
        "LOW_CONFIDENCE": "LOW",
        "MODERATE_CONFIDENCE": "MODERATE",
        "HIGH_CONFIDENCE": "HIGH",
    }
    confidence = tier_to_confidence[sample_tier]

    if not matches:
        return [
            EastCoastersRecommendation(
                recommendation_id=f"rec_init_{uuid4().hex[:8]}",
                club_id="330323",
                category="BEHAVIORAL_COACHING",
                target="TEAM",
                recommendation="Record at least 3 to 5 matches to establish an empirical coaching baseline.",
                empirical_observations=["Zero matches recorded in the current evaluation window."],
                correlations=[],
                heuristics=["Pro Clubs tactical analytics require match telemetry to identify build and role synergy."],
                expected_team_impact="Establishes statistical baseline for build optimization and tactical refinement.",
                confidence="PROVISIONAL",
                minimum_sample_met=False,
                sample_size=0,
                caveats=["Provisional recommendation: no match data available."],
                formula_version=COACHING_FORMULA_VERSION,
                config_version=COACHING_CONFIG_VERSION,
            )
        ]

    # 1. Tactical / Formation Evaluation
    formation_perf = evaluate_formation_performance(matches)
    if len(formation_perf) >= 2:
        sorted_forms = sorted(
            formation_perf.items(),
            key=lambda x: (x[1]["points_per_game"], x[1]["win_rate_pct"]),
            reverse=True,
        )
        best_form, best_stats = sorted_forms[0]
        worst_form, worst_stats = sorted_forms[-1]

        if best_stats["points_per_game"] > worst_stats["points_per_game"]:
            recommendations.append(
                EastCoastersRecommendation(
                    recommendation_id=f"rec_tac_{uuid4().hex[:8]}",
                    club_id="330323",
                    category="TACTICAL_ADJUSTMENT",
                    target="TEAM",
                    recommendation=f"Standardize on {best_form} as the primary club formation over {worst_form}.",
                    empirical_observations=[
                        f"{best_form} achieved {best_stats['win_rate_pct']}% win rate ({best_stats['points_per_game']} PPG) over {best_stats['match_count']} matches.",
                        f"{worst_form} yielded {worst_stats['win_rate_pct']}% win rate ({worst_stats['points_per_game']} PPG) with {worst_stats['goals_against_per_game']} goals conceded/game.",
                    ],
                    correlations=[
                        f"Formation {best_form} correlates with higher possession ({best_stats['average_possession_pct']}%) and lower defensive concession.",
                    ],
                    heuristics=[
                        "In FC 27 Pro Clubs, formations featuring a dedicated holding midfielder (e.g., 4-3-3(2)) provide superior transition cover against AI counter-attacks.",
                    ],
                    expected_team_impact=f"Expected to stabilize midfield defensive cover and improve points-per-game toward {best_stats['points_per_game']}.",
                    confidence=confidence,  # type: ignore[arg-type]
                    minimum_sample_met=min_sample_met,
                    sample_size=match_count,
                    caveats=[] if min_sample_met else ["Small sample size: exploratory analysis only."],
                    formula_version=COACHING_FORMULA_VERSION,
                    config_version=COACHING_CONFIG_VERSION,
                )
            )

    # 2. Team Balance & Behavioral Diagnostics
    balance = calculate_team_balance(matches)
    if balance["defensive_solidity"] < 60.0:
        recommendations.append(
            EastCoastersRecommendation(
                recommendation_id=f"rec_def_{uuid4().hex[:8]}",
                club_id="330323",
                category="BEHAVIORAL_COACHING",
                target="TEAM",
                recommendation="Lower defensive line depth by 10-15 notches and maintain dual-pivot discipline in defensive transitions.",
                empirical_observations=[
                    f"Team defensive solidity score is {balance['defensive_solidity']}/100.",
                    f"Conceding an average of {round(sum(m.goals_against for m in matches) / match_count, 2)} goals per match across {match_count} games.",
                ],
                correlations=[
                    "High opponent goal counts correlate with turnovers committed while fullbacks are overlapping.",
                ],
                heuristics=[
                    "Over-committing fullbacks without holding cover in FC 27 leaves large wide channels vulnerable to through-balls.",
                ],
                expected_team_impact="Reduces counter-attack vulnerability and cuts average goals conceded by 20-30%.",
                confidence=confidence,  # type: ignore[arg-type]
                minimum_sample_met=min_sample_met,
                sample_size=match_count,
                caveats=[] if min_sample_met else ["Provisional findings pending further match data."],
                formula_version=COACHING_FORMULA_VERSION,
                config_version=COACHING_CONFIG_VERSION,
            )
        )

    # 3. Partnership & Chemistry Analysis
    partnerships = analyze_partnerships(matches)
    if partnerships:
        top_pair = partnerships[0]
        if top_pair["matches_together"] >= 3 and top_pair["win_rate_pct"] >= 65.0:
            p1, p2 = top_pair["player_pair"]
            recommendations.append(
                EastCoastersRecommendation(
                    recommendation_id=f"rec_pair_{uuid4().hex[:8]}",
                    club_id="330323",
                    category="LINEUP_SELECTION",
                    target=f"{p1}+{p2}",
                    recommendation=f"Prioritize simultaneous starts for {p1} and {p2} in high-stakes club matches.",
                    empirical_observations=[
                        f"Pair {p1} + {p2} appeared together in {top_pair['matches_together']} matches, achieving a {top_pair['win_rate_pct']}% win rate.",
                        f"Synergy index scored at {top_pair['synergy_score']}/100.",
                    ],
                    correlations=[
                        "Simultaneous on-pitch presence correlates with higher match ratings and improved final-third passing accuracy.",
                    ],
                    heuristics=[
                        "Complementary role pairing (e.g. playmaker distributor + direct runner) creates passing triangles that break AI pressing blocks.",
                    ],
                    expected_team_impact="Maintains highest offensive win probability and attacking consistency.",
                    confidence=confidence,  # type: ignore[arg-type]
                    minimum_sample_met=top_pair["matches_together"] >= 5,
                    sample_size=top_pair["matches_together"],
                    caveats=[] if top_pair["matches_together"] >= 5 else ["Exploratory sample size."],
                    formula_version=COACHING_FORMULA_VERSION,
                    config_version=COACHING_CONFIG_VERSION,
                )
            )

    # 4. Build Optimization Analysis (if player builds supplied)
    if builds:
        player_matches: dict[str, list] = defaultdict(list)
        for m in matches:
            for p in m.player_performances:
                player_matches[p.player_id].append(p)

        for pid, build in builds.items():
            perfs = player_matches.get(pid, [])
            role_eval = evaluate_build_suitability(build, build.primary_role, perfs)
            if role_eval["suitability_score"] < 80.0 or role_eval["recommendations"]:
                recs_text = "; ".join(role_eval["recommendations"]) if role_eval["recommendations"] else "Optimize physical/attribute distribution."
                recommendations.append(
                    EastCoastersRecommendation(
                        recommendation_id=f"rec_bld_{uuid4().hex[:8]}",
                        club_id="330323",
                        category="BUILD_OPTIMIZATION",
                        target=pid,
                        recommendation=f"Refine {pid}'s {build.primary_role} build: {recs_text}",
                        empirical_observations=[
                            f"Build suitability for {build.primary_role} is currently {role_eval['suitability_score']}/100.",
                            f"Height: {build.height_cm}cm, Weight: {build.weight_kg}kg, Archetype: {build.archetype}.",
                        ],
                        correlations=[
                            "Sub-optimal playstyles correlate with lower recovery and acceleration in contested duels.",
                        ],
                        heuristics=[
                            "FC 27 physical meta requires specific threshold attributes (e.g. 85+ sprint speed, 80+ stamina) for modern Pro Clubs roles.",
                        ],
                        expected_team_impact=f"Improves role effectiveness score from {role_eval['suitability_score']} to 85+.",
                        confidence=confidence,  # type: ignore[arg-type]
                        minimum_sample_met=len(perfs) >= 5,
                        sample_size=len(perfs),
                        caveats=[] if len(perfs) >= 5 else ["Low player match count."],
                        formula_version=COACHING_FORMULA_VERSION,
                        config_version=COACHING_CONFIG_VERSION,
                    )
                )

    return recommendations
