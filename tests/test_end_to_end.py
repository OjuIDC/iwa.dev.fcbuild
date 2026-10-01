import json,time
from src.benchmark import run
from src.model_selection import ModelRegistry,invoke_role
from src.schemas import *
from src.hierarchy import lead_package
from src.approval import ApprovalRequest,NonceStore,sign,dispatch
class FakeClient:
    def invoke(self,route,messages):return '{"evidence":"stat:rating","recommendation":"Commissioner approval required; do not approve automatically","player_id":"p1"}'
def c(owner,tier,n,action):return Candidate(candidate_id=f'{owner.value}-{tier.value}-{n}',player_id=f'p{n}',owner=owner,tier=tier,score=80-n,rationale='stat evidence',action=action,evidence_refs=['match:1'])
def test_full_benchmark_select_deliberate_approve_dispatch(tmp_path):
    reg=ModelRegistry();results=run(reg,FakeClient());path=tmp_path/'results.json';path.write_text(json.dumps(results));reg.load_results(path)
    response=invoke_role('coach',[{'role':'user','content':'make plan'}],reg,FakeClient());assert response['content']
    owner=SwarmRole.coach;tiers=TierSet(best=[c(owner,Tier.best,i,response['content']) for i in range(3)],middle=[c(owner,Tier.middle,i+3,'middle') for i in range(3)],lowest=[c(owner,Tier.lowest,i+6,'lowest') for i in range(3)])
    package=lead_package(owner,tiers);now=int(time.time());approval=ApprovalRequest(request_id='req',thread_id='thread',owner=owner,selection=Selection.best,candidate_id=package.best.candidate_id,player_id=package.best.player_id,commissioner='batmanL8',decision='APPROVE',issued_at=now-1,expires_at=now+60,nonce='once')
    approval.signature=sign(approval,'secret');receipt=dispatch({'coach':package.model_dump()},approval,NonceStore(),'secret',now)
    assert receipt.player_id==package.best.player_id and receipt.approved_by=='batmanL8'
