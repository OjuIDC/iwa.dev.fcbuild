from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .agents import SupervisorAgent
from .foundry import FoundryNarrativeClient, FoundrySettings
from .models import AudienceProfile, EventIngestResponse, MatchEvent, MatchRecap, MatchState, PersonalizedInsight

DEFAULT_PROFILES = {
    "fan-en": AudienceProfile(profile_id="fan-en", persona="fan", locale="en", favorite_club="Northbridge-FC", favorite_player="NB-9", metric_interests=["shooting", "pressing"]),
    "analyst-en": AudienceProfile(profile_id="analyst-en", persona="analyst", locale="en", detail="deep", metric_interests=["passing", "possession", "pressing"]),
    "broadcaster-es": AudienceProfile(profile_id="broadcaster-es", persona="broadcaster", locale="es", detail="compact"),
    "fan-fr": AudienceProfile(profile_id="fan-fr", persona="fan", locale="fr", favorite_club="Riverside-Athletic", metric_interests=["shooting", "defending"]),
}


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[MatchEvent] = Field(min_length=1, max_length=500)


@dataclass
class Subscriber:
    profile_id: str
    queue: asyncio.Queue[PersonalizedInsight]


class BroadcastHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, match_id: str, profile_id: str) -> Subscriber:
        subscriber = Subscriber(profile_id, asyncio.Queue(maxsize=64))
        async with self._lock:
            self._subscribers[match_id].append(subscriber)
        return subscriber

    async def unsubscribe(self, match_id: str, subscriber: Subscriber) -> None:
        async with self._lock:
            if subscriber in self._subscribers.get(match_id, []):
                self._subscribers[match_id].remove(subscriber)

    async def publish(self, items: list[PersonalizedInsight]) -> None:
        async with self._lock:
            for item in items:
                for subscriber in list(self._subscribers.get(item.match_id, [])):
                    if subscriber.profile_id != item.profile_id:
                        continue
                    if subscriber.queue.full():
                        try:
                            subscriber.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    subscriber.queue.put_nowait(item)


_DEMO_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MatchPulse Live</title><style>
:root{--navy:#071a2b;--blue:#16b8f3;--lime:#c8f560;--ink:#edf7ff;--muted:#9eb4c7}*{box-sizing:border-box}
body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:radial-gradient(circle at 80% 0,#123d56,var(--navy) 42%);color:var(--ink);min-height:100vh}
header{padding:28px 5vw 14px;display:flex;justify-content:space-between;align-items:center}.brand{font-weight:800;letter-spacing:.04em}.live{color:var(--lime)}
main{display:grid;grid-template-columns:minmax(260px,1fr) minmax(360px,1.6fr);gap:22px;padding:16px 5vw 48px}.card{background:#0c2437dd;border:1px solid #2a4b61;border-radius:18px;padding:20px;box-shadow:0 16px 45px #0005}
label{display:block;color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;margin:14px 0 6px}select,button{width:100%;padding:12px;border-radius:10px;border:1px solid #3b6177;background:#102f46;color:var(--ink)}button{margin-top:16px;background:var(--blue);color:#00131e;border:0;font-weight:800;cursor:pointer}
.score{font-size:2.5rem;font-weight:900;margin:12px 0}.overlay{min-height:155px;border-left:5px solid var(--lime);padding:18px;background:#061521;border-radius:10px}.overlay h2{margin:0 0 8px;font-size:1.3rem}.overlay p{color:#d5e7f2;line-height:1.5}.meta{font-family:ui-monospace,monospace;color:var(--blue);font-size:.78rem}
.feed{margin-top:18px;display:grid;gap:10px}.feed article{padding:12px;border:1px solid #29465a;border-radius:10px;background:#0a1d2c}.pill{display:inline-block;padding:4px 8px;border-radius:99px;background:#173c51;color:var(--lime);font-size:.7rem}.status{color:var(--muted);font-size:.85rem}@media(max-width:800px){main{grid-template-columns:1fr}}
</style></head><body><header><div class="brand">MATCHPULSE <span class="live">LIVE</span></div><div class="status" id="status">Ready</div></header>
<main><section class="card"><span class="pill">SYNTHETIC DATA ONLY</span><h1>Every match, made for every fan.</h1><p>One evidence-backed football moment becomes a different experience for an analyst, broadcaster, or fan - in English, Spanish, or French.</p>
<label>Audience profile</label><select id="profile"><option>fan-en</option><option>analyst-en</option><option>broadcaster-es</option><option>fan-fr</option></select>
<label>Match</label><input id="match" value="demo-2026-10-27" style="width:100%;padding:12px;border-radius:10px;border:1px solid #3b6177;background:#102f46;color:white">
<button onclick="connect()">Connect live overlay</button><p class="status">Run <code>python -m matchpulse.synthetic</code> in another terminal.</p></section>
<section class="card"><div class="score">LIVE INTELLIGENCE</div><div class="overlay" id="overlay"><h2>Waiting for a meaningful moment</h2><p>Insights appear only after the explainability agent validates source event evidence.</p></div><div class="feed" id="feed"></div></section></main>
<script>let ws;function connect(){if(ws)ws.close();const p=document.getElementById('profile').value,m=document.getElementById('match').value;const proto=location.protocol==='https:'?'wss':'ws';ws=new WebSocket(`${proto}://${location.host}/ws/matches/${m}?profile_id=${p}`);status.textContent='Connecting';ws.onopen=()=>status.textContent='Live';ws.onmessage=e=>{const x=JSON.parse(e.data);if(x.type==='welcome')return;const o=document.getElementById('overlay');o.innerHTML=`<div class="meta">${x.locale.toUpperCase()} · ${x.persona} · ${Math.floor(x.clock_ms/60000)}:${String(Math.floor((x.clock_ms%60000)/1000)).padStart(2,'0')}</div><h2>${x.title}</h2><p>${x.body}</p><div class="meta">Evidence: ${x.evidence.event_ids.join(', ')}</div>`;const a=document.createElement('article');a.innerHTML=`<strong>${x.overlay_text}</strong><div class="meta">priority ${x.priority}</div>`;feed.prepend(a);while(feed.children.length>5)feed.lastChild.remove()};ws.onclose=()=>status.textContent='Disconnected'}</script></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="MatchPulse Live", version="0.1.0", description="Real-time, multilingual, personalized football intelligence from synthetic events")
    app.state.pipeline = SupervisorAgent()
    app.state.profiles = dict(DEFAULT_PROFILES)
    app.state.hub = BroadcastHub()
    foundry_settings = FoundrySettings.from_env()
    app.state.foundry = FoundryNarrativeClient(foundry_settings) if foundry_settings else None

    def profile(profile_id: str) -> AudienceProfile:
        item = app.state.profiles.get(profile_id)
        if item is None:
            raise HTTPException(404, "profile not found")
        return item

    async def ingest(event: MatchEvent) -> EventIngestResponse:
        started = time.perf_counter()
        if os.getenv("CHALLENGE_SYNTHETIC_ONLY", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            raise HTTPException(503, "challenge safety gate CHALLENGE_SYNTHETIC_ONLY must remain enabled")
        try:
            applied, outputs, warnings, _handoffs = app.state.pipeline.process(event, app.state.profiles.values())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if app.state.foundry:
            enriched = []
            for item in outputs:
                result, warning = app.state.foundry.rewrite(item)
                enriched.append(result)
                if warning:
                    warnings.append(warning)
            outputs = enriched
        await app.state.hub.publish(outputs)
        return EventIngestResponse(accepted=True, duplicate=applied.duplicate,
            latency_ms=round((time.perf_counter() - started) * 1000, 3), state=applied.state,
            insights=outputs, warnings=warnings)

    @app.get("/health/live")
    async def health_live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> dict:
        return {"status": "ready", "synthetic_only": True,
                "foundry_mode": "configured" if app.state.foundry else "deterministic-fallback",
                "profiles": len(app.state.profiles)}

    @app.get("/demo", response_class=HTMLResponse)
    async def demo() -> str:
        return _DEMO_HTML

    @app.get("/v1/profiles", response_model=list[AudienceProfile])
    async def list_profiles() -> list[AudienceProfile]:
        return list(app.state.profiles.values())

    @app.put("/v1/profiles/{profile_id}", response_model=AudienceProfile)
    async def put_profile(profile_id: str, body: AudienceProfile) -> AudienceProfile:
        if profile_id != body.profile_id:
            raise HTTPException(400, "path and body profile_id must match")
        app.state.profiles[profile_id] = body
        return body

    @app.post("/v1/events", response_model=EventIngestResponse, status_code=202)
    async def post_event(event: MatchEvent) -> EventIngestResponse:
        return await ingest(event)

    @app.post("/v1/events/batch", response_model=list[EventIngestResponse], status_code=202)
    async def post_batch(batch: EventBatch) -> list[EventIngestResponse]:
        responses = []
        for event in batch.events:
            responses.append(await ingest(event))
        return responses

    @app.get("/v1/matches/{match_id}/state", response_model=MatchState)
    async def get_state(match_id: str) -> MatchState:
        state = app.state.pipeline.state.get(match_id)
        if state is None:
            raise HTTPException(404, "match not found")
        return state

    @app.get("/v1/matches/{match_id}/recap", response_model=MatchRecap)
    async def recap(match_id: str, profile_id: str = Query("fan-en")) -> MatchRecap:
        try:
            return app.state.pipeline.recap(match_id, profile(profile_id))
        except KeyError as exc:
            raise HTTPException(404, "match not found") from exc

    @app.websocket("/ws/matches/{match_id}")
    async def websocket_match(websocket: WebSocket, match_id: str, profile_id: str = Query("fan-en")) -> None:
        if profile_id not in app.state.profiles:
            await websocket.close(code=4404, reason="profile not found")
            return
        await websocket.accept()
        subscriber = await app.state.hub.subscribe(match_id, profile_id)
        await websocket.send_json({"type": "welcome", "match_id": match_id, "profile_id": profile_id})
        try:
            while True:
                item = await subscriber.queue.get()
                await websocket.send_json(item.model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            await app.state.hub.unsubscribe(match_id, subscriber)

    return app


app = create_app()
