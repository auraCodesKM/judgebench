"""M1 exit condition, end to end with fakes only at the network edges (Forms, GitHub, Telegram,
Sheets): a Form submission produces "received" and "profiled" Telegram messages and a
Leaderboard row. The real-API version of this is the manual check in M0_SETUP.md."""

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.api.ingest import sync_event
from judgebench.db import models as m
from judgebench.google.projection import publish_leaderboard
from judgebench.telegram.notify import TelegramBot, drain
from judgebench.worker.pipeline import run_profile_stage
from tests.conftest import make_event
from tests.test_ingest import QUESTIONS, response
from tests.test_worker_pipeline import FakeGitHub

pytestmark = pytest.mark.db


def test_form_submission_to_telegram_and_leaderboard(session_factory: sessionmaker[Session], session: Session) -> None:
    event = make_event(session, "campus")
    event.telegram_chat_id = -100555
    for provider, ids in [
        ("google_forms", {"form_id": "F", "questions": QUESTIONS}),
        ("google_sheets", {"spreadsheet_id": "S"}),
    ]:
        session.add(
            m.EventIntegration(
                tenant_id=event.tenant_id, event_id=event.id, provider=provider, external_ids=ids, status="ready"
            )
        )
    session.commit()

    telegram: list[str] = []

    def tg(req: httpx.Request) -> httpx.Response:
        telegram.append(json.loads(req.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(telegram)}})

    bot = TelegramBot("t", httpx.Client(transport=httpx.MockTransport(tg)))
    sheet: list[Sequence[Sequence[Any]]] = []

    # one loop iteration of `judgebench run`, twice (the second must be a no-op)
    for _ in range(2):
        sync_event(session, event, lambda fid, since: [response("r1", "Team Alpha")])
        drain(session, bot)
        while run_profile_stage(session_factory, FakeGitHub()):
            pass
        drain(session, bot)
        publish_leaderboard(session, event, lambda sid, tab, rows: sheet.append(rows))

    assert len(telegram) == 2  # exactly one message per stage, nothing re-sent
    assert telegram[0] == "[Team Alpha] Submission received (v1)"
    assert telegram[1].startswith("[Team Alpha] Profiled (commit 3f2c1ab): Python")
    assert telegram[1].endswith("-> adapters: Agentic, RAG")
    assert len(sheet) == 1  # second iteration changed nothing, so no second write
    [row] = sheet[0]
    assert row[:5] == ["Team Alpha", 1, "3f2c1ab", "PROFILED", "Agentic, LLM/RAG"]
