"""FC27 Graph API.

Run with:  uvicorn src.graph_api:create_app --factory --host 0.0.0.0 --port 8001

Authorisation (header ``X-API-Key``):
  service token       POST /run, GET /threads/{id}
  commissioner token  POST /threads/{id}/decision-challenge, POST /threads/{id}/decision
Any other combination returns 403; a missing or unknown key returns 401.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from .approval_store import ApprovalStore, StoreError, request_hash
from .db_migrate import migrate
from .schemas import DecisionIntent, SignedDecision
from .security import Authenticator, Principal, Role, require_role
from .settings import DEFAULT_PLATFORM, FC_VERSION, ConfigError, env_bool, read_secret

log = logging.getLogger("fc27.graph_api")

ThreadIdPath = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")


@dataclass
class ApiSettings:
    postgres_uri: str
    authenticator: Authenticator
    signing_secret: str
    approval_ttl_seconds: int = 120
    redis_url: str | None = None
    run_migrations: bool = True

    @classmethod
    def from_env(cls) -> "ApiSettings":
        try:
            auth = Authenticator(
                read_secret("GRAPH_SERVICE_TOKEN", min_length=32),
                read_secret("GRAPH_COMMISSIONER_TOKEN_SHA256"),
                read_secret("COMMISSIONER_ID"),
            )
            return cls(
                postgres_uri=read_secret("POSTGRES_URI"),
                authenticator=auth,
                signing_secret=read_secret("APPROVAL_SIGNING_SECRET", min_length=32),
                approval_ttl_seconds=int(os.getenv("APPROVAL_TTL_SECONDS", "120")),
                redis_url=read_secret("REDIS_URL", required=False),
                run_migrations=env_bool("GRAPH_RUN_MIGRATIONS", True),
            )
        except ValueError as exc:  # ConfigError is a RuntimeError; Authenticator raises ValueError
            raise ConfigError(str(exc)) from exc


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str | None = Field(default=None, max_length=128)
    club_id: str = Field(default_factory=lambda: os.getenv("FC27_CLUB_ID", "330323"), pattern=r"^[0-9]{1,12}$")
    platform: str = Field(default=DEFAULT_PLATFORM, pattern=r"^common-gen5$")
    fc_version: str = Field(default=FC_VERSION, pattern=r"^27$")
    match: dict | None = None
    trigger: str = Field(default="api", pattern=r"^[a-z_]{1,32}$")


Pipeline = Callable[[dict[str, Any]], dict[str, Any]]
Publisher = Callable[[str, dict[str, Any]], None]


def _make_initial_state(req: RunRequest) -> Callable[[str], dict[str, Any]]:
    def build(thread_id: str) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "club_id": req.club_id,
            "platform": req.platform,
            "fc_version": FC_VERSION,
            "trigger": req.trigger,
            "match": req.match,
            "status": "running",
            "approved": None,
            "audit_log": [],
            "errors": [],
        }

    return build


def _redis_publisher(redis_url: str | None) -> Publisher:
    if not redis_url:
        return lambda channel, payload: None
    import redis

    client = redis.Redis.from_url(redis_url, decode_responses=True)

    def publish(channel: str, payload: dict[str, Any]) -> None:
        try:
            client.publish(channel, json.dumps(payload))
        except Exception:  # notifications are best-effort; the DB is the source of truth
            log.warning("redis publish failed for channel %s", channel)

    return publish


def _store_error(exc: StoreError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def create_app(settings: ApiSettings | None = None, *, pipeline: Pipeline | None = None,
               publisher: Publisher | None = None, runner=None) -> FastAPI:
    """``runner`` is a :class:`src.graph.GraphRunner`. When neither ``pipeline``
    nor ``runner`` is given, a LangGraph runner with a PostgresSaver is built
    at startup. ``pipeline`` (without runner) is for tests of the API layer."""
    settings = settings or ApiSettings.from_env()
    store = ApprovalStore(settings.postgres_uri)
    publish = publisher or _redis_publisher(settings.redis_url)
    graph = {"runner": runner}

    def run_pipeline(state: dict[str, Any]) -> dict[str, Any]:
        if pipeline is not None:
            return pipeline(state)
        return graph["runner"].start(state)

    def resume_graph(thread_id: str) -> str:
        if graph["runner"] is None:
            return "not-configured"
        try:
            graph["runner"].resume(thread_id)
            return "resumed"
        except Exception:  # decision is committed in PostgreSQL; graph resume is retried at startup
            log.exception("graph resume failed for %s; will retry on next startup", thread_id)
            return "pending"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.run_migrations:
            migrate(settings.postgres_uri)
        if graph["runner"] is None and pipeline is None:
            from .graph import GraphDeps, GraphRunner

            graph["runner"] = GraphRunner.from_conninfo(settings.postgres_uri, GraphDeps(decision_reader=store.load))
            graph["owned"] = True
        if graph["runner"] is not None:
            for thread_id in store.terminal_thread_ids():
                resume_graph(thread_id)  # reconcile decisions committed while the graph was not resumed
        yield
        if graph.get("owned"):
            graph["runner"].pool.close()

    app = FastAPI(title="FC27 Graph API", version=FC_VERSION, lifespan=lifespan)

    def principal(x_api_key: str | None = Header(default=None)) -> Principal:
        return settings.authenticator.authenticate(x_api_key)

    def service(p: Principal = Depends(principal)) -> Principal:
        return require_role(p, Role.service)

    def commissioner(p: Principal = Depends(principal)) -> Principal:
        return require_role(p, Role.commissioner)

    @app.get("/health")
    def health() -> dict:
        try:
            with psycopg.connect(settings.postgres_uri, connect_timeout=3) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:
            raise HTTPException(503, "postgres unavailable") from exc
        return {"status": "ok", "postgres": "ok", "fc_version": FC_VERSION}

    @app.post("/run", status_code=201)
    def run(req: RunRequest, _p: Principal = Depends(service)) -> dict:
        try:
            state = store.create_run(
                club_id=req.club_id,
                thread_id=req.thread_id,
                initial_state=_make_initial_state(req),
                req_hash=request_hash(req.model_dump(mode="json")),
            )
        except StoreError as exc:
            raise _store_error(exc) from exc
        try:
            result = run_pipeline(state)
        except Exception as exc:  # pipeline failure is recorded, never left as running
            log.exception("pipeline failed for %s", state["thread_id"])
            result = {**state, "status": "failed", "errors": list(state.get("errors", [])) + [type(exc).__name__]}
        result = {**result, "thread_id": state["thread_id"]}
        try:
            final = store.complete_run(state["thread_id"], result)
        except StoreError as exc:
            raise _store_error(exc) from exc
        if final["status"] == "awaiting_approval":
            publish("fc27:approval:requested", {"thread_id": final["thread_id"], "status": final["status"]})
        return final

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str = ThreadIdPath, _p: Principal = Depends(service)) -> dict:
        row = store.load(thread_id)
        if row is None:
            raise HTTPException(404, "thread not found")
        state = dict(row["state"])
        state.update(status=row["status"], package_version=row["package_version"], approved_by=row["approved_by"])
        return state

    @app.post("/threads/{thread_id}/decision-challenge")
    def decision_challenge(intent: DecisionIntent, thread_id: str = ThreadIdPath,
                           p: Principal = Depends(commissioner)) -> dict:
        try:
            return store.issue_challenge(thread_id, intent, p, settings.approval_ttl_seconds)
        except StoreError as exc:
            raise _store_error(exc) from exc

    @app.post("/threads/{thread_id}/decision")
    def decision(body: SignedDecision, thread_id: str = ThreadIdPath, p: Principal = Depends(commissioner)) -> dict:
        try:
            state = store.apply_decision(thread_id, body, p, settings.signing_secret)
        except StoreError as exc:
            raise _store_error(exc) from exc
        publish("fc27:approval:decided", {"thread_id": thread_id, "status": state["status"]})
        return {**state, "graph_resume": resume_graph(thread_id)}

    return app
