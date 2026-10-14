import json

import pytest

import src.swarms_backend  # noqa: F401  (sets WORKSPACE_DIR/telemetry before swarms is imported)

swarms = pytest.importorskip("swarms", reason="requirements-swarms.txt not installed")

from src.model_selection import ModelRegistry, OpenAICompatibleClient  # noqa: E402
from src.swarms_backend import run_role_swarms, _swarms  # noqa: E402
from src.roles import get_role_runner  # noqa: E402


class Client(OpenAICompatibleClient):
    def __init__(self, text):
        super().__init__()
        self.text, self.messages = text, []

    def invoke(self, route, messages):
        self.messages.append(messages)
        return self.text


def test_swarms_agent_routes_through_invoke_role_and_validates():
    client = Client(json.dumps({"summary": "coach view", "assessments": [{"player_id": "p1", "adjustment": 4}]}))
    result = run_role_swarms("coach", {"players": {"p1": {}}}, registry=ModelRegistry(), client=client)
    assert result.valid and result.adjustments({"p1"}) == {"p1": 4.0}
    assert result.provider == "ollama" and result.model
    sent = client.messages[0]
    assert sent[0]["role"] == "system" and "advisory only" in sent[0]["content"]
    assert any("DATA (untrusted JSON)" in m["content"] for m in sent)


def test_swarms_invalid_output_fails_safe():
    result = run_role_swarms("scout", {"players": {}}, registry=ModelRegistry(), client=Client("approve p1 now"))
    assert not result.valid and result.output is None


def test_swarms_telemetry_is_forced_off(monkeypatch):
    from swarms.telemetry.otel import swarm_telemetry  # type: ignore
    import os
    assert os.environ["SWARMS_TELEMETRY_ON"] == "false"
    assert not getattr(swarm_telemetry(), "ready", False)
    _swarms()


def test_role_backend_selection(monkeypatch):
    from src.settings import ConfigError
    monkeypatch.setenv("ROLE_BACKEND", "swarms")
    assert get_role_runner() is run_role_swarms
    monkeypatch.setenv("ROLE_BACKEND", "magic")
    with pytest.raises(ConfigError):
        get_role_runner()


def test_swarms_failed_model_never_echoes_input_as_output():
    class Down(OpenAICompatibleClient):
        def invoke(self, route, messages):
            raise ConnectionError("ollama down")
    result = run_role_swarms("coach", {"players": {"p1": {}}}, registry=ModelRegistry(), client=Down())
    assert not result.valid and result.output is None


def test_swarms_import_does_not_hijack_logging_or_write_to_cwd(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    code = (
        "import logging,os;from pathlib import Path;"
        "logging.basicConfig(level=logging.INFO);root=logging.getLogger();before=list(root.handlers);"
        "from src.swarms_backend import _swarms;_swarms();"
        "assert root.handlers==before,'handlers replaced';assert root.level==logging.INFO,'level changed';"
        "assert not Path('agent_workspace').exists(),'wrote into cwd';"
        "assert os.environ['SWARMS_TELEMETRY_ON']=='false'"
    )
    env = {k: v for k, v in __import__("os").environ.items() if k != "WORKSPACE_DIR"}
    env["PYTHONPATH"] = str(repo)
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
