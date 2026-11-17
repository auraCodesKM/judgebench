"""§14.2 row: cross-event access. M1 proves it at the event-scoped query layer and the
Telegram authorisation stub; the full Telegram Copilot path is M3."""

import pytest
from sqlalchemy.orm import Session

from judgebench.db import models as m
from judgebench.db.scope import AccessDenied, scope_for_telegram_user
from judgebench.telegram.auth import authorise
from tests.conftest import make_event, make_judge, make_submission, make_team

pytestmark = pytest.mark.db

JUDGE_B = 2002


@pytest.fixture
def two_events(session: Session) -> tuple[m.Event, m.Event]:
    a, b = make_event(session, "event-a"), make_event(session, "event-b")
    make_submission(session, make_team(session, a, "Alpha"))
    make_submission(session, make_team(session, b, "Bravo"), response_id="r2")
    make_judge(session, b, JUDGE_B)
    session.commit()
    return a, b


def denials(s: Session) -> list[m.AuditEvent]:
    return s.query(m.AuditEvent).filter_by(action="access_denied").all()


def test_scope_derived_from_membership_only(session: Session, two_events: tuple[m.Event, m.Event]) -> None:
    a, b = two_events
    scope = scope_for_telegram_user(session, JUDGE_B)
    assert scope.event_ids == frozenset({b.id})


def test_judge_of_b_cannot_find_team_in_a(session: Session, two_events: tuple[m.Event, m.Event]) -> None:
    scope = scope_for_telegram_user(session, JUDGE_B)
    assert scope.find_team(session, "Alpha") is None
    assert scope.find_team(session, "Bravo") is not None
    assert all(sub.event_id in scope.event_ids for sub in scope.submissions(session))


def test_explicit_event_a_is_denied_and_logged(session: Session, two_events: tuple[m.Event, m.Event]) -> None:
    a, _ = two_events
    with pytest.raises(AccessDenied):
        authorise(session, telegram_user_id=JUDGE_B, event_id=a.id, action="/team Alpha")
    session.commit()
    [denied] = denials(session)
    assert denied.event_id == a.id and denied.actor == f"telegram:{JUDGE_B}"


def test_unknown_telegram_user_denied_and_logged(session: Session, two_events: tuple[m.Event, m.Event]) -> None:
    _, b = two_events
    with pytest.raises(AccessDenied):
        authorise(session, telegram_user_id=999, event_id=b.id, action="/leaderboard")
    session.commit()
    assert len(denials(session)) == 1


def test_removed_judge_loses_access_immediately(session: Session, two_events: tuple[m.Event, m.Event]) -> None:
    _, b = two_events
    session.query(m.JudgeMembership).filter_by(telegram_user_id=JUDGE_B).update({"status": "removed"})
    session.commit()
    assert scope_for_telegram_user(session, JUDGE_B).event_ids == frozenset()
    with pytest.raises(AccessDenied):
        authorise(session, telegram_user_id=JUDGE_B, event_id=b.id, action="/team Bravo")


def test_composite_fk_blocks_cross_event_submission(session: Session, two_events: tuple[m.Event, m.Event]) -> None:
    from sqlalchemy.exc import IntegrityError

    _, b = two_events
    alpha = session.query(m.Team).filter_by(name="Alpha").one()
    session.add(
        m.Submission(
            tenant_id=b.tenant_id,
            event_id=b.id,
            team_id=alpha.id,
            form_response_id="evil",
            version=1,
            repo_url="https://github.com/x/y",
            form_answers={},
            submitted_at=m.utcnow(),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.xfail(reason="M3: Telegram Judge Copilot webhook not built yet", strict=True)
def test_copilot_webhook_denies_cross_event_question() -> None:
    from judgebench.telegram.app import app  # type: ignore[attr-defined]  # noqa: F401

    raise AssertionError("implement in M3: POST webhook '/team Alpha' as judge of B -> denied + logged")
