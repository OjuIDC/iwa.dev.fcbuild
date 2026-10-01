"""
live_ws_server.py
=================
FC27 Pro Clubs — Live WebSocket Broadcast Server
East Coasters FC | Club ID: 330323

Bridges Redis pub/sub → WebSocket clients (browser, mobile, Windhover).
Any service that publishes to Redis channels is automatically broadcast
to all connected WebSocket clients in real time.

Channels subscribed:
  fc27:live:goal         — Goal scored (either team)
  fc27:live:match_start  — Match detected as started
  fc27:live:match_end    — Match ended / post-match detected
  fc27:live:match_state  — Periodic state update (every 5 min)
  fc27:match:new         — New match appeared in API (Tier 1)
  fc27:tilt:alert        — Tilt detected (3+ consecutive losses)
  fc27:fairplay:flag     — Fair Play violation flagged

Run:
  python -m src.live_ws_server        # port 8765
  WS_PORT=9000 python -m src.live_ws_server

Connect from browser:
  const ws = new WebSocket('ws://localhost:8765');
  ws.onmessage = e => console.log(JSON.parse(e.data));
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import weakref
from typing import Set

import redis.asyncio as aioredis
import websockets
from websockets.server import WebSocketServerProtocol

log = logging.getLogger("fc27.ws")
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [fc27-ws] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

REDIS_URL = os.getenv("REDIS_URL",  "redis://redis:6379")
WS_HOST   = os.getenv("WS_HOST",   "0.0.0.0")
WS_PORT   = int(os.getenv("WS_PORT", "8765"))
CLUB_ID   = os.getenv("FC27_CLUB_ID", "330323")
FC_VERSION= "27"
PLATFORM  = os.getenv("FC_PLATFORM", "common-gen5")

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

# ── Connected clients ──────────────────────────────────────────────────────────

_clients: Set[WebSocketServerProtocol] = weakref.WeakSet()

async def broadcast(message: str) -> None:
    """Send message to all connected WebSocket clients."""
    if not _clients:
        return
    dead = set()
    for ws in list(_clients):
        try:
            await ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            dead.add(ws)
    for ws in dead:
        _clients.discard(ws)

# ── WebSocket Handler ──────────────────────────────────────────────────────────

async def ws_handler(websocket: WebSocketServerProtocol) -> None:
    """Handle a new WebSocket connection."""
    _clients.add(websocket)
    client_addr = websocket.remote_address
    log.info("Client connected: %s | Total: %d", client_addr, len(_clients))

    # Send welcome + current club identity on connect
    await websocket.send(json.dumps({
        "type":       "CONNECTED",
        "club":       "East Coasters FC",
        "club_id":    CLUB_ID,
        "platform":   PLATFORM,
        "fc_version": FC_VERSION,
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels":   CHANNELS,
    }))

    try:
        async for message in websocket:
            # Handle ping from client
            try:
                data = json.loads(message)
                if data.get("type") == "PING":
                    await websocket.send(json.dumps({"type": "PONG", "ts": time.time()}))
            except Exception:
                pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(websocket)
        log.info("Client disconnected: %s | Total: %d", client_addr, len(_clients))

# ── Redis Subscriber ───────────────────────────────────────────────────────────

async def redis_subscriber() -> None:
    """Subscribe to all FC27 Redis channels and broadcast to WebSocket clients."""
    redis  = await aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe(*CHANNELS)
    log.info("Subscribed to %d Redis channels", len(CHANNELS))

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        channel = message["channel"]
        data    = message["data"]

        # Enrich with routing metadata
        try:
            payload = json.loads(data)
            payload["_channel"] = channel
        except Exception:
            payload = {"raw": data, "_channel": channel}

        await broadcast(json.dumps(payload))
        log.debug("Broadcast [%s] to %d clients", channel, len(_clients))

# ── Health endpoint (HTTP on WS_PORT+1) ───────────────────────────────────────

async def health_server() -> None:
    """Simple HTTP health endpoint for Docker health checks."""
    from aiohttp import web

    async def health(request):
        return web.json_response({
            "status":     "ok",
            "clients":    len(_clients),
            "channels":   len(CHANNELS),
            "fc_version": FC_VERSION,
            "platform":   PLATFORM,
        })

    app    = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site   = web.TCPSite(runner, WS_HOST, WS_PORT + 1)
    await site.start()
    log.info("Health endpoint: http://%s:%d/health", WS_HOST, WS_PORT + 1)

# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("FC27 Live WebSocket Server starting on ws://%s:%d", WS_HOST, WS_PORT)
    log.info("Redis: %s", REDIS_URL)

    try:
        await health_server()
    except ImportError:
        log.warning("aiohttp not installed — health endpoint disabled (pip install aiohttp)")

    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        log.info("WebSocket server ready ✅")
        await redis_subscriber()  # blocks, broadcasting until shutdown

if __name__ == "__main__":
    asyncio.run(main())
