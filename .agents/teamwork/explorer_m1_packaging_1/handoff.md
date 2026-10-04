# Handoff Report: Phase 2 Milestone 1 Packaging & Scope Protection Design

**Author**: `explorer_m1_packaging_1`  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_packaging_1`  
**Date**: 2026-10-04T02:53:30Z  
**Target Milestone**: Phase 2 Milestone 1 (Packaging, Dependencies, and Scope Guards)  

---

## 1. Observation

### 1.1 Existing Packaging Configuration
1. **`pyproject.toml`** (`c:\Users\iwadev\dev\iwa.dev.fcbuild\pyproject.toml` lines 12–29):
   ```toml
   dependencies = [
       "fastapi>=0.115,<1",
       "uvicorn[standard]>=0.30,<1",
       "httpx>=0.27,<1",
       "redis>=5,<7",
       "pydantic>=2.8,<3",
       "aiohttp>=3.10,<4",
       "websockets>=15,<17",
       "numpy>=1.26,<3",
       "opencv-python-headless>=4.10,<5",
       "pytesseract>=0.3.13,<1",
       "Pillow>=12.3,<13",
       "psycopg[binary]>=3.2,<4",
       "psycopg-pool>=3.2,<4",
       "python-multipart>=0.0.12,<1",
       "langgraph==1.2.12",
       "langgraph-checkpoint-postgres==3.1.2",
   ]
   ```
2. **`requirements.txt`** (`c:\Users\iwadev\dev\iwa.dev.fcbuild\requirements.txt` lines 1–18):
   ```text
   # Runtime dependencies for the graph/watcher/ws/rundown/rtsp images.
   # Kept in sync with [project].dependencies in pyproject.toml (tests/test_packaging.py enforces it).
   fastapi>=0.115,<1
   uvicorn[standard]>=0.30,<1
   httpx>=0.27,<1
   redis>=5,<7
   pydantic>=2.8,<3
   aiohttp>=3.10,<4
   websockets>=15,<17
   numpy>=1.26,<3
   opencv-python-headless>=4.10,<5
   pytesseract>=0.3.13,<1
   Pillow>=12.3,<13
   psycopg[binary]>=3.2,<4
   psycopg-pool>=3.2,<4
   python-multipart>=0.0.12,<1
   langgraph==1.2.12
   langgraph-checkpoint-postgres==3.1.2
   ```
3. **`tests/test_packaging.py`** (`c:\Users\iwadev\dev\iwa.dev.fcbuild\tests\test_packaging.py` lines 7–26):
   - `_reqs("requirements.txt")` parses lines, strips whitespace, and skips `#` and `-r`.
   - `test_pyproject_matches_runtime_requirements()` enforces:
     `assert project["dependencies"] == _reqs("requirements.txt")`
     `assert project["requires-python"] == ">=3.11"`
   - `test_test_tools_are_not_runtime_dependencies()` enforces that `"pytest"` and `"pyyaml"` are not in `requirements.txt`.
   - `test_langgraph_is_pinned_exactly()` enforces exact pins for `langgraph` and `langgraph-checkpoint-postgres`.

### 1.2 Existing Scope Protection Tests
1. **`tests/test_phase1_scope.py`** (`c:\Users\iwadev\dev\iwa.dev.fcbuild\tests\test_phase1_scope.py` lines 52–63):
   ```python
   def test_no_phase2plus_code():
       """C-8, AC-58: Ensure src/dashboard, src/identity, and Phase 2+ dispatch files do not exist."""
       src_dir = ROOT / "src"
       assert src_dir.is_dir(), "src directory must exist"

       # 1. Ensure src/dashboard and src/identity do not exist
       for forbidden_mod in ("dashboard", "identity"):
           dir_path = src_dir / forbidden_mod
           file_path = src_dir / f"{forbidden_mod}.py"
           assert not dir_path.exists(), f"Phase 2+ directory must not exist in Phase 1: {dir_path.relative_to(ROOT)}"
           assert not file_path.exists(), f"Phase 2+ file must not exist in Phase 1: {file_path.relative_to(ROOT)}"
   ```
   *Direct impact*: If `src/identity/` or `src/identity.py` is created in Phase 2 Milestone 1, line 61 fails immediately.
2. **`tests/test_phase1_scope.py`** (lines 86–125):
   ```python
   def test_no_webauthn_imports():
       """C-8, AC-58: Ensure no 'import webauthn', 'from webauthn', or 'py_webauthn' in any file under src/."""
       ...
       # AST and text regex checks over all *.py in src/
   ```
   *Direct impact*: If `src/identity/webauthn.py` or any file under `src/` imports `webauthn`, lines 105 and 118–122 fail immediately.
3. **Dispatch prefix enforcement** (lines 18–29):
   ```python
   FORBIDDEN_DISPATCH_PREFIXES = (
       "worker",
       "relay",
       "tracker",
       "tracking",
       "ingest",
       "assess",
       "baseline",
       "analytics",
       "personalize",
       "agent",
   )
   ```
   *Direct impact*: This array already forbids `worker` and `relay` under `src/dispatch/`.
4. **Environment and docker-compose settings**:
   - `docker-compose.yml` line 202: `DISPATCH_ENABLED: "false"`.
   - `docs/DISPATCH_ACTIVATION.md` line 12: `DISPATCH_ENABLED=false` at all times.
   - `tests/e2e/test_tier1_features.py` lines 592 & 597:
     `assert os.getenv("DISPATCH_ENABLED", "false").lower() == "false"`
     `assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false"`

---

## 2. Logic Chain

1. **Packaging List Equality**:
   - `test_packaging.py::test_pyproject_matches_runtime_requirements` uses exact Python list equality (`assert project["dependencies"] == _reqs("requirements.txt")`).
   - Therefore, the four new dependencies (`webauthn>=2.5,<3`, `pyotp>=2.9,<3`, `cryptography>=44,<45`, `argon2-cffi>=23,<25`) must be appended in the **identical order** to both `pyproject.toml` and `requirements.txt`.
   - None of the four dependencies conflict with existing pinned dependencies (`langgraph==1.2.12`, `langgraph-checkpoint-postgres==3.1.2`), nor do they contain substrings `"pytest"` or `"pyyaml"`.
   - Thus, all assertions in `tests/test_packaging.py` remain satisfied.

2. **Permitting Phase 2 Scope**:
   - In `tests/test_phase1_scope.py::test_no_phase2plus_code`, `"identity"` must be removed from `forbidden_mod`. `"dashboard"` (Phase 4) and Phase 3 player-agent modules (`player_agents`, `player_agent`) remain forbidden.
   - In `tests/test_phase1_scope.py::test_no_webauthn_imports`, `src/identity/` and `src/graph_api.py` must be explicitly authorized to import `webauthn`. Non-identity modules (specifically `src/dispatch/`, `src/ocr_parsing.py`, `src/roles.py`, etc.) must still be prevented from importing `webauthn`.

3. **Enforcing Phase 3+ Boundary Protections**:
   - Phase 3 introduces the dispatch worker, outbox relay, and player agents. In Phase 2, these must remain strictly dormant.
   - To guarantee this boundary programmatically, `tests/test_phase1_scope.py` must add four explicit boundary guard tests:
     a. `test_no_dispatch_worker`: Asserts no worker files exist under `src/dispatch/` or `src/`, no `dispatch-worker` service exists in `docker-compose.yml`, and `DISPATCH_WORKER_ENABLED` is false.
     b. `test_no_outbox_relay`: Asserts no relay files exist under `src/dispatch/` or `src/`, no `outbox-relay` service exists in `docker-compose.yml`, and `G-OUTBOX` remains unsatisfied in `docs/DISPATCH_ACTIVATION.md`.
     c. `test_dispatch_enabled_is_false`: Asserts `os.getenv("DISPATCH_ENABLED", "false").lower() == "false"` and `docker-compose.yml` sets `DISPATCH_ENABLED: "false"`.
     d. `test_dispatch_worker_enabled_is_false`: Asserts `os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false"` and `docker-compose.yml` does not enable any dispatch worker.

4. **Preserving Historical Phase 1 Regression Signatures**:
   - Retaining the existing function names (`test_no_phase2plus_code`, `test_no_webauthn_imports`, `test_no_tracking_writes_outside_tests`, `test_docs_state_disabled_and_gates`, `test_activation_doc_forbids_phase1_enablement`) ensures that test harnesses targeting these exact node IDs pass without regression.
   - Creating an additional or aliased `tests/test_phase2_scope.py` ensures forward compatibility for Phase 2 test targets.

---

## 3. Caveats

1. **Docker Compose Execution**: Static YAML/regex inspection of `docker-compose.yml` is used so that tests do not require the Docker daemon or Docker CLI to be running during unit/static test execution.
2. **Pip Install in CI**: Adding dependencies to `pyproject.toml` and `requirements.txt` specifies the packaging contract; installing them into the active virtual environment requires executing `pip install -r requirements.txt` or `pip install -e .` in the environment runner.
3. No other caveats.

---

## 4. Conclusion & Proposed Code Edits

### 4.1 Changes to `pyproject.toml`
**File**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\pyproject.toml`  
**Target lines**: 28–30  

```diff
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -27,6 +27,10 @@ dependencies = [
     "python-multipart>=0.0.12,<1",
     "langgraph==1.2.12",
     "langgraph-checkpoint-postgres==3.1.2",
+    "webauthn>=2.5,<3",
+    "pyotp>=2.9,<3",
+    "cryptography>=44,<45",
+    "argon2-cffi>=23,<25",
 ]
 
 [project.optional-dependencies]
```

### 4.2 Changes to `requirements.txt`
**File**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\requirements.txt`  
**Target lines**: 17–19  

```diff
--- a/requirements.txt
+++ b/requirements.txt
@@ -16,3 +16,7 @@ psycopg-pool>=3.2,<4
 python-multipart>=0.0.12,<1
 langgraph==1.2.12
 langgraph-checkpoint-postgres==3.1.2
+webauthn>=2.5,<3
+pyotp>=2.9,<3
+cryptography>=44,<45
+argon2-cffi>=23,<25
```

### 4.3 Changes to `tests/test_phase1_scope.py`
**File**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\tests\test_phase1_scope.py`  
**Full drop-in replacement**:

```python
"""Phase 1/2 Scope and Boundary Protection Tests (AC-58, C-8, C-2, C-3).

Enforces:
1. Absence of Phase 3+ runtime code (dashboard, player-agents, dispatch workers, relays, trackers).
2. Permitted scoping of WebAuthn imports to src/identity/ and src/graph_api.py (zero WebAuthn in dispatch).
3. Strict dispatch dormancy: DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false at all times.
4. Absence of mutations/writes to companion tracking tables outside tests.
5. Correctness of docs/DISPATCH_ACTIVATION.md (all 6 gates, exact disabled message, and 4 mandatory statements).
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# C-8: Prefixes forbidden under src/dispatch/
FORBIDDEN_DISPATCH_PREFIXES = (
    "worker",
    "relay",
    "tracker",
    "tracking",
    "ingest",
    "assess",
    "baseline",
    "analytics",
    "personalize",
    "agent",
)

# C-8: Tracking companion tables that must not be mutated outside tests
TRACKING_TABLES = (
    "player_observations",
    "dispatch_assessments",
    "dispatch_plans",
    "dispatch_delivery_attempts",
)

# Rollout gates required in docs/DISPATCH_ACTIVATION.md
REQUIRED_GATES = (
    "G-MIGRATION",
    "G-GRANTS",
    "G-WORKER",
    "G-ID",
    "G-ATOMIC",
    "G-OUTBOX",
)

EXACT_DISABLED_MESSAGE = "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"


def test_no_phase2plus_code():
    """C-8, AC-58: Ensure src/dashboard, Phase 3+ player-agent, and dispatch worker/relay code do not exist."""
    src_dir = ROOT / "src"
    assert src_dir.is_dir(), "src directory must exist"

    # 1. Ensure src/dashboard (Phase 4) and player-agent (Phase 3) do not exist.
    # Note: src/identity/ is explicitly permitted in Phase 2.
    forbidden_mods = ("dashboard", "player_agents", "player_agent")
    for forbidden_mod in forbidden_mods:
        dir_path = src_dir / forbidden_mod
        file_path = src_dir / f"{forbidden_mod}.py"
        assert not dir_path.exists(), f"Phase 3+ directory must not exist: {dir_path.relative_to(ROOT)}"
        assert not file_path.exists(), f"Phase 3+ file must not exist: {file_path.relative_to(ROOT)}"

    # 2. Ensure forbidden prefixes do not exist under src/dispatch/
    dispatch_dir = src_dir / "dispatch"
    assert dispatch_dir.is_dir(), "src/dispatch directory must exist"

    offending_files: list[str] = []
    for item in dispatch_dir.rglob("*"):
        if item.name == "__pycache__":
            continue
        rel = item.relative_to(ROOT)
        stem_lower = item.stem.lower()
        name_lower = item.name.lower()
        for prefix in FORBIDDEN_DISPATCH_PREFIXES:
            if stem_lower.startswith(prefix) or name_lower.startswith(prefix):
                offending_files.append(str(rel))
                break

    assert not offending_files, (
        f"Phase 3+ files or directories found in src/dispatch: {offending_files}. "
        f"Forbidden prefixes: {FORBIDDEN_DISPATCH_PREFIXES}"
    )


def test_no_webauthn_imports():
    """C-8, AC-58: Ensure webauthn imports are restricted to src/identity/ and src/graph_api.py."""
    src_dir = ROOT / "src"
    assert src_dir.is_dir()

    violations: list[str] = []
    text_regex = re.compile(
        r"^\s*(import\s+webauthn\b|from\s+webauthn\b|import\s+.*py_webauthn|from\s+.*py_webauthn)",
        re.MULTILINE | re.IGNORECASE,
    )
    literal_regex = re.compile(r"\bpy_webauthn\b", re.IGNORECASE)

    for py_file in src_dir.rglob("*.py"):
        rel = py_file.relative_to(ROOT)
        # Phase 2 adaptation: src/identity/ and src/graph_api.py are authorized for WebAuthn
        if "identity" in rel.parts or rel.name == "graph_api.py":
            continue

        content = py_file.read_text(encoding="utf-8")

        # Text/Regex check
        for line_no, line in enumerate(content.splitlines(), start=1):
            if text_regex.search(line) or literal_regex.search(line):
                violations.append(f"{rel}:{line_no}: {line.strip()}")

        # AST analysis
        try:
            tree = ast.parse(content, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.lower()
                    if name == "webauthn" or name.startswith("webauthn.") or "py_webauthn" in name:
                        violations.append(f"{rel}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").lower()
                if mod == "webauthn" or mod.startswith("webauthn.") or "py_webauthn" in mod:
                    violations.append(f"{rel}:{node.lineno}: from {node.module} import ...")

    assert not violations, f"Unauthorized WebAuthn imports found outside identity scope in src/:\n" + "\n".join(violations)


def test_no_tracking_writes_outside_tests():
    """C-8, AC-58: Ensure no Python file under src/ writes to companion tracking tables."""
    src_dir = ROOT / "src"
    assert src_dir.is_dir()

    mutation_regexes = {
        table: re.compile(
            rf"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?)\s+(public\.)?{table}\b",
            re.IGNORECASE | re.MULTILINE,
        )
        for table in TRACKING_TABLES
    }

    violations: list[str] = []
    for py_file in src_dir.rglob("*.py"):
        rel = py_file.relative_to(ROOT)
        content = py_file.read_text(encoding="utf-8")

        for table, regex in mutation_regexes.items():
            matches = list(regex.finditer(content))
            for match in matches:
                line_no = content[:match.start()].count("\n") + 1
                matched_text = match.group(0).replace("\n", " ").strip()
                violations.append(f"{rel}:{line_no}: {matched_text} ({table})")

    assert not violations, (
        f"Forbidden write/mutation queries for tracking companion tables found under src/:\n"
        + "\n".join(violations)
    )


def test_no_dispatch_worker():
    """Phase 3+ boundary guard: assert NO dispatch worker exists (filesystem, compose, env)."""
    src_dir = ROOT / "src"

    # 1. No worker files under src/dispatch/
    dispatch_dir = src_dir / "dispatch"
    if dispatch_dir.is_dir():
        worker_files = [
            str(p.relative_to(ROOT))
            for p in dispatch_dir.rglob("*")
            if p.name != "__pycache__" and (p.stem.lower().startswith("worker") or p.name.lower().startswith("worker"))
        ]
        assert not worker_files, f"Phase 3+ dispatch worker file found in src/dispatch: {worker_files}"

    # 2. No worker modules directly under src/
    for name in ("worker", "dispatch_worker"):
        assert not (src_dir / name).exists(), f"Phase 3+ worker directory found: src/{name}"
        assert not (src_dir / f"{name}.py").exists(), f"Phase 3+ worker file found: src/{name}.py"

    # 3. No dispatch-worker service in docker-compose.yml
    compose_path = ROOT / "docker-compose.yml"
    if compose_path.is_file():
        content = compose_path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*dispatch-worker:\s*$", content, re.MULTILINE), (
            "Phase 3+ dispatch-worker service found in docker-compose.yml"
        )

    # 4. DISPATCH_WORKER_ENABLED invariant
    assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false", (
        "Phase 3+ invariant violation: DISPATCH_WORKER_ENABLED must be false"
    )


def test_no_outbox_relay():
    """Phase 3+ boundary guard: assert NO outbox relay exists (filesystem, compose, activation docs)."""
    src_dir = ROOT / "src"

    # 1. No relay files under src/dispatch/
    dispatch_dir = src_dir / "dispatch"
    if dispatch_dir.is_dir():
        relay_files = [
            str(p.relative_to(ROOT))
            for p in dispatch_dir.rglob("*")
            if p.name != "__pycache__" and (p.stem.lower().startswith("relay") or p.name.lower().startswith("relay"))
        ]
        assert not relay_files, f"Phase 3+ outbox relay file found in src/dispatch: {relay_files}"

    # 2. No relay modules directly under src/
    for name in ("relay", "outbox_relay"):
        assert not (src_dir / name).exists(), f"Phase 3+ relay directory found: src/{name}"
        assert not (src_dir / f"{name}.py").exists(), f"Phase 3+ relay file found: src/{name}.py"

    # 3. No outbox-relay service in docker-compose.yml
    compose_path = ROOT / "docker-compose.yml"
    if compose_path.is_file():
        content = compose_path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*outbox-relay:\s*$", content, re.MULTILINE), (
            "Phase 3+ outbox-relay service found in docker-compose.yml"
        )

    # 4. G-OUTBOX activation gate cannot be satisfied before Phase 3
    doc_path = ROOT / "docs" / "DISPATCH_ACTIVATION.md"
    assert doc_path.is_file(), "DISPATCH_ACTIVATION.md must exist"
    doc_content = doc_path.read_text(encoding="utf-8")
    assert "G-OUTBOX cannot be satisfied before Phase 3" in doc_content, (
        "docs/DISPATCH_ACTIVATION.md must state 'G-OUTBOX cannot be satisfied before Phase 3'"
    )


def test_dispatch_enabled_is_false():
    """Phase 1/2 safety invariant: assert DISPATCH_ENABLED=false is enforced universally."""
    assert os.getenv("DISPATCH_ENABLED", "false").lower() == "false", (
        "Phase 1/2 safety invariant violation: DISPATCH_ENABLED must be false"
    )

    compose_path = ROOT / "docker-compose.yml"
    if compose_path.is_file():
        content = compose_path.read_text(encoding="utf-8")
        assert re.search(r'DISPATCH_ENABLED:\s*"?false"?', content), (
            "docker-compose.yml must set DISPATCH_ENABLED: 'false'"
        )


def test_dispatch_worker_enabled_is_false():
    """Phase 1/2 safety invariant: assert DISPATCH_WORKER_ENABLED=false is enforced universally."""
    assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false", (
        "Phase 1/2 safety invariant violation: DISPATCH_WORKER_ENABLED must be false"
    )

    compose_path = ROOT / "docker-compose.yml"
    if compose_path.is_file():
        content = compose_path.read_text(encoding="utf-8")
        assert not re.search(r'DISPATCH_WORKER_ENABLED:\s*"?true"?', content), (
            "docker-compose.yml must not set DISPATCH_WORKER_ENABLED: 'true'"
        )


def test_docs_state_disabled_and_gates():
    """C-2, C-3: DISPATCH_ACTIVATION.md lists G-MIGRATION..G-OUTBOX and the exact disabled message."""
    doc_path = ROOT / "docs" / "DISPATCH_ACTIVATION.md"
    assert doc_path.is_file(), f"Required activation document missing: {doc_path.relative_to(ROOT)}"

    content = doc_path.read_text(encoding="utf-8")

    # Verify all 6 gates are listed
    for gate in REQUIRED_GATES:
        assert gate in content, f"docs/DISPATCH_ACTIVATION.md must document activation gate {gate}"

    # Verify exact disabled message
    assert EXACT_DISABLED_MESSAGE in content, (
        f"docs/DISPATCH_ACTIVATION.md must contain exact disabled message:\n"
        f"'{EXACT_DISABLED_MESSAGE}'"
    )


def test_activation_doc_forbids_phase1_enablement():
    """C-2, C-3: DISPATCH_ACTIVATION.md contains required contractual statements forbidding enablement."""
    doc_path = ROOT / "docs" / "DISPATCH_ACTIVATION.md"
    assert doc_path.is_file(), f"Required activation document missing: {doc_path.relative_to(ROOT)}"

    content = doc_path.read_text(encoding="utf-8")

    # Statement 1: Phase 1 MUST NOT be enabled in any deployed environment
    assert re.search(
        r"Phase 1 MUST NOT be enabled in any deployed environment",
        content,
        re.IGNORECASE,
    ), "Missing required statement: 'Phase 1 MUST NOT be enabled in any deployed environment'"

    # Statement 2: Passing Phase 1 tests alone does not authorize activation
    assert re.search(
        r"passing (the )?Phase 1 tests alone does not authorize activation",
        content,
        re.IGNORECASE,
    ), "Missing required statement: 'Passing Phase 1 tests alone does not authorize activation'"

    # Statement 3: There is no production dispatch-worker in Phase 1 (G-WORKER cannot pass)
    assert re.search(
        r"there is no production dispatch-worker in Phase 1",
        content,
        re.IGNORECASE,
    ), "Missing required statement: 'There is no production dispatch-worker in Phase 1'"
    assert re.search(
        r"G-WORKER",
        content,
    ), "Missing reference to G-WORKER in relation to missing Phase 1 dispatch-worker"

    # Statement 4: G-OUTBOX cannot be satisfied before Phase 3 (not_recordable_before_phase_3)
    assert re.search(
        r"G-OUTBOX cannot be satisfied before Phase 3",
        content,
        re.IGNORECASE,
    ), "Missing required statement: 'G-OUTBOX cannot be satisfied before Phase 3'"
    assert "not_recordable_before_phase_3" in content, (
        "docs/DISPATCH_ACTIVATION.md must reference 'not_recordable_before_phase_3' error code for G-OUTBOX"
    )
```

### 4.4 Companion Suite: `tests/test_phase2_scope.py` (Optional / Recommended Alias)
If the project structure prefers a distinct test module for Phase 2, `tests/test_phase2_scope.py` can either re-export all symbols from `tests.test_phase1_scope` or be placed alongside it as a symlink/wrapper:

```python
"""Phase 2 Scope and Boundary Protection Suite."""
from tests.test_phase1_scope import (
    test_no_phase2plus_code,
    test_no_webauthn_imports,
    test_no_tracking_writes_outside_tests,
    test_no_dispatch_worker,
    test_no_outbox_relay,
    test_dispatch_enabled_is_false,
    test_dispatch_worker_enabled_is_false,
    test_docs_state_disabled_and_gates,
    test_activation_doc_forbids_phase1_enablement,
)

__all__ = [
    "test_no_phase2plus_code",
    "test_no_webauthn_imports",
    "test_no_tracking_writes_outside_tests",
    "test_no_dispatch_worker",
    "test_no_outbox_relay",
    "test_dispatch_enabled_is_false",
    "test_dispatch_worker_enabled_is_false",
    "test_docs_state_disabled_and_gates",
    "test_activation_doc_forbids_phase1_enablement",
]
```

---

## 5. Verification Method

### 5.1 Verification Commands
The implementing agent should execute the following test commands after applying the line edits:

1. **Verify Packaging Synchronization**:
   ```bash
   pytest tests/test_packaging.py -v
   ```
   *Expected outcome*: 3 passed in <0.5s.
   - `test_pyproject_matches_runtime_requirements` PASS (exact list match).
   - `test_test_tools_are_not_runtime_dependencies` PASS.
   - `test_langgraph_is_pinned_exactly` PASS.

2. **Verify Adapted Scope and Boundary Guards**:
   ```bash
   pytest tests/test_phase1_scope.py -v
   ```
   *Expected outcome*: 9 passed in <1.0s.
   - `test_no_phase2plus_code` PASS (permits `src/identity/`).
   - `test_no_webauthn_imports` PASS (permits `webauthn` in identity scope).
   - `test_no_tracking_writes_outside_tests` PASS.
   - `test_no_dispatch_worker` PASS (asserts no worker files/services).
   - `test_no_outbox_relay` PASS (asserts no relay files/services).
   - `test_dispatch_enabled_is_false` PASS (`DISPATCH_ENABLED=false`).
   - `test_dispatch_worker_enabled_is_false` PASS (`DISPATCH_WORKER_ENABLED=false`).
   - `test_docs_state_disabled_and_gates` PASS.
   - `test_activation_doc_forbids_phase1_enablement` PASS.

3. **Combined Packaging & Scope Command**:
   ```bash
   pytest tests/test_packaging.py tests/test_phase1_scope.py -q
   ```
   *Expected outcome*: `12 passed`.

### 5.2 Files to Inspect
- `pyproject.toml` lines 12–33
- `requirements.txt` lines 1–22
- `tests/test_phase1_scope.py`

### 5.3 Invalidation Conditions
- Any discrepancy in package order or version string between `pyproject.toml` and `requirements.txt`.
- Inclusion of `pytest` or `pyyaml` in `requirements.txt`.
- Emergence of any `worker` or `relay` file under `src/dispatch/`.
- Any environment or configuration setting `DISPATCH_ENABLED=true` or `DISPATCH_WORKER_ENABLED=true`.
