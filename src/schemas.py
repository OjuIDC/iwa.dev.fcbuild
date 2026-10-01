from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, model_validator
class SwarmRole(str,Enum): scout="scout"; analyst="analyst"; coach="coach"; tactics="tactics"; director="director"
class Tier(str,Enum): best="best"; middle="middle"; lowest="lowest"
class Selection(str,Enum): best="best"; best_runner_up="best_runner_up"; middle="middle"; middle_runner_up="middle_runner_up"; lowest="lowest"; ignore="ignore"
class PlayerObservation(BaseModel): player_id:str=Field(min_length=1,max_length=80); match_id:str; metrics:dict[str,float|int|str]; evidence:list[str]=Field(min_length=1); observer_agent:str
class Candidate(BaseModel): candidate_id:str; player_id:str; owner:SwarmRole; tier:Tier; score:float=Field(ge=0,le=100); rationale:str=Field(min_length=1,max_length=4000); action:str=Field(min_length=1,max_length=4000); evidence_refs:list[str]=Field(min_length=1)
class TierSet(BaseModel): best:list[Candidate]=Field(min_length=3,max_length=3); middle:list[Candidate]=Field(min_length=3,max_length=3); lowest:list[Candidate]=Field(min_length=3,max_length=3)
class LeadPackage(BaseModel): owner:SwarmRole; best:Candidate; best_runner_up:Candidate; middle:Candidate; middle_runner_up:Candidate; lowest:Candidate; ignore:Candidate
class DispatchReceipt(BaseModel): request_id:str; candidate_id:str; player_id:str; owner:SwarmRole; approved_by:str; dispatched_at:int; instruction_hash:str

class LeadPackageSet(BaseModel):
    scout: LeadPackage
    analyst: LeadPackage
    coach: LeadPackage
    tactics: LeadPackage
    director: LeadPackage

class DecisionKind(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


_THREAD_ID_PATTERN = r"^[A-Za-z0-9-]{1,128}$"
_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


class DecisionIntent(BaseModel):
    """What the Commissioner wants to decide. Never carries identity: the
    authenticated credential, not the body, determines who approves."""

    model_config = ConfigDict(extra="forbid")

    decision: DecisionKind
    owner: SwarmRole | None = None
    selection: Selection | None = None
    candidate_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    player_id: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _approval_needs_exact_selection(self):
        fields = (self.owner, self.selection, self.candidate_id, self.player_id)
        if self.decision is DecisionKind.APPROVE and not all(f is not None for f in fields):
            raise ValueError("APPROVE requires owner, selection, candidate_id and player_id")
        if self.decision is DecisionKind.REJECT and any(f is not None for f in fields):
            raise ValueError("REJECT applies to the whole thread and must not name a candidate")
        return self


class SignedDecision(DecisionIntent):
    """Decision payload issued by the decision-challenge endpoint and signed by
    the Commissioner with APPROVAL_SIGNING_SECRET (HMAC-SHA256)."""

    thread_id: str = Field(pattern=_THREAD_ID_PATTERN)
    package_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    issued_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _window(self):
        if self.expires_at <= self.issued_at:
            raise ValueError("invalid approval window")
        return self
