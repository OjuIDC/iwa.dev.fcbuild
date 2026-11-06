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
def test_ollama_to_lm_studio_fallback(tmp_path,monkeypatch):
    monkeypatch.setenv('LM_STUDIO_FALLBACK_ENABLED','true');reg=ModelRegistry();result=run(reg,FakeClient());p=tmp_path/'r.json';p.write_text(json.dumps(result));reg.load_results(p)
    client=FakeClient({('ollama','qwen2.5:7b')});response=invoke_role('coach',[{'role':'user','content':'test'}],reg,client)
    assert response['fallback_used'] is True and response['route'].provider=='lm_studio'
def test_all_routes_fail(tmp_path,monkeypatch):
    monkeypatch.setenv('LM_STUDIO_FALLBACK_ENABLED','true');reg=ModelRegistry();result=run(reg,FakeClient());p=tmp_path/'r.json';p.write_text(json.dumps(result));reg.load_results(p)
    with pytest.raises(RuntimeError):invoke_role('player',[],reg,FakeClient({('ollama','llama3.2:3b'),('lm_studio','llama3.2:3b')}))

def test_disabled_catalog_entries_are_excluded(tmp_path):
    cat=json.loads(Path('config/model_catalog.json').read_text())
    for m in cat['models']:
        if 'coach' in m['roles'] and m['provider']=='ollama': m['enabled']=False
    p=tmp_path/'cat.json';p.write_text(json.dumps(cat))
    reg=ModelRegistry(catalog_path=p)
    assert all(m['provider']!='ollama' for m in reg.candidates('coach'))

def test_unhealthy_forced_model_does_not_beat_healthy(tmp_path,monkeypatch):
    monkeypatch.setenv('LM_STUDIO_FALLBACK_ENABLED','true')
    reg=ModelRegistry();client=FakeClient({('lm_studio','qwen2.5:7b')})
    # lm_studio::qwen2.5:7b fails its benchmark (unhealthy); forcing it must not beat healthy ollama.
    result=run(reg,client);p=tmp_path/'r.json';p.write_text(json.dumps(result));reg.load_results(p)
    assert not reg.healthy(next(m for m in reg.candidates('coach') if m['provider']=='lm_studio'),'coach')
    monkeypatch.setenv('COACH_MODEL','qwen2.5:7b')
    assert reg.select('coach').provider=='ollama'

def test_lm_studio_is_not_called_when_disabled(tmp_path,monkeypatch):
    monkeypatch.delenv('LM_STUDIO_FALLBACK_ENABLED',raising=False)
    reg=ModelRegistry();result=run(reg,FakeClient());p=tmp_path/'r.json';p.write_text(json.dumps(result));reg.load_results(p)
    client=FakeClient({('ollama','qwen2.5:7b')})
    with pytest.raises(RuntimeError):invoke_role('coach',[{'role':'user','content':'x'}],reg,client)
    assert all(provider!='lm_studio' for provider,_ in client.calls)
    for value in ('false','0','no'):
        monkeypatch.setenv('LM_STUDIO_FALLBACK_ENABLED',value);assert not ModelRegistry().lm_studio_enabled

def test_invalid_lm_studio_flag_fails_clearly(monkeypatch):
    from src.settings import ConfigError
    monkeypatch.setenv('LM_STUDIO_FALLBACK_ENABLED','maybe')
    with pytest.raises(ConfigError):ModelRegistry()


def test_ollama_base_url_defaults_to_remote_lab_server(monkeypatch):
    monkeypatch.delenv('OLLAMA_BASE_URL', raising=False)
    assert ModelRegistry().select('analyst').base_url == 'http://araba.ojuidc.olutech.systems:11434'


def test_ollama_base_url_env_override(monkeypatch):
    monkeypatch.setenv('OLLAMA_BASE_URL', 'http://ollama:11434')
    reg = ModelRegistry()
    assert reg.select('analyst').base_url == 'http://ollama:11434'
    assert all(r.base_url == 'http://ollama:11434' for r in reg.fallback_chain('coach') if r.provider == 'ollama')
