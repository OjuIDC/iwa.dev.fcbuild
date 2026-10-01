"""
live_match_watcher.py
=====================
FC27 Pro Clubs — Live Match Watcher (Tier 1)
East Coasters FC | Club ID: 330323 | Platform: common-gen5

HOW IT WORKS:
  The FC27 API has NO in-progress/live endpoint. Match data appears
  ~30 seconds after a match ends. This watcher polls get_club_matches
  every 30s, compares the latest matchId to the last known one, and
  when a NEW match appears it:
    1. Publishes full match data to Redis pub/sub channel 'fc27:match:new'
    2. Broadcasts to all WebSocket clients (live dashboard)
    3. Triggers the LangGraph swarm pipeline automatically
    4. Sends a push notification summary

  This is the ONLY tier that requires zero Xbox setup. Works right now.

TIERS:
  Tier 1 (this file): API polling — post-match, ~30s delay, no Xbox needed
  Tier 2 (rtsp_frame_analyzer.py): Xbox RTSP stream — DURING match, requires Dev Mode
  Tier 3 (host_agent/capture_agent.py): Windows capture — DURING match, retail Xbox ok

Run:
  python -m src.live_match_watcher
  # or via Docker: docker compose up live-watcher
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional

import httpx
import redis.asyncio as aioredis

log = logging.getLogger("fc27.watcher")
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [fc27-watcher] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ── Config ─────────────────────────────────────────────────────────────────────

CLUB_ID      = os.getenv("FC27_CLUB_ID",   "330323")
PLATFORM     = os.getenv("FC_PLATFORM",    "common-gen5")
FC_VERSION   = "27"
MCP_BASE     = os.getenv("FC27_MCP_URL",   "http://fc27-mcp:8000")
REDIS_URL    = os.getenv("REDIS_URL",      "redis://redis:6379")
GRAPH_URL    = os.getenv("GRAPH_URL",      "http://graph-api:8001")
# Service credential: may start runs and read state; it can never approve.
GRAPH_SERVICE_TOKEN = os.getenv("GRAPH_SERVICE_TOKEN", "")
POLL_INTERVAL= int(os.getenv("POLL_INTERVAL_SEC", "30"))   # EA rate limit safe at 30s
MATCH_TYPES  = ["leagueMatch", "cup", "clubFriendly"]

# Redis channels
CH_NEW_MATCH   = "fc27:match:new"
CH_MATCH_END   = "fc27:match:end"
CH_TILT_ALERT  = "fc27:tilt:alert"
CH_FAIR_PLAY   = "fc27:fairplay:flag"

# ── MCP Client ─────────────────────────────────────────────────────────────────

async def mcp_get_matches(match_type: str = "leagueMatch") -> list[dict]:
    """Fetch latest matches via MCP server."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name":      "get_matches",
            "arguments": {"club_id": CLUB_ID, "match_type": match_type},
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(MCP_BASE, json=payload)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(result["error"]["message"])
        data = json.loads(result["result"]["content"][0]["text"])
        return data.get("matches", [])

# ── Match State Store (Redis-backed) ──────────────────────────────────────────

async def get_last_known_match_id(redis: aioredis.Redis, match_type: str) -> Optional[str]:
    key = f"fc27:watcher:{CLUB_ID}:{match_type}:last_match_id"
    val = await redis.get(key)
    return val.decode() if val else None

async def set_last_known_match_id(redis: aioredis.Redis, match_type: str, match_id: str) -> None:
    key = f"fc27:watcher:{CLUB_ID}:{match_type}:last_match_id"
    await redis.set(key, match_id)

async def get_recent_results(redis: aioredis.Redis, n: int = 5) -> list[str]:
    """Return last n match results for tilt detection."""
    key = f"fc27:watcher:{CLUB_ID}:results"
    vals = await redis.lrange(key, 0, n - 1)
    return [v.decode() for v in vals]

async def push_result(redis: aioredis.Redis, result: str) -> None:
    key = f"fc27:watcher:{CLUB_ID}:results"
    await redis.lpush(key, result)
    await redis.ltrim(key, 0, 9)   # keep last 10

# ── Tilt Detection ─────────────────────────────────────────────────────────────

async def check_tilt(redis: aioredis.Redis) -> Optional[dict]:
    """
    Returns tilt alert dict if 3+ consecutive losses detected, else None.
    Publishes to fc27:tilt:alert channel automatically.
    """
    results = await get_recent_results(redis, n=5)
    consecutive_losses = 0
    for r in results:
        if r == "loss":
            consecutive_losses += 1
        else:
            break

    if consecutive_losses >= 3:
        alert = {
            "type":               "TILT_DETECTED",
            "consecutive_losses": consecutive_losses,
            "recent_form":        results,
            "club_id":            CLUB_ID,
            "platform":           PLATFORM,
            "fc_version":         FC_VERSION,
            "ts":                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action":             "Squad rotation brief ready. Commissioner review required.",
        }
        await redis.publish(CH_TILT_ALERT, json.dumps(alert))
        log.warning("TILT DETECTED — %d consecutive losses. Alert published.", consecutive_losses)
        return alert
    return None

# ── Fair Play Scanner ──────────────────────────────────────────────────────────

def scan_fair_play(match: dict) -> list[dict]:
    """
    Scan match data for statistical anomalies that may indicate exploit play.
    Returns list of flags (empty = clean).
    """
    flags = []
    clubs = match.get("clubs", {})

    for cid, cdata in clubs.items():
        players = match.get("players", {}).get(cid, {})
        for gamertag, stats in players.items():
            # Flag 1: Perfect pass accuracy (>97%) with high volume
            pass_att = int(stats.get("passattempts", 0))
            pass_made = int(stats.get("passesmade", 0))
            if pass_att > 20 and pass_made / max(pass_att, 1) > 0.97:
                flags.append({
                    "type":     "SUSPICIOUS_PASS_ACCURACY",
                    "player":   gamertag,
                    "value":    f"{pass_made}/{pass_att} ({100*pass_made//pass_att}%)",
                    "severity": "LOW",
                })

            # Flag 2: Impossible rating (>10.0)
            rating = float(stats.get("rating", 0))
            if rating > 10.0:
                flags.append({
                    "type":     "IMPOSSIBLE_RATING",
                    "player":   gamertag,
                    "value":    rating,
                    "severity": "HIGH",
                })

            # Flag 3: Goals > shots (impossible in normal play)
            goals  = int(stats.get("goals", 0))
            shots  = int(stats.get("shots", 0))
            if goals > 0 and shots > 0 and goals > shots:
                flags.append({
                    "type":     "GOALS_EXCEED_SHOTS",
                    "player":   gamertag,
                    "value":    f"{goals}G / {shots}S",
                    "severity": "CRITICAL",
                })

    return flags

# ── Graph Trigger ──────────────────────────────────────────────────────────────

async def trigger_swarm_analysis(match: dict) -> Optional[str]:
    """
    Trigger the LangGraph swarm pipeline for a completed match.
    Returns thread_id if successful.
    """
    payload   = {
        "club_id":    CLUB_ID,
        "platform":   PLATFORM,
        "fc_version": FC_VERSION,
        "trigger":    "live_match_watcher",
        "match":      match,
    }
    if not GRAPH_SERVICE_TOKEN:
        log.warning("GRAPH_SERVICE_TOKEN is not set; cannot trigger the graph")
        return None
    try:
        # The Graph API allocates the thread id (fc27-<clubId>-run-<YYYYMMDD>-<seq>).
        async with httpx.AsyncClient(timeout=float(os.getenv("GRAPH_RUN_TIMEOUT_SEC", "900"))) as client:
            resp = await client.post(f"{GRAPH_URL}/run", json=payload, headers={"X-API-Key": GRAPH_SERVICE_TOKEN})
            resp.raise_for_status()
            thread_id = resp.json().get("thread_id")
            log.info("Swarm pipeline triggered. thread_id=%s", thread_id)
            return thread_id
    except Exception as exc:
        log.warning("Could not trigger swarm: %s", type(exc).__name__)
        return None

# ── Match Processor ────────────────────────────────────────────────────────────

async def process_new_match(redis: aioredis.Redis, match: dict, match_type: str) -> None:
    """
    Called when a new match is detected. Extracts result, publishes events,
    checks tilt, scans fair play, triggers swarm.
    """
    clubs       = match.get("clubs", {})
    our_club    = clubs.get(CLUB_ID, {})
    result      = our_club.get("result", "unknown")   # "win" | "loss" | "tie"
    goals_for   = our_club.get("goals", "?")
    goals_ag    = our_club.get("goalsAgainst", "?")

    # Find opponent name
    opp_name = "Unknown"
    for cid, cdata in clubs.items():
        if str(cid) != str(CLUB_ID):
            opp_name = cdata.get("name", "Unknown")
            break

    log.info(
        "NEW MATCH DETECTED — East Coasters %s-%s %s (%s) | type=%s",
        goals_for, goals_ag, opp_name, result.upper(), match_type,
    )

    # Build enriched event payload
    event = {
        "type":        "NEW_MATCH",
        "club_id":     CLUB_ID,
        "platform":    PLATFORM,
        "fc_version":  FC_VERSION,
        "match_type":  match_type,
        "result":      result,
        "score":       f"{goals_for}-{goals_ag}",
        "opponent":    opp_name,
        "ts":          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "match":       match,
    }

    # 1. Store result for tilt detection
    await push_result(redis, result)

    # 2. Publish new match event
    await redis.publish(CH_NEW_MATCH, json.dumps(event))
    log.info("Published to %s", CH_NEW_MATCH)

    # 3. Fair Play scan
    fp_flags = scan_fair_play(match)
    if fp_flags:
        fp_event = {
            "type":      "FAIR_PLAY_FLAGS",
            "club_id":   CLUB_ID,
            "match":     match.get("matchId", ""),
            "opponent":  opp_name,
            "flags":     fp_flags,
            "ts":        event["ts"],
        }
        await redis.publish(CH_FAIR_PLAY, json.dumps(fp_event))
        log.warning("FAIR PLAY: %d flag(s) detected for match vs %s", len(fp_flags), opp_name)

    # 4. Tilt check
    await check_tilt(redis)

    # 5. Trigger swarm analysis
    thread_id = await trigger_swarm_analysis(match)
    if thread_id:
        event["thread_id"] = thread_id
        log.info("Swarm analysis running on thread %s", thread_id)

    # 6. Publish match end (for WebSocket clients showing "analysis ready")
    await redis.publish(CH_MATCH_END, json.dumps({
        "type":      "ANALYSIS_READY",
        "result":    result,
        "score":     f"{goals_for}-{goals_ag}",
        "opponent":  opp_name,
        "thread_id": thread_id,
        "ts":        event["ts"],
    }))

# ── Poll Loop ──────────────────────────────────────────────────────────────────

async def poll_match_type(redis: aioredis.Redis, match_type: str) -> None:
    """Poll one match type and detect new matches."""
    try:
        matches = await mcp_get_matches(match_type)
    except Exception as exc:
        log.error("Poll failed for %s: %s", match_type, exc)
        return

    if not matches:
        return

    latest     = matches[0]
    match_id   = str(latest.get("matchId", ""))
    last_known = await get_last_known_match_id(redis, match_type)

    if match_id and match_id != last_known:
        await set_last_known_match_id(redis, match_type, match_id)
        if last_known is not None:   # don't fire on first-ever boot
            await process_new_match(redis, latest, match_type)
        else:
            log.info("Watcher initialised. Last known %s matchId: %s", match_type, match_id)

# ── Main Loop ──────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("FC27 Live Match Watcher starting...")
    log.info("Club: East Coasters FC (ID: %s) | Platform: %s | Poll: %ds",
             CLUB_ID, PLATFORM, POLL_INTERVAL)
    log.info("Redis: %s | MCP: %s", REDIS_URL, MCP_BASE)
    log.info("Channels: %s | %s | %s | %s",
             CH_NEW_MATCH, CH_MATCH_END, CH_TILT_ALERT, CH_FAIR_PLAY)

    redis = await aioredis.from_url(REDIS_URL, decode_responses=False)

    try:
        await redis.ping()
        log.info("Redis connected ✅")
    except Exception as exc:
        log.error("Redis connection failed: %s", exc)
        raise

    log.info("Polling every %ds for match types: %s", POLL_INTERVAL, MATCH_TYPES)

    while True:
        for match_type in MATCH_TYPES:
            await poll_match_type(redis, match_type)
            await asyncio.sleep(1)   # stagger requests, respect rate limit

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
