"""
live_ws_server.py
=================
FC27 Pro Clubs — Live WebSocket Broadcast Server
East Coasters FC | Club ID: 330323

Bridges Redis pub/sub → WebSocket clients (browser, mobile, Windhover).
Any service that publishes to Redis channels is broadcast to all
authenticated WebSocket clients in real time.

Channels subscribed:
  fc27:live:goal         — Goal scored (either team)
  fc27:live:match_start  — Match detected as started
  fc27:live:match_end    — Match ended / post-match detected
  fc27:live:match_state  — Periodic state update (every 5 min)
  fc27:match:new         — New match appeared in API (Tier 1)
  fc27:tilt:alert        — Tilt detected (3+ consecutive losses)
  fc27:fairplay:flag     — Fair Play violation flagged

Security (see docs/SECURITY_BOUNDARIES.md and docs/OPERATIONS.md):
  * WS_AUTH_TOKEN (>= 32 chars) is required; the server refuses to start without it.
  * Handshake auth, either:
      - header  ``Authorization: Bearer <token>``            (non-browser clients), or
      - subprotocols ``["fc27.v1", "fc27.bearer.<token>"]``  (browsers; the server
        selects ``fc27.v1`` and never echoes the token back).
  * Origin must exactly match one entry of WS_ALLOWED_ORIGINS. Requests without
    an Origin header are rejected unless WS_ALLOW_NO_ORIGIN=true.
  * Bounded message size, handshake/ping/close timeouts, per-client send queue.

Run:
  WS_AUTH_TOKEN=<32+ chars> python -m src.live_ws_server   # 127.0.0.1:8765

Connect from browser:
  const ws = new WebSocket('ws://localhost:8765', ['fc27.v1', 'fc27.bearer.' + token]);
  ws.onmessage = e => console.log(JSON.parse(e.data));
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol

log = logging.getLogger("fc27.ws")
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO),
    format="%(asctime)s [fc27-ws] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

REDIS_URL = os.getenv("REDIS_URL",  "redis://redis:6379")
CLUB_ID   = os.getenv("FC27_CLUB_ID", "330323")
FC_VERSION= "27"
PLATFORM  = os.getenv("FC_PLATFORM", "common-gen5")

MIN_TOKEN_LEN   = 32
SUBPROTOCOL     = Subprotocol("fc27.v1")
BEARER_PROTO_PREFIX = "fc27.bearer."

# Close codes (RFC 6455 §7.4.1)
CLOSE_GOING_AWAY   = 1001
CLOSE_POLICY       = 1008
CLOSE_TRY_AGAIN    = 1013  # slow consumer / overloaded

CHANNELS = [
    "fc27:live:goal",
    "fc27:live:match_start",
    "fc27:live:match_end",
    "fc27:live:match_state",
    "fc27:match:new",
    "fc27:match:end",
    "fc27:tilt:alert",
    "fc27:fairplay:flag",
    "fc27:rundown:ready",
    "fc27:approval:requested",
    "fc27:approval:decided",
    "fc27:voice:transcript",
    "fc27:voice:confirmation_required",
]


class ConfigError(RuntimeError):
    """Raised when the server refuses to start because of unsafe configuration."""


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_origins(raw: str) -> frozenset[str]:
    origins = {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}
    for o in origins:
        if "*" in o or o.lower() == "null":
            raise ConfigError("WS_ALLOWED_ORIGINS must list exact origins (no wildcard / null).")
        parts = urlsplit(o)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path:
            raise ConfigError(f"WS_ALLOWED_ORIGINS entry is not an origin: {o!r}")
    return frozenset(origins)


@dataclass
class WSConfig:
    token: str
    allowed_origins: frozenset[str]
    allow_no_origin: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    max_message_bytes: int = 4096        # inbound frames (clients only send PING)
    open_timeout: float = 10.0
    ping_interval: float = 20.0
    ping_timeout: float = 20.0
    close_timeout: float = 5.0
    send_timeout: float = 5.0            # per-message send to one client
    client_queue: int = 64               # per-client outbound backlog
    max_clients: int = 200
    health_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or len(self.token) < MIN_TOKEN_LEN:
            raise ConfigError(
                f"WS_AUTH_TOKEN must be at least {MIN_TOKEN_LEN} characters; refusing to start.")
        if not self.token.isascii() or any(c.isspace() or c == "," for c in self.token):
            raise ConfigError("WS_AUTH_TOKEN must be ASCII without whitespace or commas.")
        if not self.allowed_origins and not self.allow_no_origin:
            raise ConfigError("WS_ALLOWED_ORIGINS is empty; no client could connect.")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "WSConfig":
        target_env = os.environ if env is None else env
        return cls(
            token=target_env.get("WS_AUTH_TOKEN", ""),
            allowed_origins=_parse_origins(target_env.get("WS_ALLOWED_ORIGINS", "")),
            allow_no_origin=_truthy(target_env.get("WS_ALLOW_NO_ORIGIN")),
            host=target_env.get("WS_HOST", "127.0.0.1"),
            port=int(target_env.get("WS_PORT", "8765")),
            max_message_bytes=int(target_env.get("WS_MAX_MESSAGE_BYTES", "4096")),
            send_timeout=float(target_env.get("WS_SEND_TIMEOUT_SEC", "5")),
            client_queue=int(target_env.get("WS_CLIENT_QUEUE", "64")),
            max_clients=int(target_env.get("WS_MAX_CLIENTS", "200")),
        )

# ── Connected clients ──────────────────────────────────────────────────────────

class _Client:
    """One connection plus its bounded outbound queue and sender task."""

    def __init__(self, ws: ServerConnection, cfg: WSConfig):
        self.ws = ws
        self.cfg = cfg
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max(1, cfg.client_queue))
        self.task: Optional[asyncio.Task] = None
        self.dropped = False

    def offer(self, message: str) -> bool:
        try:
            self.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            return False

    async def sender(self) -> None:
        try:
            while True:
                msg = await self.queue.get()
                await asyncio.wait_for(self.ws.send(msg), timeout=self.cfg.send_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # any send error drops only this client
            log.info("dropping client after send failure (%s)", type(exc).__name__)
            _drop(self, CLOSE_TRY_AGAIN, "send failed")


_clients: set[_Client] = set()
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def _safe_close(ws: ServerConnection, code: int, reason: str) -> None:
    try:
        await ws.close(code, reason)  # bounded by close_timeout
    except Exception:
        pass


def _drop(client: _Client, code: int, reason: str) -> None:
    if client.dropped:
        return
    client.dropped = True
    _clients.discard(client)
    if client.task and client.task is not asyncio.current_task():
        client.task.cancel()
    _spawn(_safe_close(client.ws, code, reason))


async def broadcast(message: str) -> int:
    """Queue ``message`` for every client without awaiting any socket.

    A client whose queue is full (slow consumer) is dropped with 1013; it never
    delays delivery to the others. Returns the number of clients queued.
    """
    delivered = 0
    for client in list(_clients):
        if client.offer(message):
            delivered += 1
        else:
            log.info("dropping slow client (queue full)")
            _drop(client, CLOSE_TRY_AGAIN, "slow consumer")
    return delivered

# ── Handshake checks ───────────────────────────────────────────────────────────

def _offered_subprotocols(request: Request) -> list[str]:
    out: list[str] = []
    for value in request.headers.get_all("Sec-WebSocket-Protocol"):
        out.extend(p.strip() for p in value.split(",") if p.strip())
    return out


def _presented_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization")
    if auth:
        scheme, _, tok = auth.partition(" ")
        if scheme.lower() == "bearer" and tok.strip():
            return tok.strip()
        return None
    for proto in _offered_subprotocols(request):
        if proto.startswith(BEARER_PROTO_PREFIX):
            return proto[len(BEARER_PROTO_PREFIX):]
    return None


def make_process_request(cfg: WSConfig):
    expected = cfg.token.encode()

    def process_request(connection: ServerConnection, request: Request) -> Optional[Response]:
        origin = request.headers.get("Origin")
        if origin is None:
            if not cfg.allow_no_origin:
                return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")
        elif origin.rstrip("/") not in cfg.allowed_origins:
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")

        token = _presented_token(request)
        if token is None or not hmac.compare_digest(token.encode("utf-8", "replace"), expected):
            resp = connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
            resp.headers["WWW-Authenticate"] = 'Bearer realm="fc27-ws"'
            return resp

        if len(_clients) >= cfg.max_clients:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Too many clients\n")
        return None

    return process_request


def select_subprotocol(
    connection: ServerConnection, subprotocols: Sequence[Subprotocol]
) -> Optional[Subprotocol]:
    # Never select (echo) the bearer subprotocol; it carries the secret.
    return SUBPROTOCOL if SUBPROTOCOL in subprotocols else None

# ── WebSocket Handler ──────────────────────────────────────────────────────────

def _welcome() -> str:
    return json.dumps({
        "type":       "CONNECTED",
        "club":       "East Coasters FC",
        "club_id":    CLUB_ID,
        "platform":   PLATFORM,
        "fc_version": FC_VERSION,
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels":   CHANNELS,
    })


def make_handler(cfg: WSConfig):
    async def ws_handler(websocket: ServerConnection) -> None:
        client = _Client(websocket, cfg)
        client.offer(_welcome())
        client.task = asyncio.get_running_loop().create_task(client.sender())
        _clients.add(client)
        log.info("Client connected | Total: %d", len(_clients))
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    continue
                try:
                    data = json.loads(message)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("type") == "PING":
                    if not client.offer(json.dumps({"type": "PONG", "ts": time.time()})):
                        _drop(client, CLOSE_TRY_AGAIN, "slow consumer")
                        break
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            _clients.discard(client)
            client.dropped = True
            if client.task:
                client.task.cancel()
            log.info("Client disconnected | Total: %d", len(_clients))

    return ws_handler


def serve_ws(cfg: WSConfig):
    """Return the websockets ``serve`` context manager configured from ``cfg``."""
    return serve(
        make_handler(cfg), cfg.host, cfg.port,
        process_request=make_process_request(cfg),
        subprotocols=[SUBPROTOCOL],
        select_subprotocol=select_subprotocol,
        max_size=cfg.max_message_bytes,
        max_queue=16,
        open_timeout=cfg.open_timeout,
        ping_interval=cfg.ping_interval,
        ping_timeout=cfg.ping_timeout,
        close_timeout=cfg.close_timeout,
        compression=None,
        server_header=None,
    )

# ── Redis Subscriber ───────────────────────────────────────────────────────────

def _redis_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


async def _consume(pubsub) -> None:
    await pubsub.subscribe(*CHANNELS)
    log.info("Subscribed to %d Redis channels", len(CHANNELS))
    async for message in pubsub.listen():
        if not isinstance(message, dict) or message.get("type") != "message":
            continue
        channel = message.get("channel")
        data    = message.get("data")
        if data is None:
            continue
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                payload = {"data": payload}
            payload["_channel"] = channel
        except (TypeError, ValueError):
            payload = {"raw": data, "_channel": channel}
        n = await broadcast(json.dumps(payload, default=str))
        log.debug("Broadcast [%s] to %d clients", channel, n)


async def redis_subscriber(redis_client: Any = None, *, stop: Optional[asyncio.Event] = None,
                           max_backoff: float = 30.0) -> None:
    """Subscribe to FC27 channels and broadcast; reconnects with capped backoff.

    ``redis_client`` may be injected (tests use a fake). Without it a client is
    created from REDIS_URL, whose credentials are never logged.
    """
    stop = stop or asyncio.Event()
    backoff = 1.0
    while not stop.is_set():
        client = redis_client
        try:
            if client is None:
                import redis.asyncio as aioredis
                client = aioredis.from_url(REDIS_URL, decode_responses=True)
            await _consume(client.pubsub())
            if redis_client is not None:
                return  # injected source exhausted
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Redis subscriber error (%s); retrying in %.0fs",
                        type(exc).__name__, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(max_backoff, backoff * 2)

# ── Health endpoint (HTTP on WS_PORT+1) ───────────────────────────────────────

async def health_server(cfg: WSConfig):
    """Simple HTTP health endpoint for Docker health checks (no auth, no secrets)."""
    from aiohttp import web

    async def health(request):
        return web.json_response({
            "status":     "ok",
            "clients":    len(_clients),
            "fc_version": FC_VERSION,
        })

    app    = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site   = web.TCPSite(runner, cfg.host, cfg.port + 1)
    await site.start()
    log.info("Health endpoint on %s:%d/health", cfg.host, cfg.port + 1)
    return runner

# ── Main ───────────────────────────────────────────────────────────────────────

async def main(cfg: Optional[WSConfig] = None) -> int:
    try:
        cfg = cfg or WSConfig.from_env()
    except (ConfigError, ValueError) as exc:
        log.error("%s", exc)
        return 2

    log.info("FC27 Live WebSocket Server starting on ws://%s:%d (origins=%d, no-origin=%s)",
             cfg.host, cfg.port, len(cfg.allowed_origins), cfg.allow_no_origin)
    log.info("Redis host: %s", _redis_host(REDIS_URL))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass

    runner = None
    if cfg.health_enabled:
        try:
            runner = await health_server(cfg)
        except ImportError:
            log.warning("aiohttp not installed — health endpoint disabled")

    async with serve_ws(cfg):
        log.info("WebSocket server ready")
        sub = asyncio.create_task(redis_subscriber(stop=stop))
        await stop.wait()
        sub.cancel()
        for client in list(_clients):
            _drop(client, CLOSE_GOING_AWAY, "server shutdown")
        await asyncio.gather(sub, *list(_bg_tasks), return_exceptions=True)
    if runner is not None:
        await runner.cleanup()
    log.info("WebSocket server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
