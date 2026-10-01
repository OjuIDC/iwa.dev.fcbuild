"""Live WebSocket server: handshake auth, Origin allowlist, broadcast isolation.

Real servers on 127.0.0.1 with an ephemeral port; Redis is faked.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import src.live_ws_server as ws

TOKEN = "w" * 24 + "fedcba9876543210"  # 40 chars, test-only
ORIGIN = "http://localhost:3000"


def _cfg(**kw) -> ws.WSConfig:
    base = dict(token=TOKEN, allowed_origins=frozenset({ORIGIN, "https://fc27.example"}),
                host="127.0.0.1", port=0, open_timeout=2, ping_interval=5, ping_timeout=5,
                close_timeout=1, send_timeout=0.2, client_queue=4, health_enabled=False)
    base.update(kw)
    return ws.WSConfig(**base)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 15))


async def _with_server(cfg, body):
    ws._clients.clear()
    async with ws.serve_ws(cfg) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            return await body(f"ws://127.0.0.1:{port}")
        finally:
            ws._clients.clear()


async def _status_of(url, **kw) -> int:
    with pytest.raises(InvalidStatus) as ei:
        async with connect(url, open_timeout=3, **kw):
            pass
    return ei.value.response.status_code


def test_websocket_rejects_missing_auth():
    async def body(url):
        assert await _status_of(url, origin=ORIGIN) == 401
        assert await _status_of(url, origin=ORIGIN,
                                additional_headers={"Authorization": "Bearer " + "x" * 40}) == 401
        assert await _status_of(url, origin=ORIGIN,
                                additional_headers={"Authorization": "Basic " + TOKEN}) == 401
        assert await _status_of(url, origin=ORIGIN,
                                subprotocols=["fc27.v1", "fc27.bearer." + TOKEN[:-1]]) == 401
        assert await _status_of(url, origin=ORIGIN, subprotocols=["fc27.v1"]) == 401
    run(_with_server(_cfg(), body))


def test_websocket_rejects_untrusted_origin():
    good = {"Authorization": f"Bearer {TOKEN}"}

    async def body(url):
        for bad in ["http://evil.example", "http://localhost", "http://localhost:3000.evil.example",
                    "null", "HTTP://LOCALHOST:3000x"]:
            assert await _status_of(url, origin=bad, additional_headers=good) == 403
        # Missing Origin is rejected by default (WS_ALLOW_NO_ORIGIN unset).
        assert await _status_of(url, additional_headers=good) == 403
    run(_with_server(_cfg(), body))


def test_websocket_no_origin_allowed_only_when_enabled():
    async def body(url):
        async with connect(url, open_timeout=3,
                           additional_headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            assert json.loads(await c.recv())["type"] == "CONNECTED"
        # Still requires auth and still enforces the allowlist when Origin IS sent.
        assert await _status_of(url) == 401
        assert await _status_of(url, origin="http://evil.example",
                                additional_headers={"Authorization": f"Bearer {TOKEN}"}) == 403
    run(_with_server(_cfg(allow_no_origin=True), body))


def test_websocket_accepts_valid_token_and_origin_and_receives_broadcast():
    class FakePubSub:
        def __init__(self, items):
            self.items = items
            self.subscribed = ()

        async def subscribe(self, *channels):
            self.subscribed = channels

        async def listen(self):
            for item in self.items:
                yield item

    class FakeRedis:
        def __init__(self, items):
            self.ps = FakePubSub(items)

        def pubsub(self):
            return self.ps

    async def body(url):
        hdr_client = await connect(url, origin=ORIGIN, open_timeout=3,
                                   additional_headers={"Authorization": f"Bearer {TOKEN}"})
        browser = await connect(url, origin="https://fc27.example", open_timeout=3,
                                subprotocols=["fc27.v1", "fc27.bearer." + TOKEN])
        try:
            assert browser.subprotocol == "fc27.v1"          # token is never echoed back
            assert hdr_client.subprotocol is None
            for c in (hdr_client, browser):
                hello = json.loads(await c.recv())
                assert hello["type"] == "CONNECTED" and hello["fc_version"] == "27"
            fake = FakeRedis([
                {"type": "subscribe", "channel": "fc27:live:goal", "data": 1},
                {"type": "message", "channel": "fc27:live:goal", "data": json.dumps({"scorer": "AJ"})},
                {"type": "message", "channel": "fc27:tilt:alert", "data": "not-json"},
            ])
            await ws.redis_subscriber(fake)
            assert "fc27:live:goal" in fake.ps.subscribed
            for c in (hdr_client, browser):
                m1 = json.loads(await asyncio.wait_for(c.recv(), 3))
                m2 = json.loads(await asyncio.wait_for(c.recv(), 3))
                assert m1 == {"scorer": "AJ", "_channel": "fc27:live:goal"}
                assert m2 == {"raw": "not-json", "_channel": "fc27:tilt:alert"}
            await browser.send(json.dumps({"type": "PING"}))
            assert json.loads(await asyncio.wait_for(browser.recv(), 3))["type"] == "PONG"
        finally:
            await hdr_client.close()
            await browser.close()
    run(_with_server(_cfg(), body))


def test_websocket_enforces_max_message_size():
    async def body(url):
        async with connect(url, origin=ORIGIN, open_timeout=3,
                           additional_headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            await c.recv()
            await c.send("x" * 5000)
            with pytest.raises(ConnectionClosed) as ei:
                for _ in range(5):
                    await asyncio.wait_for(c.recv(), 3)
            assert ei.value.rcvd is not None and ei.value.rcvd.code == 1009
    run(_with_server(_cfg(max_message_bytes=1024), body))


def test_websocket_max_clients():
    async def body(url):
        hdr = {"Authorization": f"Bearer {TOKEN}"}
        async with connect(url, origin=ORIGIN, open_timeout=3, additional_headers=hdr) as c:
            await c.recv()
            assert await _status_of(url, origin=ORIGIN, additional_headers=hdr) == 503
    run(_with_server(_cfg(max_clients=1), body))


class _FakeWS:
    def __init__(self, mode: str):
        self.mode = mode
        self.sent: list[str] = []
        self.closed_with = None

    async def send(self, msg):
        if self.mode == "hang":
            await asyncio.Event().wait()
        if self.mode == "boom":
            raise RuntimeError("socket exploded")  # not ConnectionClosed
        self.sent.append(msg)

    async def close(self, code=1000, reason=""):
        self.closed_with = code


def test_broadcast_isolates_slow_and_failing_clients():
    async def body():
        ws._clients.clear()
        cfg = _cfg(send_timeout=0.1, client_queue=8)
        fast, hang, boom = _FakeWS("ok"), _FakeWS("hang"), _FakeWS("boom")
        clients = []
        for f in (fast, hang, boom):
            c = ws._Client(f, cfg)
            c.task = asyncio.get_running_loop().create_task(c.sender())
            ws._clients.add(c)
            clients.append(c)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for i in range(5):
            await ws.broadcast(f"m{i}")      # never awaits a socket
        assert loop.time() - t0 < 0.05
        await asyncio.sleep(0.4)
        assert fast.sent == [f"m{i}" for i in range(5)]
        live = {c.ws for c in ws._clients}
        assert live == {fast}
        assert hang.closed_with == ws.CLOSE_TRY_AGAIN and boom.closed_with == ws.CLOSE_TRY_AGAIN
        for c in clients:
            if c.task:
                c.task.cancel()
        ws._clients.clear()

        # Backlog overflow (consumer not draining) drops only that client.
        stuck_ws, ok_ws = _FakeWS("hang"), _FakeWS("ok")
        small = _cfg(client_queue=2)
        stuck, ok = ws._Client(stuck_ws, small), ws._Client(ok_ws, _cfg(client_queue=8))
        ws._clients.update({stuck, ok})
        for i in range(3):
            await ws.broadcast(f"b{i}")
        await asyncio.sleep(0)
        assert ws._clients == {ok} and ok.queue.qsize() == 3
        assert stuck_ws.closed_with == ws.CLOSE_TRY_AGAIN
        ws._clients.clear()
    run(body())


@pytest.mark.parametrize("env,msg", [
    ({}, "WS_AUTH_TOKEN"),
    ({"WS_AUTH_TOKEN": "short", "WS_ALLOWED_ORIGINS": ORIGIN}, "WS_AUTH_TOKEN"),
    ({"WS_AUTH_TOKEN": TOKEN, "WS_ALLOWED_ORIGINS": "*"}, "wildcard"),
    ({"WS_AUTH_TOKEN": TOKEN, "WS_ALLOWED_ORIGINS": "http://a.example/*"}, "wildcard"),
    ({"WS_AUTH_TOKEN": TOKEN, "WS_ALLOWED_ORIGINS": ""}, "empty"),
])
def test_websocket_refuses_unsafe_config(env, msg):
    with pytest.raises(ws.ConfigError, match=msg):
        ws.WSConfig.from_env(env)


def test_websocket_defaults_are_safe():
    cfg = ws.WSConfig.from_env({"WS_AUTH_TOKEN": TOKEN, "WS_ALLOWED_ORIGINS": ORIGIN})
    assert cfg.host == "127.0.0.1" and cfg.allow_no_origin is False
    assert cfg.max_message_bytes <= 64 * 1024
    assert ws.WSConfig.from_env({"WS_AUTH_TOKEN": TOKEN, "WS_ALLOWED_ORIGINS": ORIGIN,
                                 "WS_ALLOW_NO_ORIGIN": "true"}).allow_no_origin is True


def test_websocket_main_refuses_without_token(monkeypatch):
    monkeypatch.delenv("WS_AUTH_TOKEN", raising=False)
    assert asyncio.run(ws.main()) == 2


def test_redis_url_and_token_never_logged(caplog, monkeypatch):
    secret_url = "redis://:s3cr3t-pass@redis-host:6379/0"
    monkeypatch.setattr(ws, "REDIS_URL", secret_url)
    assert ws._redis_host(secret_url) == "redis-host"

    class Boom:
        def pubsub(self):
            raise ConnectionError(secret_url)

    async def body():
        stop = asyncio.Event()
        task = asyncio.create_task(ws.redis_subscriber(Boom(), stop=stop, max_backoff=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)

    with caplog.at_level(logging.DEBUG, logger="fc27.ws"):
        run(body())

        async def handshake(url):
            async with connect(url, origin=ORIGIN, open_timeout=3,
                               additional_headers={"Authorization": f"Bearer {TOKEN}"}) as c:
                await c.recv()
        run(_with_server(_cfg(), handshake))
    text = caplog.text
    assert "s3cr3t" not in text and TOKEN not in text and "redis://" not in text
