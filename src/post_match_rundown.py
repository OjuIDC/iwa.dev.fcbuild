"""
post_match_rundown.py
=====================
FC27 Pro Clubs — Post-Match Rundown Agent
East Coasters FC | Club ID: 330323 | Platform: common-gen5

Triggered automatically when fc27:match:end fires on Redis.
Calls get_matches via MCP, parses full match data, and generates
a structured match facts rundown using Azure OpenAI.

Publishes to Redis: fc27:rundown:ready
Broadcasts via ws-server to all connected clients.

Structure of a rundown:
  - Match header (score, opponent, result, date, match type)
  - Key match facts (possession, shots, pass acc, tackles)
  - Player ratings table (all East Coasters members)
  - Man of the Match
  - Goal timeline
  - Performance gaps identified
  - Commissioner-gated: training recommendations
  - Fair Play verdict
  - AI narrative (Azure OpenAI)

Run standalone:
  python -m src.post_match_rundown --match-type leagueMatch
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

import httpx
import redis.asyncio as aioredis

log = logging.getLogger("fc27.rundown")
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [fc27-rundown] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ── Config ─────────────────────────────────────────────────────────────────────
CLUB_ID      = os.getenv("FC27_CLUB_ID",  "330323")
PLATFORM     = os.getenv("FC_PLATFORM",   "common-gen5")
FC_VERSION   = "27"
MCP_BASE     = os.getenv("FC27_MCP_URL",  "http://fc27-mcp:8000")
REDIS_URL    = os.getenv("REDIS_URL",     "redis://redis:6379")
COMMISSIONER = os.getenv("COMMISSIONER",  "batmanL8")

CH_MATCH_END  = "fc27:match:end"
CH_RUNDOWN    = "fc27:rundown:ready"
CH_FAIR_PLAY  = "fc27:fairplay:flag"

# Archetype ID → name map (FC27 confirmed)
ARCH_NAMES = {
    "1":  "Finisher",  "2":  "Target",     "3":  "Maverick",
    "4":  "Boss",      "5":  "Sentinel",   "6":  "Guardian",
    "7":  "Anchor",    "8":  "Magician",   "9":  "Recycler",
    "10": "Creator",   "11": "Maestro",    "12": "Spark",
    "13": "Progressor","14": "Architect",
}

# Performance gap mapping (stat → drill → skill move)
GAP_MAP = {
    "pass_accuracy_low":    {"threshold": 75, "stat": "Vision/ShortPass",    "drill": "Rondo Circuit",         "move": "Drag Back (Hold LT + RS 180°)"},
    "tackle_success_low":   {"threshold": 60, "stat": "DefAwareness/Tackles", "drill": "Shadow Defending",      "move": "Jockey (hold LT)"},
    "shots_on_target_low":  {"threshold": 40, "stat": "Finishing/Composure",  "drill": "1v1 Finishing Drill",   "move": "Fake Shot (B→A)"},
    "rating_below_avg":     {"threshold": 6.5,"stat": "General Performance",  "drill": "Full Match Simulation", "move": "Ball Roll (Hold RS)"},
    "goals_conceded_high":  {"threshold": 3,  "stat": "DefAwareness/Reactions","drill": "Defensive Shape Drill","move": "Contain (hold LT)"},
    "possession_low":       {"threshold": 45, "stat": "BallControl/Stamina",  "drill": "Press Resistance Drill","move": "Roulette (RS rotate)"},
}

# ── MCP Client ─────────────────────────────────────────────────────────────────

async def mcp_call(tool: str, arguments: dict) -> dict:
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method":  "tools/call",
        "params":  {"name": tool, "arguments": {**arguments, "platform": PLATFORM}},
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(MCP_BASE, json=payload)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(result["error"]["message"])
        return json.loads(result["result"]["content"][0]["text"])

# ── Match Parser ───────────────────────────────────────────────────────────────

def _parse_match(match: dict, our_club_id: str) -> dict:
    """
    Extract all structured facts from a raw FC27 match dict.
    Returns a clean, flat dict ready for AI narrative generation.
    """
    clubs   = match.get("clubs", {})
    players = match.get("players", {})
    our     = clubs.get(our_club_id, {})
    ts      = match.get("timestamp", int(time.time()))

    # Opponent
    opp_id   = next((cid for cid in clubs if str(cid) != str(our_club_id)), None)
    opp      = clubs.get(opp_id, {}) if opp_id else {}
    opp_name = opp.get("name", "Opponent")

    # Result
    goals_for  = int(our.get("goals", 0))
    goals_ag   = int(our.get("goalsAgainst", 0))
    result_raw = our.get("result", "")
    if result_raw == "generatedWin" or goals_for > goals_ag:
        result = "WIN"
    elif result_raw == "generatedLoss" or goals_for < goals_ag:
        result = "LOSS"
    else:
        result = "DRAW"

    # Club-level stats
    club_stats = {
        "possession":       float(our.get("possession", 0)),
        "shots":            int(our.get("shots", 0)),
        "shots_on_target":  int(our.get("shotsOnTarget", 0)),
        "tackles":          int(our.get("tackles", 0)),
        "tackles_won":      int(our.get("tackleAttempts", 0)),
        "interceptions":    int(our.get("interceptions", 0)),
        "clearances":       int(our.get("clearances", 0)),
        "corners":          int(our.get("corners", 0)),
        "offsides":         int(our.get("offsides", 0)),
        "yellow_cards":     int(our.get("yellowCard", 0)),
        "red_cards":        int(our.get("redCard", 0)),
    }

    # Pass accuracy %
    pass_att  = int(our.get("passAttempts", 0))
    pass_made = int(our.get("passesMade", 0))
    club_stats["pass_attempts"] = pass_att
    club_stats["passes_made"]   = pass_made
    club_stats["pass_accuracy"] = round(100 * pass_made / max(pass_att, 1), 1)

    # Opponent stats
    opp_stats = {
        "possession":      float(opp.get("possession", 0)),
        "shots":           int(opp.get("shots", 0)),
        "shots_on_target": int(opp.get("shotsOnTarget", 0)),
        "pass_accuracy":   round(100 * int(opp.get("passesMade", 0)) / max(int(opp.get("passAttempts", 1)), 1), 1),
    }

    # Player performances
    our_players_raw = players.get(our_club_id, {})
    our_players = []
    for gt, ps in our_players_raw.items():
        arch_id   = str(ps.get("archetypeid", ""))
        arch_name = ARCH_NAMES.get(arch_id, f"arch{arch_id}")
        goals_p   = int(ps.get("goals", 0))
        assists_p = int(ps.get("assists", 0))
        rating_p  = float(ps.get("rating", 0))
        pass_a    = int(ps.get("passattempts", 0))
        pass_m    = int(ps.get("passesmade", 0))
        pass_pct  = round(100 * pass_m / max(pass_a, 1), 1)
        tackles_p = int(ps.get("tackleattempts", 0))
        tackwon_p = int(ps.get("tackles", 0))
        shots_p   = int(ps.get("shots", 0))
        mom       = bool(ps.get("mom", 0))

        our_players.append({
            "gamertag":       gt,
            "archetype":      arch_name,
            "arch_id":        arch_id,
            "position":       ps.get("position", ""),
            "rating":         rating_p,
            "goals":          goals_p,
            "assists":        assists_p,
            "shots":          shots_p,
            "passes":         pass_a,
            "pass_pct":       pass_pct,
            "tackles":        tackles_p,
            "tackles_won":    tackwon_p,
            "mom":            mom,
            "is_commissioner":gt.lower() == COMMISSIONER.lower(),
        })

    # Sort by rating desc
    our_players.sort(key=lambda p: p["rating"], reverse=True)

    # MOM
    mom_player = next((p for p in our_players if p["mom"]), None)
    if not mom_player and our_players:
        mom_player = our_players[0]

    # Performance gaps
    gaps = []
    if club_stats["pass_accuracy"] < GAP_MAP["pass_accuracy_low"]["threshold"]:
        gaps.append({**GAP_MAP["pass_accuracy_low"], "actual": club_stats["pass_accuracy"]})
    if goals_ag >= GAP_MAP["goals_conceded_high"]["threshold"]:
        gaps.append({**GAP_MAP["goals_conceded_high"], "actual": goals_ag})
    if club_stats["possession"] < GAP_MAP["possession_low"]["threshold"]:
        gaps.append({**GAP_MAP["possession_low"], "actual": club_stats["possession"]})
    shot_pct = round(100 * club_stats["shots_on_target"] / max(club_stats["shots"], 1), 1)
    if shot_pct < GAP_MAP["shots_on_target_low"]["threshold"]:
        gaps.append({**GAP_MAP["shots_on_target_low"], "actual": shot_pct})
    for p in our_players:
        if p["rating"] < GAP_MAP["rating_below_avg"]["threshold"]:
            gaps.append({**GAP_MAP["rating_below_avg"],
                         "actual": p["rating"], "player": p["gamertag"]})

    return {
        "match_id":      str(match.get("matchId", "")),
        "timestamp":     ts,
        "date_str":      time.strftime("%d %b %Y", time.gmtime(ts)),
        "match_type":    match.get("matchType", "leagueMatch"),
        "opponent":      opp_name,
        "goals_for":     goals_for,
        "goals_against": goals_ag,
        "result":        result,
        "score":         f"{goals_for}–{goals_ag}",
        "club_stats":    club_stats,
        "opp_stats":     opp_stats,
        "players":       our_players,
        "mom":           mom_player,
        "gaps":          gaps,
        "fc_version":    FC_VERSION,
        "platform":      PLATFORM,
        "club_id":       our_club_id,
    }

# ── AI Narrative Generator ─────────────────────────────────────────────────────

async def generate_ai_narrative(facts: dict) -> str:
    """
    Use AppSDK.ai.chat (Azure OpenAI) to generate a match narrative
    from structured match facts. Returns narrative string.
    Called from the web app — this function builds the prompt.
    """
    players_text = "\n".join([
        f"  {p['gamertag']} ({p['archetype']}) — {p['rating']} rating | "
        f"{p['goals']}G {p['assists']}A | {p['pass_pct']}% pass acc | "
        f"{p['tackles_won']}/{p['tackles']} tackles"
        f"{'  ⭐ MOM' if p['mom'] else ''}"
        for p in facts["players"]
    ])

    gaps_text = "\n".join([
        f"  • {g['stat']}: {g['actual']} (below {g['threshold']}) → Drill: {g['drill']} | Move: {g['move']}"
        for g in facts["gaps"]
    ]) or "  None identified — strong performance across the board."

    prompt = f"""You are ANALYST UNIT, the post-match analyst for East Coasters FC (FC27 Pro Clubs, Club ID: 330323).

Generate a professional, punchy post-match rundown for the Commissioner (batmanL8).

MATCH FACTS:
Result: East Coasters FC {facts['score']} {facts['opponent']} | {facts['result']} | {facts['date_str']}
Possession: {facts['club_stats']['possession']}% vs {facts['opp_stats']['possession']}%
Shots: {facts['club_stats']['shots']} (on target: {facts['club_stats']['shots_on_target']}) vs {facts['opp_stats']['shots']}
Pass accuracy: {facts['club_stats']['pass_accuracy']}% vs {facts['opp_stats']['pass_accuracy']}%
Tackles won: {facts['club_stats']['tackles_won']}/{facts['club_stats']['tackles']}
Yellow cards: {facts['club_stats']['yellow_cards']} | Red cards: {facts['club_stats']['red_cards']}

PLAYER RATINGS:
{players_text}

PERFORMANCE GAPS DETECTED:
{gaps_text}

Write exactly this structure:
1. One-paragraph match verdict (2-3 sentences, punchy, direct)
2. Three bullet key match facts (use the numbers above)
3. Man of the Match: name, archetype, one sentence why
4. One-sentence each on top 2 performers
5. Performance gaps: list each gap with the recommended drill and controller input
6. One closing line directed at Commissioner batmanL8 about what to approve next

Keep it tight. No waffle. Use the real numbers. End with:
"⚠️ Training recommendations pending Commissioner batmanL8 approval."
"""
    return prompt   # returned to web app for AppSDK.ai.chat call

# ── Rundown Builder ────────────────────────────────────────────────────────────

async def build_rundown(match: dict) -> dict:
    """
    Build complete structured rundown from a raw FC27 match dict.
    Returns dict with all facts + AI prompt ready for narrative generation.
    """
    facts = _parse_match(match, CLUB_ID)

    # Fair play scan
    fp_flags = []
    for p in facts["players"]:
        if p["pass_pct"] > 97 and p["passes"] > 20:
            fp_flags.append({"player": p["gamertag"], "issue": "Suspicious pass accuracy", "severity": "LOW"})

    # Build formatted facts card (for non-AI display)
    facts_card = {
        "header": f"East Coasters FC {facts['score']} {facts['opponent']}",
        "result_badge": facts["result"],
        "date": facts["date_str"],
        "match_type": facts["match_type"],
        "score": facts["score"],
        "opponent": facts["opponent"],
        "stats": {
            "possession":    f"{facts['club_stats']['possession']}%",
            "shots":         f"{facts['club_stats']['shots']} ({facts['club_stats']['shots_on_target']} on target)",
            "pass_accuracy": f"{facts['club_stats']['pass_accuracy']}%",
            "tackles":       f"{facts['club_stats']['tackles_won']}/{facts['club_stats']['tackles']}",
            "interceptions": facts["club_stats"]["interceptions"],
            "yellow_cards":  facts["club_stats"]["yellow_cards"],
            "red_cards":     facts["club_stats"]["red_cards"],
            "corners":       facts["club_stats"]["corners"],
        },
        "opp_stats": facts["opp_stats"],
        "players": facts["players"],
        "mom": facts["mom"],
        "gaps": facts["gaps"],
        "fair_play_flags": fp_flags,
        "fair_play_clean": len(fp_flags) == 0,
        "ai_prompt": await generate_ai_narrative(facts),
        "commissioner": COMMISSIONER,
        "approval_required": True,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fc_version": FC_VERSION,
        "platform": PLATFORM,
        "club_id": CLUB_ID,
    }
    return facts_card

# ── Redis Listener ─────────────────────────────────────────────────────────────

async def listen_and_run() -> None:
    """
    Subscribe to fc27:match:end. On each new match, fetch latest match data,
    build rundown, publish to fc27:rundown:ready.
    """
    redis  = await aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe(CH_MATCH_END)
    log.info("Post-match rundown agent listening on %s ...", CH_MATCH_END)

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue

        log.info("Match end event received — fetching latest match data")

        try:
            data      = await mcp_call("get_matches", {"club_id": CLUB_ID, "match_type": "leagueMatch"})
            matches   = data.get("matches", [])
            if not matches:
                log.warning("No matches returned from API")
                continue

            latest    = matches[0]
            rundown   = await build_rundown(latest)
            await redis.publish(CH_RUNDOWN, json.dumps(rundown))
            log.info("Rundown published → %s | Result: %s %s %s",
                     CH_RUNDOWN, rundown["header"], rundown["result_badge"],
                     rundown["date"])

            # Publish fair play flags directly to commissioner channel
            if not rundown["fair_play_clean"]:
                await redis.publish(CH_FAIR_PLAY, json.dumps({
                    "source":  "post_match_rundown",
                    "match":   rundown["header"],
                    "flags":   rundown["fair_play_flags"],
                    "ts":      rundown["ts"],
                }))
                log.warning("Fair play flags published for Commissioner review")

        except Exception as exc:
            log.error("Rundown generation failed: %s", exc)

# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        # Single-shot: fetch latest match, print rundown, exit
        async def _once():
            data    = await mcp_call("get_matches", {"club_id": CLUB_ID, "match_type": "leagueMatch"})
            matches = data.get("matches", [])
            if not matches:
                print("No matches found")
                return
            rundown = await build_rundown(matches[0])
            print(json.dumps(rundown, indent=2))
        asyncio.run(_once())
    else:
        asyncio.run(listen_and_run())
