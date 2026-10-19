# MatchPulse Live in Visual Studio

This document is the authoritative guide for opening and working with
MatchPulse Live in Visual Studio.

---

## 1. Required Visual Studio edition and workload

| Requirement | Detail |
| ----------- | ------ |
| Edition | Visual Studio 2022 (17.x) or Visual Studio 2026 (18.x), any edition |
| Workload | **Python development** (`Microsoft.VisualStudio.Workload.Python`) — *not installed in the environment that produced this solution* |
| Workload | **.NET desktop development** — optional, only required if you want the `dotnet msbuild` validation path |
| Workload | **Azure development** — optional, only required for the in-IDE Bicep authoring experience |
| Workload | **Container development** — optional, only required for in-IDE Docker support |
| Tooling | .NET SDK 8.0 or later (for `dotnet msbuild` solution parsing) |
| Tooling | Azure CLI 2.x with Bicep (`az bicep install`) for infrastructure validation |
| Tooling | Docker Desktop with Compose v2 for the container path |

### Current environment status

The machine that generated this solution has **no Visual Studio installation**
(`vswhere.exe -all -products * -format json` returns `[]`; only the Visual
Studio Installer shell is present). The available toolchain is:

| Tool | Version |
| ---- | ------- |
| Python | 3.14.8 (`py -0p` shows only 3.14 / 3.14t) |
| .NET SDK | 10.0.402 (MSBuild 18.9.15) |
| Azure CLI | 2.91.0 |
| Bicep CLI | not installed standalone (`az bicep install` installs it) |
| Docker | 29.9.1 |
| Docker Compose | v5.6.0 |
| GitHub CLI | 2.102.0 |

**VISUAL STUDIO PYTHON WORKLOAD REQUIRED**

Because the Python development workload is absent, native Visual Studio Python
project validation is **blocked**, not successful. This solution therefore uses
only solution folders and solution items — a project-system form that every
supported Visual Studio edition can open without any workload. No invalid or
invented project format is included.

The command-line path in section 5 is the **authoritative** validation path.
Python editing, IntelliSense, and debugging inside the IDE require the
Python development workload; once installed, see section 8.

---

## 2. Solution layout

```
MatchPulse.Live.sln
├── src
│   └── MatchPulse.Live.Api          # FastAPI application package
├── tests
│   └── MatchPulse.Live.Tests        # pytest suite
├── infrastructure
│   └── MatchPulse.Live.Azure        # Bicep, parameters, validation scripts
├── build
│   └── MatchPulse.Live.Build        # Dockerfile, Compose, CI, scripts
└── documentation
    └── MatchPulse.Live.Docs         # README, docs, submission material
```

Solution configurations: **Debug**, **Release**, **ChallengeDemo**
(all `Any CPU`).

---

## 3. Python interpreter setup

Python 3.12 is the reference interpreter for this project. Python 3.11+ is
supported.

```bash
# Windows (PowerShell)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / macOS
python3.12 -m venv .venv
source .venv/bin/activate
```

If Python 3.12 is not installed, any supported interpreter works. The
reference CI workflow pins 3.12.

---

## 4. Opening the solution

1. Install the **Python development** workload through the Visual Studio
   Installer (Visual Studio Installer → Modify → Individual components /
   Workloads).
2. Open `MatchPulse.Live.sln` (`File` → `Open` → `Project/Solution`).
3. If Visual Studio prompts to install missing workloads, accept the
   **Python development** workload.
4. Select the interpreter:
   `Tools` → `Options` → `Python` → `Interpreter` → **Add Local Interpreter**
   → **Existing environment** → browse to
   `.venv\Scripts\python.exe` (Windows) or `.venv/bin/python` (macOS/Linux).
5. If Visual Studio offers to configure the Python environment, choose the
   `.venv` interpreter and the `src` folder as the search path. The package
   installs itself in editable mode, so the search path is already correct.

---

## 5. Restoring dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Optional, only needed for the Azure managed-identity Foundry path:

```bash
python -m pip install -e ".[azure]"
```

---

## 6. Running the application

| Purpose | Command |
| ------- | ------- |
| FastAPI service | `uvicorn matchpulse.service:app --host 127.0.0.1 --port 8080` |
| Synthetic producer | `matchpulse-synthetic --url http://127.0.0.1:8080 --delay 0.15 --events 50` |
| Test suite | `python -m pytest -q` |
| Coverage gate | `python -m pytest --cov=matchpulse --cov-report=term-missing --cov-fail-under=80` |
| Static checks | `python -m ruff check src tests scripts` |
| Byte-compile | `python -m compileall src` |
| Preflight | `python scripts/preflight.py` |
| Solution validation | `python scripts/validate_solution.py` |
| API + WebSocket smoke | `python scripts/smoke_test.py` |
| Bicep validation | `az bicep build --file infra/main.bicep` |
| Container stack | `docker compose -f compose.challenge.yml up --build -d` |

The same launch behaviors are also available as VS Code launch configurations
in `.vscode/launch.json` (FastAPI service, synthetic producer, test suite,
preflight, Bicep validation).

---

## 7. Expected local URLs

| URL | Purpose |
| --- | ------- |
| `http://127.0.0.1:8080/health/live` | Liveness probe |
| `http://127.0.0.1:8080/health/ready` | Readiness probe (reports `foundry_mode`) |
| `http://127.0.0.1:8080/v1/profiles` | Audience profiles |
| `http://127.0.0.1:8080/demo` | Live overlay demo page |
| `ws://127.0.0.1:8080/ws/matches/{match_id}?profile_id={profile_id}` | WebSocket stream |

---

## 8. Adding native Python projects (requires the Python workload)

Once the **Python development** workload is installed, promote the two Python
components to native Visual Studio projects:

1. Right-click the `src` solution folder → **Add** → **Existing Project**.
2. Create a new **Python Application** project named `MatchPulse.Live.Api`
   in the `src` folder, then remove the default `Program.py` and add the
   existing `src/matchpulse/*.py` files as `Existing Items`.
3. Create a new **Python Unit Test** project named `MatchPulse.Live.Tests`
   in the `tests` folder and add the existing `tests/*.py` files as
   `Existing Items`.
4. Keep `CHALLENGE_SYNTHETIC_ONLY=true` in the project environment settings.
5. Leave `FOUNDRY_ENDPOINT`, `FOUNDRY_DEPLOYMENT`, and `FOUNDRY_API_KEY`
   unset for local work so the deterministic pipeline is used.

Do not add `.pyproj` files to the committed solution by hand: they cannot be
validated without the workload, and an unvalidated project file is worse than
a clean solution folder.

---

## 9. Local Foundry configuration (environment variables only)

Foundry is **optional**. Without it, the deterministic pipeline produces every
insight on its own.

| Variable | Required | Notes |
| -------- | -------- | ----- |
| `CHALLENGE_SYNTHETIC_ONLY` | Yes | Must stay `true` |
| `FOUNDRY_ENDPOINT` | For Foundry | Azure OpenAI endpoint, no trailing slash |
| `FOUNDRY_DEPLOYMENT` | For Foundry | Deployment name |
| `FOUNDRY_API_VERSION` | No | Defaults to `2024-10-21` |
| `FOUNDRY_TIMEOUT_SECONDS` | No | Defaults to `1.4` |
| `FOUNDRY_AUTH_MODE` | No | `api_key` or `managed_identity` |
| `FOUNDRY_AZURE_SCOPE` | No | Defaults to `https://cognitiveservices.azure.com/.default` |
| `FOUNDRY_API_KEY` | Only for the local API-key path | Runtime secret, never committed |

Prefer `FOUNDRY_AUTH_MODE=managed_identity` wherever an Azure managed
identity is available; it needs no stored secret.

---

## 10. No-secret rules

- Never commit `.env`, `.env.local`, publish profiles, connection strings,
  Foundry API keys, Azure client secrets, or GitHub tokens.
- `.gitignore` already excludes `.env`, `.env.*`, `.vs/`, `*.suo`, `*.user`,
  `*.log`, `.venv/`, and caches.
- `.env.example` contains **placeholders only** and is safe to commit.
- Visual Studio per-user artifacts (`.vs/`, `*.suo`, `*.user`) must never be
  committed.
- Prefer environment-variable **names** in launch configurations and workflow
  files; never environment-variable **values** that carry a secret.
- Run `python scripts/preflight.py` and the repository secret scan before
  publishing.

---

## 11. Validating the solution without Visual Studio

```bash
python scripts/validate_solution.py
dotnet msbuild MatchPulse.Live.sln -version
```

`validate_solution.py` checks that the solution parses, that every solution
item resolves to a real file, that the three configurations exist, that no
absolute or user-specific paths are present, and that no per-user artifacts
are referenced. It is also covered by the pytest suite in
`tests/test_solution_layout.py`.
