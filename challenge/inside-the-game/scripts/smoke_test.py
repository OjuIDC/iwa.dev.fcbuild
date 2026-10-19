"""API and WebSocket smoke test for a running MatchPulse Live instance.

Usage:
    python scripts/smoke_test.py [--url http://127.0.0.1:8080]

Checks the live and readiness probes, the profile catalogue, the demo page,
a synthetic event round trip, the match state, the recap, and the WebSocket
overlay stream. Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

import httpx

DEFAULT_URL = "http://127.0.0.1:8080"
# Match ids are unique per run so that the smoke test can be repeated against
# a long-lived instance without colliding with an earlier run.
RUN_ID = uuid.uuid4().hex[:8]
DEMO_MATCH_ID = f"smoke-{RUN_ID}"
WEBSOCKET_MATCH_ID = f"smoke-ws-{RUN_ID}"


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"{status}: {label}{(' - ' + detail) if detail else ''}")
    return condition


async def websocket_check(base_url: str) -> bool:
    try:
        import websockets
    except ImportError:
        return check("websocket dependency available", False, "install 'websockets'")
    ws_base = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    uri = f"{ws_base}/ws/matches/{WEBSOCKET_MATCH_ID}?profile_id=fan-en"
    try:
        async with websockets.connect(uri, open_timeout=10) as socket:
            welcome = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            if not check("websocket welcome frame", welcome.get("type") == "welcome", json.dumps(welcome)[:120]):
                return False
            event = {
                "match_id": WEBSOCKET_MATCH_ID,
                "sequence": 1,
                "clock_ms": 45_000,
                "event_type": "shot",
                "team_id": "Northbridge-FC",
                "player_id": "NB-9",
                "outcome": "on_target",
                "speed_kmh": 108,
                "source": "synthetic",
            }
            async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
                response = await client.post("/v1/events", json=event)
                response.raise_for_status()
            frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
            return check(
                "websocket overlay frame",
                frame.get("profile_id") == "fan-en" and bool(frame.get("evidence", {}).get("event_ids")),
                json.dumps(frame)[:160],
            )
    except Exception as exc:
        return check("websocket overlay frame", False, f"{type(exc).__name__}: {exc}")


async def run(base_url: str) -> int:
    failures = 0
    async with httpx.AsyncClient(base_url=base_url, timeout=20) as client:
        live = await client.get("/health/live")
        failures += not check("GET /health/live", live.status_code == 200, live.text[:120])

        ready = await client.get("/health/ready")
        payload = ready.json() if ready.status_code == 200 else {}
        failures += not check(
            "GET /health/ready reports synthetic-only",
            ready.status_code == 200 and payload.get("synthetic_only") is True,
            ready.text[:160],
        )

        profiles = await client.get("/v1/profiles")
        profile_payload = profiles.json() if profiles.status_code == 200 else []
        failures += not check(
            "GET /v1/profiles",
            profiles.status_code == 200 and len(profile_payload) == 4,
            f"count={len(profile_payload)}",
        )
        locales = sorted({item.get("locale") for item in profile_payload})
        failures += not check(
            "profiles cover en/es/fr",
            set(locales) == {"en", "es", "fr"},
            ",".join(str(item) for item in locales),
        )

        demo = await client.get("/demo")
        failures += not check("GET /demo", demo.status_code == 200 and "MatchPulse" in demo.text, demo.text[:80])

        event = {
            "match_id": DEMO_MATCH_ID,
            "sequence": 1,
            "clock_ms": 45_000,
            "event_type": "shot",
            "team_id": "Northbridge-FC",
            "player_id": "NB-9",
            "outcome": "on_target",
            "speed_kmh": 108,
            "source": "synthetic",
        }
        ingest = await client.post("/v1/events", json=event)
        failures += not check(
            "POST /v1/events accepts a synthetic event",
            ingest.status_code == 202 and ingest.json().get("accepted") is True,
            ingest.text[:160],
        )

        rejected = await client.post("/v1/events", json={**event, "sequence": 2, "source": "real"})
        failures += not check(
            "POST /v1/events rejects a non-synthetic source",
            rejected.status_code == 422,
            f"status={rejected.status_code}",
        )

        state = await client.get(f"/v1/matches/{DEMO_MATCH_ID}/state")
        failures += not check("GET /v1/matches/{id}/state", state.status_code == 200, state.text[:120])

        recap = await client.get(f"/v1/matches/{DEMO_MATCH_ID}/recap", params={"profile_id": "analyst-en"})
        recap_payload = recap.json() if recap.status_code == 200 else {}
        failures += not check(
            "GET /v1/matches/{id}/recap",
            recap.status_code == 200 and recap_payload.get("locale") == "en" and bool(recap_payload.get("evidence_event_ids")),
            recap.text[:160],
        )

    failures += not await websocket_check(base_url)
    print(f"RESULT: {'PASS' if not failures else 'FAIL'} ({failures} failed check(s))")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MatchPulse Live API and WebSocket smoke test")
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    return asyncio.run(run(args.url))


if __name__ == "__main__":
    sys.exit(main())
