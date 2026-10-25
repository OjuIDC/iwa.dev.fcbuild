"""Functional Analytics Core for FC 27 Pro Clubs East Coasters Platform.

All analytical routines are pure deterministic functions without side effects,
external I/O, or hidden state. All calculations carry explicit formula versions.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.schemas import (
    ClubMatch,
    Lineup,
    PlayerBuild,
    PlayerMatch,
    TeamSnapshot,
)

FORMULA_VERSION = "fc27-v1.0"


def determine_sample_size_tier(match_count: int) -> str:
    """Returns the sample-size confidence tier according to platform governance."""
    if match_count <= 2:
        return "INSUFFICIENT_DATA"
    elif match_count <= 4:
        return "EXPLORATORY"
    elif match_count <= 9:
        return "LOW_CONFIDENCE"
    elif match_count <= 19:
        return "MODERATE_CONFIDENCE"
    else:
        return "HIGH_CONFIDENCE"


def calculate_player_contribution(performances: list[PlayerMatch]) -> dict[str, Any]:
    """Computes pure per-90 metrics, consistency ratings, and phase scores for a player."""
    if not performances:
        return {
            "matches_analyzed": 0,
            "total_minutes": 0,
            "sample_size_tier": "INSUFFICIENT_DATA",
            "formula_version": FORMULA_VERSION,
        }

    total_minutes = sum(p.minutes_played for p in performances)
    matches_count = len(performances)
    n90 = (total_minutes / 90.0) if total_minutes > 0 else 1.0

    ratings = [p.match_rating for p in performances]
    avg_rating = round(sum(ratings) / len(ratings), 2)
    variance = round(sum((r - avg_rating) ** 2 for r in ratings) / len(ratings), 3)

    total_goals = sum(p.goals for p in performances)
    total_assists = sum(p.assists for p in performances)
    total_shots = sum(p.shots for p in performances)
    total_shots_on_target = sum(p.shots_on_target for p in performances)
    total_passes_att = sum(p.passes_attempted for p in performances)
    total_passes_comp = sum(p.passes_completed for p in performances)
    total_tackles_att = sum(p.tackles_attempted for p in performances)
    total_tackles_won = sum(p.tackles_won for p in performances)
    total_interceptions = sum(p.interceptions for p in performances)
    total_poss_won = sum(p.possession_won for p in performances)
    total_poss_lost = sum(p.possession_lost for p in performances)

    pass_acc = round((total_passes_comp / total_passes_att * 100.0), 1) if total_passes_att > 0 else 0.0
    tackle_win_rate = round((total_tackles_won / total_tackles_att * 100.0), 1) if total_tackles_att > 0 else 0.0

    # Offensive threat score (0-100)
    g90 = total_goals / n90
    a90 = total_assists / n90
    shot_acc = (total_shots_on_target / total_shots * 100.0) if total_shots > 0 else 0.0
    offensive_score = min(100.0, round((g90 * 35.0) + (a90 * 25.0) + (shot_acc * 0.4), 1))

    # Defensive impact score (0-100)
    t90 = total_tackles_won / n90
    int90 = total_interceptions / n90
    defensive_score = min(100.0, round((t90 * 12.0) + (int90 * 10.0) + (tackle_win_rate * 0.4), 1))

    # Ball retention / progression score (0-100)
    turnover_ratio = (total_poss_lost / n90) if n90 > 0 else 0.0
    retention_score = max(0.0, min(100.0, round(pass_acc - (turnover_ratio * 3.0) + 15.0, 1)))

    return {
        "player_id": performances[0].player_id,
        "matches_analyzed": matches_count,
        "total_minutes": total_minutes,
        "avg_match_rating": avg_rating,
        "rating_variance": variance,
        "goals_per_90": round(g90, 2),
        "assists_per_90": round(a90, 2),
        "pass_accuracy_pct": pass_acc,
        "tackle_success_pct": tackle_win_rate,
        "interceptions_per_90": round(int90, 2),
        "offensive_threat_score": offensive_score,
        "defensive_impact_score": defensive_score,
        "ball_retention_score": retention_score,
        "sample_size_tier": determine_sample_size_tier(matches_count),
        "formula_version": FORMULA_VERSION,
    }


def evaluate_build_suitability(
    build: PlayerBuild,
    target_role: str,
    performances: list[PlayerMatch] | None = None,
) -> dict[str, Any]:
    """Pure rule-grounded evaluation of a PlayerBuild's physical attributes and playstyles for an assigned role."""
    attrs = build.attributes
    ht = build.height_cm
    wt = build.weight_kg

    # Role profile requirement weights
    role_benchmarks: dict[str, dict[str, float]] = {
        "Advanced Forward": {"pace": 0.35, "shooting": 0.40, "dribbling": 0.15, "physical": 0.10},
        "Target Forward": {"physical": 0.40, "shooting": 0.35, "passing": 0.15, "pace": 0.10},
        "Playmaker": {"passing": 0.45, "dribbling": 0.30, "shooting": 0.15, "pace": 0.10},
        "Box-to-Box": {"stamina": 0.25, "passing": 0.25, "defending": 0.25, "pace": 0.25},
        "Anchor": {"defending": 0.45, "physical": 0.35, "passing": 0.15, "pace": 0.05},
        "Ball Playing Defender": {"defending": 0.40, "passing": 0.25, "physical": 0.25, "pace": 0.10},
        "Attacking Fullback": {"pace": 0.40, "passing": 0.25, "defending": 0.20, "physical": 0.15},
        "Sweeper Keeper": {"diving": 0.30, "speed": 0.25, "kicking": 0.25, "reflexes": 0.20},
    }

    benchmarks = role_benchmarks.get(target_role, {"pace": 0.25, "passing": 0.25, "defending": 0.25, "physical": 0.25})

    weighted_score = 0.0
    for key, weight in benchmarks.items():
        val = attrs.get(key, 70)
        weighted_score += val * weight

    # Physical modifiers
    if target_role in ("Target Forward", "Ball Playing Defender", "Stopper"):
        if ht < 182:
            weighted_score -= 8.0  # Height penalty for aerial roles
        elif ht >= 188:
            weighted_score += 4.0
    elif target_role in ("Winger", "Inside Forward", "Attacking Fullback"):
        if wt > 80:
            weighted_score -= 5.0  # Weight penalty for speed/agility roles
        elif ht <= 178:
            weighted_score += 3.0

    suitability_score = round(max(0.0, min(100.0, weighted_score)), 1)

    recommendations: list[str] = []
    if suitability_score < 75.0:
        recommendations.append(f"Consider reallocating skill points to key attributes for {target_role}.")
    if "Quick Step" not in build.playstyles and target_role in ("Advanced Forward", "Winger", "Attacking Fullback"):
        recommendations.append("Equipping 'Quick Step' playstyle significantly improves acceleration in transition.")
    if "Anticipate" not in build.playstyles and target_role in ("Anchor", "Ball Playing Defender"):
        recommendations.append("Equipping 'Anticipate' or 'Anticipate+' drastically increases tackle retention rate.")

    return {
        "build_id": build.build_id,
        "player_id": build.player_id,
        "target_role": target_role,
        "suitability_score": suitability_score,
        "height_cm": ht,
        "weight_kg": wt,
        "recommendations": recommendations,
        "formula_version": FORMULA_VERSION,
    }


def evaluate_formation_performance(matches: list[ClubMatch]) -> dict[str, dict[str, Any]]:
    """Evaluates win rates, scoring efficiency, and defensive metrics broken down by formation."""
    by_formation: dict[str, list[ClubMatch]] = defaultdict(list)
    for m in matches:
        by_formation[m.formation].append(m)

    result: dict[str, dict[str, Any]] = {}
    for form, f_matches in by_formation.items():
        total = len(f_matches)
        wins = sum(1 for m in f_matches if m.result == "WIN")
        draws = sum(1 for m in f_matches if m.result == "DRAW")
        losses = sum(1 for m in f_matches if m.result == "LOSS")
        gf = sum(m.goals_for for m in f_matches)
        ga = sum(m.goals_against for m in f_matches)

        win_rate = round((wins / total * 100.0), 1)
        points_per_game = round(((wins * 3 + draws) / total), 2)
        gf_pg = round(gf / total, 2)
        ga_pg = round(ga / total, 2)

        poss_list = [m.team_stats.get("possession_pct", 50.0) for m in f_matches if "possession_pct" in m.team_stats]
        avg_poss = round(sum(poss_list) / len(poss_list), 1) if poss_list else 50.0

        result[form] = {
            "match_count": total,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "win_rate_pct": win_rate,
            "points_per_game": points_per_game,
            "goals_for_per_game": gf_pg,
            "goals_against_per_game": ga_pg,
            "average_possession_pct": avg_poss,
            "sample_size_tier": determine_sample_size_tier(total),
            "formula_version": FORMULA_VERSION,
        }

    return result


def analyze_partnerships(matches: list[ClubMatch]) -> list[dict[str, Any]]:
    """Identifies and scores key player pairs when active together on the pitch."""
    pair_matches: dict[tuple[str, str], list[ClubMatch]] = defaultdict(list)

    for m in matches:
        lineup = sorted(m.lineup)
        for i in range(len(lineup)):
            for j in range(i + 1, len(lineup)):
                pair_matches[(lineup[i], lineup[j])].append(m)

    partnerships: list[dict[str, Any]] = []
    for (p1, p2), m_list in pair_matches.items():
        if len(m_list) < 2:
            continue
        wins = sum(1 for m in m_list if m.result == "WIN")
        draws = sum(1 for m in m_list if m.result == "DRAW")
        total = len(m_list)
        win_rate = round((wins / total * 100.0), 1)
        synergy_score = round(win_rate * 0.7 + (wins * 3 + draws) / total * 10.0, 1)

        partnerships.append({
            "player_pair": [p1, p2],
            "pair_key": f"{p1}+{p2}",
            "matches_together": total,
            "win_rate_pct": win_rate,
            "synergy_score": min(100.0, synergy_score),
            "sample_size_tier": determine_sample_size_tier(total),
            "formula_version": FORMULA_VERSION,
        })

    partnerships.sort(key=lambda x: (x["matches_together"], x["synergy_score"]), reverse=True)
    return partnerships


def calculate_team_balance(matches: list[ClubMatch]) -> dict[str, float]:
    """Calculates team-level performance indicators (0-100) across tactical phases."""
    if not matches:
        return {
            "attacking_threat": 50.0,
            "possession_control": 50.0,
            "defensive_solidity": 50.0,
            "transition_efficiency": 50.0,
            "pressing_intensity": 50.0,
        }

    n = len(matches)
    avg_gf = sum(m.goals_for for m in matches) / n
    avg_ga = sum(m.goals_against for m in matches) / n

    poss_vals = [float(m.team_stats.get("possession_pct", 50.0)) for m in matches]
    pass_vals = [float(m.team_stats.get("pass_accuracy_pct", 80.0)) for m in matches]
    avg_poss = sum(poss_vals) / len(poss_vals)
    avg_pass = sum(pass_vals) / len(pass_vals)

    shots_vals = [float(m.team_stats.get("shots", 8.0)) for m in matches]
    avg_shots = sum(shots_vals) / len(shots_vals)

    attacking_threat = min(100.0, round((avg_gf * 25.0) + (avg_shots * 3.5), 1))
    possession_control = min(100.0, round((avg_poss * 0.8) + (avg_pass * 0.5) - 20.0, 1))
    defensive_solidity = max(0.0, min(100.0, round(100.0 - (avg_ga * 30.0), 1)))
    transition_efficiency = round((attacking_threat * 0.5) + (possession_control * 0.5), 1)
    pressing_intensity = round(min(100.0, max(20.0, (avg_poss * 0.6) + (defensive_solidity * 0.4))), 1)

    return {
        "attacking_threat": attacking_threat,
        "possession_control": possession_control,
        "defensive_solidity": defensive_solidity,
        "transition_efficiency": transition_efficiency,
        "pressing_intensity": pressing_intensity,
    }


def generate_team_snapshot(
    matches: list[ClubMatch],
    builds: dict[str, PlayerBuild] | None = None,
    window_name: str = "Last 10 Matches",
) -> TeamSnapshot:
    """Synthesizes match history and builds into an authoritative TeamSnapshot."""
    match_count = len(matches)
    roster_set: set[str] = set()
    for m in matches:
        roster_set.update(m.lineup)

    formation_stats = evaluate_formation_performance(matches)
    team_balance = calculate_team_balance(matches)

    strengths: list[str] = []
    weaknesses: list[str] = []

    if team_balance["possession_control"] >= 65.0:
        strengths.append(f"High possession dominance ({team_balance['possession_control']} score).")
    else:
        weaknesses.append("Struggles against high-pressing opponents during build-up phase.")

    if team_balance["defensive_solidity"] >= 65.0:
        strengths.append("Resilient defensive structure conceding few goals per match.")
    else:
        weaknesses.append(f"Vulnerable to counter-attacks; defensive solidity is {team_balance['defensive_solidity']}.")

    if team_balance["attacking_threat"] >= 70.0:
        strengths.append("High conversion rate inside the final third.")
    else:
        weaknesses.append("Relies heavily on isolated individual brilliance for chance creation.")

    best_formation = max(formation_stats.items(), key=lambda x: (x[1]["points_per_game"], x[1]["match_count"]))[0] if formation_stats else "4-3-3(2)"
    strengths.append(f"Highest tactical consistency observed in {best_formation}.")

    return TeamSnapshot(
        snapshot_id=f"snap_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        club_id="330323",
        club_name="East Coasters",
        evaluation_window=window_name,
        match_count=match_count,
        roster=sorted(roster_set),
        active_builds=builds or {},
        formations_evaluated=list(formation_stats.keys()),
        strengths=strengths,
        weaknesses=weaknesses,
        sample_size_tier=determine_sample_size_tier(match_count),  # type: ignore[arg-type]
        created_at=datetime.now(timezone.utc).isoformat(),
    )
