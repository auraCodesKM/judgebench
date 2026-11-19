"""reset-demo wipes only demo events; preflight's quota estimate counts our own calls since PT midnight."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.demo import Preflight, calls_today, event_empty, quota_check, reset_demo
from tests import fixtures_repos as repos
from tests.fake_llm import fake
from tests.test_m2_flow import deps, drive, submit

pytestmark = pytest.mark.db


def test_reset_demo_empties_the_event(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "Team Beta")
    event = session.get_one(m.Event, sub.event_id)
    event.slug, event.telegram_chat_id = "demo-test", -5
    session.add(
        m.EventIntegration(
            tenant_id=event.tenant_id,
            event_id=event.id,
            provider="google_sheets",
            external_ids={"spreadsheet_id": "S"},
            cursor={"tabs": {"x": 1}},
        )
    )
    session.add(
        m.EventIntegration(
            tenant_id=event.tenant_id,
            event_id=event.id,
            provider="google_forms",
            external_ids={},
            cursor={"since": "2026-01-01T00:00:00Z"},
        )
    )
    session.commit()
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    for i, n in enumerate(session.query(m.Notification).all()):
        n.telegram_message_id = i + 1
    session.add(
        m.Notification(
            tenant_id=event.tenant_id,
            event_id=event.id,
            stage="EVENT_READY",
            text="Event ready",
            telegram_message_id=999,
        )
    )
    session.commit()
    deleted, cleared = [], []
    r = reset_demo(session, event, lambda c, mid: deleted.append(mid) or True, cleared.append)
    session.commit()
    assert r.teams == 1 and 999 in deleted and cleared == ["S"]
    assert session.query(m.Team).filter_by(event_id=event.id).count() == 0
    assert session.query(m.Notification).filter_by(event_id=event.id).count() == 0
    forms = session.query(m.EventIntegration).filter_by(provider="google_forms").one()
    assert forms.cursor["since"] > "2026-11"  # moved to now: older responses are never re-read
    p = Preflight()
    event_empty(p, session, event, 0)
    assert not p.problems


def test_reset_refuses_a_real_event(session: Session) -> None:
    from tests.conftest import make_event

    with pytest.raises(SystemExit):
        reset_demo(session, make_event(session, "campus-ai-2026"), lambda c, m: True, lambda s: None)


def test_quota_estimate(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "Team Q")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    for u in session.query(m.UsageRecord).all():
        u.model = "gemini-3.6-flash (Gemini API free tier)"
    session.commit()
    used = calls_today(session, "gemini-3.6-flash")
    assert used == session.query(m.UsageRecord).count() > 0
    p = Preflight()
    quota_check(p, session, "gemini-3.6-flash", teams=1)
    assert (20 - used >= 14) == (not p.problems)
    assert sub
