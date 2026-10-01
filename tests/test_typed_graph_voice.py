import os
os.environ.setdefault('VOICE_API_TOKEN','voice-secret');os.environ.setdefault('GRAPH_SERVICE_TOKEN','graph-service-secret')
from src.graph import initial_state,run_pipeline
from src.voice_gateway import parse_command
class R:pass
def dashboard():
 players={f'p{i}':{'rating':9-i*.3,'goals':i%3,'assists':i%2,'passattempts':20,'passesmade':15} for i in range(9)}
 return {'matches':[{'players':{'330323':players}}],'members':{},'overall':{}}
def test_graph_persists_typed_lead_packages(monkeypatch):
 monkeypatch.setattr('src.graph.ask',lambda role,system,data,registry=None:f'{role} recommendation')
 state=run_pipeline(initial_state('fc27-330323-run-20261001-001'),registry=R(),dashboard=dashboard())
 assert state['status']=='awaiting_approval'
 assert set(state['lead_packages'])=={'scout','analyst','coach','tactics','director'}
 for package in state['lead_packages'].values():assert set(package)=={'owner','best','best_runner_up','middle','middle_runner_up','lowest','ignore'}
def test_voice_approval_is_never_executed():
 result=parse_command('approve the coach best recommendation now')
 assert result['safe'] is False and result['confirmation_required'] is True and result['intent']=='approval_attempt'
def test_voice_reject_and_execute_are_blocked():
 for phrase in ('reject thread abc','execute the tactics package','activate training'):
  assert parse_command(phrase)['safe'] is False
def test_voice_read_only_thread_command():
 result=parse_command('read status for thread demo-123')
 assert result['safe'] is True and result['intent']=='read_thread' and result['thread_id']=='demo-123'
