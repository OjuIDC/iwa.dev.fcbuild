#!/usr/bin/env bash
set -euo pipefail
python -m uvicorn matchpulse.service:app --host 127.0.0.1 --port 8080 &
server=$!
trap 'kill "$server" 2>/dev/null || true' EXIT
for _ in {1..40}; do
  curl -fsS http://127.0.0.1:8080/health/ready >/dev/null && break
  sleep .25
done
python -m matchpulse.synthetic --delay .15 --events 50
wait "$server"
