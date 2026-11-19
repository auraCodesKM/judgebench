"""Compact Telegram: one progress message per team edited in place; new messages only for alerts and
the summary card; cost folded into the card; per-chat spacing; batches coalesced."""

import json
import time
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.telegram.compact import PROGRESS, classify, progress_text
from judgebench.telegram.notify import TelegramBot, drain
from judgebench.worker.stages import run_next
from tests import fixtures_repos as repos
from tests.fake_llm import fake
from tests.test_m2_flow import deps, submit

pytestmark = pytest.mark.db


def recording_bot(calls: list[tuple[str, dict[str, Any]]], min_gap: float = 0.0) -> TelegramBot:
    def respond(req: httpx.Request) -> httpx.Response:
        calls.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    return TelegramBot("1:T", httpx.Client(transport=httpx.MockTransport(respond)), min_gap=min_gap)


def sends(calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [str(p["text"]) for meth, p in calls if meth == "sendMessage"]


def test_one_progress_message_per_team_then_edits(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "Team Alpha")
    session.get_one(m.Event, sub.event_id).telegram_chat_id = -100
    session.commit()
    calls: list[tuple[str, dict[str, Any]]] = []
    bot, d = recording_bot(calls), deps(repos.ALPHA_RAG, fake())
    with session_factory() as s:
        drain(s, bot, mode="compact")  # "received"
    while run_next(session_factory, d) is not None:
        with session_factory() as s:
            drain(s, bot, mode="compact")  # as `run` does after every stage

    texts = sends(calls)
    progress = [t for t in texts if t.startswith("Team Alpha\n")]
    edits = [p for meth, p in calls if meth == "editMessageText"]
    assert len(progress) == 1 and len(edits) == 3  # one message; one edit per pipeline stage (coalesced)
    assert all(e["message_id"] == 1 for e in edits)
    every_stage_but_ingestion = {s for s, _ in PROGRESS} - {"RECEIVED"}  # the test helper skips ingestion
    assert edits[-1]["text"] == progress_text("Team Alpha", 1, every_stage_but_ingestion)
    critic = [t for t in texts if "Critic:" in t]
    card = [t for t in texts if "Summary:" in t]
    assert len(critic) == 1 and "revised to 9/20" in critic[0]  # an alert: its own message
    assert len(card) == 1 and "Top concern:" in card[0] and "Ask:" in card[0] and "Cost:" in card[0]
    assert not any(t.startswith("[Team Alpha] Cost:") for t in texts)  # folded into the card
    assert len(texts) == 3  # progress + critic revision + card: nothing else
    session.expire_all()
    assert session.query(m.Notification).filter(m.Notification.sent_at.is_(None)).count() == 0


def test_batch_is_coalesced(session_factory: sessionmaker[Session], session: Session) -> None:
    for name in ("Team A", "Team B", "Team C"):
        sub = submit(session, name)
    session.get_one(m.Event, sub.event_id).telegram_chat_id = -100
    session.commit()
    d = deps(repos.ALPHA_RAG, fake())
    while run_next(session_factory, d) is not None:
        pass
    calls: list[tuple[str, dict[str, Any]]] = []
    with session_factory() as s:
        while drain(s, recording_bot(calls), mode="compact", limit=100):
            pass
    texts = sends(calls)
    assert len([t for t in texts if t.split("\n")[0] in ("Team A", "Team B", "Team C")]) == 3  # one per team
    assert not [p for meth, p in calls if meth == "editMessageText"]  # all stages at once: no edits needed
    assert len(texts) == 9  # 3 x (progress + critic revision + card)


def test_classification() -> None:
    assert classify("CI", "CI: passed") == {"progress"}
    assert classify("CRITIC", "Critic: all scores supported by cited evidence") == {"progress"}
    assert classify("CRITIC", 'Critic: "x" -> revised to 9/20, flagged') == {"progress", "alert"}
    assert classify("ALERT", "Claim contradicted") == {"alert"} and classify("COST", "Cost") == {"silent"}
    assert classify("SOMETHING_NEW", "x") == {"alert"}  # unknown stages are shown, never dropped


def test_per_chat_spacing() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    bot = recording_bot(calls, min_gap=0.05)
    start = time.monotonic()
    for _ in range(3):
        bot.send(-1, "x")
    assert time.monotonic() - start >= 0.1  # 3 posts, 2 gaps
