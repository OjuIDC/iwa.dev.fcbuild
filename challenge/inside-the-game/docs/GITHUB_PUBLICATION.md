# GitHub publication

This repository is published as a standalone public challenge repository. It is
deliberately separate from the private program repository: no private history,
no parent configuration, and no unrelated files are included.

---

## Published location

| Property | Value |
| -------- | ----- |
| Owner | `ofayese` |
| Repository | `matchpulse-live` |
| URL | <https://github.com/ofayese/matchpulse-live> |
| Visibility | Public |
| Default branch | `main` |
| License | Apache-2.0 (`LICENSE`, attribution in `NOTICE`) |

---

## Repository layout

```text
matchpulse-live/
├── MatchPulse.Live.sln      # Visual Studio solution (solution folders + items)
├── README.md
├── README-VISUAL-STUDIO.md
├── LICENSE  NOTICE  SECURITY.md  CONTRIBUTING.md  CODE_OF_CONDUCT.md
├── pyproject.toml  Dockerfile  compose.challenge.yml
├── .gitignore  .dockerignore  .env.example
├── src/matchpulse/          # FastAPI application package
├── tests/                   # pytest suite
├── scripts/                 # preflight, solution validation, smoke test, bootstrap
├── data/demo-match.jsonl    # seeded synthetic match
├── docs/                    # architecture, challenge alignment, deployment, publication
├── submission/              # pitch, project description, demo script, checklist
├── infra/                   # Bicep template, parameters example, validation scripts
└── .github/workflows/       # challenge-ci.yml, azure-deploy.yml
```

The repository contains exactly one root commit. It does not contain the
parent program's history.

---

## Continuous integration

`.github/workflows/challenge-ci.yml` runs on every pull request targeting
`main` and on every push to `main`.

| Control | Value |
| ------- | ----- |
| Permissions | `permissions: contents: read` |
| Interpreter | Python 3.12 |
| Dependency install | `python -m pip install -e '.[dev]'` |
| Byte-compile | `python -m compileall src` |
| Lint | `python -m ruff check src tests scripts` |
| Tests | `python -m pytest -q` |
| Coverage | `python -m pytest --cov=matchpulse --cov-report=term-missing --cov-fail-under=80` |
| Preflight | `python scripts/preflight.py` |
| Solution validation | `python scripts/validate_solution.py` |
| Container build | `docker build -t matchpulse-live:ci .` |
| Compose validation | `docker compose -f compose.challenge.yml config` |

The workflow stores no plaintext secret and uses no secret at all.

`.github/workflows/azure-deploy.yml` validates the Bicep template on pull
requests that touch `infra/`. Its deployment job is inert (`if: false`) until
the Azure configuration described in `docs/AZURE_DEPLOYMENT.md` exists.

---

## Branch protection

**Branch protection is not enabled by this repository's contents.** It must be
configured manually in the GitHub repository settings under
`Settings` → `Branches` → `Branch protection rules`.

Recommended rules for `main`:

- Require a pull request before merging.
- Require status checks to pass: `Validate (Python 3.12)` and `Container build`.
- Require branches to be up to date before merging.
- Do not allow force pushes.
- Do not allow deletions.

This has **not** been verified through the GitHub API and is therefore
recorded as a manual step rather than a completed action.

---

## Required environment variables

The challenge runs with **no** environment variables. The variables below are
optional and are only needed for the optional Microsoft Foundry enrichment.

| Variable | Purpose | Where it belongs |
| -------- | ------- | ---------------- |
| `CHALLENGE_SYNTHETIC_ONLY` | Must remain `true`; ingestion is refused otherwise | Runtime environment |
| `FOUNDRY_ENDPOINT` | Azure OpenAI / Foundry endpoint | Runtime environment or GitHub environment variable |
| `FOUNDRY_DEPLOYMENT` | Deployed model name | Runtime environment or GitHub environment variable |
| `FOUNDRY_API_VERSION` | API version, default `2024-10-21` | Runtime environment |
| `FOUNDRY_TIMEOUT_SECONDS` | Timeout before the deterministic fallback, default `1.4` | Runtime environment |
| `FOUNDRY_AUTH_MODE` | `managed_identity` or `api_key` | Runtime environment |
| `FOUNDRY_AZURE_SCOPE` | Token scope for managed identity | Runtime environment |
| `FOUNDRY_API_KEY` | Runtime secret for the local API-key path only | **Never** committed |

For the optional Azure deployment workflow, the GitHub `azure-production`
environment additionally needs `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `ACR_NAME` (or an approved
GHCR image reference), `CONTAINER_APP_NAME`, `FOUNDRY_ENDPOINT`, and
`FOUNDRY_DEPLOYMENT`. These are stored as **GitHub environment variables**, not
as secrets in the workflow file, and the workflow authenticates with OIDC
rather than a client secret.

---

## No-secret policy

- `.env`, `.env.*`, publish profiles, connection strings, API keys, Azure
  client secrets, GitHub tokens, and private keys are never committed.
- `.gitignore` excludes `.env`, `.env.*` (with `!.env.example`), `.vs/`,
  `*.suo`, `*.user`, `.venv/`, caches, and logs.
- `.env.example` contains placeholders only.
- `scripts/preflight.py` scans every tracked file for GitHub tokens, Azure
  storage keys, private keys, Azure client secrets, assigned Foundry keys,
  publish-profile credentials, user-profile absolute paths, and private
  repository references, and fails the build on a match.
- GitHub's own secret scanning and push protection apply on the remote
  repository.

---

## Reproducing the published state

```bash
git clone https://github.com/ofayese/matchpulse-live.git
cd matchpulse-live
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python -m pytest -q
python scripts/preflight.py
```
