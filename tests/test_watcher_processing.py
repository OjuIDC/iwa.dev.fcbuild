"""Live match watcher: idempotent Redis processed-match state (no network, no real Redis)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import src.live_match_watcher as w


class FakeRedis:
    """Minimal in-memory async Redis covering the commands the watcher uses."""

    def __init__(self):
        self.kv: dict[str, bytes] = {}
        self.hashes: dict[str, dict[str, bytes]] = {}
        self.lists: dict[str, list[bytes]] = {}
        self.expiry: dict[str, float] = {}
        self.now = 1000.0
        self.published: list[tuple[str, dict]] = []
        self.fail_publish = 0

    @staticmethod
    def _b(v) -> bytes:
        return v if isinstance(v, bytes) else str(v).encode()

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _alive(self, key: str) -> bool:
        exp = self.expiry.get(key)
        if exp is not None and exp <= self.now:
            self.kv.pop(key, None)
            self.hashes.pop(key, None)
            self.lists.pop(key, None)
            self.expiry.pop(key, None)
            return False
        return key in self.kv or key in self.hashes or key in self.lists

    async def ping(self):
        return True

    async def get(self, key):
        return self.kv.get(key) if self._alive(key) else None

    async def set(self, key, value, nx=False, ex=None):
        if nx and self._alive(key):
            return None
        self.kv[key] = self._b(value)
        self.expiry.pop(key, None)
        if ex is not None:
            self.expiry[key] = self.now + ex
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if self._alive(k):
                n += 1
            for store in (self.kv, self.hashes, self.lists, self.expiry):
                store.pop(k, None)
        return n

    async def exists(self, key):
        return int(self._alive(key))

    async def expire(self, key, seconds):
        if not self._alive(key):
            return False
        self.expiry[key] = self.now + seconds
        return True

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field) if self._alive(key) else None

    async def hgetall(self, key):
        # decode_responses=False: real redis returns bytes field names too
        return {f.encode(): v for f, v in self.hashes.get(key, {}).items()} if self._alive(key) else {}

    async def hset(self, key, field=None, value=None, mapping=None):
        self._alive(key)
        h = self.hashes.setdefault(key, {})
        items = dict(mapping or {})
        if field is not None:
            items[field] = value
        for f, v in items.items():
            h[f] = self._b(v)
        return len(items)

    async def hsetnx(self, key, field, value):
        self._alive(key)
        h = self.hashes.setdefault(key, {})
        if field in h:
            return 0
        h[field] = self._b(value)
        return 1

    async def hincrby(self, key, field, amount=1):
        self._alive(key)
        h = self.hashes.setdefault(key, {})
        h[field] = self._b(int(h.get(field, b"0")) + amount)
        return int(h[field])

    async def lpush(self, key, *values):
        self._alive(key)
        lst = self.lists.setdefault(key, [])
        for v in values:
            lst.insert(0, self._b(v))
        return len(lst)

    async def ltrim(self, key, start, end):
        if key in self.lists:
            self.lists[key] = self.lists[key][start:end + 1]
        return True

    async def lrange(self, key, start, end):
        lst = self.lists.get(key, []) if self._alive(key) else []
        return lst[start:end + 1] if end >= 0 else lst[start:]

    async def publish(self, channel, message):
        if self.fail_publish:
            self.fail_publish -= 1
            raise ConnectionError("publish failed")
        self.published.append((channel, json.loads(message)))
        return 1

    def channel(self, name):
        return [p for c, p in self.published if c == name]


def _match(mid: int, ts: int, result="win") -> dict:
    return {
        "matchId": str(mid), "timestamp": ts,
        "clubs": {w.CLUB_ID: {"result": result, "goals": 2, "goalsAgainst": 1},
                  "999": {"name": "Opp"}},
        "players": {},
    }


class Harness:
    """Mocks the MCP proxy and Graph API over httpx.MockTransport."""

    def __init__(self, monkeypatch, matches):
        self.matches = matches
        self.graph_status = [201]
        self.graph_calls: list[str] = []
        self.mcp_headers: list[httpx.Headers] = []
        self.graph_headers: list[httpx.Headers] = []
        self.seq = 0
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/run":
                self.graph_headers.append(request.headers)
                body = json.loads(request.content)
                self.graph_calls.append(body["match"]["matchId"])
                status = self.graph_status.pop(0) if len(self.graph_status) > 1 else self.graph_status[0]
                if status >= 400:
                    return httpx.Response(status, json={"detail": "boom"})
                self.seq += 1
                return httpx.Response(status, json={"thread_id": f"fc27-{w.CLUB_ID}-run-20261001-{self.seq:03d}"})
            self.mcp_headers.append(request.headers)
            rpc = json.loads(request.content)
            if rpc["method"] == "tools/list":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
            text = json.dumps({"matches": self.matches})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {"content": [{"type": "text", "text": text}]}})

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(w.httpx, "AsyncClient",
                            lambda *a, **kw: real_client(*a, transport=transport, **kw))
        monkeypatch.setattr(w, "GRAPH_SERVICE_TOKEN", "svc-token-placeholder")
        monkeypatch.setenv("MCP_API_TOKEN", "mcp-token-placeholder")


def run(coro):
    return asyncio.run(coro)


async def _baseline(redis):
    """Mark first boot as done so subsequent matches count as new."""
    await redis.set(w._baseline_key("leagueMatch"), "1")


def test_watcher_does_not_mark_failed_match_processed(monkeypatch):
    redis = FakeRedis()
    h = Harness(monkeypatch, [_match(101, 10)])
    h.graph_status = [500, 201]
    run(_baseline(redis))

    out = run(w.poll_match_type(redis, "leagueMatch"))
    assert out == [("101", "failed")]
    assert run(w.match_state(redis, "101")) != w.STATE_DONE
    assert run(redis.hget(w._record_key("101"), "attempts")) == b"1"
    assert run(redis.get(w._lease_key("101"))) is None, "lease released after failure"
    assert w.CH_MATCH_END not in [c for c, _ in redis.published]

    # Next poll retries and succeeds; only now is the match done.
    out = run(w.poll_match_type(redis, "leagueMatch"))
    assert out == [("101", "done")]
    assert run(w.match_state(redis, "101")) == w.STATE_DONE
    assert h.graph_calls == ["101", "101"]
    # Side effects from the failed attempt were not repeated on retry.
    assert len(redis.channel(w.CH_NEW_MATCH)) == 1
    assert len(redis.lists[f"fc27:watcher:{w.CLUB_ID}:results"]) == 1
    end = redis.channel(w.CH_MATCH_END)
    assert len(end) == 1 and end[0]["match_id"] == "101" and end[0]["match_type"] == "leagueMatch"
    assert end[0]["thread_id"] == f"fc27-{w.CLUB_ID}-run-20261001-001"


def test_watcher_retry_is_idempotent(monkeypatch):
    redis = FakeRedis()
    h = Harness(monkeypatch, [_match(201, 10)])
    run(_baseline(redis))
    assert run(w.poll_match_type(redis, "leagueMatch")) == [("201", "done")]
    # Re-polling the same match (duplicate detection) does nothing.
    assert run(w.poll_match_type(redis, "leagueMatch")) == [("201", "skipped")]
    assert run(w.handle_match(redis, _match(201, 10), "leagueMatch")) == "skipped"
    assert h.graph_calls == ["201"]
    assert len(redis.channel(w.CH_NEW_MATCH)) == 1
    assert len(redis.channel(w.CH_MATCH_END)) == 1


def test_watcher_crash_mid_processing_is_retried_after_lease_expiry(monkeypatch):
    redis = FakeRedis()
    h = Harness(monkeypatch, [_match(301, 10)])
    run(_baseline(redis))
    # Simulate a worker that took the lease and crashed before finishing.
    run(redis.set(w._lease_key("301"), "dead-worker", nx=True, ex=w.LEASE_TTL))
    run(redis.hset(w._record_key("301"), mapping={"state": "processing", "attempts": "1"}))
    assert run(w.poll_match_type(redis, "leagueMatch")) == [("301", "busy")]
    assert h.graph_calls == []
    redis.advance(w.LEASE_TTL + 1)
    assert run(w.poll_match_type(redis, "leagueMatch")) == [("301", "done")]
    assert h.graph_calls == ["301"]


def test_watcher_processes_all_unseen_matches_oldest_first(monkeypatch):
    redis = FakeRedis()
    # EA order: newest first.
    h = Harness(monkeypatch, [_match(3, 30), _match(2, 20), _match(1, 10)])
    run(_baseline(redis))
    run(w.mark_done(redis, "2"))   # already processed earlier
    out = run(w.poll_match_type(redis, "leagueMatch"))
    assert out == [("1", "done"), ("2", "skipped"), ("3", "done")]
    assert h.graph_calls == ["1", "3"]


def test_watcher_first_boot_baselines_without_triggering(monkeypatch):
    redis = FakeRedis()
    h = Harness(monkeypatch, [_match(11, 20), _match(10, 10)])
    out = run(w.poll_match_type(redis, "leagueMatch"))
    assert {o for _, o in out} == {"baseline"}
    assert h.graph_calls == [] and redis.published == []
    h.matches = [_match(12, 30), _match(11, 20), _match(10, 10)]
    assert run(w.poll_match_type(redis, "leagueMatch"))[-1] == ("12", "done")
    assert h.graph_calls == ["12"]


def test_watcher_bounded_attempts_then_dead(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(w, "MAX_ATTEMPTS", 3)
    h = Harness(monkeypatch, [_match(401, 10)])
    h.graph_status = [503]
    run(_baseline(redis))
    outcomes = [run(w.poll_match_type(redis, "leagueMatch"))[0][1] for _ in range(5)]
    assert outcomes == ["failed", "failed", "dead", "skipped", "skipped"]
    assert len(h.graph_calls) == 3
    rec = run(redis.hgetall(w._record_key("401")))
    assert rec[b"state"] == b"dead" and rec[b"attempts"] == b"3" and b"HTTPStatusError" in rec[b"last_error"]


def test_watcher_processing_exception_never_raises(monkeypatch):
    redis = FakeRedis()
    h = Harness(monkeypatch, [_match(501, 10)])
    run(_baseline(redis))
    redis.fail_publish = 1          # Redis publish blows up mid-processing
    assert run(w.poll_match_type(redis, "leagueMatch")) == [("501", "failed")]
    assert run(w.poll_match_type(redis, "leagueMatch")) == [("501", "done")]

    async def boom(_mt):
        raise RuntimeError("mcp down")
    monkeypatch.setattr(w, "mcp_get_matches", boom)
    assert run(w.poll_match_type(redis, "leagueMatch")) == []


def test_watcher_sends_mcp_bearer_and_graph_key(monkeypatch, caplog):
    redis = FakeRedis()
    h = Harness(monkeypatch, [_match(601, 10)])
    run(_baseline(redis))
    with caplog.at_level("DEBUG"):
        run(w.poll_match_type(redis, "leagueMatch"))
    assert h.mcp_headers and all(hd["authorization"] == "Bearer mcp-token-placeholder" for hd in h.mcp_headers)
    assert h.graph_headers[0]["x-api-key"] == "svc-token-placeholder"
    assert "mcp-token-placeholder" not in caplog.text and "svc-token-placeholder" not in caplog.text


def test_watcher_does_not_log_redis_url():
    import re
    from pathlib import Path
    src = Path(w.__file__).read_text()
    assert not re.search(r"log\.\w+\([^)]*REDIS_URL", src)
