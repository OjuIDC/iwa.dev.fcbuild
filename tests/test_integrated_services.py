import json
from src.post_match_rundown import _parse_match
class Reg: pass
class FakeRegistry: pass
def test_match_parse_handles_string_keys():
 m={'matchId':'1','clubs':{'330323':{'goals':2,'goalsAgainst':1,'result':'win'},'9':{'name':'Opp'}},'players':{'330323':{}}}
 assert _parse_match(m,'330323')['result']=='WIN'
