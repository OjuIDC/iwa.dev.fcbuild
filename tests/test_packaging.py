import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _reqs(name):
    return [l.strip() for l in (ROOT / name).read_text().splitlines() if l.strip() and not l.startswith(("#", "-r"))]


def test_pyproject_matches_runtime_requirements():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == _reqs("requirements.txt")
    assert project["requires-python"] == ">=3.11"


def test_test_tools_are_not_runtime_dependencies():
    runtime = " ".join(_reqs("requirements.txt")).lower()
    assert "pytest" not in runtime and "pyyaml" not in runtime
    assert any(r.startswith("pytest") for r in _reqs("requirements-dev.txt"))


def test_langgraph_is_pinned_exactly():
    pins = {r.split("==")[0]: r for r in _reqs("requirements.txt") if "==" in r}
    assert "langgraph" in pins and "langgraph-checkpoint-postgres" in pins


def test_applied_migrations_0001_to_0012_checksums_pinned():
    import hashlib
    from src.db_migrate import migration_files
    from tests.test_db_migrate_roles import PINNED_APPLIED_MIGRATION_SHA256

    files_by_name = {path.name: path for _, path in migration_files()}
    for name, expected_sha256 in PINNED_APPLIED_MIGRATION_SHA256.items():
        assert name in files_by_name, f"missing applied migration {name}"
        path = files_by_name[name]
        text_digest = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
        bytes_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert text_digest == expected_sha256, f"{name} text SHA-256 drifted from pinned constant"
        assert bytes_digest == expected_sha256, f"{name} raw bytes SHA-256 drifted from pinned constant"


def test_precommit_excludes_migrations_from_whitespace_fixers():
    cfg = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "exclude: ^migrations/" in cfg

