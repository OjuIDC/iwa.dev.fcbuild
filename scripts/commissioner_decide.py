#!/usr/bin/env python3
"""Commissioner decision client. Runs on the Commissioner's own workstation.

Secrets are read from the environment or from *_FILE paths and never printed:
  GRAPH_SERVICE_TOKEN       read the thread to show its lead packages
  GRAPH_COMMISSIONER_TOKEN  authenticate as the Commissioner
  APPROVAL_SIGNING_SECRET   sign the decision (HMAC-SHA256)

Commands:
  hash-token                                   print SHA-256 of a token read from stdin/prompt
  show   <thread_id>                           list approvable candidates
  approve <thread_id> <owner> <selection>      approve one exact candidate
  reject  <thread_id>                          reject the whole thread

Example:
  python scripts/commissioner_decide.py approve fc27-330323-run-20261001-001 coach best --notes "reviewed"
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.approval import sign  # noqa: E402
from src.settings import read_secret  # noqa: E402

SELECTIONS = ("best", "best_runner_up", "middle", "middle_runner_up", "lowest", "ignore")


def base_url() -> str:
    return os.getenv("FC27_GRAPH_URL", "http://127.0.0.1:8001").rstrip("/")


def get_thread(thread_id: str) -> dict:
    r = httpx.get(f"{base_url()}/threads/{thread_id}", headers={"X-API-Key": read_secret("GRAPH_SERVICE_TOKEN")}, timeout=30)
    r.raise_for_status()
    return r.json()


def submit(thread_id: str, intent: dict, assume_yes: bool) -> int:
    commissioner = {"X-API-Key": read_secret("GRAPH_COMMISSIONER_TOKEN")}
    ch = httpx.post(f"{base_url()}/threads/{thread_id}/decision-challenge", headers=commissioner, json=intent, timeout=30)
    if ch.status_code != 200:
        print(f"challenge refused: {ch.status_code} {ch.json().get('detail')}", file=sys.stderr)
        return 1
    payload = ch.json()
    print("You are about to sign:")
    for key in ("thread_id", "decision", "owner", "selection", "candidate_id", "player_id", "notes", "package_version", "expires_at"):
        print(f"  {key:16} {payload.get(key)}")
    if not assume_yes and input("Type the decision word to confirm: ").strip().upper() != payload["decision"]:
        print("not confirmed; nothing submitted")
        return 1
    payload["signature"] = sign(payload, read_secret("APPROVAL_SIGNING_SECRET", min_length=32))
    payload["proof_type"] = "cli_hmac"
    r = httpx.post(f"{base_url()}/threads/{thread_id}/decision", headers=commissioner, json=payload, timeout=30)
    if r.status_code != 200:
        print(f"decision refused: {r.status_code} {r.json().get('detail')}", file=sys.stderr)
        return 1
    print(f"recorded: {r.json()['status']} by {r.json().get('approved_by')}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash-token")
    s = sub.add_parser("show"); s.add_argument("thread_id")
    a = sub.add_parser("approve"); a.add_argument("thread_id"); a.add_argument("owner"); a.add_argument("selection", choices=SELECTIONS)
    rj = sub.add_parser("reject"); rj.add_argument("thread_id")
    for x in (a, rj):
        x.add_argument("--notes", default="")
        x.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    args = p.parse_args(argv)

    if args.cmd == "hash-token":
        token = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Commissioner token: ")
        print(hashlib.sha256(token.encode()).hexdigest())
        return 0
    if args.cmd == "show":
        state = get_thread(args.thread_id)
        print(f"{state['thread_id']} status={state['status']} package_version={state.get('package_version')}")
        for owner, pkg in (state.get("lead_packages") or {}).items():
            for sel in SELECTIONS:
                c = pkg.get(sel)
                if c:
                    print(f"  {owner:9} {sel:17} player={c['player_id']:20} score={c['score']:6} candidate={c['candidate_id']}")
        return 0
    if args.cmd == "reject":
        return submit(args.thread_id, {"decision": "REJECT", "notes": args.notes}, args.yes)
    state = get_thread(args.thread_id)
    candidate = ((state.get("lead_packages") or {}).get(args.owner) or {}).get(args.selection)
    if not candidate:
        print("no such candidate in this thread", file=sys.stderr)
        return 1
    intent = {"decision": "APPROVE", "owner": args.owner, "selection": args.selection,
              "candidate_id": candidate["candidate_id"], "player_id": candidate["player_id"], "notes": args.notes}
    return submit(args.thread_id, intent, args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
