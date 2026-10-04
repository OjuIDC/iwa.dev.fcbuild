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
