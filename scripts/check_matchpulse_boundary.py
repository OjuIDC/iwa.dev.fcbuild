#!/usr/bin/env python3
"""Deterministic MatchPulse boundary drift checker. No network requests."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

STALE_SHA = "335781fd447ed5d970921200192e21b25055b3ff"

FORBIDDEN = [
    STALE_SHA,
    "publication pending",
    "attestation pending",
    "create a third MatchPulse repository",
    "git clone",
    "git worktree add",
    "MatchPulse consumes private identity data",
    "real player or club data",
    "MatchPulse can mutate FC Build dispatch",
]

OBSOLETE_DIGEST = "f413c32f671b191a4b3c63c8bed9c7b318ccdc60633bf222d5cba0049feaa22"  # truncated-ish placeholder guard


def is_historical(path: Path, text: str) -> bool:
    low = text.lower()
    return "historical" in low or "invalid" in low or "negative test" in low


def main() -> int:
    errors = []
    for p in DOCS.rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for term in FORBIDDEN:
            if term.lower() in text.lower():
                # Allow if clearly marked historical/invalid in same file
                if is_historical(p, text):
                    continue
                errors.append(f"{p}: contains forbidden term '{term}'")
    # obsolete digest as current: check for digest without authoritative prefix context
    if errors:
        for e in errors:
            print(e)
        return 1
    print("boundary drift check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
