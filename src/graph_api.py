"""FC27 Graph API.

Run with:  uvicorn src.graph_api:create_app --factory --host 0.0.0.0 --port 8001

Authorisation (header ``X-API-Key``):
  service token       POST /v1/runs, GET /v1/runs/{id}, POST /v1/runs/{id}/resume, POST /run, GET /threads/{id}
  commissioner token  POST /v1/runs/{id}/decision-challenge, POST /v1/runs/{id}/decision, POST /v1/approval-requests/{id}/decisions
Any other combination returns 403; a missing or unknown key returns 401.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from .approval_store import ApprovalStore, StoreError, request_hash
from .correlation import CorrelationMiddleware
from .db_migrate import migrate
from .db_roles import check_runtime_posture, redact_conninfo
from .dispatch.gates import (
    RECORDABLE_GATES,
    DeploymentIdentity,
    dispatch_effective,
    evaluate_gates,
    gate_payload_sha256,
    verify_gate_signature,
)
from .graph import DECIDED_STATUSES
from .schemas import (
    ApprovalRequestProjection,
    DecisionIntent,
    DecisionResponse,
    DispatchBlock,
    DispatchConfigResponse,
    HealthLiveResponse,
    HealthReadyResponse,
    HealthResponse,
    KillRunRequest,
    KillRunResponse,
    ResumeRequest,
    ResumeResponse,
    RunProjection,
    SignedDecision,
)
from .security import Authenticator, Principal, Role, require_role
from .settings import DEFAULT_PLATFORM, FC_VERSION, ConfigError, env_bool, read_secret

log = logging.getLogger("fc27.graph_api")

ThreadIdPath = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9-]+$")


@dataclass
class ApiSettings:
    postgres_uri: str
    authenticator: Authenticator
    signing_secret: str | None = None
    approval_ttl_seconds: int = 120
    redis_url: str | None = None
    run_migrations: bool = False
    checkpoint_setup: bool = False
    enforce_db_posture: bool = True
    dispatch_enabled: bool = False
    cli_hmac_enabled: bool = True
    approval_request_ttl_hours: int = 72
    expiry_interval_seconds: int = 60
    code_revision: str | None = None
    deployment_id: str | None = None

    @classmethod
    def from_env(cls) -> "ApiSettings":
        try:
            cli_hmac = env_bool("APPROVAL_CLI_HMAC_ENABLED", True)
            comm_id = read_secret("COMMISSIONER_ID")
            if not re.match(r"^[A-Za-z0-9._-]{1,64}$", comm_id):
                raise ConfigError("COMMISSIONER_ID must match ^[A-Za-z0-9._-]{1,64}$")
            auth = Authenticator(
                read_secret("GRAPH_SERVICE_TOKEN", min_length=32),
                read_secret("GRAPH_COMMISSIONER_TOKEN_SHA256"),
                comm_id,
            )
            return cls(
                postgres_uri=read_secret("POSTGRES_URI"),
                authenticator=auth,
                signing_secret=read_secret(
                    "APPROVAL_SIGNING_SECRET", required=cli_hmac, min_length=32 if cli_hmac else 0
                ),
                approval_ttl_seconds=int(os.getenv("APPROVAL_TTL_SECONDS", "120")),
                redis_url=read_secret("REDIS_URL", required=False),
                run_migrations=env_bool("GRAPH_RUN_MIGRATIONS", False),
                checkpoint_setup=env_bool("GRAPH_CHECKPOINT_SETUP", False),
                dispatch_enabled=env_bool("DISPATCH_ENABLED", False),
                cli_hmac_enabled=cli_hmac,
                approval_request_ttl_hours=int(os.getenv("APPROVAL_REQUEST_TTL_HOURS", "72")),
                code_revision=read_secret("FC27_CODE_REVISION", required=False) or os.getenv("FC27_CODE_REVISION"),
                deployment_id=read_secret("FC27_DEPLOYMENT_ID", required=False) or os.getenv("FC27_DEPLOYMENT_ID"),
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc


class GateChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["passed", "failed"] = "passed"
    evidence_ref: str = Field(default="manual-verification", min_length=1, max_length=500)


class GateRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate_id: str
    status: Literal["passed", "failed"]
    evidence_ref: str = Field(min_length=1, max_length=500)
    code_revision: str
    deployment_id: str
    fc_version: Literal["27"]
    platform: Literal["common-gen5"]
    issued_at: int
    expires_at: int
    nonce: str
    signature: str


def reevaluate_gates(app: FastAPI) -> None:
    """Evaluates all activation gates and updates app.state.dispatch_effective."""
    settings: ApiSettings = getattr(app.state, "settings", None)
    if settings is None:
        return

    identity = DeploymentIdentity(
        code_revision=settings.code_revision or "",
        deployment_id=settings.deployment_id or "",
        fc_version=FC_VERSION,
        platform=DEFAULT_PLATFORM,
    )
    with psycopg.connect(settings.postgres_uri) as conn:
        results = evaluate_gates(conn, identity)

    effective = dispatch_effective(settings.dispatch_enabled, results)
    app.state.dispatch_effective = effective
    app.state.gate_report = {
        "configured": settings.dispatch_enabled,
        "effective": effective,
        "gates": [{"gate_id": r.gate_id, "passed": r.passed, "detail": r.detail} for r in results],
        "code_revision": settings.code_revision,
        "deployment_id": settings.deployment_id,
        "fc_version": FC_VERSION,
        "platform": DEFAULT_PLATFORM,
    }
    if settings.dispatch_enabled and not effective:
        failed = [r.gate_id for r in results if not r.passed]
        log.critical("dispatch configured but blocked; failing gates: %s", failed)


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
        except Exception:
            log.warning("redis publish failed for channel %s", channel)

    return publish


def _store_error(exc: StoreError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _to_run_projection(state: dict[str, Any]) -> dict[str, Any]:
    legacy_sel = None
    if "dispatch" in state and isinstance(state["dispatch"], dict):
        legacy_sel = {**state["dispatch"], "note": "recorded selection only; never delivered"}

    return {
        "thread_id": state["thread_id"],
        "status": state.get("status", "unknown"),
        "club_id": state.get("club_id", ""),
        "platform": state.get("platform", DEFAULT_PLATFORM),
        "fc_version": state.get("fc_version", FC_VERSION),
        "package_version": state.get("package_version"),
        "approval_request_id": str(state["approval_request_id"]) if state.get("approval_request_id") else None,
        "approved_by": state.get("approved_by"),
        "approved_at": str(state.get("approved_at")) if state.get("approved_at") else None,
        "errors": list(state.get("errors", []) or []),
        "graph_next": list(state.get("graph_next", []) or []),
        "graph_interrupted": bool(state.get("graph_interrupted", False)),
        "dispatch_enabled": False,
        "lead_packages": state.get("lead_packages"),
        "legacy_selection_record": legacy_sel,
        "approval_request": state.get("approval_request"),
    }


def create_app(
    settings: ApiSettings | None = None,
    *,
    pipeline: Pipeline | None = None,
    publisher: Publisher | None = None,
    runner=None,
) -> FastAPI:
    """Creates the FastAPI application for FC27 Graph API."""
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
        except Exception:
            log.exception("graph resume failed for %s; will retry on next startup", thread_id)
            return "pending"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.run_migrations:
            migrate(settings.postgres_uri)

        if settings.enforce_db_posture:
            with psycopg.connect(settings.postgres_uri) as conn:
                violations = check_runtime_posture(conn)
                if violations:
                    raise ConfigError(f"database role posture violation: {violations}")

        if graph["runner"] is None and pipeline is None:
            from .graph import GraphDeps, GraphRunner
            from .roles import get_role_runner
            from .tracing import tracer_from_env

            graph["runner"] = GraphRunner.from_conninfo(
                settings.postgres_uri,
                GraphDeps(role_runner=get_role_runner(), decision_reader=store.load, tracer=tracer_from_env()),
                setup=settings.checkpoint_setup,
            )
            graph["owned"] = True

        if graph["runner"] is not None:
            for thread_id in store.terminal_thread_ids():
                resume_graph(thread_id)

        app.state.settings = settings
        reevaluate_gates(app)
        stop_event = asyncio.Event()

        async def _background_loop():
            while not stop_event.is_set():
                try:
                    await asyncio.to_thread(store.expire_due)
                except Exception:
                    log.exception("background expiry failed")
                try:
                    await asyncio.to_thread(reevaluate_gates, app)
                except Exception:
                    log.exception("background gate reevaluation failed")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=settings.expiry_interval_seconds)
                except asyncio.TimeoutError:
                    pass

        bg_task = asyncio.create_task(_background_loop())

        yield

        stop_event.set()
        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass

        if graph.get("owned"):
            graph["runner"].pool.close()

    app = FastAPI(title="FC27 Graph API", version=FC_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.dispatch_effective = False
    app.state.gate_report = {"configured": settings.dispatch_enabled, "effective": False, "gates": []}
    app.add_middleware(CorrelationMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError):
        clean_errors = []
        for err in exc.errors():
            err_dict = dict(err)
            err_dict.pop("input", None)
            if "ctx" in err_dict and isinstance(err_dict["ctx"], dict):
                err_dict["ctx"] = {k: str(v) if isinstance(v, Exception) else v for k, v in err_dict["ctx"].items() if k != "input"}
            clean_errors.append(err_dict)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(clean_errors)})

    def principal(x_api_key: str | None = Header(default=None)) -> Principal:
        return settings.authenticator.authenticate(x_api_key)

    def service(p: Principal = Depends(principal)) -> Principal:
        return require_role(p, Role.service)

    def commissioner(p: Principal = Depends(principal)) -> Principal:
        return require_role(p, Role.commissioner)

    # --------------------------------------------------------------------------
    # Health Probes
    # --------------------------------------------------------------------------
    @app.get("/health/live", response_model=HealthLiveResponse)
    def health_live() -> dict:
        return {"status": "ok", "process": "live", "fc_version": FC_VERSION}

    @app.get("/health/ready")
    def health_ready() -> dict:
        try:
            with psycopg.connect(settings.postgres_uri, connect_timeout=3) as conn:
                conn.execute("SELECT 1").fetchone()
                if settings.enforce_db_posture:
                    violations = check_runtime_posture(conn)
                    if violations:
                        return JSONResponse(
                            status_code=503,
                            content={
                                "status": "degraded",
                                "ready": False,
                                "reason": f"db_role_posture violation: {violations}",
                            },
                        )
                cp_exists = conn.execute("SELECT to_regclass('public.checkpoints')").fetchone()[0]
                if not cp_exists:
                    return JSONResponse(
                        status_code=503,
                        content={"status": "degraded", "ready": False, "reason": "checkpointer tables not found"},
                    )
                inconsistencies = conn.execute(
                    "SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'"
                ).fetchone()[0]
                if inconsistencies > 0:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "status": "degraded",
                            "ready": False,
                            "reason": f"approval inconsistencies detected: {inconsistencies}",
                        },
                    )
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "ready": False, "reason": f"postgres unavailable: {type(exc).__name__}"},
            )

        if settings.dispatch_enabled:
            effective = getattr(app.state, "dispatch_effective", False)
            if not effective:
                report = getattr(app.state, "gate_report", {}) or {}
                failed = [g["gate_id"] for g in report.get("gates", []) if not g.get("passed")]
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "degraded",
                        "ready": False,
                        "reason": f"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: {', '.join(failed)})",
                    },
                )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "ready": False,
                    "reason": "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false",
                },
            )

        return {
            "status": "ok",
            "ready": True,
            "postgres": "ok",
            "db_role_posture": "ok",
            "approval_consistency": 0,
            "checkpointer": "ok",
            "dispatch": "disabled",
            "dispatch_enabled": False,
            "fc_version": FC_VERSION,
            "platform": DEFAULT_PLATFORM,
        }

    @app.get("/health", response_model=HealthResponse)
    def health() -> dict:
        try:
            with psycopg.connect(settings.postgres_uri, connect_timeout=3) as conn:
                conn.execute("SELECT 1").fetchone()
                inconsistencies = 0
                has_view = conn.execute("SELECT to_regclass('public.fc27_approval_inconsistencies')").fetchone()[0]
                if has_view:
                    inconsistencies = conn.execute(
                        "SELECT count(*) FROM public.fc27_approval_inconsistencies WHERE kind <> 'legacy_unlinked'"
                    ).fetchone()[0]
        except Exception as exc:
            raise HTTPException(503, "postgres unavailable") from exc
        return {
            "status": "ok",
            "postgres": "ok",
            "fc_version": FC_VERSION,
            "approval_consistency": inconsistencies,
            "dispatch": "disabled",
            "db_role_posture": "ok",
        }

    @app.get("/v1/config/dispatch")
    def config_dispatch(_p: Principal = Depends(principal)) -> dict:
        report = getattr(app.state, "gate_report", {}) or {}
        return {
            "configured": settings.dispatch_enabled,
            "effective": getattr(app.state, "dispatch_effective", False),
            "gates": report.get("gates", []),
            "code_revision": settings.code_revision,
            "deployment_id": settings.deployment_id,
            "fc_version": FC_VERSION,
            "platform": DEFAULT_PLATFORM,
        }

    # --------------------------------------------------------------------------
    # Activation Gates Endpoints
    # --------------------------------------------------------------------------
    @app.post("/v1/activation-gates/{gate_id}/challenge")
    def gate_challenge(
        request: Request,
        gate_id: str = Path(pattern=r"^G-[A-Z]+$"),
        body: GateChallengeRequest = Body(default_factory=GateChallengeRequest),
        p: Principal = Depends(commissioner),
    ) -> dict:
        if not settings.cli_hmac_enabled:
            raise HTTPException(403, "proof_type_disabled")

        if gate_id not in ("G-ID", "G-ATOMIC", "G-OUTBOX"):
            raise HTTPException(422, "automatic gate")

        if gate_id not in RECORDABLE_GATES:
            raise HTTPException(409, "not_recordable_before_phase_3")

        if not settings.code_revision or not settings.deployment_id:
            raise HTTPException(409, "deployment_identity_missing")

        with psycopg.connect(settings.postgres_uri) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT extract(epoch from now())::bigint AS now_ts")
                now_ts = cur.fetchone()["now_ts"]
                issued_at = int(now_ts)
                expires_at = issued_at + min(settings.approval_ttl_seconds, 300)
                nonce = secrets.token_urlsafe(32)
                payload = {
                    "gate_id": gate_id,
                    "status": body.status,
                    "evidence_ref": body.evidence_ref,
                    "code_revision": settings.code_revision,
                    "deployment_id": settings.deployment_id,
                    "fc_version": FC_VERSION,
                    "platform": DEFAULT_PLATFORM,
                    "issued_at": issued_at,
                    "expires_at": expires_at,
                    "nonce": nonce,
                }
                digest = gate_payload_sha256(payload)
                cur.execute(
                    """
                    INSERT INTO public.approval_nonces (
                        nonce, gate_id, payload_sha256, issued_at, expires_at, created_by, subject_type
                    ) VALUES (
                        %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, 'activation_gate'
                    )
                    """,
                    (nonce, gate_id, digest, issued_at, expires_at, p.name),
                )
        return payload

    @app.post("/v1/activation-gates/{gate_id}")
    def gate_record(
        request: Request,
        gate_id: str,
        body: GateRecordRequest,
        p: Principal = Depends(commissioner),
    ) -> dict:
        if not settings.cli_hmac_enabled:
            raise HTTPException(403, "proof_type_disabled")

        if body.gate_id != gate_id:
            raise HTTPException(409, "gate_id path and body mismatch")

        if gate_id not in ("G-ID", "G-ATOMIC", "G-OUTBOX"):
            raise HTTPException(422, "automatic gate")

        if gate_id not in RECORDABLE_GATES:
            raise HTTPException(409, "not_recordable_before_phase_3")

        if not settings.code_revision or not settings.deployment_id:
            raise HTTPException(409, "deployment_identity_missing")

        try:
            verify_gate_signature(body.model_dump(), body.signature, settings.signing_secret)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(403, "invalid gate signature") from exc

        cid = getattr(request.state, "correlation_id", None) or uuid4()
        payload_dict = body.model_dump()
        payload_digest = gate_payload_sha256(payload_dict)
        sig_hash = hashlib.sha256(body.signature.encode("utf-8")).hexdigest()

        with psycopg.connect(settings.postgres_uri) as conn, conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT nonce, gate_id, payload_sha256, consumed_at, revoked_at,
                           extract(epoch FROM expires_at)::bigint AS expires_at,
                           now() > expires_at AS expired,
                           to_char(now() + interval '60 seconds' AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS next_eval
                      FROM public.approval_nonces
                     WHERE nonce = %s
                       FOR UPDATE
                    """,
                    (body.nonce,),
                )
                nonce_row = cur.fetchone()
                if nonce_row is None:
                    raise HTTPException(409, "unknown gate nonce")
                if nonce_row["gate_id"] != gate_id:
                    raise HTTPException(409, "nonce is bound to a different gate")
                if nonce_row["consumed_at"] is not None:
                    raise HTTPException(409, "nonce already consumed")
                if nonce_row["revoked_at"] is not None:
                    raise HTTPException(409, "nonce revoked")
                if nonce_row["expired"]:
                    raise HTTPException(409, "nonce expired")
                if nonce_row["payload_sha256"] != payload_digest:
                    raise HTTPException(409, "nonce payload mismatch")

                cur.execute(
                    "UPDATE public.approval_nonces SET consumed_at = now() WHERE nonce = %s",
                    (body.nonce,),
                )
                cur.execute(
                    """
                    INSERT INTO public.activation_gate_log (
                        gate_id, status, evidence_ref, code_revision, deployment_id,
                        fc_version, platform, nonce, signature_sha256, recorded_by
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING id
                    """,
                    (
                        gate_id,
                        body.status,
                        body.evidence_ref,
                        body.code_revision,
                        body.deployment_id,
                        body.fc_version,
                        body.platform,
                        body.nonce,
                        sig_hash,
                        f"commissioner:{p.name}",
                    ),
                )
                log_id = cur.fetchone()["id"]
                next_evaluation_at = nonce_row["next_eval"]

            from .events import record_event
            old_f = conn.row_factory
            conn.row_factory = tuple_row
            try:
                record_event(
                    conn,
                    event_type="activation.gate_recorded",
                    entity_type="activation_gate",
                    entity_id=gate_id,
                    actor=f"commissioner:{p.name}",
                    correlation_id=cid,
                    visibility="audit_only",
                    source_table="activation_gate_log",
                    source_id=str(log_id),
                )
            finally:
                conn.row_factory = old_f

        return {
            "gate": gate_id,
            "effective_after": "next_evaluation",
            "next_evaluation_at": next_evaluation_at,
        }

    # --------------------------------------------------------------------------
    # Core Graph Run Endpoints (POST /v1/runs, POST /run)
    # --------------------------------------------------------------------------
    def create_run_internal(req: RunRequest) -> dict:
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
        except Exception as exc:
            log.exception("pipeline failed for %s", state["thread_id"])
            result = {**state, "status": "failed", "errors": list(state.get("errors", [])) + [type(exc).__name__]}

        result = {**result, "thread_id": state["thread_id"]}
        approval_req_id = None
        if isinstance(result.get("approval_request"), dict):
            approval_req_id = result["approval_request"].get("approval_request_id")
        if not approval_req_id:
            approval_req_id = result.get("approval_request_id")

        try:
            final = store.complete_run(state["thread_id"], result, approval_request_id=approval_req_id)
        except StoreError as exc:
            raise _store_error(exc) from exc

        if final["status"] == "awaiting_approval":
            publish("fc27:approval:requested", {"thread_id": final["thread_id"], "status": final["status"]})

        return _to_run_projection(final)

    @app.post("/v1/runs", status_code=201, response_model=RunProjection)
    def v1_create_run(req: RunRequest, _p: Principal = Depends(service)) -> dict:
        return create_run_internal(req)

    @app.post("/run", status_code=201, include_in_schema=False)
    def run(req: RunRequest, _p: Principal = Depends(service)) -> dict:
        return create_run_internal(req)

    # --------------------------------------------------------------------------
    # Run Inspection Endpoints (GET /v1/runs/{id}, GET /threads/{id})
    # --------------------------------------------------------------------------
    def get_run_internal(thread_id: str) -> dict:
        row = store.load(thread_id)
        if row is None:
            raise HTTPException(404, "thread not found")
        state = dict(row["state"])
        state.update(
            status=row["status"],
            package_version=row["package_version"],
            approved_by=row["approved_by"],
            approved_at=row.get("approved_at"),
            approval_request_id=row.get("approval_request_id"),
        )
        return _to_run_projection(state)

    @app.get("/v1/runs/{thread_id}", response_model=RunProjection)
    def v1_get_run(thread_id: str = ThreadIdPath, _p: Principal = Depends(service)) -> dict:
        return get_run_internal(thread_id)

    @app.get("/threads/{thread_id}", include_in_schema=False)
    def get_thread(thread_id: str = ThreadIdPath, _p: Principal = Depends(service)) -> dict:
        return get_run_internal(thread_id)

    # --------------------------------------------------------------------------
    # Approval Request Projection (GET /v1/runs/{id}/approval-request)
    # --------------------------------------------------------------------------
    @app.get("/v1/runs/{thread_id}/approval-request", response_model=ApprovalRequestProjection)
    def get_approval_request(thread_id: str = ThreadIdPath, _p: Principal = Depends(principal)) -> dict:
        row = store.load(thread_id)
        if row is None:
            raise HTTPException(404, "thread not found")
        req_id = row.get("approval_request_id")
        if not req_id:
            raise HTTPException(404, "approval request not found")

        with store.connect() as conn:
            req_row = conn.execute(
                """SELECT approval_request_id, thread_id, package_version, status,
                          created_at, expires_at, (expires_at <= now()) AS is_expired
                     FROM public.approval_requests
                    WHERE approval_request_id = %s""",
                (req_id,),
            ).fetchone()
        if not req_row:
            raise HTTPException(404, "approval request not found")

        state = dict(row.get("state") or {})
        lead_pkgs = state.get("lead_packages") or {}
        display_summary = {}
        approvable_owners = []
        for owner, pkg in lead_pkgs.items():
            if isinstance(pkg, dict) and pkg.get("approvable"):
                approvable_owners.append(owner)
                best = pkg.get("best")
                if isinstance(best, dict):
                    display_summary[owner] = {
                        "candidate_id": best.get("candidate_id"),
                        "player_id": best.get("player_id"),
                        "tier": best.get("tier"),
                        "score": best.get("score"),
                    }

        return {
            "approval_request_id": str(req_row["approval_request_id"]),
            "thread_id": req_row["thread_id"],
            "package_version": req_row["package_version"],
            "package_hash": req_row["package_version"],
            "status": req_row["status"],
            "created_at": str(req_row["created_at"]),
            "expires_at": str(req_row["expires_at"]),
            "is_expired": bool(req_row["is_expired"]),
            "approvable_owners": approvable_owners,
            "display_summary": display_summary,
        }

    # --------------------------------------------------------------------------
    # Restricted Resume (POST /v1/runs/{id}/resume)
    # --------------------------------------------------------------------------
    @app.post("/v1/runs/{thread_id}/resume", response_model=ResumeResponse)
    def v1_resume(
        _req: ResumeRequest,
        thread_id: str = ThreadIdPath,
        _p: Principal = Depends(service),
    ) -> dict:
        row = store.load(thread_id)
        if row is None:
            raise HTTPException(404, "thread not found")
        if row.get("status") not in DECIDED_STATUSES:
            raise HTTPException(409, "approval_not_committed")

        res = resume_graph(thread_id)
        return {"thread_id": thread_id, "status": row["status"], "graph_resume": res}

    # --------------------------------------------------------------------------
    # Decision Challenge Endpoints
    # --------------------------------------------------------------------------
    def require_hmac() -> None:
        if not settings.cli_hmac_enabled:
            raise HTTPException(403, "proof_type_disabled")

    def decision_challenge_internal(intent: DecisionIntent, thread_id: str, p: Principal) -> dict:
        try:
            return store.issue_challenge(thread_id, intent, p, settings.approval_ttl_seconds)
        except StoreError as exc:
            raise _store_error(exc) from exc

    @app.post("/v1/runs/{thread_id}/decision-challenge", dependencies=[Depends(require_hmac)])
    def v1_decision_challenge(
        intent: DecisionIntent,
        thread_id: str = ThreadIdPath,
        p: Principal = Depends(commissioner),
    ) -> dict:
        return decision_challenge_internal(intent, thread_id, p)

    @app.post("/threads/{thread_id}/decision-challenge", dependencies=[Depends(require_hmac)], include_in_schema=False)
    def decision_challenge(
        intent: DecisionIntent,
        thread_id: str = ThreadIdPath,
        p: Principal = Depends(commissioner),
    ) -> dict:
        return decision_challenge_internal(intent, thread_id, p)

    # --------------------------------------------------------------------------
    # Authoritative Decision Application
    # --------------------------------------------------------------------------
    def apply_decision_internal(request: Request, thread_id: str, body: SignedDecision, p: Principal) -> dict:
        if not settings.cli_hmac_enabled:
            raise HTTPException(403, "proof_type_disabled")

        cid = getattr(request.state, "correlation_id", None)
        dispatch_effective = getattr(request.app.state, "dispatch_effective", False)
        from .dispatch.creation import create_dispatch_for_approval

        try:
            state = store.apply_decision(
                thread_id,
                body,
                p,
                settings.signing_secret,
                correlation_id=cid,
                dispatch_enabled=dispatch_effective,
                dispatch_creator=create_dispatch_for_approval,
            )
        except StoreError as exc:
            raise _store_error(exc) from exc

        publish("fc27:approval:decided", {"thread_id": thread_id, "status": state["status"]})
        res = resume_graph(thread_id)

        dispatch_id = state.get("dispatch_id")
        if dispatch_effective and dispatch_id:
            dispatch_block = DispatchBlock(
                enabled=True,
                created=True,
                message="Dispatch created.",
                dispatch_id=str(dispatch_id),
            )
        else:
            dispatch_block = DispatchBlock(
                enabled=False,
                created=False,
                message="Dispatch is disabled. This approval records the selection only; nothing was delivered to any player.",
                dispatch_id=None,
            )

        return {
            **state,
            "thread_id": thread_id,
            "status": state["status"],
            "package_version": state.get("package_version"),
            "approval_audit_id": state.get("approval_audit_id"),
            "approval_request_id": str(state["approval_request_id"]) if state.get("approval_request_id") else None,
            "dispatch": dispatch_block.model_dump(mode="json"),
            "graph_resume": res,
        }

    @app.post("/v1/runs/{thread_id}/decision", response_model=DecisionResponse, dependencies=[Depends(require_hmac)])
    def v1_decision(
        request: Request,
        body: SignedDecision,
        thread_id: str = ThreadIdPath,
        p: Principal = Depends(commissioner),
    ) -> dict:
        return apply_decision_internal(request, thread_id, body, p)

    @app.post("/threads/{thread_id}/decision", dependencies=[Depends(require_hmac)], include_in_schema=False)
    def decision(
        request: Request,
        body: SignedDecision,
        thread_id: str = ThreadIdPath,
        p: Principal = Depends(commissioner),
    ) -> dict:
        return apply_decision_internal(request, thread_id, body, p)

    @app.post(
        "/v1/approval-requests/{approval_request_id}/decisions",
        response_model=DecisionResponse,
        dependencies=[Depends(require_hmac)],
    )
    def approval_request_decision(
        request: Request,
        body: SignedDecision,
        approval_request_id: str = Path(pattern=r"^[0-9a-f-]{36}$"),
        p: Principal = Depends(commissioner),
    ) -> dict:
        with store.connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM public.approval_requests WHERE approval_request_id = %s",
                (approval_request_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "approval request not found")

        return apply_decision_internal(request, row["thread_id"], body, p)

    # --------------------------------------------------------------------------
    # Emergency Run Kill Switch (POST /v1/runs/{id}/kill)
    # --------------------------------------------------------------------------
    @app.post("/v1/runs/{thread_id}/kill", response_model=KillRunResponse)
    def kill_run(
        request: Request,
        req: KillRunRequest,
        thread_id: str = ThreadIdPath,
        p: Principal = Depends(principal),
    ) -> dict:
        if p.role not in (Role.service, Role.commissioner):
            raise HTTPException(403, "insufficient privileges to kill run")

        with store.connect() as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT nonce FROM public.approval_nonces WHERE thread_id = %s AND consumed_at IS NULL AND revoked_at IS NULL FOR UPDATE",
                    (thread_id,),
                )
                th_row = conn.execute(
                    "SELECT thread_id, status, approval_request_id, state FROM public.graph_threads WHERE thread_id = %s FOR UPDATE",
                    (thread_id,),
                ).fetchone()
                if th_row is None:
                    raise HTTPException(404, "thread not found")

                if th_row["status"] in ("approved", "rejected", "revision_requested", "expired", "failed"):
                    raise HTTPException(409, "cannot kill terminal thread")

                req_id = th_row["approval_request_id"]
                target_status = "failed"
                if req_id is not None:
                    conn.execute(
                        "UPDATE public.approval_requests SET status = 'expired', expired_at = now(), version = version + 1 WHERE approval_request_id = %s AND status = 'awaiting_approval'",
                        (req_id,),
                    )
                    target_status = "expired"

                conn.execute(
                    "UPDATE public.approval_nonces SET revoked_at = now() WHERE thread_id = %s AND consumed_at IS NULL AND revoked_at IS NULL",
                    (thread_id,),
                )

                state = dict(th_row["state"] or {})
                errors = list(state.get("errors", []) or []) + [f"killed: {req.reason}"]
                state["errors"] = errors
                state["status"] = target_status
                conn.execute(
                    "UPDATE public.graph_threads SET status = %s, state = %s, updated_at = now() WHERE thread_id = %s",
                    (target_status, Jsonb(state), thread_id),
                )

                cid = getattr(request.state, "correlation_id", uuid4())
                try:
                    from src.events import record_event

                    with conn.transaction():
                        if target_status == "expired":
                            record_event(
                                conn,
                                event_type="approval.expired",
                                entity_type="approval_request",
                                entity_id=str(req_id),
                                actor=p.name,
                                correlation_id=cid,
                                visibility="staff",
                                scope_club_id=state.get("club_id", "330323"),
                                source_table="approval_requests",
                                source_id=str(req_id),
                                from_status="awaiting_approval",
                                to_status="expired",
                                reason=f"run killed: {req.reason}",
                            )
                        else:
                            record_event(
                                conn,
                                event_type="system.degraded",
                                entity_type="graph_thread",
                                entity_id=thread_id,
                                actor=p.name,
                                correlation_id=cid,
                                visibility="staff",
                                scope_club_id=state.get("club_id", "330323"),
                                reason=f"run killed: {req.reason}",
                            )
                except Exception:
                    pass

        return {
            "thread_id": thread_id,
            "status": target_status,
            "killed": True,
            "reason": req.reason,
        }

    return app
