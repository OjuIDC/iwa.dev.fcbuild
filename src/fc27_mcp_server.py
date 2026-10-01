"""
fc27_mcp_server.py
==================
FC27 Pro Clubs — MCP Tool Server
East Coasters FC Intelligence Platform

Transport : SSE  (Server-Sent Events) on port 8000
            Also supports stdio (pass --stdio flag)
Platform  : common-gen5  (FC27 default; also common-gen4, nx)
Rate limit: 30 requests / minute per endpoint (EA hard cap ~180/hr)
Cache     : clubs/info 1 hr | clubs/matches 5 min | members 10 min
Security  : clubId numeric validation (prevents prompt-injection → SSRF)
            Akamai bypass headers abstracted inside server
            No EA credentials stored or required

Credits   : fc27-clubs-api by 1erkandogan
            github.com/1erkandogan/fc27-clubs-api

Usage:
    python fc27_mcp_server.py            # SSE on :8000
    python fc27_mcp_server.py --stdio    # stdio for Claude Desktop / LangGraph
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ── Config ─────────────────────────────────────────────────────────────────────

FC_VERSION  = "27"
FC_BASE_URL = os.getenv("FC_BASE_URL", "https://proclubs.ea.com/api/fc")
PLATFORM    = os.getenv("FC_PLATFORM",  "common-gen5")
PORT        = int(os.getenv("MCP_PORT", "8000"))
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO")
ALLOWED_ORIGIN = os.getenv("MCP_ALLOWED_ORIGIN", "http://localhost")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [fc27-mcp] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fc27_mcp")

# Tiered cache TTLs (seconds)
CACHE_TTL: dict[str, int] = {
    "clubs/info":                3600,
    "clubs/overallStats":        3600,
    "allTimeLeaderboard/search": 3600,
    "allTimeLeaderboard":        3600,
    "clubs/matches":             300,
    "members/career/stats":      600,
    "members/stats":             300,
}

# Akamai-bypass headers — must mirror a real browser session exactly
_HEADERS = {
    "User-Agent":       (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Origin":           "https://www.ea.com",
    "Referer":          "https://www.ea.com/",
    "sec-fetch-dest":   "empty",
    "sec-fetch-mode":   "cors",
    "sec-fetch-site":   "same-site",
}

# ── Validation ─────────────────────────────────────────────────────────────────

def _validate_club_id(club_id: Any) -> str:
    """
    Reject anything that is not a 1-12 digit numeric string.
    Prevents prompt-injection payloads like '../../admin' or '1; DROP TABLE'
    from reaching EA endpoints.
    """
    s = str(club_id).strip()
    if not re.fullmatch(r"\d{1,12}", s):
        raise ValueError(
            f"Invalid clubId '{s}': must be a 1–12 digit numeric string. "
            f"Tip: extract it from the ea.com URL when viewing your club page "
            f"(The Grounds → The Clubhouse → your club → copy clubId=XXXXX from URL)."
        )
    return s

def _validate_platform(platform: str) -> str:
    allowed = {"common-gen5", "common-gen4", "nx"}
    if platform not in allowed:
        raise ValueError(f"Invalid platform '{platform}'. Allowed: {allowed}")
    return platform

def _validate_match_type(match_type: str) -> str:
    allowed = {"leagueMatch", "cup", "clubFriendly"}
    if match_type not in allowed:
        raise ValueError(f"Invalid matchType '{match_type}'. Allowed: {allowed}")
    return match_type

# ── Rate Limiter (30 req/min per endpoint) ─────────────────────────────────────

class _EndpointRateLimiter:
    """Per-endpoint sliding-window rate limiter. Thread-safe."""

    def __init__(self, max_calls: int = 30, window: int = 60):
        self._max    = max_calls
        self._window = window
        self._buckets: dict[str, deque[float]] = {}
        self._lock   = threading.Lock()

    def acquire(self, endpoint: str) -> None:
        with self._lock:
            now    = time.monotonic()
            bucket = self._buckets.setdefault(endpoint, deque())
            while bucket and now - bucket[0] > self._window:
                bucket.popleft()
            if len(bucket) >= self._max:
                wait = self._window - (now - bucket[0])
                raise RuntimeError(
                    f"Rate limit reached for '{endpoint}' "
                    f"({self._max} req/{self._window}s). Retry in {wait:.1f}s."
                )
            bucket.append(now)

_rate = _EndpointRateLimiter()

# ── Cache ──────────────────────────────────────────────────────────────────────

class _Cache:
    def __init__(self):
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock  = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key in self._store:
                val, exp = self._store[key]
                if time.monotonic() < exp:
                    return val
                del self._store[key]
        return None

    def set(self, key: str, val: Any, ttl: int) -> None:
        with self._lock:
            self._store[key] = (val, time.monotonic() + ttl)

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]
            return len(keys)

_cache = _Cache()

# ── Audit Metadata Helper ──────────────────────────────────────────────────────

def _audit(tool: str, params: dict, cached: bool) -> dict:
    return {
        "fc_version": FC_VERSION,
        "platform":   PLATFORM,
        "tool":       tool,
        "params":     params,
        "cached":     cached,
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

# ── Core HTTP ──────────────────────────────────────────────────────────────────

def ea_get(endpoint: str, params: dict) -> tuple[Any, bool]:
    """
    GET FC_BASE_URL/endpoint?params with full Akamai-bypass header stack,
    per-endpoint rate limiting, and tiered caching.

    Returns (data, from_cache).
    """
    params.setdefault("platform", PLATFORM)
    cache_key = f"{endpoint}:{json.dumps(params, sort_keys=True)}"

    cached = _cache.get(cache_key)
    if cached is not None:
        log.debug("CACHE HIT  %s", endpoint)
        return cached, True

    _rate.acquire(endpoint)
    log.info("EA GET  /%s  %s", endpoint, params)

    qs  = urlencode(params)
    url = f"{FC_BASE_URL}/{endpoint}?{qs}"
    req = Request(url, headers=_HEADERS)

    try:
        with urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as exc:
        raise RuntimeError(f"EA API HTTP {exc.code} on /{endpoint}: {exc.reason}")
    except URLError as exc:
        raise RuntimeError(f"EA API network error on /{endpoint}: {exc.reason}")

    ttl = next((v for k, v in CACHE_TTL.items() if endpoint.startswith(k)), 60)
    _cache.set(cache_key, data, ttl)
    return data, False

# ── MCP Tool Implementations ───────────────────────────────────────────────────

def search_club(club_name: str, platform: str = PLATFORM) -> dict:
    """
    Search for a club by name. Returns clubId + record.

    NOTE: /clubs/search returns 404 in FC27. This tool uses
    allTimeLeaderboard/search instead. If the result is empty,
    the audit message instructs the user to extract clubId manually
    from ea.com (The Grounds → Clubhouse → club page URL).
    """
    _validate_platform(platform)
    params = {"platform": platform, "clubName": club_name}
    data, from_cache = ea_get("allTimeLeaderboard/search", params)

    audit = _audit("search_club", params, from_cache)

    if not data:
        audit["fallback"] = (
            "No club found via API. Extract clubId manually: "
            "log into ea.com → EA FC27 → The Grounds → The Clubhouse "
            "→ navigate to your club → copy clubId=XXXXXX from the browser URL."
        )
        return {"results": [], "audit": audit}

    results = [
        {
            "clubId":     str(c.get("clubId", "")),
            "name":       c.get("name", ""),
            "divisionId": c.get("divisionId", ""),
            "wins":       c.get("wins", 0),
            "losses":     c.get("losses", 0),
            "ties":       c.get("ties", 0),
            "points":     c.get("points", 0),
            "seasons":    c.get("seasons", 0),
            "platform":   platform,
        }
        for c in (data if isinstance(data, list) else [data])
    ]
    return {"results": results, "audit": audit}


def get_club_info(club_id: str, platform: str = PLATFORM) -> dict:
    """Full club profile: name, crest, stadium, season record."""
    club_id  = _validate_club_id(club_id)
    _validate_platform(platform)
    params   = {"platform": platform, "clubIds": club_id}
    data, fc = ea_get("clubs/info", params)
    return {
        "data":  data.get(club_id, data),
        "audit": _audit("get_club_info", params, fc),
    }


def get_club_overall_stats(club_id: str, platform: str = PLATFORM) -> dict:
    """All-time W/D/L, goals for/against, clean sheets, promotions, titles."""
    club_id  = _validate_club_id(club_id)
    _validate_platform(platform)
    params   = {"platform": platform, "clubIds": club_id}
    data, fc = ea_get("clubs/overallStats", params)
    return {
        "data":  data.get(club_id, data),
        "audit": _audit("get_club_overall_stats", params, fc),
    }


def get_member_stats(club_id: str, platform: str = PLATFORM) -> dict:
    """Career + recent match stats for all club members."""
    club_id   = _validate_club_id(club_id)
    _validate_platform(platform)
    career_p  = {"platform": platform, "clubId": club_id}
    recent_p  = {"platform": platform, "clubId": club_id}
    career, c1 = ea_get("members/career/stats", career_p)
    recent, c2 = ea_get("members/stats",         recent_p)
    return {
        "career": career,
        "recent": recent,
        "audit":  _audit("get_member_stats", career_p, c1 and c2),
    }


def get_matches(
    club_id:    str,
    match_type: str = "leagueMatch",
    platform:   str = PLATFORM,
) -> dict:
    """Last 5 club matches with full per-player stats and events."""
    club_id    = _validate_club_id(club_id)
    _validate_platform(platform)
    _validate_match_type(match_type)
    params     = {"platform": platform, "clubIds": club_id, "matchType": match_type}
    data, fc   = ea_get("clubs/matches", params)
    return {
        "matches": data if isinstance(data, list) else [data],
        "count":   len(data) if isinstance(data, list) else 1,
        "audit":   _audit("get_matches", params, fc),
    }


def get_club_dashboard(club_id: str, platform: str = PLATFORM) -> dict:
    """
    Composite: overall stats + last 5 matches + member career stats.
    Single call for agent context loading. Costs 3 EA requests (cached after first call).
    """
    club_id = _validate_club_id(club_id)
    _validate_platform(platform)

    overall  = get_club_overall_stats(club_id, platform)
    matches  = get_matches(club_id, "leagueMatch", platform)
    members  = get_member_stats(club_id, platform)

    return {
        "overall": overall["data"],
        "matches": matches["matches"],
        "members": members["career"],
        "audit":   _audit("get_club_dashboard", {"club_id": club_id, "platform": platform}, False),
    }


# ── MCP Tool Registry ──────────────────────────────────────────────────────────

TOOLS: dict[str, Any] = {
    "search_club":            search_club,
    "get_club_info":          get_club_info,
    "get_club_overall_stats": get_club_overall_stats,
    "get_member_stats":       get_member_stats,
    "get_matches":            get_matches,
    "get_club_dashboard":     get_club_dashboard,
}

SCHEMA: list[dict] = [
    {
        "name":        "search_club",
        "description": (
            "Search FC27 Pro Clubs by name. Returns clubId and season record. "
            "Use this first to resolve a club name to its numeric clubId. "
            "If empty, falls back to a manual-extraction audit message."
        ),
        "inputSchema": {
            "type":       "object",
            "properties": {
                "club_name": {"type": "string",  "description": "Club name or partial name"},
                "platform":  {"type": "string",  "description": "EA platform (default: common-gen5)"},
            },
            "required": ["club_name"],
        },
    },
    {
        "name":        "get_club_info",
        "description": "Full club profile: name, crest, stadium, season record.",
        "inputSchema": {
            "type":       "object",
            "properties": {
                "club_id":  {"type": "string", "description": "Numeric club ID (1–12 digits)"},
                "platform": {"type": "string"},
            },
            "required": ["club_id"],
        },
    },
    {
        "name":        "get_club_overall_stats",
        "description": "All-time club stats: W/D/L, goals for/against, clean sheets, promotions, titles.",
        "inputSchema": {
            "type":       "object",
            "properties": {
                "club_id":  {"type": "string"},
                "platform": {"type": "string"},
            },
            "required": ["club_id"],
        },
    },
    {
        "name":        "get_member_stats",
        "description": "Career and recent match stats for every member of the club.",
        "inputSchema": {
            "type":       "object",
            "properties": {
                "club_id":  {"type": "string"},
                "platform": {"type": "string"},
            },
            "required": ["club_id"],
        },
    },
    {
        "name":        "get_matches",
        "description": "Last 5 matches for a club with per-player ratings, goals, assists, pass accuracy, tackles.",
        "inputSchema": {
            "type":       "object",
            "properties": {
                "club_id":    {"type": "string"},
                "match_type": {
                    "type":        "string",
                    "enum":        ["leagueMatch", "cup", "clubFriendly"],
                    "description": "Default: leagueMatch",
                },
                "platform":   {"type": "string"},
            },
            "required": ["club_id"],
        },
    },
    {
        "name":        "get_club_dashboard",
        "description": (
            "Composite context loader: overall stats + last 5 matches + member career stats. "
            "Use once at agent startup to prime context. Results are cached."
        ),
        "inputSchema": {
            "type":       "object",
            "properties": {
                "club_id":  {"type": "string"},
                "platform": {"type": "string"},
            },
            "required": ["club_id"],
        },
    },
]

# ── MCP Request Handler ────────────────────────────────────────────────────────

def handle_mcp(request: dict) -> dict:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities":    {"tools": {}},
                "serverInfo":      {"name": "fc27-mcp", "version": FC_VERSION},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": SCHEMA}}

    if method == "tools/call":
        name = request.get("params", {}).get("name", "")
        args = request.get("params", {}).get("arguments", {})
        if name not in TOOLS:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        try:
            result = TOOLS[name](**args)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            }
        except Exception as exc:
            log.error("Tool '%s' error: %s", name, exc)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    return {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }

# ── SSE HTTP Server ────────────────────────────────────────────────────────────

class _SSEHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "fc_version": FC_VERSION, "platform": PLATFORM})
        elif self.path == "/tools":
            self._json(200, SCHEMA)
        elif self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type",  "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.end_headers()
            # Send initial ready event
            self.wfile.write(b"event: ready\ndata: {\"server\":\"fc27-mcp\"}\n\n")
            self.wfile.flush()
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            req    = json.loads(body)
            result = handle_mcp(req)
        except Exception as exc:
            result = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        self._json(200, result)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type",                 "application/json")
        self.send_header("Content-Length",               str(len(body)))
        self.send_header("Access-Control-Allow-Origin",  ALLOWED_ORIGIN)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

# ── stdio mode (Claude Desktop / LangGraph) ────────────────────────────────────

def run_stdio():
    log.info("fc27-mcp running on stdio (fc_version=%s, platform=%s)", FC_VERSION, PLATFORM)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": str(exc)}}), flush=True)
            continue
        print(json.dumps(handle_mcp(req)), flush=True)

# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--stdio" in sys.argv:
        run_stdio()
    else:
        log.info("fc27-mcp SSE server starting on :%d (fc_version=%s, platform=%s)",
                 PORT, FC_VERSION, PLATFORM)
        HTTPServer(("0.0.0.0", PORT), _SSEHandler).serve_forever()
