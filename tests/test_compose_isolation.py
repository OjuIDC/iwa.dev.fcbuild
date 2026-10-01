"""Static checks on the resolved Compose model: secret isolation, network
exposure, image pinning and fail-closed configuration. Requires the docker CLI
(only `docker compose config` is used; no containers are started)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")

APPROVAL_SECRETS = {"GRAPH_COMMISSIONER_TOKEN_SHA256", "APPROVAL_SIGNING_SECRET", "POSTGRES_URI",
                    "GRAPH_COMMISSIONER_TOKEN", "GRAPH_API_TOKEN"}


def _env_file(tmp_path, drop=()):
    text = (ROOT / "config.example.env").read_text()
    text = re.sub(r"=replace_with_[a-z0-9_]+", "=placeholder_validation_only_0123456789abcdef0123456789abcdef", text)
    lines = [l for l in text.splitlines() if l.split("=", 1)[0] not in drop]
    path = tmp_path / "validation.env"
    path.write_text("\n".join(lines) + "\n")
    return path


def _config(env, *args):
    cmd = ["docker", "compose", "--env-file", str(env), *args, "config", "--format", "json"]
    # Isolate from the caller's environment: shell variables override --env-file.
    clean = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME") or k.startswith("DOCKER_")}
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=clean)


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    env = _env_file(tmp_path_factory.mktemp("env"))
    out = _config(env, "--profile", "voice", "--profile", "rtsp")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_only_graph_api_receives_approval_material(model):
    for name, svc in model["services"].items():
        leaked = APPROVAL_SECRETS & set((svc.get("environment") or {}).keys())
        if name == "graph-api":
            assert {"GRAPH_COMMISSIONER_TOKEN_SHA256", "APPROVAL_SIGNING_SECRET", "POSTGRES_URI"} <= leaked
            assert "GRAPH_COMMISSIONER_TOKEN" not in leaked and "GRAPH_API_TOKEN" not in leaked
        else:
            assert not leaked, f"{name} receives {sorted(leaked)}"


def test_only_graph_api_can_reach_postgres(model):
    on_data = sorted(n for n, s in model["services"].items() if "fc27_data" in (s.get("networks") or {}))
    assert on_data == ["graph-api", "postgres"]
    assert model["networks"]["fc27_data"].get("internal") is True


def test_host_ports_bind_loopback_only(model):
    for name, svc in model["services"].items():
        for port in svc.get("ports") or []:
            assert port.get("host_ip") == "127.0.0.1", f"{name} publishes {port}"


def test_images_are_pinned(model):
    for name, svc in model["services"].items():
        image = svc.get("image")
        if image and "fc27" not in image:
            assert "@sha256:" in image and not image.split("@")[0].endswith(":latest"), f"{name}: {image}"


@pytest.mark.parametrize("missing", ["GRAPH_SERVICE_TOKEN", "GRAPH_COMMISSIONER_TOKEN_SHA256", "APPROVAL_SIGNING_SECRET",
                                     "COMMISSIONER_ID", "MCP_API_TOKEN", "WS_AUTH_TOKEN", "POSTGRES_PASSWORD"])
def test_missing_secret_fails_closed(tmp_path, missing):
    out = _config(_env_file(tmp_path, drop=(missing,)))
    assert out.returncode != 0 and missing in out.stderr


def test_dockerfiles_pin_base_images():
    for path in ROOT.glob("Dockerfile.*"):
        first = next(l for l in path.read_text().splitlines() if l.startswith("FROM"))
        assert "@sha256:" in first, path.name
