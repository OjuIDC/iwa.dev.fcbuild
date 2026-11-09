# Visual Studio Enterprise 2026 & Antigravity Setup Guide

- **Status**: Current
- **Audience**: Developers, Operators, Reviewers
- **Last verified commit**: `4a7d3509cbdc975e556868e7ca2e957528e869f4`
- **Source of truth**: `iwa.dev.fcbuild.sln`, `iwa.dev.fcbuild.pyproj`, `pyproject.toml`, `.gitignore`, `tests/test_fc27_repository_cleanup.py`, `tests/test_visual_studio_project.py`
- **Supersedes**: `NONE`

This guide documents how to open, navigate, test, and maintain `iwa.dev.fcbuild` in **Visual Studio Enterprise 2026** alongside **Antigravity** using the tracked solution (`iwa.dev.fcbuild.sln`) and Python project (`iwa.dev.fcbuild.pyproj`) while reusing the existing repository-root virtual environment (`.venv`).

---

## 1. Verified Visual Studio 2026 Capability Baseline

| Capability | Verified Status | Repository Policy |
|---|---|---|
| **Visual Studio Product** | Visual Studio Enterprise 2026 (`18.11.3`) | Supported IDE via `iwa.dev.fcbuild.sln` (Format Version `12.00`, Visual Studio Version `18`) |
| **MSBuild Engine** | MSBuild `18.11.1.51303` | Builds the solution and Python project through the installed Visual Studio Python targets |
| **Python Workload** | Installed (`Microsoft.VisualStudio.Workload.Python`) | Required for `.pyproj` project loading, Python Environments window, and Test Explorer pytest integration |
| **Python Test Adapter** | Available (`Pytest`) | Configured via `<TestFramework>Pytest</TestFramework>`, `<UnitTestRootDirectory>tests</UnitTestRootDirectory>`, `<UnitTestPattern>test_*.py</UnitTestPattern>` |
| **Docker Compose CLI** | Available (`docker compose` version `5.6.1`) | Authoritative CLI interface for Compose topology inspection (`docker compose config`) |
| **Visual Studio Docker Workload** | Not installed | Do **not** create or commit a `.dcproj` project; manage `Dockerfile*` and `docker-compose*.yml` as solution/project items and validate via CLI |
| **Repository Virtual Environment** | `.venv` (`Python 3.11.9`, `pip 26.3`, `pytest 8.4.2`) | Reuse existing `.venv`; never create a second virtual environment or commit `.venv/` |

### Building the Python Solution

The installed Visual Studio Python Console App template documents an empty `<Target Name="CoreCompile" />` as the opt-in that enables its MSBuild `Build` target. This target does not compile Python; use pytest and Test Explorer to execute tests. The project keeps the template's Python Tools import, which itself imports `Microsoft.Common.targets`.

Resolve the installed Visual Studio MSBuild executable and build the solution or project from the repository root:

```powershell
$programFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
$vswhere = Join-Path $programFilesX86 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsInstall = & $vswhere -latest -products '*' -version '[18.0,19.0)' -requires Microsoft.Component.MSBuild Microsoft.VisualStudio.Workload.Python -property installationPath
$msbuild = Join-Path $vsInstall 'MSBuild\Current\Bin\MSBuild.exe'
& $msbuild '.\iwa.dev.fcbuild.sln' /t:Build /v:minimal
& $msbuild '.\iwa.dev.fcbuild.pyproj' /t:Build /v:minimal
```

Both commands were verified with Visual Studio Enterprise 2026 `18.11.3` and MSBuild `18.11.1.51303`. `Clean` and `Rebuild` are also supported. Build only validates the project graph; it does not run pytest, start services, or make provider requests. If MSBuild reports `MSB4057` for missing `CoreCompile`, confirm the project still imports `Microsoft.PythonTools.targets` and retains the template-supported empty `CoreCompile` target; do not add a .NET compiler import.

---

## 2. Opening `iwa.dev.fcbuild.sln` (Replacing Folder View)

Always open the repository through the solution file rather than Folder View (`Open a local folder`):

1. Launch **Visual Studio Enterprise 2026**.
2. Select **Open a project or solution** and choose `C:\src\iwa.dev.fcbuild\iwa.dev.fcbuild.sln` (or run `devenv.exe iwa.dev.fcbuild.sln` from the repository root).
3. Solution Explorer loads:
   - The primary Python project **`iwa.dev.fcbuild`** (`iwa.dev.fcbuild.pyproj`) indexing `src\`, `tests\`, `migrations\`, `scripts\`, `config\`, `docs\`, `host_agent\`, `voice\`, and `.github\`.
   - Seven logical Solution Folders for quick architectural navigation without moving or duplicating files on disk:
     - **`Application`** (`src\`)
     - **`Tests`** (`tests\`)
     - **`Database and Migrations`** (`migrations\`, `src\db_migrate.py`, `src\db_roles.py`)
     - **`Infrastructure and Containers`** (`Dockerfile*`, `docker-compose*.yml`)
     - **`Documentation`** (`README.md`, `MIGRATION_GUIDE.md`, `SECURITY.md`, `TEST_RESULTS.md`, `THIRD_PARTY_NOTICES.md`, `docs\`)
     - **`Configuration`** (`pyproject.toml`, `requirements*.txt`, `config.example.env`, `config\`)
     - **`Automation`** (`scripts\`, `.github\workflows\`)

### Why Visual Studio Showed a Virtual Environment Prompt in Folder View

When opening `C:\src\iwa.dev.fcbuild` in **Folder View**, Visual Studio detects `pyproject.toml` and `requirements.txt` at the workspace root and displays an info bar offering to create a new virtual environment. Opening `iwa.dev.fcbuild.sln` loads `iwa.dev.fcbuild.pyproj`, which sets:

- `<Interpreter Include=".venv\">` with relative paths `Scripts\python.exe` and `Scripts\pythonw.exe`
- `<InterpreterId>MSBuild|env|$(MSBuildProjectFullPath)</InterpreterId>`
- `<SuppressEnvironmentCreationPrompt>True</SuppressEnvironmentCreationPrompt>`

This binds the project directly to the existing repository-root `.venv` and suppresses the environment creation prompt without storing machine-specific absolute paths or usernames in tracked files.

---

## 3. Selecting and Verifying the Existing `.venv`

The repository-root `.venv` must be reused in place. Never create a second virtual environment and never delete the existing `.venv` during routine setup.

### Command-Line Verification

Verify the existing `.venv` from PowerShell at the repository root:

```powershell
Test-Path '.\.venv\Scripts\python.exe'
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip --version
.\.venv\Scripts\python.exe -m pytest --version
```

### One-Time Visual Studio Environment Selection Procedure

If Visual Studio has not yet cached the active interpreter in your local (gitignored) `.vs/` state, complete these exact one-time steps:

1. Open `C:\src\iwa.dev.fcbuild\iwa.dev.fcbuild.sln`.
2. Open `View` -> `Other Windows` -> `Python Environments`.
3. Select the `iwa.dev.fcbuild` project.
4. Add or select the existing environment: `C:\src\iwa.dev.fcbuild\.venv\Scripts\python.exe`.
5. Mark it as the active project environment.
6. Dismiss or disable any prompt to create a new virtual environment (`SuppressEnvironmentCreationPrompt` is already enabled in `iwa.dev.fcbuild.pyproj`).
7. Refresh **Test Explorer** (`Test` -> `Test Explorer`).
8. Confirm pytest tests are discovered from `tests/`.

> **Hygiene Rule**: Do not commit any `.vs/` directory, `.user` file, `.suo` file, `.userosscache` file, `.sln.docstates` file, or machine-specific interpreter path. All IDE session state stays in gitignored local files.

---

## 4. Understanding the Blue Lock Glyph in Solution Explorer

In Visual Studio Solution Explorer, tracked files display a **blue padlock icon** next to the file name:

- **Classification**: `SOURCE_CONTROL_TRACKED_UNMODIFIED`
- **Meaning**: The file is tracked by Git and currently has no uncommitted local modifications in the working tree.
- **Filesystem Permissions**: Tracked files are **not** read-only (`IsReadOnly=False`) and are not locked by Windows or any background process.
- **Behavior When Editing**: As soon as you modify and save a tracked file in Visual Studio or Antigravity, the blue lock icon automatically changes to a **red checkmark** indicating a pending Git modification. Reverting the change restores the blue lock icon.

---

## 5. Test Explorer and Pytest Execution

`iwa.dev.fcbuild.pyproj` configures Visual Studio's Python Test Adapter to use `pytest` against the `tests` directory (`test_*.py`):

- **CLI Discovery Verification**:
  ```powershell
  .\.venv\Scripts\python.exe -m pytest --collect-only -q
  ```
- **Offline Fast Suites (No PostgreSQL Container Required)**:
  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_fc27_repository_cleanup.py tests/test_documentation_consistency.py tests/test_packaging.py -q
  ```
- **PostgreSQL-Backed Integration Tests**:
  Tests that exercise PostgreSQL 16 migrations (`0001`–`0012`), role/grant enforcement, LangGraph `PostgresSaver`, and transactional FC 27 persistence require a disposable loopback PostgreSQL 16 test container (`TEST_POSTGRES_URI` / `TEST_POSTGRES_ADMIN_URI` on `127.0.0.1:55432`) as documented in [`DEVELOPMENT.md`](DEVELOPMENT.md) and [`TESTING.md`](TESTING.md). Do **not** point pytest at any operational database or unrelated local container.

---

## 6. Safe vs. Prohibited Startup and Command Actions

To preserve the platform's fail-closed operational posture, default project startup in `iwa.dev.fcbuild.pyproj` is bound to the offline documentation validator (`scripts\check_documentation_consistency.py --check --strict`) and **never** starts any network service, bot, or polling worker.

### Safe Developer Actions (Allowed)

- Running offline pytest suites or individual pytest files in Test Explorer or PowerShell
- Static documentation validation:
  ```powershell
  .\.venv\Scripts\python.exe scripts/check_documentation_consistency.py --check --strict
  ```
- Sibling repository boundary verification:
  ```powershell
  .\.venv\Scripts\python.exe scripts/check_matchpulse_boundary.py
  ```
- Read-only status inspection of disabled-by-default workers and bots:
  ```powershell
  .\.venv\Scripts\python.exe scripts/fc27_discord_bot.py --status
  .\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status
  ```
- Offline Docker Compose configuration rendering:
  ```powershell
  docker compose config
  ```

### Prohibited Default Startup Actions (Never Configure as IDE Launch Profiles)

Do **not** configure Visual Studio startup properties, launch profiles, `.runsettings`, or MSBuild targets to execute:

- `python scripts/fc27_discord_bot.py --run` (never connect to Discord Gateway or send Discord messages from IDE startup)
- `python scripts/fc27_polling_worker.py --run` (never trigger live provider polling or background loops from IDE startup)
- Live EA / community provider traffic or synthetic poll-state generation
- `docker compose up` or starting operational containers by default
- Operational database migrations against live/durable state
- Local or remote LLM inference execution

---

## 7. Git Integration and Command-Line Docker Compose Usage

- **Visual Studio Git Changes Window**: Use `View` -> `Git Changes` (or PowerShell `git status --short` / `git diff`) to inspect modifications, switch branches, and stage explicit files. Never use `git add .` or `git add -A`, and never stage `.vs/`, `.venv/`, `secrets/`, `.local-data/`, or `.local-artifacts/`.
- **Command-Line Docker Compose (`5.6.1`)**: Because the Visual Studio Docker workload is intentionally omitted (`no .dcproj`), run all Compose validation commands from the integrated PowerShell terminal (`View` -> `Terminal`):
  ```powershell
  docker compose -f docker-compose.yml config
  docker compose -f docker-compose.yml --profile bootstrap config
  docker compose -f docker-compose.yml -f docker-compose.community-worker.yml --profile fc27-polling config
  docker compose -f docker-compose.yml -f docker-compose.discord.yml --profile fc27-discord config
  ```

---

## 8. Antigravity Compatibility

`iwa.dev.fcbuild.sln` and `iwa.dev.fcbuild.pyproj` are designed for zero-conflict coexistence with **Antigravity**:

- **Workspace Root**: Open `C:\src\iwa.dev.fcbuild` as the workspace root in Antigravity while keeping `C:\src\iwa.dev.fcbuild\iwa.dev.fcbuild.sln` open in Visual Studio 2026.
- **Shared Repository Tree**: Both tools inspect the exact same in-place directories (`src/`, `tests/`, `migrations/`, `scripts/`, `docs/`, `config/`, `host_agent/`, `voice/`, and root `Dockerfile*` / `docker-compose*.yml`) without file duplication.
- **Shared `.venv` Execution**: Both Visual Studio and Antigravity invoke `.\.venv\Scripts\python.exe` and `.\.venv\Scripts\pytest.exe`.
- **Clean Git State**: Because `.vs/`, `*.suo`, `*.user`, `*.userosscache`, `*.sln.docstates`, `.venv/`, `secrets/`, `.local-data/`, and `.local-artifacts/` are ignored in `.gitignore`, Visual Studio IDE activity never pollutes Antigravity's `git status` or exposes secrets.

---

## 9. Maintenance: Resetting `.vs` Safely and Rebuilding `.venv`

### Resetting `.vs` Cache Safely

If Visual Studio Solution Explorer or Test Explorer caches stale state after switching branches:

1. Close Visual Studio Enterprise 2026 (`devenv.exe`).
2. Remove only the gitignored `.vs` cache directory from PowerShell:
   ```powershell
   Remove-Item -LiteralPath '.\.vs' -Recurse -Force -ErrorAction SilentlyContinue
   ```
3. Reopen `iwa.dev.fcbuild.sln` and let Test Explorer rediscover `tests/`.

### Rebuilding `.venv` Only When Necessary

Do not delete or recreate `.venv` during normal development. If a Python upgrade or corrupted package installation explicitly requires rebuilding `.venv` in the future, follow the environment bootstrap commands in [`DEVELOPMENT.md`](DEVELOPMENT.md) Section 1 (`python -m venv .venv` followed by `pip install -r requirements.txt -r requirements-dev.txt`).

---

## 10. Source-of-Truth Runbooks and Related Documentation

This setup guide does not duplicate operational or security policies. Refer to the authoritative repository documents below:

- **System Architecture & Trust Zones**: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **Local Development & Static Gates**: [`DEVELOPMENT.md`](DEVELOPMENT.md)
- **Test Suites & Isolation Matrix**: [`TESTING.md`](TESTING.md) and [`FC27_TEST_ISOLATION_MATRIX.md`](FC27_TEST_ISOLATION_MATRIX.md)
- **Day-2 Operations & Secret Rotation**: [`OPERATIONS.md`](OPERATIONS.md)
- **Security Boundaries & Authority Model**: [`SECURITY_BOUNDARIES.md`](SECURITY_BOUNDARIES.md)
- **Deployment Profiles & Container Hardening**: [`DEPLOYMENT_AND_SECURITY.md`](DEPLOYMENT_AND_SECURITY.md) *(note: durable-control-plane Compose hardening and deployment/security documentation reconciliation are tracked separately on branch `ops/fc27-akole-durable-control-plane`)*
