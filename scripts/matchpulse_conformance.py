#!/usr/bin/env python3
"""Deterministic synthetic-data conformance harness for MatchPulse (external service)."""
import argparse
import json
import os
import sys
import urllib.parse

DEFAULT_BASE = "http://127.0.0.1:8080"
GENERATOR_VERSION = "1.0.0"


def is_loopback(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.getenv("MATCHPULSE_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--allow-non-loopback", action="store_true")
    args = ap.parse_args()

    if not is_loopback(args.base_url) and not args.allow_non_loopback:
        print("refusing non-loopback URL without --allow-non-loopback", file=sys.stderr)
        return 2

    # Minimal scenario placeholder; real HTTP calls added per contract discovery.
    summary = {
        "generator_version": GENERATOR_VERSION,
        "base_url": args.base_url,
        "scenarios": 0,
        "passed": 0,
        "failed": 0,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
