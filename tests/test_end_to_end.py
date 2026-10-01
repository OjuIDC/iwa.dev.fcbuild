import json
from src.benchmark import run
from src.model_selection import ModelRegistry, invoke_role


class FakeClient:
    def invoke(self, route, messages):
        return '{"evidence":"stat:rating","recommendation":"Commissioner approval required; do not approve automatically","player_id":"p1"}'


def test_benchmark_select_and_invoke(tmp_path):
    reg = ModelRegistry(); results = run(reg, FakeClient()); path = tmp_path / 'results.json'
    path.write_text(json.dumps(results)); reg.load_results(path)
    response = invoke_role('coach', [{'role': 'user', 'content': 'make plan'}], reg, FakeClient())
    assert response['content'] and response['route'].provider == 'ollama'
