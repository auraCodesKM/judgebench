"""Overall confidence is derived from the criteria, and flags can only lower it."""

import pytest

from judgebench.agents.scoring import Score, overall_confidence


def s(conf: float, mx: float = 20, flagged: bool = False) -> Score:
    return Score("c", "C", mx, mx / 2, "r", [], conf, flagged=flagged)


@pytest.mark.parametrize(
    "scores,flags,want",
    [
        ([s(0.1)] * 6, [], "low"),  # every criterion 0.1 -> never "high" (the reported bug)
        ([s(0.8)] * 6, [], "high"),
        ([s(0.5)] * 6, [], "medium"),
        ([s(0.9, 80), s(0.1, 20)], [], "high"),  # weighted by max score: 0.74
        ([s(0.9, 20), s(0.1, 80)], [], "low"),  # 0.26
        ([s(0.8), s(0.8, flagged=True)], [], "medium"),  # a flag caps at medium
        ([s(0.8)] * 2, ["a", "b", "c"], "low"),  # 3+ flags -> low
        ([s(0.1, flagged=True)], [], "low"),  # flags never raise
    ],
)
def test_overall_confidence(scores: list[Score], flags: list[str], want: str) -> None:
    assert overall_confidence(scores, flags) == want


def test_confidence_is_computed_from_cited_evidence_not_self_report() -> None:
    import uuid

    from judgebench.agents.scoring import evidence_confidence
    from judgebench.agents.views import EvidenceView

    def e(i: str, status: str, src: str = "code", behavioural: bool = False) -> EvidenceView:
        return EvidenceView(i, uuid.uuid4(), "k.x", src, status, "loc", "obs", behavioural, None)

    ev = {
        "E1": e("E1", "VERIFIED", "ci", behavioural=True),
        "E2": e("E2", "OBSERVED"),
        "E3": e("E3", "MISSING"),
        "E4": e("E4", "INFERRED"),
    }
    assert evidence_confidence([], ev) == 0.1  # nothing cited: low, whatever the model claims
    assert evidence_confidence(["E9"], ev) == 0.1  # invented IDs don't count
    assert evidence_confidence(["E1", "E2"], ev) == 1.0  # behaviour-verified + two source types
    assert evidence_confidence(["E2"], ev) == 0.7
    assert evidence_confidence(["E3", "E4"], ev) == 0.35  # only absences and inferences: low


def test_no_behavioural_evidence_caps_overall_at_medium() -> None:
    assert overall_confidence([s(0.9)] * 6, [], behavioural=False) == "medium"
    assert overall_confidence([s(0.9)] * 6, [], behavioural=True) == "high"
