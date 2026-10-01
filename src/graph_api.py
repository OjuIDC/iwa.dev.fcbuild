from __future__ import annotations
import json
import os
from contextlib import contextmanager
from typing import Any

import psycopg
import redis
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .graph import decide, initial_state, run_pipeline
from .schemas import HumanDecision

app = FastAPI(title="FC27 Graph API", version="27")
REDIS_URL = os.environ["REDIS_URL"]
POSTGRES_URI = os.environ["POSTGRES_URI"]
TOKEN = os.environ["GRAPH_API_TOKEN"]
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

@contextmanager
def db():
    with psycopg.connect(POSTGRES_URI) as conn:
        yield conn

def ensure_schema() -> None:
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_threads (
                thread_id text PRIMARY KEY,
                state jsonb NOT NULL,
                status text NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS approval_audit (
                id bigserial PRIMARY KEY,
                thread_id text NOT NULL REFERENCES graph_threads(thread_id),
                commissioner text NOT NULL,
                decision text NOT NULL CHECK (decision IN ('APPROVE','REJECT')),
                notes text NOT NULL DEFAULT '',
                owner text NOT NULL,
                selection text NOT NULL,
                candidate_id text NOT NULL,
                player_id text NOT NULL,
                decided_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        conn.commit()

@app.on_event("startup")
def startup() -> None:
    ensure_schema()

def auth(x_api_key: str | None) -> None:
    if not x_api_key or x_api_key != TOKEN:
        raise HTTPException(401, "invalid API key")

def save_state(state: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO graph_threads(thread_id,state,status) VALUES (%s,%s::jsonb,%s)
               ON CONFLICT(thread_id) DO UPDATE SET state=EXCLUDED.state,status=EXCLUDED.status,updated_at=now()""",
            (state["thread_id"], json.dumps(state), state["status"]),
        )
        conn.commit()

def load_state(thread_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT state FROM graph_threads WHERE thread_id=%s", (thread_id,)).fetchone()
    return row[0] if row and isinstance(row[0], dict) else (json.loads(row[0]) if row else None)

class RunRequest(BaseModel):
    thread_id: str | None = None
    club_id: str = Field(default="330323", pattern=r"^\d{1,12}$")
    platform: str = "common-gen5"
    match: dict | None = None
    trigger: str = "api"

Decision = HumanDecision

@app.get("/health")
def health() -> dict:
    r.ping()
    with db() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "redis": "ok", "postgres": "ok"}

@app.post("/run", status_code=202)
def run(req: RunRequest, x_api_key: str | None = Header(default=None)) -> dict:
    auth(x_api_key)
    state = run_pipeline(initial_state(req.thread_id, req.club_id, req.match))
    save_state(state)
    r.publish("fc27:approval:requested", json.dumps({"thread_id": state["thread_id"], "status": state["status"]}))
    return state

@app.get("/threads/{thread_id}")
def get_thread(thread_id: str, x_api_key: str | None = Header(default=None)) -> dict:
    auth(x_api_key)
    state = load_state(thread_id)
    if not state:
        raise HTTPException(404, "thread not found")
    return state

@app.post("/threads/{thread_id}/decision")
def decision(thread_id: str, body: Decision, x_api_key: str | None = Header(default=None)) -> dict:
    auth(x_api_key)
    state = load_state(thread_id)
    if not state:
        raise HTTPException(404, "thread not found")
    expected = os.getenv("COMMISSIONER", "batmanL8")
    if body.commissioner != expected:
        raise HTTPException(403, "commissioner identity mismatch")
    try:
        state = decide(state, body.decision, body.notes, body.owner.value, body.selection.value, body.candidate_id, body.player_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    save_state(state)
    with db() as conn:
        conn.execute(
            "INSERT INTO approval_audit(thread_id,commissioner,decision,notes,owner,selection,candidate_id,player_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (thread_id, body.commissioner, body.decision, body.notes, body.owner.value, body.selection.value, body.candidate_id, body.player_id),
        )
        conn.commit()
    r.publish("fc27:approval:decided", json.dumps({"thread_id": thread_id, "status": state["status"]}))
    return state
