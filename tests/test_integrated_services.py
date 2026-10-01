import json
from src.graph import initial_state,run_pipeline,decide
from src.post_match_rundown import _parse_match
class Reg: pass
class FakeRegistry: pass
def test_match_parse_handles_string_keys():
 m={'matchId':'1','clubs':{'330323':{'goals':2,'goalsAgainst':1,'result':'win'},'9':{'name':'Opp'}},'players':{'330323':{}}}
 assert _parse_match(m,'330323')['result']=='WIN'
def test_graph_flow(monkeypatch):
 monkeypatch.setattr('src.graph.ask',lambda role,system,data,registry=None:f'{role} advisory Commissioner approval required')
 state=run_pipeline(initial_state('t'),registry=FakeRegistry(),dashboard={'overall':{},'matches':[],'members':{}})
 assert state['status']=='awaiting_approval' and len(state['reports'])==5
 item=state['lead_packages']['coach']['best']
 state=decide(state,'APPROVE','ok','coach','best',item['candidate_id'],item['player_id']);assert state['approved'] is True
