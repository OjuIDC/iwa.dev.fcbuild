"""FC27 orchestration as a LangGraph StateGraph with PostgreSQL checkpoints.

    START -> search -> scout -> analyst -> coach -> tactics -> director
          -> prepare_approval -> approval (interrupt) -> finalize_decision -> END
    search fails -> END (status=failed)

* Checkpointer: ``langgraph.checkpoint.postgres.PostgresSaver``; every run uses
  ``durability="sync"`` so a checkpoint is persisted after every node.
* ``thread_id`` (fc27-<clubId>-run-<YYYYMMDD>-<seq>) is the LangGraph
  configurable thread id. Checkpoint metadata always carries
  ``{"fc_version": "27", "platform": "common-gen5", "club_id": ...}``.
* The approval node calls ``interrupt()``. The graph is resumed only by the
  Graph API after the Commissioner's signed decision has been committed to
  PostgreSQL. The resume value is ignored for authority: the node re-reads the
  committed decision from the database and fails closed if none exists, so
  supplying state or resume data directly cannot approve anything.
"""
from __future__ import annotations

import json
import logging
import operator
import os
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict, cast
from uuid import uuid4

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RunnableConfig, interrupt

from .decision import package_version
from .lead_packages import (
    POLICY_VERSION,
    build_lead_packages,
    emit_pre_approval_validation_trace,
    players_from_dashboard,
    validate_pre_approval_packages,
)
from .model_selection import ModelRegistry
from .roles import PROMPT_VERSION, RoleResult, run_role
from .settings import DEFAULT_PLATFORM, FC_VERSION
from .thread_ids import parse_thread_id
from .tracing import NullTracer

log = logging.getLogger("fc27.graph")
ROLE_NODES = ("scout", "analyst", "coach", "tactics", "director")

DECIDED_STATUSES = ("approved", "rejected", "revision_requested")

TERMINAL_STATUS_MAP = {
    "approved": "APPROVED, NOT DISPATCHED",
    "rejected": "REJECTED, NO ACTION TAKEN",
    "revision_requested": "REVISION_REQUESTED, NO ACTION TAKEN",
}


class FC27State(TypedDict, total=False):
    thread_id: str
    club_id: str
    fc_version: str
    platform: str
    trigger: str
    run_seq: int
    match: dict | None
    dashboard: dict
    candidates: list[dict]
    role_reports: dict[str, dict]
    lead_packages: dict[str, dict]
    package_version: str | None
    pre_approval_validation: dict
    formation: str | None
    lineup: dict | None
    assignments: list[dict] | dict | None
    roster: list[str] | None
    analytics_metadata: dict | None
    recommendations: list[dict] | None
    approval_request_id: str | None
    approval_request: dict
    approval_decision: dict
    status: str
    errors: Annotated[list[str], operator.add]
    model_ids: dict[str, str]
    prompt_version: str
    policy_version: str
    code_revision: str
    audit_log: Annotated[list[dict], operator.add]


class ApprovalNotCommitted(PermissionError):
    """Resume was attempted without a committed Commissioner decision."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _audit(node: str, state: FC27State, summary: str) -> list[dict]:
    return [{"node": node, "ts": _now(), "summary": summary, "fc_version": FC_VERSION,
             "platform": state.get("platform", DEFAULT_PLATFORM)}]


def mcp_call(tool: str, args: dict) -> dict:
    """Call the MCP proxy (the only component allowed to reach EA)."""
    url = os.getenv("FC27_MCP_URL", "http://fc27-mcp:8000").rstrip("/") + "/mcp"
    token = os.getenv("MCP_API_TOKEN", "")
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
                   headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"MCP error: {body['error'].get('message', 'unknown')}")
    return json.loads(body["result"]["content"][0]["text"])


@dataclass
class GraphDeps:
    mcp: Callable[[str, dict], dict] = mcp_call
    role_runner: Callable[..., RoleResult] = run_role
    registry_factory: Callable[[], ModelRegistry] = ModelRegistry
    # Returns the committed DB row for a thread: {"status", "approved_by", "approved_at", "decision_payload"}.
    decision_reader: Callable[[str], dict | None] = field(default=lambda thread_id: None)
    tracer: Any = field(default_factory=NullTracer)


def build_graph(deps: GraphDeps) -> StateGraph:
    def search(state: FC27State) -> dict:
        try:
            dashboard = deps.mcp("get_club_dashboard", {"club_id": state["club_id"], "platform": state["platform"]})
        except Exception as exc:
            return {"status": "failed", "errors": [f"search failed: {type(exc).__name__}"],
                    "audit_log": _audit("search", state, "dashboard unavailable")}
        players = players_from_dashboard(dashboard, state["club_id"])
        return {"dashboard": dashboard, "candidates": [{"player_id": p} for p in sorted(players)],
                "audit_log": _audit("search", state, f"dashboard loaded; {len(players)} players")}

    def role_node(role: str):
        def node(state: FC27State) -> dict:
            data = {"club_id": state["club_id"], "platform": state["platform"],
                    "players": players_from_dashboard(state.get("dashboard", {}), state["club_id"]),
                    "overall": (state.get("dashboard") or {}).get("overall"),
                    "prior_reports": {k: (v.get("output") or {}).get("summary") for k, v in (state.get("role_reports") or {}).items()}}
            result = deps.role_runner(role, data, registry=deps.registry_factory())
            reports = {**(state.get("role_reports") or {}), role: result.model_dump(mode="json")}
            model_ids = {**(state.get("model_ids") or {}), role: f"{result.provider}::{result.model}" if result.model else "none"}
            errors = [] if result.valid else [f"{role}: {result.error}"]
            return {"role_reports": reports, "model_ids": model_ids, "errors": errors,
                    "audit_log": _audit(role, state, "valid structured output" if result.valid else f"invalid: {result.error}")}
        node.__name__ = role
        return node

    def prepare_approval(state: FC27State) -> dict:
        results = {k: RoleResult.model_validate(v) for k, v in (state.get("role_reports") or {}).items()}
        if "lead_packages" in state and state.get("lead_packages") is not None:
            packages = state["lead_packages"]
        else:
            packages = build_lead_packages(state.get("dashboard", {}), state["club_id"], results, state["thread_id"])
        computed_version = package_version(packages) if isinstance(packages, dict) else None
        target_version = state.get("package_version") if "package_version" in state else computed_version

        validation = validate_pre_approval_packages(
            packages,
            package_version_hash=target_version,
            candidates=state.get("candidates"),
            thread_id=state.get("thread_id"),
            lineup=state.get("lineup"),
            formation=state.get("formation"),
            assignments=state.get("assignments"),
            roster=state.get("roster"),
            analytics_metadata=state.get("analytics_metadata"),
            recommendations=state.get("recommendations"),
        )
        validation_summary = validation.to_safe_summary()

        if not validation.valid or validation.review_required or bool(validation.reason_codes):
            reason_list = list(validation.reason_codes) or ["APPROVAL_POLICY_VIOLATION"]
            return {
                "lead_packages": packages if isinstance(packages, dict) else {},
                "package_version": None,
                "pre_approval_validation": validation_summary,
                "approval_request_id": None,
                "approval_request": {},
                "status": "failed",
                "errors": [f"pre_approval_validation: {code}" for code in reason_list],
                "audit_log": _audit(
                    "prepare_approval",
                    state,
                    f"pre-approval validation failed: {', '.join(reason_list)}",
                ),
            }

        version = computed_version
        approvable = sorted(k for k, v in packages.items() if isinstance(v, dict) and v.get("approvable"))
        req_id = state.get("approval_request_id") or str(uuid4())
        request = {
            "thread_id": state["thread_id"],
            "package_version": version,
            "package_hash": version,
            "approvable_owners": approvable,
            "approval_request_id": req_id,
            "safe_summary": f"approvable owners: {approvable or 'none'}",
            "required_authority": "commissioner",
            "fc_version": FC_VERSION,
            "platform": state["platform"],
            "validation_summary": validation_summary,
        }
        return {
            "lead_packages": packages,
            "package_version": version,
            "pre_approval_validation": validation_summary,
            "approval_request_id": req_id,
            "approval_request": request,
            "status": "awaiting_approval",
            "audit_log": _audit("prepare_approval", state, f"approvable owners: {approvable or 'none'}"),
        }

    def approval(state: FC27State) -> dict:
        pre_val = state.get("pre_approval_validation") or {}
        if state.get("status") == "failed" or pre_val.get("valid") is False or pre_val.get("review_required") is True:
            raise ApprovalNotCommitted("pre-approval validation did not pass; approval interrupt blocked")
        # Pauses here until the Graph API resumes the thread after a committed decision.
        interrupt(state["approval_request"])
        row = deps.decision_reader(state["thread_id"])
        if not row or row.get("status") not in DECIDED_STATUSES:
            raise ApprovalNotCommitted("no committed Commissioner decision for this thread")

        payload = row.get("decision_payload") or {}
        expected_version = state.get("package_version")
        if payload.get("package_version") != expected_version:
            raise ApprovalNotCommitted("committed decision refers to a different package version")
        if row.get("package_version") != expected_version:
            raise ApprovalNotCommitted("committed thread refers to a different package version")

        state_req_id = state.get("approval_request_id")
        row_req_id = row.get("approval_request_id")
        if state_req_id and row_req_id and str(row_req_id) != state_req_id:
            raise ApprovalNotCommitted("committed decision refers to a different approval request")

        actor_type = row.get("actor_type") or payload.get("actor_type")
        authority_basis = row.get("authority_basis") or payload.get("authority_basis")
        if actor_type and actor_type != "commissioner":
            raise ApprovalNotCommitted(f"unsupported actor type in Phase 1: {actor_type}")
        if authority_basis and authority_basis != "primary":
            raise ApprovalNotCommitted(f"unsupported authority basis in Phase 1: {authority_basis}")

        db_status = row["status"]
        terminal_status = TERMINAL_STATUS_MAP.get(db_status, db_status)

        decision = {
            "status": db_status,
            "terminal_status": terminal_status,
            "approved_by": row.get("approved_by"),
            "approved_at": str(row.get("approved_at")),
            **{k: payload.get(k) for k in ("decision", "owner", "selection", "candidate_id", "player_id", "nonce")},
        }
        return {
            "approval_decision": decision,
            "status": terminal_status,
            "audit_log": _audit("approval", state, f"{terminal_status} by {row.get('approved_by')}"),
        }

    def finalize_decision(state: FC27State) -> dict:
        status = state.get("status", "unknown")
        return {"audit_log": _audit("finalize_decision", state, f"run finalized as {status}")}

    tracer = deps.tracer

    def traced(name: str, fn, as_type: str = "span"):
        def wrapper(state: FC27State) -> dict:
            tid = state.get("thread_id", "unknown")
            with tracer.span(f"node:{name}", tid, {"node": name, "club_id": state.get("club_id")}, as_type=as_type):
                try:
                    update = fn(state)
                except ApprovalNotCommitted as exc:
                    tracer.event("approval_resume_rejected", tid, {"reason": str(exc)}, level="WARNING")
                    raise
            for err in update.get("errors", []) or []:
                tracer.event("validation_failure", tid, {"node": name, "error": err}, level="WARNING")
            if name == "prepare_approval":
                if isinstance(update.get("pre_approval_validation"), dict):
                    emit_pre_approval_validation_trace(
                        tracer,
                        thread_id=tid,
                        club_id=state.get("club_id"),
                        result=update["pre_approval_validation"],
                    )
                if update.get("status") == "awaiting_approval" and isinstance(update.get("approval_request"), dict):
                    tracer.event(
                        "approval_interrupt",
                        tid,
                        {
                            "package_version": update.get("package_version"),
                            "approvable_owners": update["approval_request"].get("approvable_owners", []),
                        },
                    )
            if name == "approval":
                tracer.event("approval_resumed", tid, {"status": update.get("status")})
            if name in ROLE_NODES:
                report = update["role_reports"][name]
                tracer.event("role_call", tid, {"role": name, "valid": report["valid"], "model": update["model_ids"][name],
                                                 "prompt_version": report.get("prompt_version"), "fallback_used": report.get("fallback_used")})
            return update
        wrapper.__name__ = name
        return wrapper

    g = StateGraph(cast(Any, FC27State))
    g.add_node("search", traced("search", search, "tool"))
    for role in ROLE_NODES:
        g.add_node(role, traced(role, role_node(role), "agent"))
    g.add_node("prepare_approval", traced("prepare_approval", prepare_approval))
    g.add_node("approval", traced("approval", approval, "guardrail"))
    g.add_node("finalize_decision", traced("finalize_decision", finalize_decision))
    g.add_edge(START, "search")
    g.add_conditional_edges("search", lambda s: END if s.get("status") == "failed" else "scout", ["scout", END])
    for a, b in zip(ROLE_NODES, ROLE_NODES[1:]):
        g.add_edge(a, b)
    g.add_edge(ROLE_NODES[-1], "prepare_approval")
    g.add_conditional_edges(
        "prepare_approval",
        lambda s: (
            "finalize_decision"
            if (
                s.get("status") == "failed"
                or (s.get("pre_approval_validation") or {}).get("valid") is False
                or (s.get("pre_approval_validation") or {}).get("review_required") is True
            )
            else "approval"
        ),
        ["approval", "finalize_decision"],
    )
    g.add_edge("approval", "finalize_decision")
    g.add_edge("finalize_decision", END)
    return g


def run_config(thread_id: str, club_id: str, platform: str = DEFAULT_PLATFORM) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id},
            "metadata": {"fc_version": FC_VERSION, "platform": platform, "club_id": club_id}}


class GraphRunner:
    """Owns the compiled graph and its PostgresSaver checkpointer."""

    pool: Any = None

    def __init__(self, checkpointer, deps: GraphDeps, pool: Any = None):
        self.checkpointer = checkpointer
        self.tracer = deps.tracer
        self.decision_reader = deps.decision_reader
        self.app = build_graph(deps).compile(checkpointer=checkpointer)
        self.pool = pool

    @classmethod
    def from_conninfo(cls, conninfo: str, deps: GraphDeps, max_pool: int = 10, *, setup: bool = False) -> "GraphRunner":
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool: ConnectionPool[Connection[dict[str, Any]]] = ConnectionPool(
            conninfo, max_size=max_pool, open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        saver = PostgresSaver(pool)
        if setup:
            saver.setup()
        runner = cls(saver, deps, pool=pool)
        return runner

    def start(self, initial: dict) -> dict:
        thread_id: str = initial["thread_id"]
        club_id: str = initial["club_id"]
        platform: str = initial.get("platform", DEFAULT_PLATFORM)
        parts = parse_thread_id(thread_id) or {}
        state = {**initial, "fc_version": FC_VERSION, "run_seq": int(parts.get("seq", 0) or 0),
                 "prompt_version": PROMPT_VERSION, "policy_version": POLICY_VERSION,
                 "code_revision": os.getenv("FC27_CODE_REVISION", "unknown"), "status": "running",
                 "errors": [], "audit_log": []}
        cfg = run_config(thread_id, club_id, platform)
        try:
            self.app.invoke(state, cfg, durability="sync")
        finally:
            self.tracer.flush()
        return self.values(thread_id)

    def resume(self, thread_id: str) -> dict:
        snap = self.app.get_state({"configurable": {"thread_id": thread_id}})
        if not self._approval_pending(snap):
            return dict(snap.values)  # nothing pending: terminal threads never re-enter approval
        row = self.decision_reader(thread_id)
        if not row or row.get("status") not in DECIDED_STATUSES:
            return self.values(thread_id)
        v = snap.values
        try:
            self.app.invoke(Command(resume={"thread_id": thread_id}),
                            run_config(thread_id, v["club_id"], v.get("platform", DEFAULT_PLATFORM)), durability="sync")
        finally:
            self.tracer.flush()
        return self.values(thread_id)

    @staticmethod
    def _approval_pending(snap) -> bool:
        # After a failed resume attempt LangGraph reports next=() but keeps the
        # errored "approval" task pending, so inspect tasks rather than next.
        return any(t.name == "approval" for t in snap.tasks) or tuple(snap.next) == ("approval",)

    def values(self, thread_id: str) -> dict:
        snap = self.app.get_state({"configurable": {"thread_id": thread_id}})
        values = dict(snap.values)
        values["graph_next"] = [t.name for t in snap.tasks] or list(snap.next)
        values["graph_interrupted"] = bool(snap.interrupts)
        return values

    def history(self, thread_id: str) -> list:
        return list(self.app.get_state_history({"configurable": {"thread_id": thread_id}}))


def run_pipeline(state: dict) -> dict:  # pragma: no cover - kept for callers expecting the old name
    raise RuntimeError("run_pipeline was replaced by GraphRunner.start; see src/graph.py")
