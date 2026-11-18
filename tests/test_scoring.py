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
