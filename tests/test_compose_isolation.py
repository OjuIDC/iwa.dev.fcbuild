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

APPROVAL_SECRETS = {
    "GRAPH_COMMISSIONER_TOKEN_SHA256",
    "APPROVAL_SIGNING_SECRET",
    "GRAPH_COMMISSIONER_TOKEN",
    "GRAPH_API_TOKEN",
}

ALL_DB_SECRETS = {
    "postgres_admin_uri",
    "fc27_migrator_password",
    "fc27_app_password",
    "fc27_dispatch_password",
    "fc27_relay_password",
    "fc27_migrator_uri",
    "fc27_app_uri",
    "fc27_relay_uri",
}


@pytest.fixture(autouse=True, scope="session")
def dummy_secrets():
    """Ensure declared top-level Compose secret files exist so `docker compose config` resolves."""
    secrets_dir = ROOT / "secrets"
    created = []
    created_dir = False
    if not secrets_dir.exists():
        secrets_dir.mkdir(parents=True, exist_ok=True)
        created_dir = True
    for name in ALL_DB_SECRETS:
        f = secrets_dir / name
        if not f.exists():
            f.write_text("placeholder_secret_for_validation\n", encoding="utf-8")
            created.append(f)
    yield
    for f in created:
        try:
            f.unlink()
        except OSError:
            pass
    if created_dir:
        try:
            secrets_dir.rmdir()
        except OSError:
            pass


def _env_file(tmp_path, drop=()):
    text = (ROOT / "config.example.env").read_text(encoding="utf-8")
    text = re.sub(r"=replace_with_[a-z0-9_]+", "=placeholder_validation_only_0123456789abcdef0123456789abcdef", text)
    lines = [l for l in text.splitlines() if l.split("=", 1)[0] not in drop]
    path = tmp_path / "validation.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _config(env, *args):
    # Use docker-compose (standalone) which works in clean environments on Windows.
    # docker compose (plugin) requires additional env vars for plugin discovery.
    cmd = ["docker-compose", "--env-file", str(env), *args, "config", "--format", "json"]
    # Isolate from the caller's environment: shell variables override --env-file.
    clean = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME") or k.startswith("DOCKER_")}
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=clean)


def _service_secrets(svc: dict) -> set[str]:
    raw = svc.get("secrets") or []
    result = set()
    for item in raw:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict):
            source = item.get("source") or item.get("target")
            if source:
                result.add(source)
    return result


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    env = _env_file(tmp_path_factory.mktemp("env"))
    out = _config(env, "--profile", "voice", "--profile", "rtsp", "--profile", "bootstrap")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_signing_secret_only_in_graph_api(model, lf_model):
    for name, svc in model["services"].items():
        env = svc.get("environment") or {}
        assert "POSTGRES_URI" not in env, f"{name} has literal POSTGRES_URI in env"
        if name == "graph-api":
            assert "APPROVAL_SIGNING_SECRET" in env
            assert "GRAPH_COMMISSIONER_TOKEN_SHA256" in env
            assert "GRAPH_COMMISSIONER_TOKEN" not in env
            assert "GRAPH_API_TOKEN" not in env
        else:
            assert "APPROVAL_SIGNING_SECRET" not in env, f"{name} receives APPROVAL_SIGNING_SECRET"
            assert "GRAPH_COMMISSIONER_TOKEN_SHA256" not in env, f"{name} receives GRAPH_COMMISSIONER_TOKEN_SHA256"
            assert "GRAPH_COMMISSIONER_TOKEN" not in env
            assert "GRAPH_API_TOKEN" not in env

    for name, svc in lf_model["services"].items():
        env = svc.get("environment") or {}
        assert "POSTGRES_URI" not in env, f"{name} has literal POSTGRES_URI in env in lf_model"
        if name != "graph-api":
            assert "APPROVAL_SIGNING_SECRET" not in env
            assert "GRAPH_COMMISSIONER_TOKEN_SHA256" not in env


def test_db_secret_mounts_per_service(model):
    for name, svc in model["services"].items():
        mounted = _service_secrets(svc)
        if name == "db-bootstrap":
            assert mounted == {
                "postgres_admin_uri",
                "fc27_migrator_password",
                "fc27_app_password",
                "fc27_dispatch_password",
                "fc27_relay_password",
            }
        elif name == "db-migrate":
            assert mounted == {"fc27_migrator_uri"}
        elif name == "outbox-relay":
            assert mounted == {"fc27_relay_uri"}
        elif name == "dispatch-worker":
            assert mounted == {"fc27_dispatch_uri"}
        elif name == "graph-api":
            assert "fc27_app_uri" in mounted
            assert not (mounted & {
                "postgres_admin_uri",
                "fc27_migrator_password",
                "fc27_app_password",
                "fc27_dispatch_password",
                "fc27_relay_password",
                "fc27_migrator_uri",
                "fc27_relay_uri",
            })
        else:
            assert not (mounted & ALL_DB_SECRETS), f"{name} must not mount any DB secrets, got {mounted & ALL_DB_SECRETS}"


def test_no_db_password_in_environment(model):
    uri_pattern = re.compile(r"postgres(?:ql)?://[^:]+:[^@]+@", re.IGNORECASE)
    for name, svc in model["services"].items():
        env = svc.get("environment") or {}
        for k in env.keys():
            assert k not in ("POSTGRES_URI", "POSTGRES_ADMIN_URI"), f"{name} has {k} in environment keys"
            if k.startswith("FC27_") and k.endswith("_PASSWORD"):
                pytest.fail(f"{name} has {k} password in environment keys")
            if name != "postgres":
                assert k != "POSTGRES_PASSWORD", f"{name} has POSTGRES_PASSWORD in environment keys"

        for k, v in env.items():
            if isinstance(v, str):
                assert not uri_pattern.search(v), f"{name} has credentials embedded in env var {k}: {v}"


def test_secrets_dir_is_gitignored():
    gitignore_path = ROOT / ".gitignore"
    assert gitignore_path.is_file()
    lines = [line.strip() for line in gitignore_path.read_text(encoding="utf-8").splitlines()]
    assert any(line in ("secrets/", "secrets", "/secrets/", "/secrets") for line in lines), (
        ".gitignore must ignore secrets/ directory"
    )
    if shutil.which("git"):
        res = subprocess.run(
            ["git", "check-ignore", "secrets/test_secret"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, "git check-ignore secrets/test_secret must exit with 0"


def test_graph_api_uses_app_role(model):
    env = model["services"]["graph-api"]["environment"]
    assert env.get("POSTGRES_URI_FILE") == "/run/secrets/fc27_app_uri"
    assert env.get("GRAPH_RUN_MIGRATIONS") == "false"
    assert env.get("GRAPH_CHECKPOINT_SETUP") == "false"
    assert "POSTGRES_URI" not in env


def test_dispatch_disabled_by_default(model, lf_model):
    assert model["services"]["graph-api"]["environment"].get("DISPATCH_ENABLED") == "false"
    assert lf_model["services"]["graph-api"]["environment"].get("DISPATCH_ENABLED") == "false"
    if "dispatch-worker" in model["services"]:
        assert model["services"]["dispatch-worker"]["environment"].get("DISPATCH_ENABLED") == "false"


def test_db_migrate_has_no_other_secrets(model):
    env = model["services"]["db-migrate"]["environment"]
    allowed = {"POSTGRES_URI_FILE", "PYTHONUNBUFFERED"}
    assert set(env.keys()).issubset(allowed), f"db-migrate environment keys {set(env.keys())} not subset of {allowed}"
    assert env.get("POSTGRES_URI_FILE") == "/run/secrets/fc27_migrator_uri"


def test_only_graph_api_can_reach_postgres(model):
    on_data = sorted(n for n, s in model["services"].items() if "fc27_data" in (s.get("networks") or {}))
    assert on_data == ["db-bootstrap", "db-migrate", "dispatch-worker", "graph-api", "outbox-relay", "postgres"]
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


def test_settings_read_secret_fails_closed(tmp_path, monkeypatch):
    from src import settings
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.setenv("POSTGRES_URI_FILE", str(tmp_path / "nonexistent_secret_file"))
    with pytest.raises(settings.ConfigError):
        settings.read_secret("POSTGRES_URI")


def test_dockerfiles_pin_base_images():
    for path in ROOT.glob("Dockerfile.*"):
        first = next(l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("FROM"))
        assert "@sha256:" in first, path.name


@pytest.fixture(scope="module")
def lf_model(tmp_path_factory):
    env = _env_file(tmp_path_factory.mktemp("lfenv"))
    out = _config(env, "-f", "docker-compose.yml", "-f", "docker-compose.langfuse.yml", "--profile", "langfuse", "--profile", "bootstrap")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_langfuse_disabled_without_overlay(model):
    env = model["services"]["graph-api"]["environment"]
    assert env["LANGFUSE_ENABLED"] == "false" and "LANGFUSE_SECRET_KEY" not in env
    assert not any(n.startswith("langfuse") for n in model["services"])


def test_langfuse_profile_isolation_and_pinning(lf_model):
    services = lf_model["services"]
    assert services["graph-api"]["environment"]["LANGFUSE_ENABLED"] == "true"
    assert lf_model["networks"]["fc27_obs"].get("internal") is True
    for name, svc in services.items():
        for port in svc.get("ports") or []:
            assert port.get("host_ip") == "127.0.0.1", f"{name} publishes {port}"
        if name.startswith("langfuse"):
            assert "@sha256:" in svc["image"]
            assert not ({"GRAPH_SERVICE_TOKEN", "APPROVAL_SIGNING_SECRET", "POSTGRES_URI"} & set(svc.get("environment") or {}))
        elif name != "graph-api":
            assert "LANGFUSE_SECRET_KEY" not in (svc.get("environment") or {})
    assert services["langfuse-web"]["environment"]["TELEMETRY_ENABLED"] == "false"


@pytest.mark.parametrize("missing", ["LANGFUSE_SECRET_KEY", "LANGFUSE_ENCRYPTION_KEY", "LANGFUSE_DB_PASSWORD"])
def test_langfuse_missing_secret_fails_closed(tmp_path, missing):
    out = _config(_env_file(tmp_path, drop=(missing,)), "-f", "docker-compose.yml", "-f", "docker-compose.langfuse.yml",
                  "--profile", "langfuse", "--profile", "bootstrap")
    assert out.returncode != 0 and missing in out.stderr
