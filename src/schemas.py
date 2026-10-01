from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field, model_validator
class SwarmRole(str,Enum): scout="scout"; analyst="analyst"; coach="coach"; tactics="tactics"; director="director"
class Tier(str,Enum): best="best"; middle="middle"; lowest="lowest"
class Selection(str,Enum): best="best"; best_runner_up="best_runner_up"; middle="middle"; middle_runner_up="middle_runner_up"; lowest="lowest"; ignore="ignore"
class PlayerObservation(BaseModel): player_id:str=Field(min_length=1,max_length=80); match_id:str; metrics:dict[str,float|int|str]; evidence:list[str]=Field(min_length=1); observer_agent:str
class Candidate(BaseModel): candidate_id:str; player_id:str; owner:SwarmRole; tier:Tier; score:float=Field(ge=0,le=100); rationale:str=Field(min_length=1,max_length=4000); action:str=Field(min_length=1,max_length=4000); evidence_refs:list[str]=Field(min_length=1)
class TierSet(BaseModel): best:list[Candidate]=Field(min_length=3,max_length=3); middle:list[Candidate]=Field(min_length=3,max_length=3); lowest:list[Candidate]=Field(min_length=3,max_length=3)
class LeadPackage(BaseModel): owner:SwarmRole; best:Candidate; best_runner_up:Candidate; middle:Candidate; middle_runner_up:Candidate; lowest:Candidate; ignore:Candidate
class ApprovalRequest(BaseModel):
    request_id: str
    thread_id: str
    owner: SwarmRole
    selection: Selection
    candidate_id: str
    player_id: str
    commissioner: str
    decision: str
    issued_at: int
    expires_at: int
    nonce: str
    signature: str = ""

    @model_validator(mode="after")
    def validate_window(self):
        if self.decision != "APPROVE": raise ValueError("decision must be APPROVE")
        if self.expires_at <= self.issued_at: raise ValueError("invalid approval window")
        return self
class DispatchReceipt(BaseModel): request_id:str; candidate_id:str; player_id:str; owner:SwarmRole; approved_by:str; dispatched_at:int; instruction_hash:str

class LeadPackageSet(BaseModel):
    scout: LeadPackage
    analyst: LeadPackage
    coach: LeadPackage
    tactics: LeadPackage
    director: LeadPackage

class HumanDecision(BaseModel):
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    commissioner: str = Field(min_length=1,max_length=80)
    owner: SwarmRole
    selection: Selection
    candidate_id: str
    player_id: str
    notes: str = Field(default="",max_length=2000)
