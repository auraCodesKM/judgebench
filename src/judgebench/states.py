"""Submission state machine rules (§16.3). Pure: no DB. Persistence lives in workflow.py."""

from enum import StrEnum


class State(StrEnum):
    RECEIVED = "RECEIVED"
    PROFILED = "PROFILED"
    COLLECTING_EVIDENCE = "COLLECTING_EVIDENCE"
    EVALUATING = "EVALUATING"
    CRITIQUING = "CRITIQUING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    REVIEWED = "REVIEWED"
    PARTIAL = "PARTIAL"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


PIPELINE: tuple[State, ...] = (
    State.RECEIVED, State.PROFILED, State.COLLECTING_EVIDENCE, State.EVALUATING,
    State.CRITIQUING, State.READY_FOR_REVIEW, State.REVIEWED,
)
# FAILED is not terminal: it can resume to the last successful state.
TERMINAL = frozenset({State.REVIEWED, State.CANCELLED})
_ACTIVE = frozenset(PIPELINE[:-1])
_RESUMABLE = frozenset(PIPELINE[:-2])  # anything before READY_FOR_REVIEW

_ALLOWED: dict[State, frozenset[State]] = {
    **{a: frozenset({b}) for a, b in zip(PIPELINE, PIPELINE[1:], strict=False)},
    State.REVIEWED: frozenset(),
    State.CANCELLED: frozenset(),
    State.PARTIAL: frozenset({State.READY_FOR_REVIEW, State.REQUIRES_HUMAN}),
    State.REQUIRES_HUMAN: frozenset({State.REVIEWED}),
    State.FAILED: _RESUMABLE,
}
for _s in _ACTIVE:
    _ALLOWED[_s] |= {State.FAILED, State.CANCELLED, State.PARTIAL, State.REQUIRES_HUMAN}
for _s in (State.PARTIAL, State.REQUIRES_HUMAN, State.FAILED):
    _ALLOWED[_s] |= {State.CANCELLED}


def can_transition(current: State, target: State, *, last_successful: State | None = None) -> bool:
    if target not in _ALLOWED[current]:
        return False
    if current is State.FAILED and target in _RESUMABLE:
        return target == (last_successful or State.RECEIVED)
    return True
