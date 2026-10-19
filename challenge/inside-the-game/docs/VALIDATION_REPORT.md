# Validation report - 2026-10-19

## Verified in this workspace

- Python source, tests, and preflight compiled successfully on Python 3.14.8.
  Python 3.12 is the reference interpreter (CI pins 3.12 and the container image
  uses `python:3.12-slim`); 3.12 is not installed on the validation machine.
- Test suite: **44 passed, 0 failed**. The original bundle baseline was 9 passed,
  0 failed; no test was skipped or weakened to accommodate the Visual Studio
  conversion.
- Coverage: **88.13%** total, above the 80% gate.
- Static analysis: `ruff check src tests scripts` - **All checks passed**.
- Submission preflight: required files present, source compiles, no secrets or
  forbidden files, synthetic-only gate documented, solution configurations
  present.
- Visual Studio solution: `MatchPulse.Live.sln` parses under MSBuild 18.9.15
  (Debug, Release, and ChallengeDemo). 55 solution items, all resolving to real
  files, all repository-relative.
- Live smoke test: 52 synthetic events accepted; final state `finished`.
- API and WebSocket smoke test: **PASS (0 failed checks)** against a local
  uvicorn instance and against the Compose container.
- Localization and personalization: the same evidence produced four distinct
  outputs across English and French, and across fan, analyst, and broadcaster
  personas, all sharing one evidence event id.
- Foundry guard: valid rewrite applied; timeout, invalid JSON, service error,
  schema mismatch, and unsupported-number cases all fall back to the
  deterministic narrative; no secret value appeared in a warning, log record, or
  response.
- Docker image `matchpulse-live:local` built and ran: non-root uid 10001,
  read-only root filesystem, `/tmp` bounded at 32M, all Linux capabilities
  dropped, `no-new-privileges`, restart count 0, health `healthy`.
- Bicep: `az bicep build` and `az bicep lint` succeeded. The compiled template
  contains no hardcoded subscription id, no hardcoded tenant id, and no secret
  literal.

## Not verified here

- Native Visual Studio Python project validation. No Visual Studio installation
  exists on this machine, so the Python development workload is absent. The
  solution uses solution folders and solution items, which every Visual Studio
  edition can open. **VISUAL STUDIO PYTHON WORKLOAD REQUIRED.**
- Azure resource deployment. An Azure account and subscription are available,
  but no resource group, region, resource name set, image source, or Foundry
  deployment has been approved, so nothing was deployed.
- A live Microsoft Foundry call. No endpoint, deployment, or credential was
  supplied, so only the guarded fallback path was exercised.
- Event Hubs consumption. Event Hubs is provisioned by the Bicep template as a
  scale-out seam only; no consumer is implemented or tested.
- Public WebSocket behavior over Azure ingress.
- GitHub Actions on the remote repository, hosted demo, and public video.

## Reproduction

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m compileall src
python -m ruff check src tests scripts
python -m pytest -q
python -m pytest --cov=matchpulse --cov-report=term-missing --cov-fail-under=80
python scripts/preflight.py
python scripts/validate_solution.py
docker compose -f compose.challenge.yml up --build -d
python scripts/smoke_test.py --url http://127.0.0.1:8080
```

For final submission, run the GitHub workflow and Azure checks in
`submission/REGISTRATION_AND_SUBMISSION_CHECKLIST.md`, then update this report
with links and evidence.
