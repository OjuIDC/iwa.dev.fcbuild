# Testing instructions for judges

## Hosted demo

**URL:** `<ADD_AZURE_CONTAINER_APP_URL>/demo`

No login should be required. Keep the deployment available through the end of judging.

1. Open `/demo`.
2. Select `fan-en`, `analyst-en`, `broadcaster-es`, or `fan-fr`.
3. Click **Connect live overlay**.
4. Start the synthetic feed from a terminal, or provide an on-page/start button in the final hosted build.
5. Compare the same insight ID and evidence IDs across profiles.
6. Open `/v1/matches/demo-2026-10-27/recap?profile_id=broadcaster-es` after the feed ends.

## Local build

Requirements: Python 3.11+ or Docker.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
uvicorn matchpulse.service:app --host 127.0.0.1 --port 8080
```

In a second terminal:

```bash
matchpulse-synthetic --delay .15 --events 50
```

Open `http://127.0.0.1:8080/demo`.

## Expected behavior

- `/health/ready` returns `synthetic_only: true`.
- Valid ordered events return HTTP 202.
- Duplicate event IDs are acknowledged without duplicating state.
- Out-of-order events return HTTP 409.
- Any non-synthetic source value fails schema validation.
- Each insight includes source event IDs, calculation inputs, a formula, and confidence.
- English, Spanish, and French variants retain the same evidence.
- With no Foundry credentials, `foundry_mode` reports `deterministic-fallback` and the full demo still works.

## Known MVP limits

- State is in process memory; restarting clears the demo match.
- Multi-replica WebSocket fan-out needs a managed backplane before production scale.
- Event Hubs is provisioned as a scale-out seam; the submitted HTTP MVP does not claim an active Event Hubs consumer unless the entrant adds and demonstrates one.
- Languages beyond en/es/fr require reviewed templates or a translation adapter.
