"""MCP HTTP surface: bearer auth, body limits, bounded concurrency, shutdown.

Servers bind 127.0.0.1 on an ephemeral port. EA is mocked; never contacted.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

import src.fc27_mcp_server as srv

TOKEN = "t" * 24 + "0123456789abcdef"  # 40 chars, test-only


@pytest.fixture()
def server(monkeypatch):
    srv._cache.clear()
    srv._rate.reset()
    s = srv.make_server("127.0.0.1", 0, api_token=TOKEN, max_workers=2,
                        request_timeout=2.0, sse_keepalive_sec=0.05, sse_max_sec=5.0)
    t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield s
    s.shutdown()
    s.server_close(join_timeout=3)
    t.join(3)
    srv._cache.clear()
    srv._rate.reset()


def _req(server, method, path, body=None, token=TOKEN, headers=None):
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    h = dict(headers or {})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), raw


def _raw(server, payload: bytes) -> bytes:
    with socket.create_connection(server.server_address, timeout=5) as s:
        s.sendall(payload)
        chunks = []
        while True:
            try:
                b = s.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not b:
                break
            chunks.append(b)
    return b"".join(chunks)


RPC_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def test_mcp_auth_is_required(server):
    # Missing / malformed / wrong token -> 401 on every non-health endpoint.
    for method, path, body in [("GET", "/tools", None), ("GET", "/sse", None),
                               ("POST", "/", RPC_LIST), ("POST", "/mcp", RPC_LIST),
                               ("GET", "/nope", None)]:
        assert _req(server, method, path, body, token=None)[0] == 401
        assert _req(server, method, path, body, token="x" * 40)[0] == 401
        assert _req(server, method, path, body, token=TOKEN[:-1])[0] == 401
        assert _req(server, method, path, body, token=None,
                    headers={"Authorization": f"Basic {TOKEN}"})[0] == 401
    status, headers, raw = _req(server, "POST", "/", RPC_LIST, token=None)
    assert headers.get("WWW-Authenticate", "").startswith("Bearer")
    assert TOKEN.encode() not in raw

    # Valid token works.
    status, _, raw = _req(server, "POST", "/", RPC_LIST)
    assert status == 200 and len(json.loads(raw)["result"]["tools"]) == 6
    assert _req(server, "GET", "/tools")[0] == 200
    assert _req(server, "GET", "/nope")[0] == 404

    # /health is open and minimal.
    status, _, raw = _req(server, "GET", "/health", token=None)
    assert status == 200 and json.loads(raw) == {"status": "ok", "fc_version": "27"}


def test_mcp_server_refuses_to_start_without_strong_token(monkeypatch):
    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    with pytest.raises(srv.ConfigError):
        srv.make_server("127.0.0.1", 0)
    monkeypatch.setenv("MCP_API_TOKEN", "short-token")
    with pytest.raises(srv.ConfigError):
        srv.make_server("127.0.0.1", 0)
    with pytest.raises(srv.ConfigError):
        srv.make_server("127.0.0.1", 0, api_token="y" * 31)
    with pytest.raises(srv.ConfigError):
        srv.make_server("127.0.0.1", 0, api_token=TOKEN, allowed_origin="*")
    monkeypatch.setenv("MCP_API_TOKEN", "")
    assert srv.main() == 2  # refuses to start, exit code 2


def test_mcp_default_host_is_loopback():
    from pathlib import Path
    src = Path(srv.__file__).read_text()
    assert 'os.getenv("MCP_HOST", "127.0.0.1")' in src and "0.0.0.0" not in src
    s = srv.make_server(port=0, api_token=TOKEN)
    try:
        assert s.server_address[0] == srv.HOST
    finally:
        s.server_close()


def test_mcp_oversize_body_413(server):
    big = 64 * 1024 + 1
    out = _raw(server, (
        f"POST / HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {big}\r\n\r\n").encode())
    assert out.startswith(b"HTTP/1.0 413")
    # Exactly at the cap is accepted (and parsed).
    body = json.dumps(RPC_LIST).encode()
    body = body[:-1] + b" " * (64 * 1024 - len(body)) + b"}"
    assert len(body) == 64 * 1024
    out = _raw(server, (
        f"POST / HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
    assert out.startswith(b"HTTP/1.0 200") and b'"tools"' in out


def test_mcp_content_length_required_and_valid(server):
    auth = f"Authorization: Bearer {TOKEN}\r\n"
    assert _raw(server, f"POST / HTTP/1.1\r\nHost: x\r\n{auth}\r\n".encode()).startswith(b"HTTP/1.0 411")
    assert _raw(server, (f"POST / HTTP/1.1\r\nHost: x\r\n{auth}Transfer-Encoding: chunked\r\n\r\n"
                         "0\r\n\r\n").encode()).startswith(b"HTTP/1.0 411")
    assert _raw(server, f"POST / HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: -1\r\n\r\n".encode()).startswith(b"HTTP/1.0 400")
    assert _raw(server, f"POST / HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: ٣\r\n\r\n".encode("utf-8")).startswith(b"HTTP/1.0 400")
    # Malformed JSON -> JSON-RPC parse error, no traceback.
    out = _raw(server, f"POST / HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: 3\r\n\r\n{{x}}".encode())
    assert b"-32700" in out and b"Traceback" not in out


def test_mcp_returns_503_when_saturated(server):
    held = [server.slots.acquire(blocking=False) for _ in range(server.max_workers)]
    assert all(held)
    try:
        status, headers, raw = _req(server, "GET", "/health", token=None)
        assert status == 503 and headers.get("Retry-After") == "1"
    finally:
        for _ in held:
            server.slots.release()
    assert _req(server, "GET", "/health", token=None)[0] == 200


def test_mcp_concurrent_requests_are_served(server, monkeypatch):
    gate = threading.Event()
    started = threading.Barrier(3, timeout=5)

    def slow_tool(**_):
        started.wait()
        gate.wait(5)
        return {"ok": True}

    monkeypatch.setitem(srv.TOOLS, "get_club_info", slow_tool)
    results = []
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_club_info", "arguments": {}}}

    def worker():
        results.append(_req(server, "POST", "/", call)[0])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    started.wait()  # both requests in flight concurrently (would deadlock if single-threaded)
    # Pool (2) is now full -> third request is shed with 503.
    assert _req(server, "GET", "/health", token=None)[0] == 503
    gate.set()
    for t in threads:
        t.join(5)
    assert results == [200, 200]


def test_mcp_tools_call_end_to_end_with_mocked_ea(server, monkeypatch):
    class _Resp:
        def read(self, n=-1):
            return b'[{"matchId": "9"}]'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = []
    monkeypatch.setattr(srv, "urlopen", lambda req, timeout=None: calls.append(req) or _Resp())
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_matches", "arguments": {"club_id": "330323", "platform": "nx"}}}
    status, _, raw = _req(server, "POST", "/", call)
    result = json.loads(json.loads(raw)["result"]["content"][0]["text"])
    assert status == 200 and result["count"] == 1
    assert result["audit"]["fc_version"] == "27" and result["audit"]["platform"] == "nx"
    _req(server, "POST", "/", call)
    assert len(calls) == 1  # second served from cache


def test_mcp_cors_never_wildcard(server):
    _, headers, _ = _req(server, "GET", "/health", token=None)
    assert headers["Access-Control-Allow-Origin"] == "http://localhost"
    status, headers, _ = _req(server, "OPTIONS", "/", token=None)
    assert status == 204 and "Authorization" in headers["Access-Control-Allow-Headers"]
    assert headers["Access-Control-Allow-Origin"] != "*"


def test_mcp_sse_streams_and_stops_on_shutdown():
    s = srv.make_server("127.0.0.1", 0, api_token=TOKEN, max_workers=2,
                        sse_keepalive_sec=0.05, sse_max_sec=30)
    t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    sock = socket.create_connection(s.server_address, timeout=5)
    sock.sendall(f"GET /sse HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
    buf = b""
    while buf.count(b": keepalive") < 2:
        buf += sock.recv(4096)
    assert b"event: ready" in buf  # stream stays open beyond the first event
    t0 = time.monotonic()
    s.shutdown()
    s.server_close(join_timeout=3)  # graceful: SSE loop observes the stop flag
    assert time.monotonic() - t0 < 2
    rest = b""
    try:
        while chunk := sock.recv(4096):
            rest += chunk
    except OSError:
        pass
    sock.close()
    assert b"event: bye" in buf + rest
    t.join(3)
