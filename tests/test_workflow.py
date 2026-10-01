import pytest
from src.schemas import *
from src.hierarchy import lead_package
from src.approval import *
def c(owner,tier,n):return Candidate(candidate_id=f"{owner.value}-{tier.value}-{n}",player_id=f"p{n}",owner=owner,tier=tier,score=70,rationale="evidence based",action="advisory instruction",evidence_refs=["match:1"])
def package(owner=SwarmRole.coach):
    t=TierSet(best=[c(owner,Tier.best,i) for i in range(3)],middle=[c(owner,Tier.middle,i+3) for i in range(3)],lowest=[c(owner,Tier.lowest,i+6) for i in range(3)])
    return lead_package(owner,t)
def approval(pkg,now=1000):
    a=ApprovalRequest(request_id="r",thread_id="t",owner=pkg.owner,selection=Selection.best,candidate_id=pkg.best.candidate_id,player_id=pkg.best.player_id,commissioner="batmanL8",decision="APPROVE",issued_at=now-1,expires_at=now+60,nonce="unique")
    a.signature=sign(a,"secret");return a
def test_three_three_three_and_lead_reduction():
    p=package();assert p.best.candidate_id.endswith("-0") and p.ignore.candidate_id.endswith("-8")
def test_approved_dispatch():
    p=package();r=dispatch({"coach":p.model_dump()},approval(p),NonceStore(),"secret",1000);assert r.player_id==p.best.player_id
def test_replay_is_blocked():
    p=package();a=approval(p);store=NonceStore();dispatch({"coach":p.model_dump()},a,store,"secret",1000)
    with pytest.raises(PermissionError):dispatch({"coach":p.model_dump()},a,store,"secret",1000)
def test_tampering_is_blocked():
    p=package();a=approval(p);a.player_id="other"
    with pytest.raises(PermissionError):dispatch({"coach":p.model_dump()},a,NonceStore(),"secret",1000)
def test_expired_is_blocked():
    p=package();a=approval(p)
    with pytest.raises(PermissionError):dispatch({"coach":p.model_dump()},a,NonceStore(),"secret",2000)
def test_cross_swarm_injection_is_blocked():
    owner=SwarmRole.coach;t=TierSet(best=[c(SwarmRole.scout,Tier.best,i) for i in range(3)],middle=[c(owner,Tier.middle,i+3) for i in range(3)],lowest=[c(owner,Tier.lowest,i+6) for i in range(3)])
    with pytest.raises(ValueError):lead_package(owner,t)
