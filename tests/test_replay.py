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

    def send(text: str) -> int:
        sent.append(text)
        return len(sent)

    r = replay(session, source, send, writes.append, speed=5, sleep=sleeps.append)  # stages rendering

    stored = [format_message(teams[n.submission_id], n.text) for n in notes]
    assert sent[1:-1] == stored  # exactly the stored lines, in order, nothing generated
    assert sent[0].startswith(f"▶ Start of replay of run {source.slug}, fake-gemini, ")
    assert sent[-1] == f"■ End of {r.label}."
    assert sleeps == [max(10 / 5, MIN_GAP)] * (len(stored) - 1)
    assert (r.messages, r.teams) == (len(stored), 2)
    assert [len(w["Leaderboard"]) for w in writes] == [1, 2]  # one team, then both
    assert writes[-1]["Leaderboard"] and all(k.startswith("team:") for k in writes[-1]["Leaderboard"])


def test_compact_replay_edits_one_progress_message_per_team(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    a = submit(session, "Team A")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    source = session.get_one(m.Event, a.event_id)
    sent: list[str] = []
    edits: list[tuple[int, str]] = []

    def send(text: str) -> int:
        sent.append(text)
        return len(sent)

    replay(session, source, send, lambda tabs: None, sleep=lambda s: None, edit=lambda mid, t: edits.append((mid, t)))
    stored = {n.text for n in session.query(m.Notification).filter(m.Notification.submission_id == a.id)}
    progress = [t for t in sent if t.startswith("Team A\n")]
    assert len(progress) == 1 and edits and all(mid == sent.index(progress[0]) + 1 for mid, _ in edits)
    alerts = [t.removeprefix("[Team A] ") for t in sent if t.startswith("[Team A] ")]
    assert alerts and set(alerts) <= stored  # every alert is a stored line, verbatim
    assert any(t.startswith("Summary:") for t in alerts) and not any(t.startswith("Cost:") for t in alerts)
