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
        n.telegram_message_id = 7 if n.stage in ("PROFILED", "CI", "CLAIMS") else 100 + i  # shared progress message
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
    assert len(deleted) == len(set(deleted)) and r.telegram_failed == 0  # each message deleted once
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
    assert (int((20 - used) * 0.9) >= 17) == (not p.problems)  # 10% kept back; 17 calls per team
    assert sub


def test_demo_submit_waits_for_the_on_camera_team_then_submits_the_rest(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    from judgebench.demo import demo_submit

    sub = submit(session, "Placeholder")
    event = session.get_one(m.Event, sub.event_id)
    event.slug = "demo-x"
    session.add(m.EventIntegration(tenant_id=event.tenant_id, event_id=event.id, provider="google_forms",
                                   external_ids={"responder_uri": "https://f/viewform", "questions": {}}))  # fmt: skip
    session.commit()
    teams = [{"team_name": n} for n in ("Team Beta", "Team Alpha", "Team Zeta")]
    posted: list[tuple[str, str]] = []
    polls = {"n": 0}

    def sleep(sec: float) -> None:  # Beta's real submission "arrives" during the third poll
        polls["n"] += 1
        if polls["n"] == 3:
            submit(session, "Team Beta")

    done = demo_submit(session_factory, event, teams, ["AAAA-1111", "BBBB-2222", "CCCC-3333"], "Team Beta", 20,
                       post=lambda uri, q, t, c: posted.append((t["team_name"], c)) or 200, sleep=sleep, say=lambda m: None)  # fmt: skip
    assert done == ["Team Alpha", "Team Zeta"] and posted == [("Team Alpha", "BBBB-2222"), ("Team Zeta", "CCCC-3333")]


def test_demo_submit_refuses_real_events(session: Session) -> None:
    from judgebench.demo import demo_submit
    from tests.conftest import make_event

    with pytest.raises(SystemExit):
        demo_submit(None, make_event(session, "campus-ai-2026"), [], [], "Team Beta", 1)


def test_demo_submit_teams_and_rest(session_factory: sessionmaker[Session], session: Session) -> None:
    from judgebench.demo import demo_submit

    sub = submit(session, "Team Beta")
    event = session.get_one(m.Event, sub.event_id)
    event.slug = "demo-y"
    session.add(m.EventIntegration(tenant_id=event.tenant_id, event_id=event.id, provider="google_forms",
                                   external_ids={"responder_uri": "https://f/viewform", "questions": {}}))  # fmt: skip
    session.commit()
    teams = [{"team_name": n} for n in ("Team Beta", "Team Alpha", "Team Zeta", "Team Epsilon")]
    codes = ["A111-1111", "B222-2222", "C333-3333", "D444-4444"]
    posted: list[str] = []

    def post(uri, q, t, c):  # type: ignore[no-untyped-def]
        posted.append(t["team_name"])
        submit(session, t["team_name"])  # the run loop would ingest it
        return 200

    demo_submit(
        session_factory,
        event,
        teams,
        codes,
        "Team Beta",
        0,
        post=post,
        sleep=lambda s: None,
        say=lambda m: None,
        rest=True,
    )
    assert posted == ["Team Alpha", "Team Zeta", "Team Epsilon"]
    posted.clear()
    demo_submit(
        session_factory,
        event,
        teams,
        codes,
        "Team Beta",
        0,
        post=post,
        sleep=lambda s: None,
        say=lambda m: None,
        rest=True,
    )
    assert posted == []  # everyone is in already


def test_demo_submit_keeps_the_given_team_order(session_factory: sessionmaker[Session], session: Session) -> None:
    from judgebench.demo import demo_submit

    sub = submit(session, "Placeholder")
    event = session.get_one(m.Event, sub.event_id)
    event.slug = "demo-z"
    session.add(m.EventIntegration(tenant_id=event.tenant_id, event_id=event.id, provider="google_forms",
                                   external_ids={"responder_uri": "https://f/viewform", "questions": {}}))  # fmt: skip
    session.commit()
    teams = [{"team_name": n} for n in ("Team Beta", "Team Alpha", "Team Epsilon", "Team Zeta")]
    posted: list[str] = []
    arrived: list[object] = []
    demo_submit(session_factory, event, teams, ["A111-1111"] * 4, "Team Beta", 0,
                post=lambda u, q, t, c: posted.append(t["team_name"]) or 200,
                sleep=lambda s: arrived or arrived.append(submit(session, "Team Beta")), say=lambda m: None,
                only=["Team Zeta", "Team Epsilon"])  # fmt: skip
    assert posted == ["Team Zeta", "Team Epsilon"]
