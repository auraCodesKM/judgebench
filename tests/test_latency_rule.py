"""A speed claim against a slow successful live sample query is CONTRADICTED, deterministically."""

import uuid

from judgebench.agents.team import latency_rule
from judgebench.agents.views import ClaimView, EvidenceView


def ev(obs: str, key: str = "live.sample_query") -> EvidenceView:
    return EvidenceView("E7", uuid.uuid4(), key, "live_check", "VERIFIED", "live:POST /ask", obs, True, None)


def claim(text: str) -> ClaimView:
    return ClaimView("C1", uuid.uuid4(), text, "readme", "performance")


def test_slow_answer_contradicts_a_real_time_claim() -> None:
    v = {"claim_id": "C1", "status": "UNVERIFIED", "evidence_ids": ["E2"], "rationale": "unclear"}
    out = latency_rule(claim("answers in real time"), v, {"_evidence": [ev("POST /ask -> 200 in 8528 ms (json)")]})
    assert out["status"] == "CONTRADICTED" and out["evidence_ids"] == ["E2", "E7"]
    assert (
        out["rationale"]
        == "Rule: their app took 8.5 s to answer when I tried it. (The model alone had said unverified.)"
    )


def test_rule_does_not_fire_without_cause() -> None:
    v = {"claim_id": "C1", "status": "UNVERIFIED", "evidence_ids": [], "rationale": "x"}
    fast = {"_evidence": [ev("POST /ask -> 200 in 527 ms (json)")]}
    failed = {"_evidence": [ev("POST /ask -> 500 in 9000 ms (json)")]}
    slow = {"_evidence": [ev("POST /ask -> 200 in 9000 ms (json)")]}
    assert latency_rule(claim("answers in real time"), v, fast) == v  # fast enough
    assert latency_rule(claim("answers in real time"), v, failed) == v  # errored: not a latency measurement
    assert latency_rule(claim("98% accuracy"), v, slow) == v  # not a speed claim
