"""Replay: every team line sent is a stored Notification text, in stored order, with scaled gaps;
the Sheet fills team by team from stored evaluations; only the header/footer are added."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.replay import MIN_GAP, replay
from judgebench.telegram.notify import format_message
from tests import fixtures_repos as repos
from tests.fake_llm import fake
from tests.test_m2_flow import deps, drive, submit

pytestmark = pytest.mark.db


def test_replay_sends_only_stored_lines_and_fills_the_sheet(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    a, b = submit(session, "Team A"), submit(session, "Team B")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    notes = (
        session.query(m.Notification)
        .filter(m.Notification.submission_id.is_not(None))
        .order_by(m.Notification.created_at, m.Notification.id)
        .all()
    )
    base = notes[0].created_at
    for i, n in enumerate(notes):  # a stored run that took 10 s between lines
        n.created_at = base + timedelta(seconds=10 * i)
    session.commit()
    source = session.get_one(m.Event, a.event_id)
    teams = {a.id: "Team A", b.id: "Team B"}

    sent: list[str] = []
    writes: list[dict[str, Any]] = []
    sleeps: list[float] = []
    r = replay(session, source, sent.append, writes.append, speed=5, sleep=sleeps.append)

    stored = [format_message(teams[n.submission_id], n.text) for n in notes]
    assert sent[1:-1] == stored  # exactly the stored lines, in order, nothing generated
    assert sent[0].startswith(f"▶ Start of replay of run {source.slug}, fake-gemini, ")
    assert sent[-1] == f"■ End of {r.label}."
    assert sleeps == [max(10 / 5, MIN_GAP)] * (len(stored) - 1)
    assert (r.messages, r.teams) == (len(stored), 2)
    assert [len(w["Leaderboard"]) for w in writes] == [1, 2]  # one team, then both
    assert writes[-1]["Leaderboard"] and all(k.startswith("team:") for k in writes[-1]["Leaderboard"])
