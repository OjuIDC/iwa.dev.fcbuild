import pytest
from src.schemas import *
from src.hierarchy import lead_package
from src import approval


def c(owner, tier, n):
    return Candidate(candidate_id=f"{owner.value}-{tier.value}-{n}", player_id=f"p{n}", owner=owner, tier=tier,
                     score=70, rationale="evidence based", action="advisory instruction", evidence_refs=["match:1"])


def package(owner=SwarmRole.coach):
    t = TierSet(best=[c(owner, Tier.best, i) for i in range(3)], middle=[c(owner, Tier.middle, i + 3) for i in range(3)],
                lowest=[c(owner, Tier.lowest, i + 6) for i in range(3)])
    return lead_package(owner, t)


PAYLOAD = {"thread_id": "fc27-330323-run-20261001-001", "decision": "APPROVE", "owner": "coach", "selection": "best",
           "candidate_id": "coach-best-0", "player_id": "p0", "notes": "", "package_version": "a" * 64,
           "nonce": "n" * 43, "issued_at": 1000, "expires_at": 1120}
SECRET = "unit-test-secret-0123456789abcdef0123456789"


def test_three_three_three_and_lead_reduction():
    p = package()
    assert p.best.candidate_id.endswith("-0") and p.ignore.candidate_id.endswith("-8")


def test_cross_swarm_injection_is_blocked():
    owner = SwarmRole.coach
    t = TierSet(best=[c(SwarmRole.scout, Tier.best, i) for i in range(3)], middle=[c(owner, Tier.middle, i + 3) for i in range(3)],
                lowest=[c(owner, Tier.lowest, i + 6) for i in range(3)])
    with pytest.raises(ValueError):
        lead_package(owner, t)


def test_signature_roundtrip_and_every_field_is_bound():
    sig = approval.sign(PAYLOAD, SECRET)
    approval.verify_signature(PAYLOAD, sig, SECRET)
    for field in approval.SIGNED_FIELDS:
        tampered = {**PAYLOAD, field: "tampered" if isinstance(PAYLOAD[field], str) else PAYLOAD[field] + 1}
        with pytest.raises(approval.ApprovalSignatureError):
            approval.verify_signature(tampered, sig, SECRET)


def test_short_or_missing_secret_is_refused():
    with pytest.raises(approval.ApprovalSignatureError):
        approval.sign(PAYLOAD, "short")


def test_canonical_form_is_key_order_independent():
    assert approval.canonical_bytes(dict(reversed(list(PAYLOAD.items())))) == approval.canonical_bytes(PAYLOAD)


def test_decision_intent_rules():
    with pytest.raises(ValueError):
        DecisionIntent(decision="APPROVE", owner="coach")
    with pytest.raises(ValueError):
        DecisionIntent(decision="REJECT", owner="coach", selection="best", candidate_id="x", player_id="p")
    with pytest.raises(ValueError):
        DecisionIntent.model_validate({"decision": "REJECT", "commissioner": "batmanL8"})
