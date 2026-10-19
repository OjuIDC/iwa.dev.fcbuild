# MatchPulse Live

**MatchPulse Live** turns synthetic football events into evidence-backed live
intelligence, then renders the same moment differently for an analyst,
broadcaster, or fan in English, Spanish, or French.

It is a self-contained FastAPI service with a purposeful multi-agent pipeline,
a guarded optional Microsoft Foundry narrative editor, a Visual Studio
solution, Docker and Compose support, and an Azure Container Apps Bicep
template. It uses **no real match data**: every event is synthetic.

## What works now

- Strict synthetic event API with idempotency and ordering checks.
- Real-time match state for passes, shots, goals, tackles, pressure, and
  possession.
- Purposeful agent handoffs: interpretation → explainability →
  personalization/localization.
- Evidence attached to every insight (source event IDs, inputs, formula,
  confidence).
- Distinct analyst, fan, and broadcaster experiences.
- English, Spanish, and French deterministic storytelling.
- WebSocket overlays, automated recaps, and a polished browser demo.
- Optional Microsoft Foundry narrative rewrite with timeout, schema, and
  unsupported-number guards; the deterministic output remains the fallback.
- Managed-identity Foundry authentication for Azure, with a runtime API key as
  a local-only alternative.
- Visual Studio solution, seeded synthetic match generator, tests, CI, Docker,
  and an Azure Container Apps Bicep template.

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

python -m pytest -q
uvicorn matchpulse.service:app --host 127.0.0.1 --port 8080
```

In a second terminal, stream a match:

```bash
matchpulse-synthetic --url http://127.0.0.1:8080 --delay 0.15 --events 50
```

Then open `http://127.0.0.1:8080/demo`, select an audience, and click
**Connect live overlay**.

With Docker:

```bash
docker compose -f compose.challenge.yml up --build
```

The complete validation gate:

```bash
python -m compileall src
python -m ruff check src tests scripts
python -m pytest -q
python -m pytest --cov=matchpulse --cov-report=term-missing --cov-fail-under=80
python scripts/preflight.py
python scripts/validate_solution.py
```

`scripts/bootstrap.sh` (Linux/macOS) and `scripts/bootstrap.ps1` (Windows) run
all of the above in one step.

## Visual Studio

`MatchPulse.Live.sln` organizes the solution into `src`,
`MatchPulse.Live.Api`, `tests`, `MatchPulse.Live.Tests`, `infrastructure`,
`MatchPulse.Live.Azure`, `build`, `MatchPulse.Live.Build`, `documentation`, and
`MatchPulse.Live.Docs`, with **Debug**, **Release**, and **ChallengeDemo**
configurations.

See [`README-VISUAL-STUDIO.md`](README-VISUAL-STUDIO.md) for the exact
interpreter setup, the required workload, and the command-line fallback.

## API quick check

```bash
curl -s http://127.0.0.1:8080/health/live
curl -s http://127.0.0.1:8080/health/ready
curl -s http://127.0.0.1:8080/v1/profiles
matchpulse-synthetic --jsonl --events 5 > data/demo-match.jsonl
```

Key routes:

| Route | Purpose |
|---|---|
| `POST /v1/events` | Ingest one strict synthetic event and return personalized insights |
| `POST /v1/events/batch` | Ingest up to 500 ordered events |
| `GET /v1/matches/{id}/state` | Current explainable match state |
| `GET /v1/matches/{id}/recap?profile_id=fan-en` | Automated profile-specific recap |
| `WS /ws/matches/{id}?profile_id=fan-en` | Timed, machine-readable live overlay stream |
| `GET /health/live`, `GET /health/ready` | Liveness and readiness probes |
| `GET /demo` | Live overlay demo page |

## Microsoft Foundry mode

The live path is judgeable without cloud credentials. With no Foundry
configuration the service reports `"foundry_mode": "deterministic-fallback"`
on `/health/ready` and every insight comes from the deterministic pipeline.

To enable the optional rewrite, set environment variables (see
[`.env.example`](.env.example)):

| Variable | Required | Notes |
|---|---|---|
| `FOUNDRY_ENDPOINT` | Yes | Azure OpenAI / Foundry endpoint |
| `FOUNDRY_DEPLOYMENT` | Yes | Deployment name |
| `FOUNDRY_AUTH_MODE` | No | `managed_identity` (preferred) or `api_key` |
| `FOUNDRY_API_KEY` | Only for `api_key` | Runtime secret, never committed |
| `FOUNDRY_API_VERSION` | No | Defaults to `2024-10-21` |
| `FOUNDRY_TIMEOUT_SECONDS` | No | Defaults to `1.4` |

The model is a constrained copy editor, not the source of facts. It cannot
create match state, alter evidence, or introduce a number that is not in the
grounded input. On a timeout, invalid JSON, service error, unsupported claim,
or schema mismatch, the service uses the deterministic narrative.

## Challenge artifacts

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - end-to-end architecture,
  reliability, security, and SLOs.
- [`docs/CHALLENGE_ALIGNMENT.md`](docs/CHALLENGE_ALIGNMENT.md) - requirement
  and judging-criteria trace.
- [`docs/AZURE_DEPLOYMENT.md`](docs/AZURE_DEPLOYMENT.md) - validated
  infrastructure commands, Foundry identity, and cleanup.
- [`docs/GITHUB_PUBLICATION.md`](docs/GITHUB_PUBLICATION.md) - publication
  layout, CI, and manual configuration steps.
- [`submission/`](submission) - pitch, project description, 120-second demo
  script, testing instructions, and registration/submission checklists.
- [`infra/`](infra) - Azure Container Apps, Event Hubs, and Application
  Insights deployment template.

## Safety and scope boundary

`CHALLENGE_SYNTHETIC_ONLY=true` is mandatory. The Pydantic schema accepts
only `source="synthetic"`, and the API refuses to ingest when the challenge
safety gate is disabled. Do not include real match footage, real player data,
EA responses, club credentials, or Premier League marks in this repository or
in any demo video.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
