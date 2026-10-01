"""Audit trail for one FC27 graph run.

    python -m src.audit_query fc27-330323-run-20261001-001 [--json]

Combines two sources:
  * LangGraph PostgresSaver checkpoints (node progression, interrupt, metadata)
  * graph_threads / approval_audit (the authoritative committed decision)

Output never includes credentials, signatures or the raw dashboard. Requires
POSTGRES_URI (only available inside graph-api or on the operator host).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .graph import GraphDeps, GraphRunner
from .thread_ids import parse_thread_id

SAFE_METADATA_KEYS = ("fc_version", "platform", "club_id", "step", "source")
REDACT_PAYLOAD_KEYS = {"signature", "signature_sha256"}


class ThreadNotFound(LookupError):
    pass


def _decision(conn, thread_id: str) -> tuple[dict | None, dict | None]:
    thread = conn.execute(
        "SELECT thread_id, status, package_version, approved_by, approved_at, created_at FROM graph_threads WHERE thread_id=%s",
        (thread_id,)).fetchone()
    audit = conn.execute(
        """SELECT decision, approved_by, owner, selection, candidate_id, player_id, notes, package_version, decided_at
             FROM approval_audit WHERE thread_id=%s""", (thread_id,)).fetchone()
    return thread, audit


def get_audit(conninfo: str, thread_id: str, runner: GraphRunner | None = None) -> dict[str, Any]:
    if not parse_thread_id(thread_id):
        raise ValueError("thread_id must match fc27-<clubId>-run-<YYYYMMDD>-<seq>")
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        thread, audit = _decision(conn, thread_id)
    owned = runner is None
    runner = runner or GraphRunner.from_conninfo(conninfo, GraphDeps(), max_pool=2)
    try:
        history = runner.history(thread_id)
    finally:
        if owned:
            runner.pool.close()
    if thread is None and not history:
        raise ThreadNotFound(f"no thread or checkpoints for {thread_id}")

    checkpoints = []
    for snap in reversed(history):  # oldest first
        md = snap.metadata or {}
        checkpoints.append({
            "checkpoint_id": snap.config["configurable"].get("checkpoint_id"),
            "created_at": snap.created_at,
            **{k: md.get(k) for k in SAFE_METADATA_KEYS},
            "next": list(snap.next),
            "interrupted": any(t.interrupts for t in snap.tasks),
            "status": (snap.values or {}).get("status"),
        })
    final = dict(history[0].values) if history else {}
    return {
        "thread_id": thread_id,
        "fc_version": final.get("fc_version", "27"),
        "platform": final.get("platform", "common-gen5"),
        "db_status": thread["status"] if thread else None,
        "package_version": thread["package_version"] if thread else None,
        "node_progression": [e.get("node") for e in final.get("audit_log", [])],
        "audit_log": final.get("audit_log", []),
        "approval_request": final.get("approval_request"),
        "graph_decision": {k: v for k, v in (final.get("approval_decision") or {}).items() if k not in REDACT_PAYLOAD_KEYS} or None,
        "committed_decision": {k: (str(v) if k == "decided_at" else v) for k, v in audit.items()} if audit else None,
        "model_ids": final.get("model_ids"),
        "prompt_version": final.get("prompt_version"),
        "policy_version": final.get("policy_version"),
        "errors": final.get("errors", []),
        "checkpoints": checkpoints,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Show the audit trail for one FC27 graph run")
    p.add_argument("thread_id")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    conninfo = os.getenv("POSTGRES_URI")
    if not conninfo:
        print("POSTGRES_URI is not set", file=sys.stderr)
        return 2
    try:
        report = get_audit(conninfo, args.thread_id)
    except (ThreadNotFound, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(f"{report['thread_id']}  fc_version={report['fc_version']} platform={report['platform']} status={report['db_status']}")
    print("nodes: " + " -> ".join(n for n in report["node_progression"] if n))
    for cp in report["checkpoints"]:
        flag = " [INTERRUPT: awaiting Commissioner]" if cp["interrupted"] else ""
        print(f"  step {cp['step']:>3} {cp['source']:6} next={','.join(cp['next']) or '-':18} status={cp['status']}{flag}")
    d = report["committed_decision"]
    print(f"decision: {d['decision']} by {d['approved_by']} at {d['decided_at']} ({d.get('owner')}/{d.get('selection')} {d.get('player_id')})" if d else "decision: none committed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
