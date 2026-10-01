"""
live_match_watcher.py
=====================
FC27 Pro Clubs — Live Match Watcher (Tier 1)
Club: FC27_CLUB_ID | Platform: common-gen5

HOW IT WORKS:
  The FC27 API has NO in-progress/live endpoint. Matches appear only after
  they end. This watcher polls get_matches via the MCP proxy every
  POLL_INTERVAL_SEC for each match type, looks at ALL recent matches
  returned (not just the newest) and processes every unseen one,
  oldest-first:
    1. Records the result for tilt detection, publishes 'fc27:match:new'
       (broadcast to WebSocket clients), fair-play flags and tilt alerts
    2. Triggers the LangGraph pipeline via the Graph API (which allocates
       the thread_id)
    3. Publishes 'fc27:match:end' (ANALYSIS_READY, carries match_id,
       match_type and the match payload for the post-match rundown)
  A match is marked done in Redis only after all steps — including a
  successful graph trigger — succeed. See "Processed-match state" below.

DETECTION LATENCY:
  Not "~30s". Worst case ≈ EA publication delay + MCP cache TTL for
  clubs/matches (300s in fc27_mcp_server.CACHE_TTL) + POLL_INTERVAL_SEC.
  The MCP get_matches tool currently accepts no cache-bypass argument, so
  WATCHER_MATCH_CACHE_TTL_SEC is only forwarded (as `cache_ttl_sec`) when the
  MCP advertises that property in tools/list; otherwise it has no effect and
  the server-side TTL bounds latency.

PROCESSED-MATCH STATE (Redis only — the watcher has no PostgreSQL access):
  fc27:watcher:<club>:match:<matchId>        hash: state (processing|done|dead),
                                             attempts, last_error, thread_id,
                                             step markers (result_recorded,
                                             new_published, analysis_published)
  fc27:watcher:<club>:match:<matchId>:lease  SET NX EX lease (owner token)
  fc27:watcher:<club>:<matchType>:baseline   first-boot marker
  A crash after detection leaves no done marker, so the match is retried
  once the lease expires; duplicates are suppressed by the lease + done
  state + per-step HSETNX markers; failures retry on later polls up to
  WATCHER_MAX_ATTEMPTS, then the match is marked dead.

TIERS:
  Tier 1 (this file): API polling — post-match, no Xbox needed
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
from typing import Any, Optional

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
CLUB_NAME    = os.getenv("FC27_CLUB_NAME", "")
PLATFORM     = os.getenv("FC_PLATFORM",    "common-gen5")
FC_VERSION   = "27"
MCP_BASE     = os.getenv("FC27_MCP_URL",   "http://fc27-mcp:8000")
REDIS_URL    = os.getenv("REDIS_URL",      "redis://redis:6379")
GRAPH_URL    = os.getenv("GRAPH_URL",      "http://graph-api:8001")
# Service credential: may start runs and read state; it can never approve.
GRAPH_SERVICE_TOKEN = os.getenv("GRAPH_SERVICE_TOKEN", "")
POLL_INTERVAL= int(os.getenv("POLL_INTERVAL_SEC", "30"))   # EA rate limit safe at 30s
MATCH_TYPES  = ["leagueMatch", "cup", "clubFriendly"]
GRAPH_RUN_TIMEOUT = float(os.getenv("GRAPH_RUN_TIMEOUT_SEC", "900"))
MAX_ATTEMPTS = int(os.getenv("WATCHER_MAX_ATTEMPTS", "5"))
# Lease must outlive one processing attempt (dominated by the graph run).
LEASE_TTL    = int(os.getenv("WATCHER_LEASE_TTL_SEC", str(int(GRAPH_RUN_TIMEOUT) + 120)))
RECORD_TTL   = int(os.getenv("WATCHER_RECORD_TTL_SEC", str(30 * 86400)))
BASELINE_ON_FIRST_BOOT = os.getenv("WATCHER_BASELINE_ON_FIRST_BOOT", "true").lower() == "true"
_cache_ttl_raw = os.getenv("WATCHER_MATCH_CACHE_TTL_SEC", "").strip()
MATCH_CACHE_TTL: Optional[int] = int(_cache_ttl_raw) if _cache_ttl_raw.isdigit() else None

# Redis channels
CH_NEW_MATCH   = "fc27:match:new"
CH_MATCH_END   = "fc27:match:end"
CH_TILT_ALERT  = "fc27:tilt:alert"
CH_FAIR_PLAY   = "fc27:fairplay:flag"

STATE_PROCESSING = "processing"
STATE_DONE       = "done"
STATE_DEAD       = "dead"


def _s(val: Any) -> Optional[str]:
    if val is None:
        return None
    return val.decode() if isinstance(val, (bytes, bytearray)) else str(val)


# ── MCP Client ─────────────────────────────────────────────────────────────────

def _mcp_headers() -> dict[str, str]:
    """MCP proxy auth (Authorization: Bearer <MCP_API_TOKEN>). Never logged."""
    token = os.getenv("MCP_API_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _mcp_rpc(method: str, params: dict) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(MCP_BASE, json=payload, headers=_mcp_headers())
        resp.raise_for_status()
        result = resp.json()
    if "error" in result:
        raise RuntimeError(str(result["error"].get("message", "MCP error"))[:200])
    return result["result"]


_cache_hint_supported: Optional[bool] = None


async def _mcp_supports_cache_hint() -> bool:
    """True if the MCP get_matches schema advertises a `cache_ttl_sec` argument."""
    global _cache_hint_supported
    if _cache_hint_supported is None:
        try:
            tools = (await _mcp_rpc("tools/list", {})).get("tools", [])
            props = next((t.get("inputSchema", {}).get("properties", {})
                          for t in tools if t.get("name") == "get_matches"), {})
            _cache_hint_supported = "cache_ttl_sec" in props
        except Exception:
            return False   # retry the probe next time
        if MATCH_CACHE_TTL is not None and not _cache_hint_supported:
            log.info("MCP get_matches has no cache hint; WATCHER_MATCH_CACHE_TTL_SEC ignored "
                     "(latency bounded by MCP clubs/matches TTL + poll interval)")
    return _cache_hint_supported


async def mcp_get_matches(match_type: str = "leagueMatch") -> list[dict]:
    """Fetch recent matches via MCP server."""
    arguments: dict[str, Any] = {"club_id": CLUB_ID, "match_type": match_type}
    if MATCH_CACHE_TTL is not None and await _mcp_supports_cache_hint():
        arguments["cache_ttl_sec"] = MATCH_CACHE_TTL
    result = await _mcp_rpc("tools/call", {"name": "get_matches", "arguments": arguments})
    data = json.loads(result["content"][0]["text"])
    return [m for m in data.get("matches", []) if isinstance(m, dict)]


# ── Processed-match state (Redis) ─────────────────────────────────────────────

def _record_key(match_id: str) -> str:
    return f"fc27:watcher:{CLUB_ID}:match:{match_id}"

def _lease_key(match_id: str) -> str:
    return f"{_record_key(match_id)}:lease"

def _baseline_key(match_type: str) -> str:
    return f"fc27:watcher:{CLUB_ID}:{match_type}:baseline"


async def match_state(redis: aioredis.Redis, match_id: str) -> Optional[str]:
    return _s(await redis.hget(_record_key(match_id), "state"))


async def acquire_lease(redis: aioredis.Redis, match_id: str) -> Optional[str]:
    token = uuid.uuid4().hex
    ok = await redis.set(_lease_key(match_id), token, nx=True, ex=LEASE_TTL)
    return token if ok else None


async def release_lease(redis: aioredis.Redis, match_id: str, token: str) -> None:
    # Only the owner releases; an expired-and-retaken lease is left alone.
    if _s(await redis.get(_lease_key(match_id))) == token:
        await redis.delete(_lease_key(match_id))


async def mark_done(redis: aioredis.Redis, match_id: str, **fields: Any) -> None:
    mapping = {"state": STATE_DONE, "done_at": str(int(time.time()))}
    mapping.update({k: str(v) for k, v in fields.items() if v is not None})
    await redis.hset(_record_key(match_id), mapping=mapping)
    await redis.expire(_record_key(match_id), RECORD_TTL)


async def get_recent_results(redis: aioredis.Redis, n: int = 5) -> list[str]:
    """Return last n match results for tilt detection."""
    key = f"fc27:watcher:{CLUB_ID}:results"
    vals = await redis.lrange(key, 0, n - 1)
    return [_s(v) for v in vals]

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

class GraphTriggerError(RuntimeError):
    pass


async def trigger_swarm_analysis(match: dict) -> str:
    """
    Trigger the LangGraph swarm pipeline for a completed match.
    Returns the thread_id allocated by the Graph API; raises GraphTriggerError
    on any failure so the match is retried on a later poll.
    """
    payload   = {
        "club_id":    CLUB_ID,
        "platform":   PLATFORM,
        "fc_version": FC_VERSION,
        "trigger":    "live_match_watcher",
        "match":      match,
    }
    if not GRAPH_SERVICE_TOKEN:
        raise GraphTriggerError("GRAPH_SERVICE_TOKEN is not set; cannot trigger the graph")
    try:
        # The Graph API allocates the thread id (fc27-<clubId>-run-<YYYYMMDD>-<seq>).
        async with httpx.AsyncClient(timeout=GRAPH_RUN_TIMEOUT) as client:
            resp = await client.post(f"{GRAPH_URL}/run", json=payload, headers={"X-API-Key": GRAPH_SERVICE_TOKEN})
            resp.raise_for_status()
            thread_id = resp.json().get("thread_id")
    except Exception as exc:
        raise GraphTriggerError(f"graph trigger failed: {type(exc).__name__}") from exc
    if not thread_id:
        raise GraphTriggerError("graph response carried no thread_id")
    log.info("Swarm pipeline triggered. thread_id=%s", thread_id)
    return str(thread_id)

# ── Match Processor ────────────────────────────────────────────────────────────

def _summarise(match: dict) -> tuple[str, Any, Any, str]:
    clubs     = match.get("clubs", {}) or {}
    our_club  = clubs.get(CLUB_ID, clubs.get(str(CLUB_ID), {})) or {}
    result    = str(our_club.get("result", "unknown"))   # "win" | "loss" | "tie"
    goals_for = our_club.get("goals", "?")
    goals_ag  = our_club.get("goalsAgainst", "?")
    opp_name  = "Unknown"
    for cid, cdata in clubs.items():
        if str(cid) != str(CLUB_ID):
            opp_name = str((cdata or {}).get("name", "Unknown"))[:64]
            break
    return result, goals_for, goals_ag, opp_name


async def _once(redis: aioredis.Redis, match_id: str, step: str) -> bool:
    """Per-step idempotency marker: True only the first time `step` runs for this match."""
    return bool(await redis.hsetnx(_record_key(match_id), step, "1"))


async def process_new_match(redis: aioredis.Redis, match: dict, match_type: str) -> str:
    """
    Run all side effects for one match. Each side effect is guarded by a
    per-match step marker so a retry after a partial failure does not repeat
    it. Raises on failure (the caller records the attempt). Returns thread_id.
    """
    match_id = str(match.get("matchId", ""))
    record   = _record_key(match_id)
    result, goals_for, goals_ag, opp_name = _summarise(match)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    log.info("NEW MATCH %s — %s-%s vs %s (%s) | type=%s",
             match_id, goals_for, goals_ag, opp_name, result.upper(), match_type)

    # 1. Tilt history + new-match broadcast + fair play + tilt check (once each)
    if await _once(redis, match_id, "result_recorded"):
        await push_result(redis, result)
    if await _once(redis, match_id, "new_published"):
        await redis.publish(CH_NEW_MATCH, json.dumps({
            "type":        "NEW_MATCH",
            "club_id":     CLUB_ID,
            "platform":    PLATFORM,
            "fc_version":  FC_VERSION,
            "match_id":    match_id,
            "match_type":  match_type,
            "result":      result,
            "score":       f"{goals_for}-{goals_ag}",
            "opponent":    opp_name,
            "ts":          ts,
            "match":       match,
        }))
        fp_flags = scan_fair_play(match)
        if fp_flags:
            await redis.publish(CH_FAIR_PLAY, json.dumps({
                "type": "FAIR_PLAY_FLAGS", "club_id": CLUB_ID, "match": match_id,
                "opponent": opp_name, "flags": fp_flags, "ts": ts,
            }))
            log.warning("FAIR PLAY: %d flag(s) detected for match %s", len(fp_flags), match_id)
        await check_tilt(redis)

    # 2. Graph trigger — must succeed before the match counts as processed.
    thread_id = _s(await redis.hget(record, "thread_id"))
    if not thread_id:
        thread_id = await trigger_swarm_analysis(match)
        await redis.hset(record, mapping={"thread_id": thread_id})

    # 3. Analysis-ready event for the rundown agent / WebSocket clients.
    if await _once(redis, match_id, "analysis_published"):
        await redis.publish(CH_MATCH_END, json.dumps({
            "type":       "ANALYSIS_READY",
            "match_id":   match_id,
            "match_type": match_type,
            "result":     result,
            "score":      f"{goals_for}-{goals_ag}",
            "opponent":   opp_name,
            "thread_id":  thread_id,
            "ts":         ts,
            "match":      match,
            "fc_version": FC_VERSION,
            "platform":   PLATFORM,
        }))
    return thread_id


async def handle_match(redis: aioredis.Redis, match: dict, match_type: str) -> str:
    """
    Idempotent wrapper: lease → attempt counting → processing → done marker.
    Returns the outcome: 'done', 'skipped', 'busy', 'failed', 'dead'.
    Never raises.
    """
    match_id = str(match.get("matchId", "")).strip()
    if not match_id:
        return "skipped"
    record = _record_key(match_id)
    try:
        if await match_state(redis, match_id) in (STATE_DONE, STATE_DEAD):
            return "skipped"
        token = await acquire_lease(redis, match_id)
        if token is None:
            return "busy"     # another worker (or a crashed attempt whose lease has not expired)
    except Exception as exc:
        log.error("Redis error before processing match %s: %s", match_id, type(exc).__name__)
        return "failed"
    try:
        state = await match_state(redis, match_id)
        if state in (STATE_DONE, STATE_DEAD):
            return "skipped"
        attempts = int(await redis.hincrby(record, "attempts", 1))
        await redis.hset(record, mapping={"state": STATE_PROCESSING, "match_type": match_type,
                                          "last_attempt_at": str(int(time.time()))})
        await redis.expire(record, RECORD_TTL)
        try:
            thread_id = await process_new_match(redis, match, match_type)
        except Exception as exc:
            err = type(exc).__name__ if not isinstance(exc, GraphTriggerError) else str(exc)
            dead = attempts >= MAX_ATTEMPTS
            await redis.hset(record, mapping={"last_error": err[:200],
                                              "state": STATE_DEAD if dead else STATE_PROCESSING})
            if dead:
                log.error("Match %s failed %d/%d attempts (%s); marked dead",
                          match_id, attempts, MAX_ATTEMPTS, err)
                return "dead"
            log.warning("Match %s attempt %d/%d failed (%s); will retry on a later poll",
                        match_id, attempts, MAX_ATTEMPTS, err)
            return "failed"
        await mark_done(redis, match_id, thread_id=thread_id)
        return "done"
    except Exception as exc:
        log.error("Redis error while processing match %s: %s", match_id, type(exc).__name__)
        return "failed"
    finally:
        try:
            await release_lease(redis, match_id, token)
        except Exception:
            pass   # the lease TTL frees it


def _oldest_first(matches: list[dict]) -> list[dict]:
    """EA returns newest-first; sort by timestamp when present, else reverse order."""
    if matches and all(str(m.get("timestamp", "")).isdigit() for m in matches):
        return sorted(matches, key=lambda m: int(m["timestamp"]))
    return list(reversed(matches))

# ── Poll Loop ──────────────────────────────────────────────────────────────────

async def poll_match_type(redis: aioredis.Redis, match_type: str) -> list[tuple[str, str]]:
    """Poll one match type and process every unseen match, oldest-first. Never raises."""
    try:
        matches = await mcp_get_matches(match_type)
    except Exception as exc:
        log.error("Poll failed for %s: %s", match_type, type(exc).__name__)
        return []
    if not matches:
        return []

    outcomes: list[tuple[str, str]] = []
    try:
        if BASELINE_ON_FIRST_BOOT and not await redis.exists(_baseline_key(match_type)):
            # First-ever boot: matches already on the API are history, not new.
            for m in matches:
                mid = str(m.get("matchId", "")).strip()
                if mid and await redis.hsetnx(_record_key(mid), "state", STATE_DONE):
                    await redis.hset(_record_key(mid), mapping={"baseline": "1"})
                    await redis.expire(_record_key(mid), RECORD_TTL)
            await redis.set(_baseline_key(match_type), str(int(time.time())))
            log.info("Watcher initialised for %s with %d existing match(es)", match_type, len(matches))
            return [(str(m.get("matchId", "")), "baseline") for m in matches]
    except Exception as exc:
        log.error("Baseline failed for %s: %s", match_type, type(exc).__name__)
        return []

    for m in _oldest_first(matches):
        outcome = await handle_match(redis, m, match_type)
        outcomes.append((str(m.get("matchId", "")), outcome))
    return outcomes

# ── Main Loop ──────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("FC27 Live Match Watcher starting...")
    log.info("Club ID: %s | Platform: %s | Poll: %ds", CLUB_ID, PLATFORM, POLL_INTERVAL)
    log.info("Redis: configured (URL not logged) | MCP: %s | MCP auth: %s",
             MCP_BASE, "bearer" if os.getenv("MCP_API_TOKEN") else "MISSING")
    log.info("Channels: %s | %s | %s | %s",
             CH_NEW_MATCH, CH_MATCH_END, CH_TILT_ALERT, CH_FAIR_PLAY)

    redis = await aioredis.from_url(REDIS_URL, decode_responses=False)

    try:
        await redis.ping()
        log.info("Redis connected ✅")
    except Exception as exc:
        log.error("Redis connection failed: %s", type(exc).__name__)
        raise

    log.info("Polling every %ds for match types: %s", POLL_INTERVAL, MATCH_TYPES)

    while True:
        for match_type in MATCH_TYPES:
            try:
                await poll_match_type(redis, match_type)
            except Exception as exc:   # belt and braces: the loop never dies
                log.error("Unexpected poll error for %s: %s", match_type, type(exc).__name__)
            await asyncio.sleep(1)   # stagger requests, respect rate limit

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
