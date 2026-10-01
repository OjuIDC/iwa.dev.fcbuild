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
