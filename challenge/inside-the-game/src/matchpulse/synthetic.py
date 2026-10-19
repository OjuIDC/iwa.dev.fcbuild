from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections.abc import Iterator

import httpx

from .models import EventType, MatchEvent

TEAMS = ("Northbridge-FC", "Riverside-Athletic")
PLAYERS = {
    TEAMS[0]: ["NB-7", "NB-8", "NB-9", "NB-10", "NB-14"],
    TEAMS[1]: ["RA-4", "RA-6", "RA-9", "RA-11", "RA-18"],
}


def generate_match(match_id: str = "demo-2026-10-27", seed: int = 27, events: int = 50) -> Iterator[MatchEvent]:
    rng = random.Random(seed)
    sequence = 0
    yield MatchEvent(match_id=match_id, sequence=sequence, clock_ms=0, event_type=EventType.MATCH_START)
    sequence += 1
    clock = 8_000
    possession = {TEAMS[0]: 50.0, TEAMS[1]: 50.0}
    for _ in range(events):
        team = rng.choice(TEAMS)
        player = rng.choice(PLAYERS[team])
        roll = rng.random()
        if roll < 0.52:
            x, y = rng.uniform(5, 80), rng.uniform(5, 95)
            end_x = min(100, max(0, x + rng.uniform(-8, 35)))
            end_y = min(100, max(0, y + rng.uniform(-25, 25)))
            yield MatchEvent(match_id=match_id, sequence=sequence, clock_ms=clock, event_type=EventType.PASS,
                team_id=team, player_id=player, target_player_id=rng.choice(PLAYERS[team]),
                outcome="success" if rng.random() < 0.79 else "failure", x=x, y=y, end_x=end_x, end_y=end_y,
                pressure=round(rng.random(), 2))
        elif roll < 0.68:
            yield MatchEvent(match_id=match_id, sequence=sequence, clock_ms=clock, event_type=EventType.PRESSURE,
                team_id=team, player_id=player, pressure=round(rng.uniform(0.55, 0.98), 2))
        elif roll < 0.80:
            speed = round(rng.uniform(70, 128), 1)
            goal = rng.random() < 0.18
            yield MatchEvent(match_id=match_id, sequence=sequence, clock_ms=clock,
                event_type=EventType.GOAL if goal else EventType.SHOT, team_id=team, player_id=player,
                outcome="success" if goal else ("on_target" if rng.random() < 0.55 else "off_target"),
                x=rng.uniform(72, 96), y=rng.uniform(20, 80), speed_kmh=speed)
        elif roll < 0.92:
            yield MatchEvent(match_id=match_id, sequence=sequence, clock_ms=clock, event_type=EventType.TACKLE,
                team_id=team, player_id=player, outcome="success" if rng.random() < 0.7 else "failure",
                x=rng.uniform(20, 90), y=rng.uniform(5, 95))
        else:
            previous = possession[team]
            possession[team] = max(25.0, min(75.0, previous + rng.choice((-1, 1)) * rng.uniform(10, 16)))
            yield MatchEvent(match_id=match_id, sequence=sequence, clock_ms=clock, event_type=EventType.POSSESSION,
                team_id=team, metadata={"share": round(possession[team], 1), "previous_share": round(previous, 1)})
        sequence += 1
        clock += rng.randint(5_000, 18_000)
    yield MatchEvent(match_id=match_id, sequence=sequence, clock_ms=5_400_000, event_type=EventType.MATCH_END)


async def stream(base_url: str, delay: float, seed: int, events: int) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        for event in generate_match(seed=seed, events=events):
            response = await client.post("/v1/events", json=event.model_dump(mode="json"))
            response.raise_for_status()
            payload = response.json()
            print(json.dumps({"sequence": event.sequence, "type": event.event_type, "insights": len(payload["insights"])}))
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream a seeded synthetic match into MatchPulse Live")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--delay", type=float, default=0.12)
    parser.add_argument("--seed", type=int, default=27)
    parser.add_argument("--events", type=int, default=50)
    parser.add_argument("--jsonl", action="store_true")
    args = parser.parse_args()
    if args.jsonl:
        for event in generate_match(seed=args.seed, events=args.events):
            print(event.model_dump_json())
    else:
        asyncio.run(stream(args.url, args.delay, args.seed, args.events))


if __name__ == "__main__":
    main()
