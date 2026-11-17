"""§16.3 state machine: pure transition rules + persisted, compare-and-set transitions."""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.workflow import (
    PIPELINE,
    TERMINAL,
    IllegalTransition,
    StaleState,
    State,
    can_transition,
    claim_next,
    record_failure,
    transition,
)
from tests.conftest import make_event, make_submission, make_team

# --- pure rules ----------------------------------------------------------------------


def test_happy_path_is_allowed() -> None:
    for a, b in zip(PIPELINE, PIPELINE[1:], strict=False):
        assert can_transition(a, b), (a, b)


@pytest.mark.parametrize("a,b", [
    (State.RECEIVED, State.EVALUATING),
    (State.PROFILED, State.READY_FOR_REVIEW),
    (State.READY_FOR_REVIEW, State.RECEIVED),
    (State.PROFILED, State.RECEIVED),
])
def test_skips_and_backwards_rejected(a: State, b: State) -> None:
    assert not can_transition(a, b)


def test_any_active_state_can_fail_or_cancel() -> None:
    for s in PIPELINE[:-1]:
        assert can_transition(s, State.FAILED)
        assert can_transition(s, State.CANCELLED)


def test_terminal_states_are_terminal() -> None:
    for t in TERMINAL:
        assert not any(can_transition(t, s) for s in State)


# --- persisted transitions ------------------------------------------------------------

@pytest.fixture
def sub(session: Session) -> m.Submission:
    s = make_submission(session, make_team(session, make_event(session, "ev"), "Alpha"))
    session.commit()
    return s


@pytest.mark.db
def test_transition_persists_state_audit_and_notification_atomically(session: Session, sub: m.Submission) -> None:
    transition(session, sub.id, State.RECEIVED, State.PROFILED, message="Profiled: Python")
    session.commit()
    session.refresh(sub)

    assert sub.state == State.PROFILED
    assert sub.last_successful_state == State.PROFILED
    assert session.query(m.AuditEvent).filter_by(action="state:PROFILED").count() == 1
    [note] = session.query(m.Notification).filter_by(submission_id=sub.id).all()
    assert note.stage == "PROFILED" and note.sent_at is None


@pytest.mark.db
def test_rollback_leaves_no_notification(session: Session, sub: m.Submission) -> None:
    transition(session, sub.id, State.RECEIVED, State.PROFILED, message="x")
    session.rollback()
    session.refresh(sub)
    assert sub.state == State.RECEIVED
    assert session.query(m.Notification).count() == 0


@pytest.mark.db
def test_duplicate_delivery_is_compare_and_set(session: Session, sub: m.Submission) -> None:
    transition(session, sub.id, State.RECEIVED, State.PROFILED, message="x")
    session.commit()
    with pytest.raises(StaleState):
        transition(session, sub.id, State.RECEIVED, State.PROFILED, message="x")
    session.rollback()
    assert session.query(m.Notification).count() == 1


@pytest.mark.db
def test_illegal_transition_rejected(session: Session, sub: m.Submission) -> None:
    with pytest.raises(IllegalTransition):
        transition(session, sub.id, State.RECEIVED, State.READY_FOR_REVIEW, message="x")


@pytest.mark.db
def test_claim_is_exclusive_and_counts_attempts(session_factory: sessionmaker[Session], sub: m.Submission) -> None:
    with session_factory() as s1, session_factory() as s2:
        first = claim_next(s1, State.RECEIVED)
        second = claim_next(s2, State.RECEIVED)
    assert first == sub.id
    assert second is None  # leased by the first worker
    with session_factory() as s:
        assert s.get(m.Submission, sub.id).attempts == 1  # type: ignore[union-attr]


@pytest.mark.db
def test_crashed_worker_lease_expires_and_work_resumes(session_factory: sessionmaker[Session], sub: m.Submission) -> None:
    with session_factory() as s:
        assert claim_next(s, State.RECEIVED) == sub.id
        # worker "crashes": never transitions. Fast-forward the lease.
        s.query(m.Submission).filter_by(id=sub.id).update(
            {"lease_until": m.utcnow() - timedelta(seconds=1)})
        s.commit()
    with session_factory() as s:
        assert claim_next(s, State.RECEIVED) == sub.id
        assert s.get(m.Submission, sub.id).attempts == 2  # type: ignore[union-attr]


@pytest.mark.db
def test_retryable_failure_releases_then_exhausts(session: Session, sub: m.Submission) -> None:
    for attempt in range(1, 4):
        assert claim_next(session, State.RECEIVED, max_attempts=3) == sub.id
        record_failure(session, sub.id, "github_unavailable", retryable=True, max_attempts=3)
        session.commit()
        session.refresh(sub)
        expected = State.FAILED if attempt == 3 else State.RECEIVED
        assert sub.state == expected, attempt
    assert sub.error_category == "github_unavailable"
    assert claim_next(session, State.RECEIVED) is None


@pytest.mark.db
def test_non_retryable_failure_fails_immediately(session: Session, sub: m.Submission) -> None:
    claim_next(session, State.RECEIVED)
    record_failure(session, sub.id, "repo_not_found", retryable=False, max_attempts=3)
    session.commit()
    session.refresh(sub)
    assert sub.state == State.FAILED and sub.retryable is False
    assert session.query(m.Notification).filter_by(stage="FAILED").count() == 1


@pytest.mark.db
def test_resume_from_failed_goes_to_last_successful_state(session: Session, sub: m.Submission) -> None:
    transition(session, sub.id, State.RECEIVED, State.PROFILED, message="x")
    transition(session, sub.id, State.PROFILED, State.FAILED, message="boom")
    with pytest.raises(IllegalTransition):
        transition(session, sub.id, State.FAILED, State.RECEIVED, message="resume")
    transition(session, sub.id, State.FAILED, State.PROFILED, message="resume")
    session.commit()
    session.refresh(sub)
    assert sub.state == State.PROFILED


@pytest.mark.db
def test_sweep_fails_submission_whose_worker_crashed_every_attempt(session: Session, sub: m.Submission) -> None:
    from judgebench.workflow import sweep_exhausted

    session.query(m.Submission).filter_by(id=sub.id).update(
        {"attempts": 3, "lease_until": m.utcnow() - timedelta(seconds=1)})
    session.commit()
    assert sweep_exhausted(session, State.RECEIVED, max_attempts=3) == 1
    session.commit()
    session.refresh(sub)
    assert sub.state == State.FAILED and sub.error_category == "worker_lease_expired"
