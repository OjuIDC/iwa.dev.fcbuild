"""Peer-facing health check for the fc27-mcp container (stdlib only).

The Compose healthcheck must prove that the listener used by peer containers
(graph-api, live-watcher, post-match-rundown) is reachable, not merely that
something answers on loopback. This probe therefore:

1. resolves the container's own non-loopback address (its Compose network IP);
2. refuses to treat a loopback address as success;
3. GETs http://<that-address>:<MCP_PORT>/health.

If the server is bound to 127.0.0.1 the probe fails, which marks the container
unhealthy and keeps dependants from starting against an unreachable MCP.

Usage: python -m src.mcp_healthcheck   (exit 0 healthy, 1 unhealthy)
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.request


def peer_address() -> str:
    """Returns this container's non-loopback IPv4 address or raises RuntimeError."""
    candidates: list[str] = []
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for addr in candidates:
        try:
            if not ipaddress.ip_address(addr).is_loopback:
                return addr
        except ValueError:
            continue
    raise RuntimeError("no non-loopback address resolved for this container")


def check(port: int | None = None, address: str | None = None, timeout: float = 4.0) -> tuple[bool, str]:
    """Probes the peer-facing /health endpoint. Returns (healthy, non-secret reason)."""
    port = int(port if port is not None else os.getenv("MCP_PORT", "8000"))
    try:
        addr = address or peer_address()
    except RuntimeError as exc:
        return False, str(exc)
    try:
        if ipaddress.ip_address(addr).is_loopback:
            return False, f"refusing loopback address {addr}: peers cannot reach it"
    except ValueError:
        return False, f"invalid address {addr!r}"
    url = f"http://{addr}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed scheme
            if resp.status != 200:
                return False, f"{url} returned HTTP {resp.status}"
            body = json.loads(resp.read(4096) or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{url} unreachable: {type(exc).__name__}"
    if body.get("status") != "ok":
        return False, f"{url} reported status {body.get('status')!r}"
    return True, f"{url} ok"


def main() -> int:
    healthy, reason = check()
    print(reason)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
