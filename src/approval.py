from __future__ import annotations
import hashlib,hmac,json,os,time
from .schemas import ApprovalRequest,DispatchReceipt,LeadPackage
class NonceStore:
    def __init__(self):self.used=set()
    def consume(self,nonce):
        if nonce in self.used:raise PermissionError("approval replay detected")
        self.used.add(nonce)
def canonical(a:ApprovalRequest):return a.model_dump(exclude={"signature"})
def sign(a:ApprovalRequest,secret=None):
    key=(secret or os.environ["APPROVAL_SIGNING_SECRET"]).encode();body=json.dumps(canonical(a),sort_keys=True,separators=(",",":")).encode()
    return hmac.new(key,body,hashlib.sha256).hexdigest()
def verify(a:ApprovalRequest,nonce_store:NonceStore,secret=None,now=None):
    now=int(time.time() if now is None else now)
    if not (a.issued_at <= now <= a.expires_at):raise PermissionError("approval expired or not active")
    expected=sign(a,secret)
    if not hmac.compare_digest(a.signature,expected):raise PermissionError("invalid approval signature")
    nonce_store.consume(a.nonce)
def dispatch(packages:dict,approval:ApprovalRequest,nonce_store:NonceStore,secret=None,now=None):
    verify(approval,nonce_store,secret,now)
    package=LeadPackage.model_validate(packages[approval.owner.value]);candidate=getattr(package,approval.selection.value)
    if candidate.candidate_id!=approval.candidate_id or candidate.player_id!=approval.player_id:raise PermissionError("approval does not match selected recommendation")
    stamp=int(time.time() if now is None else now);digest=hashlib.sha256(candidate.action.encode()).hexdigest()
    return DispatchReceipt(request_id=approval.request_id,candidate_id=candidate.candidate_id,player_id=candidate.player_id,owner=approval.owner,approved_by=approval.commissioner,dispatched_at=stamp,instruction_hash=digest)
