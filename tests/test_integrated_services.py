import json
from src.graph import initial_state,run_pipeline
from src.post_match_rundown import _parse_match
class Reg: pass
class FakeRegistry: pass
def test_match_parse_handles_string_keys():
 m={'matchId':'1','clubs':{'330323':{'goals':2,'goalsAgainst':1,'result':'win'},'9':{'name':'Opp'}},'players':{'330323':{}}}
 assert _parse_match(m,'330323')['result']=='WIN'
def test_graph_flow(monkeypatch):
 monkeypatch.setattr('src.graph.ask',lambda role,system,data,registry=None:f'{role} advisory Commissioner approval required')
 state=run_pipeline(initial_state('fc27-330323-run-20261001-001'),registry=FakeRegistry(),dashboard={'overall':{},'matches':[],'members':{}})
 assert state['status']=='awaiting_approval' and len(state['reports'])==5
