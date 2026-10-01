"""
fc27_mcp_server.py
==================
FC27 Pro Clubs — MCP Tool Server
East Coasters FC Intelligence Platform

Transport : HTTP JSON-RPC (POST / or /mcp) + SSE readiness stream (GET /sse)
            Also supports stdio (pass --stdio flag)
Platform  : common-gen5  (FC27 default; also common-gen4, nx)
Rate limit: 30 requests / minute per endpoint (EA hard cap ~180/hr)
Cache     : clubs/info 1 hr | clubs/matches 5 min | members 10 min
Retry     : bounded exponential backoff with jitter (see src/mcp_retry.py)
Security  : Bearer auth (MCP_API_TOKEN, >=32 chars) on every non-health endpoint
            ASCII-only clubId validation (prevents prompt-injection → SSRF)
            Bounded worker pool, capped request bodies, default bind 127.0.0.1
            Akamai bypass headers abstracted inside server
            No EA credentials stored or required

Credits   : fc27-clubs-api by 1erkandogan
            github.com/1erkandogan/fc27-clubs-api

Usage:
    MCP_API_TOKEN=<32+ chars> python -m src.fc27_mcp_server   # HTTP on 127.0.0.1:8000
    python -m src.fc27_mcp_server --stdio                      # stdio for Claude Desktop / LangGraph
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

try:  # package import (python -m src.fc27_mcp_server, tests)
    from src.mcp_retry import RetryPolicy, UpstreamError, call_with_retry
except ImportError:  # pragma: no cover - script execution from src/
    from mcp_retry import RetryPolicy, UpstreamError, call_with_retry  # type: ignore

# ── Config ─────────────────────────────────────────────────────────────────────

FC_VERSION  = "27"
FC_BASE_URL = os.getenv("FC_BASE_URL", "https://proclubs.ea.com/api/fc")
PLATFORM    = os.getenv("FC_PLATFORM",  "common-gen5")
HOST        = os.getenv("MCP_HOST", "127.0.0.1")
PORT        = int(os.getenv("MCP_PORT", "8000"))
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO")
ALLOWED_ORIGIN = os.getenv("MCP_ALLOWED_ORIGIN", "http://localhost")

MIN_TOKEN_LEN       = 32
MAX_WORKERS         = int(os.getenv("MCP_MAX_WORKERS", "8"))
REQUEST_TIMEOUT_SEC = float(os.getenv("MCP_REQUEST_TIMEOUT_SEC", "15"))
MAX_BODY_BYTES      = int(os.getenv("MCP_MAX_BODY_BYTES", str(64 * 1024)))
MAX_SSE_STREAMS     = int(os.getenv("MCP_MAX_SSE_STREAMS", "2"))
SSE_KEEPALIVE_SEC   = float(os.getenv("MCP_SSE_KEEPALIVE_SEC", "15"))
SSE_MAX_SEC         = float(os.getenv("MCP_SSE_MAX_SEC", "300"))
EA_TIMEOUT_SEC      = float(os.getenv("MCP_EA_TIMEOUT_SEC", "12"))
EA_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
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

# Where a user can read their clubId when /search is unavailable.
CLUB_URL_PATTERN = (
    "https://www.ea.com/games/ea-sports-fc/clubs/overview?clubId=<CLUB_ID>&platform=<PLATFORM>"
)

# ── Validation ─────────────────────────────────────────────────────────────────

_CLUB_ID_RE = re.compile(r"[0-9]{1,12}")


def _validate_club_id(club_id: Any) -> str:
    """
    Accept only 1-12 ASCII digits (equivalent to ^[0-9]{1,12}$ with fullmatch).
    No whitespace stripping, no Unicode digits (e.g. Arabic-Indic or full-width),
    no signs, decimals or trailing newlines. Prevents prompt-injection payloads
    like '../../admin' or '1; DROP TABLE' from reaching EA endpoints.
    """
    if isinstance(club_id, bool) or not isinstance(club_id, (str, int)):
        raise ValueError("Invalid clubId: must be a 1–12 digit ASCII numeric string.")
    s = str(club_id)
    if not _CLUB_ID_RE.fullmatch(s):
        raise ValueError(
            f"Invalid clubId {s[:24]!r}: must be a 1–12 digit ASCII numeric string. "
            f"Tip: copy it from the ea.com club page URL: {CLUB_URL_PATTERN}"
        )
    return s


def _validate_platform(platform: str) -> str:
    allowed = {"common-gen5", "common-gen4", "nx"}
    if platform not in allowed:
        raise ValueError(f"Invalid platform. Allowed: {sorted(allowed)}")
    return platform


def _validate_match_type(match_type: str) -> str:
    allowed = {"leagueMatch", "cup", "clubFriendly"}
    if match_type not in allowed:
        raise ValueError(f"Invalid matchType. Allowed: {sorted(allowed)}")
    return match_type


def _validate_club_name(club_name: Any) -> str:
    if not isinstance(club_name, str) or not (1 <= len(club_name.strip()) <= 64):
        raise ValueError("Invalid club_name: must be a 1–64 character string.")
    if any(ord(ch) < 32 for ch in club_name):
        raise ValueError("Invalid club_name: control characters are not allowed.")
    return club_name.strip()

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
                raise UpstreamError(
                    "mcp_rate_limited", endpoint,
                    f"Rate limit reached for '{endpoint}' "
                    f"({self._max} req/{self._window}s). Retry in {wait:.1f}s.",
                    retryable=True, retry_after_sec=wait,
                )
            bucket.append(now)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

_rate = _EndpointRateLimiter()

# ── Cache ──────────────────────────────────────────────────────────────────────

class _Cache:
    """TTL cache. Thread-safe."""

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

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

_cache = _Cache()

# ── Retry policy ───────────────────────────────────────────────────────────────

_retry_policy: RetryPolicy = RetryPolicy.from_env()

# ── Audit Metadata Helper ──────────────────────────────────────────────────────

def _audit(tool: str, params: dict, cached: bool) -> dict:
    """Audit metadata. ``platform`` is the platform actually used for the request."""
    return {
        "fc_version": FC_VERSION,
        "platform":   params.get("platform", PLATFORM),
        "tool":       tool,
        "params":     params,
        "cached":     cached,
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

# ── Core HTTP ──────────────────────────────────────────────────────────────────

def _fetch_once(endpoint: str, params: dict) -> Any:
    """One upstream attempt. Raises urllib HTTPError/URLError/timeouts on failure."""
    _rate.acquire(endpoint)  # every upstream attempt counts against the limit
    url = f"{FC_BASE_URL}/{endpoint}?{urlencode(params)}"
    req = Request(url, headers=_HEADERS)
    with urlopen(req, timeout=EA_TIMEOUT_SEC) as resp:
        raw = resp.read(EA_MAX_RESPONSE_BYTES + 1)
    if len(raw) > EA_MAX_RESPONSE_BYTES:
        raise UpstreamError("upstream_response_too_large", endpoint,
                            "EA upstream response exceeded the size cap.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UpstreamError("upstream_bad_payload", endpoint,
                            "EA upstream returned a non-JSON payload.") from None


def ea_get(endpoint: str, params: dict, *, policy: Optional[RetryPolicy] = None) -> tuple[Any, bool]:
    """
    GET FC_BASE_URL/endpoint?params with full Akamai-bypass header stack,
    per-endpoint rate limiting, tiered caching and bounded retries.

    A cached response is served without touching EA. Returns (data, from_cache).
    Raises UpstreamError (structured, credential-free) on failure.
    """
    params = dict(params)
    params.setdefault("platform", PLATFORM)
    cache_key = f"{endpoint}:{json.dumps(params, sort_keys=True)}"

    cached = _cache.get(cache_key)
    if cached is not None:
        log.debug("CACHE HIT  %s", endpoint)
        return cached, True

    log.info("EA GET  /%s  platform=%s", endpoint, params.get("platform"))
    data = call_with_retry(lambda: _fetch_once(endpoint, params), endpoint,
                           policy or _retry_policy)

    ttl = next((v for k, v in CACHE_TTL.items() if endpoint.startswith(k)), 60)
    _cache.set(cache_key, data, ttl)
    return data, False

# ── MCP Tool Implementations ───────────────────────────────────────────────────

def _club_rows(data: Any) -> list[dict]:
    """Normalise EA search payloads: list, {clubId: {...}}, a single club dict, or junk."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if "clubId" in data or "name" in data:
            items = [data]
        else:
            items = list(data.values())
    else:
        items = []
    return [c for c in items if isinstance(c, dict)]


def _search_fallback(platform: str, reason: str) -> str:
    return (
        f"Club search is unavailable ({reason}). Find the clubId manually: open your "
        f"club page on ea.com (EA SPORTS FC → Clubs → search your club) and copy the "
        f"digits after 'clubId=' in the browser URL, e.g. "
        f"{CLUB_URL_PATTERN.replace('<PLATFORM>', platform)} — then call get_club_info "
        f"or get_matches with that numeric club_id."
    )


def search_club(club_name: str, platform: str = PLATFORM) -> dict:
    """
    Search for a club by name. Returns clubId + record.

    NOTE: /clubs/search returns 404 in FC27. This tool uses
    allTimeLeaderboard/search instead. On HTTP failure or empty results it
    returns manual-extraction guidance (copy clubId from the ea.com club URL)
    instead of raising.
    """
    club_name = _validate_club_name(club_name)
    _validate_platform(platform)
    params = {"platform": platform, "clubName": club_name}
    try:
        data, from_cache = ea_get("allTimeLeaderboard/search", params)
    except UpstreamError as exc:
        audit = _audit("search_club", params, False)
        audit["fallback"] = _search_fallback(
            platform, f"upstream {exc.status or exc.kind}")
        audit["error"] = exc.to_dict()
        return {"results": [], "fallback": audit["fallback"], "audit": audit}

    audit = _audit("search_club", params, from_cache)
    rows = _club_rows(data)
    if not rows:
        audit["fallback"] = _search_fallback(platform, "no results")
        return {"results": [], "fallback": audit["fallback"], "audit": audit}

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
        for c in rows
    ]
    return {"results": results, "audit": audit}


def _for_club(data: Any, club_id: str) -> Any:
    return data.get(club_id, data) if isinstance(data, dict) else data


def get_club_info(club_id: str, platform: str = PLATFORM) -> dict:
    """Full club profile: name, crest, stadium, season record."""
    club_id  = _validate_club_id(club_id)
    _validate_platform(platform)
    params   = {"platform": platform, "clubIds": club_id}
    data, fc = ea_get("clubs/info", params)
    return {
        "data":  _for_club(data, club_id),
        "audit": _audit("get_club_info", params, fc),
    }


def get_club_overall_stats(club_id: str, platform: str = PLATFORM) -> dict:
    """All-time W/D/L, goals for/against, clean sheets, promotions, titles."""
    club_id  = _validate_club_id(club_id)
    _validate_platform(platform)
    params   = {"platform": platform, "clubIds": club_id}
    data, fc = ea_get("clubs/overallStats", params)
    return {
        "data":  _for_club(data, club_id),
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
    Single call for agent context loading. Costs 4 EA requests (cached after first call).
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

def handle_mcp(request: Any) -> dict:
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected a JSON object"}}
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
        params = request.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or name not in TOOLS:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": "Unknown tool"},
            }
        if not isinstance(args, dict):
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602, "message": "arguments must be an object"}}
        try:
            result = TOOLS[name](**args)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
            }
        except UpstreamError as exc:
            log.warning("Tool '%s' upstream error: kind=%s status=%s attempts=%s",
                        name, exc.kind, exc.status, exc.attempts)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32000, "message": str(exc), "data": exc.to_dict()},
            }
        except (ValueError, TypeError) as exc:
            log.info("Tool '%s' rejected invalid params (%s)", name, type(exc).__name__)
            msg = str(exc) if isinstance(exc, ValueError) else "Invalid arguments for tool"
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32602, "message": msg}}
        except Exception as exc:  # pragma: no cover - defensive
            log.error("Tool '%s' internal error: %s", name, type(exc).__name__)
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603, "message": "Internal error"}}

    return {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": "Unknown method"},
    }

# ── Config loading ─────────────────────────────────────────────────────────────

class ConfigError(RuntimeError):
    """Raised when the server refuses to start because of unsafe configuration."""


def load_api_token(env: Optional[dict] = None) -> str:
    env = os.environ if env is None else env
    token = env.get("MCP_API_TOKEN", "")
    if not token or len(token) < MIN_TOKEN_LEN:
        raise ConfigError(
            f"MCP_API_TOKEN must be set to at least {MIN_TOKEN_LEN} characters; refusing to start.")
    if not token.isascii() or any(c.isspace() for c in token):
        raise ConfigError("MCP_API_TOKEN must be ASCII without whitespace; refusing to start.")
    return token


def _validate_origin(origin: str) -> str:
    if not origin or "*" in origin or "," in origin:
        raise ConfigError("MCP_ALLOWED_ORIGIN must be a single exact origin (no wildcard).")
    return origin

# ── HTTP Server (bounded concurrency) ──────────────────────────────────────────

_BUSY_BODY = b'{"error":"server busy","retryable":true}'
_BUSY_RESPONSE = (
    b"HTTP/1.0 503 Service Unavailable\r\n"
    b"Content-Type: application/json\r\n"
    b"Retry-After: 1\r\n"
    b"Connection: close\r\n"
    b"Content-Length: " + str(len(_BUSY_BODY)).encode() + b"\r\n\r\n" + _BUSY_BODY
)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on concurrent handlers.

    When all ``max_workers`` slots are busy the connection gets an immediate
    503 instead of queueing an unbounded number of threads.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        api_token: str,
        allowed_origin: str = ALLOWED_ORIGIN,
        max_workers: int = MAX_WORKERS,
        request_timeout: float = REQUEST_TIMEOUT_SEC,
        max_body_bytes: int = MAX_BODY_BYTES,
        max_sse_streams: int = MAX_SSE_STREAMS,
        sse_keepalive_sec: float = SSE_KEEPALIVE_SEC,
        sse_max_sec: float = SSE_MAX_SEC,
    ):
        self.api_token = load_api_token({"MCP_API_TOKEN": api_token})
        self.allowed_origin = _validate_origin(allowed_origin)
        self.max_workers = max(1, int(max_workers))
        self.request_timeout = float(request_timeout)
        self.max_body_bytes = int(max_body_bytes)
        self.sse_keepalive_sec = float(sse_keepalive_sec)
        self.sse_max_sec = float(sse_max_sec)
        self.slots = threading.BoundedSemaphore(self.max_workers)
        self.sse_slots = threading.BoundedSemaphore(max(1, int(max_sse_streams)))
        self.stopping = threading.Event()
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
        super().__init__(server_address, handler_cls)

    def process_request(self, request, client_address):
        if self.stopping.is_set() or not self.slots.acquire(blocking=False):
            self._reject_busy(request)
            return
        t = threading.Thread(target=self._work, args=(request, client_address),
                             name="fc27-mcp-worker", daemon=True)
        with self._workers_lock:
            self._workers.add(t)
        t.start()

    def _work(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception as exc:
            log.warning("request handler error: %s", type(exc).__name__)
        finally:
            self.shutdown_request(request)
            self.slots.release()
            with self._workers_lock:
                self._workers.discard(threading.current_thread())

    def _reject_busy(self, request):
        try:
            request.settimeout(1.0)
            request.sendall(_BUSY_RESPONSE)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def handle_error(self, request, client_address):  # never dump tracebacks with request data
        log.warning("request error from %s", client_address[0] if client_address else "?")

    def shutdown(self):
        self.stopping.set()
        super().shutdown()

    def server_close(self, join_timeout: float = 10.0):
        self.stopping.set()
        super().server_close()
        deadline = time.monotonic() + join_timeout
        with self._workers_lock:
            workers = list(self._workers)
        for t in workers:
            t.join(max(0.0, deadline - time.monotonic()))


class _MCPHandler(BaseHTTPRequestHandler):
    server: BoundedThreadingHTTPServer
    server_version = "fc27-mcp"
    sys_version = ""

    def setup(self):
        self.timeout = self.server.request_timeout  # per-request socket timeout
        super().setup()

    # ── helpers ──
    def _path(self) -> str:
        return urlsplit(self.path).path

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        return hmac.compare_digest(token.strip().encode("utf-8", "replace"),
                                   self.server.api_token.encode("utf-8"))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.server.allowed_origin)
        self.send_header("Vary", "Origin")

    def _json(self, code: int, payload: Any, extra: Optional[dict] = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",  "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        self.close_connection = True
        self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer realm="fc27-mcp"'})

    # ── verbs ──
    def do_GET(self):
        path = self._path()
        if path == "/health":
            self._json(200, {"status": "ok", "fc_version": FC_VERSION})
            return
        if not self._authorized():
            self._unauthorized()
            return
        if path == "/tools":
            self._json(200, SCHEMA)
        elif path == "/sse":
            self._sse()
        else:
            self._json(404, {"error": "not found"})

    def _sse(self) -> None:
        srv = self.server
        if not srv.sse_slots.acquire(blocking=False):
            self._json(503, {"error": "too many SSE streams"}, {"Retry-After": "5"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type",  "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            self.wfile.write(
                b'event: ready\ndata: {"server":"fc27-mcp","fc_version":"27"}\n\n')
            self.wfile.flush()
            deadline = time.monotonic() + srv.sse_max_sec
            while time.monotonic() < deadline:
                wait = min(srv.sse_keepalive_sec, max(0.0, deadline - time.monotonic()))
                if srv.stopping.wait(wait):
                    break
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            self.wfile.write(b"event: bye\ndata: {}\n\n")
            self.wfile.flush()
        except (OSError, socket.timeout):
            pass  # client went away
        finally:
            self.close_connection = True
            srv.sse_slots.release()

    def do_POST(self):
        if not self._authorized():
            self._unauthorized()
            return
        if self._path() not in ("/", "/mcp"):
            self.close_connection = True
            self._json(404, {"error": "not found"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._json(411, {"error": "Content-Length required (chunked bodies not accepted)"})
            return
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self.close_connection = True
            self._json(411, {"error": "Content-Length required"})
            return
        raw_len = raw_len.strip()
        if not (raw_len.isascii() and raw_len.isdigit()):
            self.close_connection = True
            self._json(400, {"error": "invalid Content-Length"})
            return
        length = int(raw_len)
        if length > self.server.max_body_bytes:
            self.close_connection = True
            self._json(413, {"error": "request body too large",
                             "max_bytes": self.server.max_body_bytes})
            return
        try:
            body = self.rfile.read(length)
        except (OSError, socket.timeout):
            self.close_connection = True
            return
        if len(body) != length:
            self.close_connection = True
            self._json(400, {"error": "incomplete body"})
            return
        try:
            req = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(200, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
            return
        self._json(200, handle_mcp(req))

    def do_OPTIONS(self):
        # CORS preflight carries no credentials by spec; it returns headers only.
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        # Request line only (method + path); headers (and the token) are never logged.
        log.debug(fmt, *args)


def make_server(
    host: Optional[str] = None,
    port: Optional[int] = None,
    *,
    api_token: Optional[str] = None,
    **kwargs: Any,
) -> BoundedThreadingHTTPServer:
    """Build the HTTP server. Raises ConfigError if the token/origin are unsafe."""
    token = load_api_token() if api_token is None else api_token
    return BoundedThreadingHTTPServer(
        (HOST if host is None else host, PORT if port is None else port),
        _MCPHandler, api_token=token, **kwargs,
    )

# ── stdio mode (Claude Desktop / LangGraph) ────────────────────────────────────

def run_stdio():
    """Local pipe transport: trust boundary is the spawning process, so no token."""
    log.info("fc27-mcp running on stdio (fc_version=%s, platform=%s)", FC_VERSION, PLATFORM)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if len(line) > MAX_BODY_BYTES:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32600, "message": "Request too large"}}), flush=True)
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        print(json.dumps(handle_mcp(req)), flush=True)

# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> int:
    if "--stdio" in sys.argv:
        run_stdio()
        return 0
    try:
        server = make_server()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    def _graceful(signum, _frame):
        log.info("signal %d received; shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    log.info("fc27-mcp HTTP server on %s:%d (fc_version=%s, platform=%s, workers=%d)",
             server.server_address[0], server.server_address[1], FC_VERSION, PLATFORM,
             server.max_workers)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("fc27-mcp stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
