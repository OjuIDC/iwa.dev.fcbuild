# Scope Analysis: Milestone 5 Scope Enforcement (`tests/test_phase1_scope.py`)

**Agent**: `explorer_m5_scope_1`  
**Role**: Milestone 5 Scope Enforcement Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_scope_1`  
**Timestamp**: 2026-10-03T21:15:00Z  
**Target Test Suite**: `tests/test_phase1_scope.py`  
**Referenced Specifications**: Spec v2.1 §31 (AC-58, §31.1, §31.2), Corrections C-2, C-3, C-8; Plan Task 9 (lines 1102–1135)

---

## 1. Executive Summary & Problem Boundary

### 1.1 Objective
Design and specify `tests/test_phase1_scope.py` to programmatically enforce Phase 1 scope boundaries, prevent scope creep into Phase 2+ (dashboard, identity, WebAuthn, player agents, tracking runtime), verify absence of tracking table mutations outside tests, and mandate strict activation documentation invariants.

### 1.2 Authoritative Boundary Requirements
From **Spec v2.1 §31.1 & AC-58** and **Plan Task 9**:
1. **No Phase 2+ Code**: `src/dashboard` and `src/identity` MUST NOT exist. No file matching `src/dispatch/{worker,relay,tracker,tracking,ingest,assess,baseline,analytics,personalize,agent}*.py` may exist.
2. **No WebAuthn Imports**: No Python file under `src/` may contain `import webauthn`, `from webauthn`, or reference `py_webauthn`.
3. **No Tracking Writes Outside Tests**: No Python file under `src/` (or any non-test path) may execute `INSERT INTO`, `UPDATE`, `DELETE FROM`, or `TRUNCATE` against companion tracking tables:
   - `player_observations`
   - `dispatch_assessments`
   - `dispatch_plans`
   - `dispatch_delivery_attempts`
4. **Activation Documentation Gates & Invariants**: `docs/DISPATCH_ACTIVATION.md` must list all six activation gates (`G-MIGRATION` through `G-OUTBOX`), contain the exact disabled message `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`, and contain the four required non-negotiable statements forbidding Phase 1 operational enablement.

---

## 2. Current Repository State & Baseline Evidence

A comprehensive inspection of the current repository state was conducted:

### 2.1 Codebase Structure (`src/`)
- **`src/dashboard`**: Does NOT exist (`assert not exists`).
- **`src/identity`**: Does NOT exist (`assert not exists`).
- **`src/dispatch/`**: Contains exactly 5 files:
  1. `src/dispatch/__init__.py`
  2. `src/dispatch/creation.py` (atomic dispatch creation in approval transaction)
  3. `src/dispatch/gates.py` (activation gate evaluation and challenge/record HMAC verification)
  4. `src/dispatch/objective.py` (objective schema validation and bounds checks)
  5. `src/dispatch/transition.py` (SECURITY DEFINER transition function wrapper)
  - Zero files match `{worker,relay,tracker,tracking,ingest,assess,baseline,analytics,personalize,agent}*.py`.
- **WebAuthn imports in `src/`**: Scanned all 35 Python files in `src/`. Zero instances of `import webauthn`, `from webauthn`, or `py_webauthn` found.
- **Tracking table mutations in `src/`**: Zero occurrences of `player_observations`, `dispatch_assessments`, `dispatch_plans`, or `dispatch_delivery_attempts` anywhere in `src/`.

### 2.2 Test Structure (`tests/`)
- Tracking companion tables are tested in `tests/test_dispatch_schema.py` and `tests/test_adversarial_m3_challenger.py` solely for schema definition and append-only trigger immutability.
- No test or fixture violates Phase 1 scope; testing schema triggers within `tests/` is explicitly permissible under AC-58 ("outside tests").

### 2.3 Documentation State (`docs/`)
- `docs/DISPATCH_ACTIVATION.md`: **Does NOT exist yet**.
- Per Plan Task 9 Step 2, running `tests/test_phase1_scope.py` in Step 2 MUST FAIL because `docs/DISPATCH_ACTIVATION.md` has not yet been authored.
- In Step 3, Milestone 5 implementer will author `docs/DISPATCH_ACTIVATION.md` and `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`, after which the entire suite will pass.

---

## 3. Detailed Test Function Design

### 3.1 Test 1: `test_no_phase2plus_code()` (C-8, AC-58)

#### Purpose
Verify that Phase 2+ features (dashboard BFF, identity subsystem, player agents, outbox relay, background workers, and tracking engine) have not leaked into the codebase.

#### Scanning Logic
1. **Forbidden Directories/Modules**:
   - `ROOT / "src" / "dashboard"` (directory or `.py`)
   - `ROOT / "src" / "identity"` (directory or `.py`)
2. **Forbidden Prefixes in `src/dispatch/`**:
   `FORBIDDEN_DISPATCH_PREFIXES = ("worker", "relay", "tracker", "tracking", "ingest", "assess", "baseline", "analytics", "personalize", "agent")`
3. Scan all entries under `src/dispatch/` recursively (`rglob("*")`):
   - Any `.py` file whose stem starts with any forbidden prefix is flagged.
   - Any subdirectory whose name starts with any forbidden prefix is flagged.
   - Case-insensitive comparison to prevent bypasses on case-insensitive filesystems.

---

### 3.2 Test 2: `test_no_webauthn_imports()` (C-8, AC-58)

#### Purpose
Enforce that no runtime code under `src/` imports or depends on WebAuthn / `py_webauthn`. Phase 2 owns WebAuthn passkey registration and authentication.

#### Scanning Logic
Two-layer detection:
1. **AST Node Visitor**:
   Parse every `.py` file under `src/` using Python's `ast.parse()`.
   - `ast.Import`: Check each `alias.name` for `"webauthn"`, prefixes `"webauthn."`, or substring `"py_webauthn"`.
   - `ast.ImportFrom`: Check `node.module` for `"webauthn"`, prefixes `"webauthn."`, or substring `"py_webauthn"`.
2. **Regex / Text Scanner**:
   Regex scan the file text for:
   - `r"^\s*(import\s+webauthn\b|from\s+webauthn\b|import\s+.*py_webauthn|from\s+.*py_webauthn)"` (multiline)
   - `r"\bpy_webauthn\b"`
3. Reports file paths and 1-indexed line numbers of any violations.

---

### 3.3 Test 3: `test_no_tracking_writes_outside_tests()` (C-8, AC-58)

#### Purpose
Enforce that while forward-compatible tracking tables exist in SQL (0003 migration), no production Python code writes to them. Writing to tracking tables is reserved for Phase 3 (dispatch plans) and Phase 5 (player observations and assessments).

#### Target Tables
- `player_observations`
- `dispatch_assessments`
- `dispatch_plans`
- `dispatch_delivery_attempts`

#### Scanning Logic
1. Find all `.py` files under `src/`.
2. Scan for mutation SQL verbs across multi-line statements:
   - `INSERT\s+INTO\s+(public\.)?{table}`
   - `UPDATE\s+(public\.)?{table}`
   - `DELETE\s+FROM\s+(public\.)?{table}`
   - `TRUNCATE(\s+TABLE)?\s+(public\.)?{table}`
   - `COPY\s+(public\.)?{table}\s+FROM`
3. Also perform a strict token search for the table names themselves in `src/`. Currently, none of the 4 table names appear in `src/` at all. If any write pattern appears, fail with path and line number.

---

### 3.4 Test 4: `test_docs_state_disabled_and_gates()` (C-2, C-3, Task 9)

#### Purpose
Verify that `docs/DISPATCH_ACTIVATION.md` exists, documents what "disabled" means with the exact error message emitted by `src/graph_api.py`, and enumerates all six activation gates.

#### Assertions
1. File `ROOT / "docs" / "DISPATCH_ACTIVATION.md"` must exist and be non-empty.
2. Contains all 6 gate identifiers:
   - `G-MIGRATION`
   - `G-GRANTS`
   - `G-WORKER`
   - `G-ID`
   - `G-ATOMIC`
   - `G-OUTBOX`
3. Contains the exact disabled message:
   `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`

---

### 3.5 Test 5: `test_activation_doc_forbids_phase1_enablement()` (C-2, C-3, Task 9)

#### Purpose
Verify that `docs/DISPATCH_ACTIVATION.md` contains the mandatory contractual statements forbidding operational enablement during Phase 1.

#### Assertions
1. **Statement 1: Deployed environment prohibition**:
   Matches regex `r"Phase 1 MUST NOT be enabled in any deployed environment"` (case-insensitive).
2. **Statement 2: Tests alone do not authorize activation**:
   Matches regex `r"passing (the )?Phase 1 tests alone does not authorize activation"` (case-insensitive).
3. **Statement 3: No production dispatch worker in Phase 1**:
   Matches regex `r"there is no production dispatch-worker in Phase 1"` (case-insensitive) AND references `G-WORKER`.
4. **Statement 4: G-OUTBOX unsatisfied before Phase 3**:
   Matches regex `r"G-OUTBOX cannot be satisfied before Phase 3"` (case-insensitive) AND contains the route error literal `"not_recordable_before_phase_3"`.

---

## 4. Complete Implementation Specification for `tests/test_phase1_scope.py`

Here is the exact code to be written by the implementer into `tests/test_phase1_scope.py`:

```python
"""Phase 1 Scope and Boundary Protection Tests (AC-58, C-8, C-2, C-3).

Enforces:
1. Absence of Phase 2+ runtime code (dashboard, identity, player-agents, workers, trackers).
2. Absence of WebAuthn / py_webauthn imports in src/.
3. Absence of mutations/writes to companion tracking tables outside tests.
4. Correctness of docs/DISPATCH_ACTIVATION.md (all 6 gates, exact disabled message, and 4 mandatory statements).
"""
from __future__ import annotations

import ast
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
    """C-8, AC-58: Ensure src/dashboard, src/identity, and Phase 2+ dispatch files do not exist."""
    src_dir = ROOT / "src"
    assert src_dir.is_dir(), "src directory must exist"

    # 1. Ensure src/dashboard and src/identity do not exist
    for forbidden_mod in ("dashboard", "identity"):
        dir_path = src_dir / forbidden_mod
        file_path = src_dir / f"{forbidden_mod}.py"
        assert not dir_path.exists(), f"Phase 2+ directory must not exist in Phase 1: {dir_path.relative_to(ROOT)}"
        assert not file_path.exists(), f"Phase 2+ file must not exist in Phase 1: {file_path.relative_to(ROOT)}"

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
        f"Phase 2+ files or directories found in src/dispatch: {offending_files}. "
        f"Forbidden prefixes: {FORBIDDEN_DISPATCH_PREFIXES}"
    )


def test_no_webauthn_imports():
    """C-8, AC-58: Ensure no 'import webauthn', 'from webauthn', or 'py_webauthn' in any file under src/."""
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

    assert not violations, f"WebAuthn imports or references found in src/:\n" + "\n".join(violations)


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
                # Find line number
                line_no = content[:match.start()].count("\n") + 1
                matched_text = match.group(0).replace("\n", " ").strip()
                violations.append(f"{rel}:{line_no}: {matched_text} ({table})")

    assert not violations, (
        f"Forbidden write/mutation queries for tracking companion tables found under src/:\n"
        + "\n".join(violations)
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

---

## 5. Specification for `docs/DISPATCH_ACTIVATION.md`

To guarantee that `test_docs_state_disabled_and_gates()` and `test_activation_doc_forbids_phase1_enablement()` pass cleanly in Step 4, `docs/DISPATCH_ACTIVATION.md` should be authored by the Milestone 5 implementer adhering to the following content:

```markdown
# Dispatch Activation and Safety Specification

## 1. Safety Invariant: Dispatch Remains Disabled in Phase 1

In Phase 1, dispatch is strictly disabled. Passing the Phase 1 tests alone does not authorize activation.
Phase 1 MUST NOT be enabled in any deployed environment.

If `DISPATCH_ENABLED=true` is set without satisfying all required activation gates, the system fails closed at startup and during readiness checks with the exact error message:
> `Phase 1 safety invariant violation: DISPATCH_ENABLED must be false`

## 2. Activation Gates Table (§31.2)

To enable dispatch in later phases, all six activation gates must pass simultaneously:

| Gate | Name | Verification Method | Phase 1 Operability |
|---|---|---|---|
| **G-MIGRATION** | Migration 0003 Applied | Automatic DB check | Evaluated |
| **G-GRANTS** | Least-Privilege Grants | Automatic DB check | Evaluated |
| **G-WORKER** | Dispatch Worker Heartbeat | Heartbeat (< 60s) | There is no production dispatch-worker in Phase 1, so G-WORKER cannot pass |
| **G-ID** | Stable EA Player Identity | Manual challenge/record nonce proof | Blocked pending Phase 3 evidence |
| **G-ATOMIC** | Atomic Approval-to-Dispatch | Manual challenge/record nonce proof | Recordable with CI proof |
| **G-OUTBOX** | Outbox Relay & Reconciliation | Relay test execution URL | G-OUTBOX cannot be satisfied before Phase 3 (`not_recordable_before_phase_3`) |

## 3. Phase 1 Boundary Statements (C-2, C-3)

The following invariants are non-negotiable:
1. **Phase 1 MUST NOT be enabled in any deployed environment.**
2. **Passing the Phase 1 tests alone does not authorize activation.**
3. **There is no production dispatch-worker in Phase 1, so G-WORKER cannot pass.**
4. **G-OUTBOX cannot be satisfied before Phase 3: its evidence is the Phase 3 relay and reconciliation test run, and the route refuses it in Phase 1 (`not_recordable_before_phase_3`).**

## 4. Manual Gate Recording Protocol (C-4)
- Challenge: `POST /v1/activation-gates/{gate_id}/challenge`
- Record: `POST /v1/activation-gates/{gate_id}`
- Bound to `FC27_CODE_REVISION` and `FC27_DEPLOYMENT_ID`.
```

---

## 6. Verification and Invalidation Method

### Verification Command
```bash
python -m pytest -o addopts="" -q tests/test_phase1_scope.py
```

### Invalidation Conditions
1. If any file named `worker.py`, `relay.py`, `tracker.py`, etc., is added to `src/dispatch/`.
2. If `src/dashboard/` or `src/identity/` is created before Phase 2.
3. If `webauthn` is imported in `src/`.
4. If an `INSERT INTO player_observations` is added to any file in `src/`.
5. If `docs/DISPATCH_ACTIVATION.md` is modified to omit any of the 4 mandatory invariant statements or the 6 gates.
