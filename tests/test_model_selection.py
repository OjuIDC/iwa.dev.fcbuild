import json
from pathlib import Path
import pytest
from src.model_selection import ModelRegistry,invoke_role
from src.benchmark import run
class FakeClient:
    def __init__(self,fail=None):self.fail=fail or set();self.calls=[]
    def invoke(self,route,messages):
        self.calls.append((route.provider,route.model))
        if (route.provider,route.model) in self.fail:raise RuntimeError('offline')
        return '{"player_id":"p1","evidence":["18/24=75%"],"recommendation":"Do not approve; Commissioner approval required because evidence supports recommendation"}'
def test_benchmark_and_selection(tmp_path):
    reg=ModelRegistry();result=run(reg,FakeClient());p=tmp_path/'r.json';p.write_text(json.dumps(result));reg.load_results(p)
    assert reg.select('player').model=='llama3.2:3b'
    assert reg.select('analyst').model=='qwen2.5:7b'
def test_ollama_to_lm_studio_fallback(tmp_path):
    reg=ModelRegistry();result=run(reg,FakeClient());p=tmp_path/'r.json';p.write_text(json.dumps(result));reg.load_results(p)
    client=FakeClient({('ollama','qwen2.5:7b')});response=invoke_role('coach',[{'role':'user','content':'test'}],reg,client)
    assert response['fallback_used'] is True and response['route'].provider=='lm_studio'
def test_all_routes_fail(tmp_path):
    reg=ModelRegistry();result=run(reg,FakeClient());p=tmp_path/'r.json';p.write_text(json.dumps(result));reg.load_results(p)
    with pytest.raises(RuntimeError):invoke_role('player',[],reg,FakeClient({('ollama','llama3.2:3b'),('lm_studio','llama3.2:3b')}))

def test_disabled_candidates_are_not_selected():
    reg=ModelRegistry()
    assert all(m.get('enabled',True) for m in reg.candidates('player'))

def test_unhealthy_forced_model_does_not_beat_healthy(tmp_path,monkeypatch):
    reg=ModelRegistry();result=run(reg,FakeClient());p=tmp_path/'r.json';p.write_text(json.dumps(result));reg.load_results(p)
    monkeypatch.setenv('COACH_MODEL','not-configured')
    assert reg.select('coach').provider=='ollama'
