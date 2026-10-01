"""MCP proxy: retry/backoff, caching, audit metadata, clubId validation, search fallback.

All EA traffic is mocked by replacing ``urlopen`` in the server module; the
real EA API is never contacted.
"""
from __future__ import annotations

import email.message
import io
import json
import time
from email.utils import formatdate
from urllib.error import HTTPError, URLError

import pytest

import src.fc27_mcp_server as srv
from src.mcp_retry import RetryPolicy, UpstreamError, call_with_retry, parse_retry_after


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        self.t += d


def _policy(clock: FakeClock, **kw) -> RetryPolicy:
    base = dict(max_attempts=4, budget_sec=30.0, base_delay=1.0, max_delay=8.0,
                max_retry_after=20.0, max_403_retries=1,
                sleep=clock.sleep, rand=lambda: 1.0, clock=clock.now)
    base.update(kw)
    return RetryPolicy(**base)


def _http_error(code: int, retry_after: str | None = None) -> HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return HTTPError("https://proclubs.ea.com/api/fc/x", code, "err", hdrs, io.BytesIO(b""))


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeEA:
    """Scripted urlopen replacement: each item is a payload or an exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []

    def __call__(self, req, timeout=None):
        self.calls.append(req.full_url)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)


@pytest.fixture()
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(srv, "_retry_policy", _policy(c))
    srv._cache.clear()
    srv._rate.reset()
    yield c
    srv._cache.clear()
    srv._rate.reset()


def _install(monkeypatch, script) -> FakeEA:
    fake = FakeEA(script)
    monkeypatch.setattr(srv, "urlopen", fake)
    return fake


# ── Retry semantics ────────────────────────────────────────────────────────────

def test_mcp_retries_429_with_retry_after(monkeypatch, clock):
    future = formatdate(time.time() + 5, usegmt=True)
    fake = _install(monkeypatch, [_http_error(429, "3"), _http_error(429, future), [{"matchId": "1"}]])
    out = srv.get_matches("330323")
    assert len(fake.calls) == 3
    assert clock.sleeps[0] == 3.0
    assert 3.5 <= clock.sleeps[1] <= 5.0  # HTTP-date form honored
    assert out["count"] == 1 and out["audit"]["cached"] is False


def test_mcp_retry_after_is_capped_and_invalid_falls_back_to_backoff(monkeypatch, clock):
    fake = _install(monkeypatch, [_http_error(429, "999"), _http_error(429, "soon"),
                                  _http_error(429, "-5"), {"ok": True}])
    srv.ea_get("clubs/info", {"clubIds": "1"})
    assert len(fake.calls) == 4
    # 999 capped to max_retry_after=20; then invalid values -> backoff 2, 4 (rand=1.0)
    assert clock.sleeps == [20.0, 2.0, 4.0]


def test_mcp_429_without_retry_after_uses_jittered_backoff():
    c = FakeClock()
    rolls = iter([0.0, 0.5, 1.0])
    calls = []

    def fn():
        calls.append(1)
        raise _http_error(429)

    with pytest.raises(UpstreamError) as ei:
        call_with_retry(fn, "clubs/matches", _policy(c, rand=lambda: next(rolls)))
    # equal jitter: d/2 + r*d/2 with d = 1, 2, 4
    assert c.sleeps == [0.5, 1.5, 4.0]
    assert len(calls) == 4
    assert ei.value.kind == "upstream_rate_limited" and ei.value.status == 429
    assert ei.value.attempts == 4


def test_mcp_retry_is_bounded(monkeypatch, clock):
    fake = _install(monkeypatch, [_http_error(503)])
    with pytest.raises(UpstreamError) as ei:
        srv.ea_get("clubs/matches", {"clubIds": "1"})
    assert len(fake.calls) == 4          # MCP_RETRY_MAX_ATTEMPTS default
    assert clock.sleeps == [1.0, 2.0, 4.0]
    err = ei.value.to_dict()
    assert err["kind"] == "upstream_unavailable" and err["attempts"] == 4 and err["status"] == 503

    # Total budget also bounds retries: 1s slept, next 2s would exceed 2.5s.
    c2 = FakeClock()
    calls = []

    def fn():
        calls.append(1)
        raise _http_error(503)

    with pytest.raises(UpstreamError) as ei2:
        call_with_retry(fn, "clubs/matches", _policy(c2, budget_sec=2.5))
    assert len(calls) == 2 and c2.sleeps == [1.0]
    assert ei2.value.kind == "retry_budget_exhausted"


def test_mcp_retry_after_beyond_budget_fails_without_sleeping():
    c = FakeClock()
    calls = []

    def fn():
        calls.append(1)
        raise _http_error(429, "15")

    with pytest.raises(UpstreamError) as ei:
        call_with_retry(fn, "x", _policy(c, budget_sec=10))
    assert calls == [1] and c.sleeps == []
    assert ei.value.kind == "retry_budget_exhausted" and ei.value.retry_after_sec == 15


def test_mcp_repeated_403_fails_closed(monkeypatch, clock):
    fake = _install(monkeypatch, [_http_error(403)])
    with pytest.raises(UpstreamError) as ei:
        srv.ea_get("clubs/info", {"clubIds": "1"})
    assert len(fake.calls) == 2           # one bounded retry, then fail closed
    assert clock.sleeps == [1.0]
    err = ei.value.to_dict()
    assert err["kind"] == "upstream_forbidden" and err["status"] == 403 and err["retryable"] is False
    assert "refused" in err["message"]
    blob = json.dumps(err)
    for leak in ("User-Agent", "Mozilla", "Referer", "proclubs.ea.com", "Authorization"):
        assert leak not in blob

    # Through the JSON-RPC surface the error is structured as well.
    srv._cache.clear()
    resp = srv.handle_mcp({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": "get_club_info", "arguments": {"club_id": "330323"}}})
    assert resp["error"]["data"]["kind"] == "upstream_forbidden"


def test_mcp_403_with_zero_retries_configured():
    c = FakeClock()
    calls = []

    def fn():
        calls.append(1)
        raise _http_error(403)

    with pytest.raises(UpstreamError):
        call_with_retry(fn, "x", _policy(c, max_403_retries=0))
    assert calls == [1] and c.sleeps == []


def test_mcp_retries_5xx_then_succeeds(monkeypatch, clock):
    fake = _install(monkeypatch, [_http_error(502), _http_error(500), _http_error(504), {"330323": {"name": "ECFC"}}])
    out = srv.get_club_info("330323")
    assert len(fake.calls) == 4
    assert clock.sleeps == [1.0, 2.0, 4.0]
    assert out["data"] == {"name": "ECFC"}


def test_mcp_retries_network_errors(monkeypatch, clock):
    fake = _install(monkeypatch, [URLError("connection refused"), TimeoutError(), {"ok": 1}])
    data, cached = srv.ea_get("clubs/info", {"clubIds": "1"})
    assert data == {"ok": 1} and cached is False
    assert len(fake.calls) == 3 and clock.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("code", [400, 401, 404, 405, 409, 410, 422])
def test_mcp_non_retryable_4xx_not_retried(monkeypatch, clock, code):
    fake = _install(monkeypatch, [_http_error(code)])
    with pytest.raises(UpstreamError) as ei:
        srv.ea_get("clubs/info", {"clubIds": "1"})
    assert len(fake.calls) == 1 and clock.sleeps == []
    assert ei.value.kind == "upstream_http_error" and ei.value.status == code


def test_parse_retry_after_forms():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("1.5") is None
    assert parse_retry_after("٣") is None  # non-ASCII digit
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # past date
    assert 9 <= parse_retry_after(formatdate(time.time() + 10, usegmt=True)) <= 10


def test_retry_policy_env(monkeypatch):
    monkeypatch.setenv("MCP_RETRY_MAX_ATTEMPTS", "6")
    monkeypatch.setenv("MCP_RETRY_BUDGET_SEC", "12")
    p = RetryPolicy.from_env()
    assert p.max_attempts == 6 and p.budget_sec == 12.0
    monkeypatch.delenv("MCP_RETRY_MAX_ATTEMPTS")
    monkeypatch.delenv("MCP_RETRY_BUDGET_SEC")
    p = RetryPolicy.from_env()
    assert p.max_attempts == 4 and p.budget_sec == 30.0


# ── Cache / rate limit / audit ─────────────────────────────────────────────────

def test_mcp_cache_hit_avoids_ea_call(monkeypatch, clock):
    fake = _install(monkeypatch, [[{"matchId": "1"}]])
    first = srv.get_matches("330323")
    second = srv.get_matches("330323")
    assert len(fake.calls) == 1
    assert first["audit"]["cached"] is False and second["audit"]["cached"] is True
    # A cached response is served even if EA is now failing hard.
    monkeypatch.setattr(srv, "urlopen", FakeEA([_http_error(403)]))
    third = srv.get_matches("330323")
    assert third["audit"]["cached"] is True and third["matches"] == [{"matchId": "1"}]


def test_mcp_rate_limit_still_enforced(monkeypatch, clock):
    monkeypatch.setattr(srv, "_rate", srv._EndpointRateLimiter(max_calls=2, window=60))
    _install(monkeypatch, [{"ok": 1}])
    srv.ea_get("clubs/info", {"clubIds": "1"})
    srv.ea_get("clubs/info", {"clubIds": "2"})
    with pytest.raises(UpstreamError) as ei:
        srv.ea_get("clubs/info", {"clubIds": "3"})
    assert ei.value.kind == "mcp_rate_limited"


def test_mcp_audit_uses_request_platform(monkeypatch, clock):
    _install(monkeypatch, [[]])
    out = srv.get_matches("330323", platform="nx")
    assert out["audit"]["fc_version"] == "27"
    assert out["audit"]["platform"] == "nx"
    dash_fake = _install(monkeypatch, [{"330323": {}}])
    dash = srv.get_club_dashboard("330323", platform="common-gen4")
    assert dash["audit"] == {**dash["audit"], "fc_version": "27", "platform": "common-gen4"}
    assert all("platform=common-gen4" in u for u in dash_fake.calls)


# ── clubId validation ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["330323", "0", "123456789012", 330323])
def test_club_id_accepts_ascii_digits_mcp(value):
    assert srv._validate_club_id(value) == str(value)


@pytest.mark.parametrize("value", [
    "٣٣٠", "３３０", "٣٣٠٣٢٣", "-330323", "+330323", " 330323", "330323 ", "33 03",
    "330.323", "330323\n", "33a0323", "330323abc", "", "1234567890123", None, True, 3.0, ["1"],
    "../../admin", "1; DROP TABLE",
])
def test_club_id_rejects_unicode_digits_mcp(value):
    with pytest.raises(ValueError):
        srv._validate_club_id(value)


def test_invalid_club_id_never_reaches_ea(monkeypatch, clock):
    fake = _install(monkeypatch, [{"ok": 1}])
    resp = srv.handle_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "get_matches", "arguments": {"club_id": "３３０"}}})
    assert resp["error"]["code"] == -32602 and fake.calls == []


# ── search_club fallback ───────────────────────────────────────────────────────

@pytest.mark.parametrize("failure", [_http_error(404), _http_error(400), URLError("down")])
def test_search_club_http_failure_returns_manual_guidance(monkeypatch, clock, failure):
    _install(monkeypatch, [failure])
    out = srv.search_club("East Coasters")
    assert out["results"] == []
    assert "clubId=" in out["fallback"] and "ea.com" in out["fallback"]
    assert "https://www.ea.com/games/ea-sports-fc/clubs/overview?clubId=<CLUB_ID>&platform=common-gen5" in out["fallback"]
    assert out["audit"]["fc_version"] == "27" and out["audit"]["platform"] == "common-gen5"


def test_search_club_empty_results_returns_guidance(monkeypatch, clock):
    _install(monkeypatch, [[]])
    out = srv.search_club("Nobody FC", platform="nx")
    assert out["results"] == [] and "clubId=" in out["fallback"]
    assert out["audit"]["platform"] == "nx"


@pytest.mark.parametrize("payload,expected", [
    ([{"clubId": 1, "name": "A"}, {"clubId": 2, "name": "B"}], ["1", "2"]),
    ({"clubId": 5, "name": "Solo"}, ["5"]),
    ({"5": {"clubId": 5, "name": "Keyed"}, "6": {"clubId": 6, "name": "K2"}}, ["5", "6"]),
    ({"error": "x", "code": 3}, []),
    (["junk", 3, None], []),
    ("string payload", []),
])
def test_search_club_handles_list_or_dict(monkeypatch, clock, payload, expected):
    _install(monkeypatch, [payload])
    out = srv.search_club("East Coasters")
    assert [r["clubId"] for r in out["results"]] == expected
    if not expected:
        assert "fallback" in out


def test_get_club_info_tolerates_list_payload(monkeypatch, clock):
    _install(monkeypatch, [[{"name": "x"}]])
    assert srv.get_club_info("330323")["data"] == [{"name": "x"}]
