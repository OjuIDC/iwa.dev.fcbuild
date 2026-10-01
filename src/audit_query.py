"""
audit_query.py
==============
FC27 Pro Clubs — Audit Trail Query Helpers
East Coasters FC Intelligence Platform

Queries the LangGraph PostgresSaver checkpoint store for:
- Full audit log per thread
- Time-travel replay to any checkpoint
- Compliance CSV export
- Tilt + Fair Play violation summaries

Run against the same Postgres instance as graph.py.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import datetime
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver

log = logging.getLogger("fc27.audit")

DB_URI     = os.getenv("POSTGRES_URI", "postgresql://fc27:fc27pass@postgres:5432/fc27_audit")
FC_VERSION = "27"
PLATFORM   = os.getenv("FC_PLATFORM", "common-gen5")

# ── Core Query Helpers ─────────────────────────────────────────────────────────

def get_full_audit(thread_id: str) -> list[dict]:
    """
    Return the complete audit log for a graph run by thread_id.
    Each entry has: node, ts, fc_version, platform, club_id, summary, data.

    Usage:
        entries = get_full_audit("your-thread-uuid")
        for e in entries:
            print(f"[{e['ts']}] {e['node']}: {e['summary']}")
    """
    with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
        config = {"configurable": {"thread_id": thread_id}}
        state  = checkpointer.get(config)

    if state is None:
        log.warning("No checkpoint found for thread_id=%s", thread_id)
        return []

    channel_values = state.channel_values if hasattr(state, "channel_values") else {}
    audit_log      = channel_values.get("audit_log", [])

    # Ensure fc_version and platform are on every entry
    for entry in audit_log:
        entry.setdefault("fc_version", FC_VERSION)
        entry.setdefault("platform",   PLATFORM)

    return audit_log


def get_all_checkpoints(thread_id: str) -> list[dict]:
    """
    Return all checkpoint snapshots for a thread — enables time travel.
    Each snapshot includes: checkpoint_id, ts, channel_values summary.

    Usage:
        checkpoints = get_all_checkpoints("your-thread-uuid")
        # checkpoints[0] is earliest, checkpoints[-1] is latest
    """
    with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
        config  = {"configurable": {"thread_id": thread_id}}
        history = list(checkpointer.list(config))

    snapshots = []
    for i, snapshot in enumerate(history):
        cv = snapshot.channel_values if hasattr(snapshot, "channel_values") else {}
        snapshots.append({
            "index":         i,
            "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id", ""),
            "ts":            snapshot.metadata.get("created_at", "") if hasattr(snapshot, "metadata") else "",
            "node":          cv.get("next", "unknown"),
            "audit_entries": len(cv.get("audit_log", [])),
            "approved":      cv.get("approved", None),
            "errors":        cv.get("errors", []),
        })

    return snapshots


def time_travel_example(thread_id: str, index: int) -> dict:
    """
    Replay state at a specific checkpoint index.
    Use this to inspect what the graph knew at any point during the run.

    Args:
        thread_id: The graph run thread ID
        index:     Checkpoint index (0 = start, -1 = latest)

    Returns:
        State snapshot at that checkpoint with full audit log up to that point.

    Usage:
        snapshot = time_travel_example("your-thread-uuid", index=2)
        print(snapshot["audit_log"])
    """
    with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
        config   = {"configurable": {"thread_id": thread_id}}
        history  = list(checkpointer.list(config))

    if not history:
        return {"error": f"No checkpoints found for thread_id={thread_id}"}

    # Clamp index
    index   = max(-len(history), min(index, len(history) - 1))
    snapshot = history[index]
    cv       = snapshot.channel_values if hasattr(snapshot, "channel_values") else {}

    return {
        "thread_id":     thread_id,
        "index":         index,
        "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id", ""),
        "fc_version":    FC_VERSION,
        "platform":      PLATFORM,
        "club_id":       cv.get("club_id", ""),
        "audit_log":     cv.get("audit_log", []),
        "approved":      cv.get("approved", None),
        "errors":        cv.get("errors", []),
        "scout_report":  cv.get("scout_report", {}).get("narrative", "")[:300] if cv.get("scout_report") else "",
        "analyst_report":cv.get("analyst_report", {}).get("narrative", "")[:300] if cv.get("analyst_report") else "",
    }


def export_compliance_csv(thread_id: str) -> str:
    """
    Export the full audit trail for a thread as a CSV string.
    Suitable for compliance reporting or copy-paste into spreadsheet.

    Columns:
        thread_id, checkpoint_node, ts, fc_version, platform,
        club_id, summary, approved, errors

    Usage:
        csv_text = export_compliance_csv("your-thread-uuid")
        with open("audit.csv", "w") as f:
            f.write(csv_text)
    """
    audit_log   = get_full_audit(thread_id)
    checkpoints = get_all_checkpoints(thread_id)

    # Merge approval status from latest checkpoint
    latest   = checkpoints[-1] if checkpoints else {}
    approved = latest.get("approved", None)
    errors   = latest.get("errors", [])

    output  = io.StringIO()
    writer  = csv.DictWriter(output, fieldnames=[
        "thread_id", "node", "ts", "fc_version", "platform",
        "club_id", "summary", "approved", "has_data", "errors",
    ])
    writer.writeheader()

    for entry in audit_log:
        writer.writerow({
            "thread_id":  thread_id,
            "node":       entry.get("node", ""),
            "ts":         entry.get("ts", ""),
            "fc_version": entry.get("fc_version", FC_VERSION),
            "platform":   entry.get("platform", PLATFORM),
            "club_id":    entry.get("club_id", ""),
            "summary":    entry.get("summary", ""),
            "approved":   approved,
            "has_data":   bool(entry.get("data")),
            "errors":     "; ".join(errors),
        })

    return output.getvalue()


def get_tilt_history(club_id: str, limit: int = 10) -> list[dict]:
    """
    Query all thread runs for a club and return ones that triggered
    tilt detection (3+ consecutive losses).

    Usage:
        tilts = get_tilt_history("330323")
        for t in tilts:
            print(t["thread_id"], t["form"])
    """
    # NOTE: Requires a threads metadata table or Langfuse for cross-thread queries.
    # This implementation queries Langfuse trace API for tilt-flagged runs.
    # Without Langfuse, iterate known thread_ids from your own tracking store.
    log.warning(
        "get_tilt_history requires either a thread registry or Langfuse integration. "
        "Pass known thread_ids to get_full_audit() individually for now."
    )
    return [{"note": "Implement with thread registry or Langfuse trace search."}]


def get_fair_play_violations(thread_id: str) -> list[dict]:
    """
    Extract any Fair Play violation entries from a thread's audit log.

    Returns:
        List of audit entries where summary contains 'fair play' or 'violation'.
    """
    audit = get_full_audit(thread_id)
    return [
        e for e in audit
        if any(kw in e.get("summary", "").lower()
               for kw in ("fair play", "violation", "exploit", "anomaly", "flag"))
    ]


def print_audit_summary(thread_id: str) -> None:
    """Pretty-print a one-page audit summary for a run."""
    audit  = get_full_audit(thread_id)
    checks = get_all_checkpoints(thread_id)
    latest = checks[-1] if checks else {}

    print(f"\n{'='*60}")
    print(f"  FC27 AUDIT SUMMARY")
    print(f"  Thread  : {thread_id}")
    print(f"  Club ID : {latest.get('club_id', 'N/A')}")
    print(f"  Platform: {PLATFORM}  |  FC Version: {FC_VERSION}")
    print(f"  Approved: {latest.get('approved', 'N/A')}")
    print(f"  Errors  : {latest.get('errors', [])}")
    print(f"  Entries : {len(audit)}")
    print(f"{'='*60}")
    for e in audit:
        print(f"  [{e.get('ts','')}] {e.get('node','').upper():12s} {e.get('summary','')[:80]}")
    print(f"{'='*60}\n")


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python audit_query.py <thread_id> [--csv] [--time-travel <index>]")
        sys.exit(1)

    tid = sys.argv[1]

    if "--csv" in sys.argv:
        print(export_compliance_csv(tid))
    elif "--time-travel" in sys.argv:
        idx = int(sys.argv[sys.argv.index("--time-travel") + 1])
        snap = time_travel_example(tid, idx)
        print(json.dumps(snap, indent=2))
    else:
        print_audit_summary(tid)
