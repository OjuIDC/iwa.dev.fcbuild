from __future__ import annotations
from .schemas import Candidate,TierSet,LeadPackage,SwarmRole
def validate_tiers(owner:SwarmRole,tiers:TierSet):
    ids=[]
    for group in (tiers.best,tiers.middle,tiers.lowest):
        for c in group:
            if c.owner!=owner:raise ValueError("cross-swarm candidate injection")
            ids.append(c.candidate_id)
    if len(ids)!=len(set(ids)):raise ValueError("duplicate candidate IDs")
def lead_package(owner:SwarmRole,tiers:TierSet)->LeadPackage:
    validate_tiers(owner,tiers)
    return LeadPackage(owner=owner,best=tiers.best[0],best_runner_up=tiers.best[1],middle=tiers.middle[0],middle_runner_up=tiers.middle[1],lowest=tiers.lowest[0],ignore=tiers.lowest[2])
