from __future__ import annotations
import hashlib
from .schemas import Candidate,LeadPackage,LeadPackageSet,SwarmRole,Tier,TierSet
from .hierarchy import lead_package
ROLES=list(SwarmRole)
def _players(dashboard,club_id):
    matches=dashboard.get("matches",[]); latest=matches[0] if matches else {}
    block=next((v for k,v in latest.get("players",{}).items() if str(k)==str(club_id)),{})
    if not block:
        members=dashboard.get("members",{})
        if isinstance(members,list): block={str(x.get("name") or x.get("gamertag") or f"player-{i}"):x for i,x in enumerate(members)}
        elif isinstance(members,dict): block=members
    return block or {"unassigned":{"rating":0}}
def _score(stats):
    a=max(int(stats.get("passattempts",stats.get("passAttempts",0)) or 0),1);m=int(stats.get("passesmade",stats.get("passesMade",0)) or 0)
    return max(0,min(100,round(float(stats.get("rating",0) or 0)*10+int(stats.get("goals",0) or 0)*5+int(stats.get("assists",0) or 0)*3+(m/a)*5,2)))
def build_lead_packages(dashboard,club_id,reports):
    observations=sorted(((_score(v),k,v) for k,v in _players(dashboard,club_id).items()),reverse=True)
    expanded=(observations*((9+len(observations)-1)//len(observations)))[:9]; result={}
    for role in ROLES:
        groups={Tier.best:expanded[:3],Tier.middle:expanded[3:6],Tier.lowest:expanded[6:9]}; data={}
        for tier,items in groups.items():
            cs=[]
            for idx,(score,player,stats) in enumerate(items):
                cid=hashlib.sha256(f"{role.value}:{tier.value}:{idx}:{player}".encode()).hexdigest()[:20]
                evidence=[f"rating={stats.get('rating',0)}",f"goals={stats.get('goals',0)}",f"assists={stats.get('assists',0)}"]
                action=f"{role.value} advisory for {player}: {reports.get(role.value,'Review evidence before action')}"
                cs.append(Candidate(candidate_id=cid,player_id=str(player),owner=role,tier=tier,score=score,rationale=f"Deterministic player score {score}; role report reviewed.",action=action[:4000],evidence_refs=evidence))
            data[tier.value]=cs
        result[role.value]=lead_package(role,TierSet(**data))
    return LeadPackageSet(**result)
