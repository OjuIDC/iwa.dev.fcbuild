# MCP proxy and live WebSocket server: security and reliability notes

Covers `src/fc27_mcp_server.py`, `src/mcp_retry.py` and `src/live_ws_server.py`.

## MCP proxy (`fc27_mcp_server.py`)

**Auth.** Every endpoint except `GET /health` requires `Authorization: Bearer <MCP_API_TOKEN>`.
The token is compared with `hmac.compare_digest`. A missing or wrong token gets a 401 with
`WWW-Authenticate: Bearer`. The server refuses to start (exit 2) if `MCP_API_TOKEN` is missing,
shorter than 32 characters, or contains non-ASCII characters or whitespace.
- `OPTIONS` (CORS preflight) is not authenticated, because browsers never attach credentials
  to a preflight. It returns only CORS headers.
- `/health` returns only `{"status":"ok","fc_version":"27"}`.
- stdio mode (`--stdio`) has no token. The trust boundary there is the parent process that
  spawned it.
- The token, the request headers and the request bodies are never logged.

**Endpoints.**
- `POST /` or `POST /mcp`: JSON-RPC (`initialize`, `tools/list`, `tools/call`).
- `GET /tools`: returns the tool schema.
- `GET /sse`: a readiness stream. It sends `event: ready`, then `: keepalive` comments every
  `MCP_SSE_KEEPALIVE_SEC`, then `event: bye` when `MCP_SSE_MAX_SEC` is reached or the server
  shuts down. At most `MCP_MAX_SSE_STREAMS` streams can be open at once.

**Concurrency and limits.**
- `ThreadingHTTPServer` runs behind a bounded semaphore (`MCP_MAX_WORKERS`). When it is
  saturated, the server writes an immediate `503` with `Retry-After: 1` instead of queueing.
- Each connection gets a socket timeout (`MCP_REQUEST_TIMEOUT_SEC`). Responses are HTTP/1.0,
  so a connection carries one request.
- `Content-Length` is required on POST. A missing length or a chunked body gets 411, a
  non-numeric length gets 400, and anything over `MCP_MAX_BODY_BYTES` (64 KiB) gets 413.
- Upstream EA responses are capped at 8 MiB.
- The cache and the rate limiter are both lock-protected.
- On SIGTERM or SIGINT the server stops accepting connections, ends open SSE streams and
  waits up to 10 s for workers to finish.
- `MCP_HOST` defaults to `127.0.0.1`. Compose sets `0.0.0.0` inside the private network.
- `MCP_ALLOWED_ORIGIN` must be one exact origin. A wildcard or a list makes the server refuse
  to start.

**EA retries (`mcp_retry.py`).** Every attempt goes through the existing 30 requests/min
per-endpoint limiter. A cache hit never calls EA, even while EA is failing.

| Upstream result | Behaviour |
|---|---|
| 429 | Uses a valid `Retry-After` (delta-seconds or HTTP-date), capped at `MCP_RETRY_AFTER_MAX_SEC`. If the header is missing or invalid, uses equal-jitter exponential backoff. |
| 500 / 502 / 503 / 504, `URLError`, timeouts, connection errors | Equal-jitter exponential backoff: `d/2 + rand*d/2`, with `d = base*2^(n-1)` capped at `max_delay`. |
| 403 | Retries at most `MCP_RETRY_MAX_403` times (default 1), then fails closed with `kind=upstream_forbidden` and `retryable=false`. |
| any other 4xx | Never retried (`upstream_http_error`). |

All retries are bounded by `MCP_RETRY_MAX_ATTEMPTS` and by the total budget
`MCP_RETRY_BUDGET_SEC`. If the next wait would exceed the budget, the call fails with
`retry_budget_exhausted` without sleeping.

Failures raise `UpstreamError`. JSON-RPC returns it as
`error.data = {kind, endpoint, status, attempts, retryable, message[, retry_after_sec]}`.
The error never contains URLs, headers or credentials.

**Validation and audit.**
- `clubId` must match `[0-9]{1,12}` with `fullmatch` and no stripping. Unicode digits, signs,
  spaces, decimals and trailing newlines are rejected.
- `audit.platform` is the platform actually sent to EA, not the global default.
- `search_club` does not raise on an HTTP failure, a network error or an empty result. It
  returns `results: []` plus a `fallback` that tells the user to copy the digits after
  `clubId=` from
  `https://www.ea.com/games/ea-sports-fc/clubs/overview?clubId=<CLUB_ID>&platform=<PLATFORM>`.
- EA payloads that come back as a list, a dict keyed by clubId, or a single club dict are
  all handled. Junk payloads do not raise.

## Live WebSocket server (`live_ws_server.py`)

**Handshake auth.** It is checked in `process_request`, before the upgrade. Two forms are
accepted:
1. `Authorization: Bearer <WS_AUTH_TOKEN>` for non-browser clients.
2. For browsers, which cannot set headers: `new WebSocket(url, ["fc27.v1", "fc27.bearer.<token>"])`.
   The server selects `fc27.v1` and never echoes the bearer subprotocol back. Browsers must
   offer `fc27.v1`, otherwise they abort the connection. Tokens must not contain whitespace
   or commas.

If a client sends an `Authorization` header, only that header is evaluated. A missing or
wrong token gets 401. The server refuses to start (exit 2) if `WS_AUTH_TOKEN` is missing or
shorter than 32 characters.

**Origin.**
- If an `Origin` header is present, it must exactly match an entry in `WS_ALLOWED_ORIGINS`
  (comma-separated, trailing `/` ignored). Otherwise the server returns 403.
- Wildcards, `null` and non-origin values make the server refuse to start.
- Connections without an `Origin` header (CLI tools, service clients) are rejected with 403
  unless `WS_ALLOW_NO_ORIGIN=true`. Even then they still need the token.

**Limits.**
- Inbound frames are capped at `WS_MAX_MESSAGE_BYTES` (4 KiB). Oversized frames are closed
  with 1009.
- Timeouts: `open_timeout` 10 s, ping interval and timeout 20 s, `close_timeout` 5 s.
- Compression is off. More than `WS_MAX_CLIENTS` connections get 503.
- `WS_HOST` defaults to `127.0.0.1`.

**Broadcast.** Each client has a bounded queue (`WS_CLIENT_QUEUE`) and its own sender task,
and every send is wrapped in `WS_SEND_TIMEOUT_SEC`. `broadcast()` never awaits a socket.
Close codes:
- A full queue, a send timeout or any send exception drops only that client, with close code
  1013.
- Shutdown closes clients with 1001.
- The handshake's 401, 403 and 503 responses are plain HTTP and carry no detail.

**Logging.** `REDIS_URL` contains a password, so only its hostname is logged. The token is
never logged. The Redis subscriber reconnects with capped exponential backoff.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `MCP_API_TOKEN` | required (≥32 chars) | Bearer token for the MCP HTTP API |
| `MCP_HOST` | `127.0.0.1` | MCP bind address |
| `MCP_MAX_WORKERS` | `8` | Maximum concurrent requests (over the limit gets 503) |
| `MCP_REQUEST_TIMEOUT_SEC` | `15` | Per-connection socket timeout |
| `MCP_MAX_BODY_BYTES` | `65536` | POST body cap (over the cap gets 413) |
| `MCP_MAX_SSE_STREAMS` | `2` | Maximum concurrent `/sse` streams |
| `MCP_SSE_KEEPALIVE_SEC` | `15` | Interval between SSE keepalive comments |
| `MCP_SSE_MAX_SEC` | `300` | Maximum SSE stream lifetime |
| `MCP_EA_TIMEOUT_SEC` | `12` | Timeout for each upstream attempt |
| `MCP_RETRY_MAX_ATTEMPTS` | `4` | Maximum upstream attempts per call |
| `MCP_RETRY_BUDGET_SEC` | `30` | Total retry wait budget |
| `MCP_RETRY_BASE_DELAY_SEC` | `1` | Base backoff delay |
| `MCP_RETRY_MAX_DELAY_SEC` | `8` | Backoff cap |
| `MCP_RETRY_AFTER_MAX_SEC` | `20` | Cap on an honored `Retry-After` |
| `MCP_RETRY_MAX_403` | `1` | Retries allowed on 403 before failing closed |
| `WS_AUTH_TOKEN` | required (≥32 chars) | WebSocket handshake token |
| `WS_ALLOWED_ORIGINS` | required (non-empty unless no-origin is allowed) | Exact allowed Origins |
| `WS_ALLOW_NO_ORIGIN` | `false` | Accept authenticated clients that send no Origin |
| `WS_HOST` | `127.0.0.1` | WebSocket bind address |
| `WS_MAX_MESSAGE_BYTES` | `4096` | Maximum inbound frame size |
| `WS_SEND_TIMEOUT_SEC` | `5` | Timeout for each send to a client |
| `WS_CLIENT_QUEUE` | `64` | Per-client outbound backlog |
| `WS_MAX_CLIENTS` | `200` | Connection cap |

## Follow-up outside this change

`src/graph.py`, `src/live_match_watcher.py` and `src/post_match_rundown.py` still call the MCP
proxy without an `Authorization` header. They must send `Authorization: Bearer $MCP_API_TOKEN`
(Compose already provides the variable to them). Until they do, their calls will get 401.
