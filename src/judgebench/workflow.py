"""Persisted workflow state (§16.3).

- Transitions are compare-and-set on the current state, so a duplicate delivery or a second
  worker can't apply the same step twice.
- Each transition writes the audit event and the Telegram outbox row in the same transaction
  as the state change: the bot announces only what the DB recorded (§6.4).
- Work is claimed with a lease (FOR UPDATE SKIP LOCKED). A crashed worker's lease expires and
  another worker resumes; nothing depends on in-memory state surviving a restart.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from judgebench.db.models import AuditEvent, Notification, Submission
from judgebench.states import PIPELINE, TERMINAL, State, can_transition

__all__ = [
    "PIPELINE",
    "TERMINAL",
    "IllegalTransition",
    "StaleState",
    "State",
    "can_transition",
    "claim_next",
    "record_failure",
    "sweep_exhausted",
    "transition",
]

LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3


class IllegalTransition(Exception):
    pass


class StaleState(Exception):
    """The submission was not in the expected state: someone else already moved it."""


def transition(
    session: Session,
    submission_id: uuid.UUID,
    expected: State,
    target: State,
    *,
    message: str,
    actor: str = "system:worker",
    detail: dict[str, Any] | None = None,
) -> None:
    row = session.execute(
        select(Submission.tenant_id, Submission.event_id, Submission.last_successful_state).where(
            Submission.id == submission_id
        )
    ).one()
    if not can_transition(expected, target, last_successful=row.last_successful_state):
        raise IllegalTransition(f"{expected} -> {target}")

    values: dict[str, Any] = {"state": target, "lease_until": None}
    if target in PIPELINE:
        values |= {"last_successful_state": target, "error_category": None, "error_detail": None, "retryable": None}
    result = session.execute(
        update(Submission).where(Submission.id == submission_id, Submission.state == expected).values(**values)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise StaleState(f"{submission_id} not in {expected}")

    scope = {"tenant_id": row.tenant_id, "event_id": row.event_id, "submission_id": submission_id}
    session.add(
        AuditEvent(**scope, actor=actor, action=f"state:{target}", target=f"{expected}->{target}", detail=detail or {})
    )
    session.add(Notification(**scope, stage=target.value, text=message))


def claim_next(
    session: Session, state: State, *, max_attempts: int = MAX_ATTEMPTS, lease: timedelta = LEASE
) -> uuid.UUID | None:
    """Lease one submission in `state`. Commits, so the lease is visible to other workers."""
    claimed = session.execute(
        text("""
        UPDATE submissions SET attempts = attempts + 1, lease_until = now() + :lease
        WHERE id = (
            SELECT id FROM submissions
            WHERE state = :state AND attempts < :max
              AND (lease_until IS NULL OR lease_until < now())
            ORDER BY submitted_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
        RETURNING id
    """),
        {"state": state.value, "max": max_attempts, "lease": lease},
    ).scalar_one_or_none()
    session.commit()
    return claimed


def record_failure(
    session: Session,
    submission_id: uuid.UUID,
    category: str,
    *,
    retryable: bool,
    max_attempts: int = MAX_ATTEMPTS,
    detail: str | None = None,
) -> None:
    sub = session.execute(select(Submission.state, Submission.attempts).where(Submission.id == submission_id)).one()
    session.execute(
        update(Submission)
        .where(Submission.id == submission_id)
        .values(error_category=category, error_detail=(detail or "")[:2000], retryable=retryable, lease_until=None)
    )
    if retryable and sub.attempts < max_attempts:
        return  # stays in its state; next claim retries it
    reason = f"Failed: {category}" + ("" if retryable else " (not retryable)")
    transition(
        session,
        submission_id,
        sub.state,
        State.FAILED,
        message=reason,
        detail={"category": category, "attempts": sub.attempts},
    )


def sweep_exhausted(session: Session, state: State, *, max_attempts: int = MAX_ATTEMPTS) -> int:
    """Fail submissions whose workers crashed on every attempt (lease expired, attempts used up)."""
    ids = session.scalars(
        select(Submission.id).where(
            Submission.state == state, Submission.attempts >= max_attempts, Submission.lease_until < text("now()")
        )
    ).all()
    for sid in ids:
        record_failure(session, sid, "worker_lease_expired", retryable=False, max_attempts=max_attempts)
    return len(ids)
